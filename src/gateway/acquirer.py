import asyncio
import logging
import os
import ssl
from typing import Dict, Any, Optional

import redis.asyncio as redis
from libfin import iso8583
from libfin.network.client import Iso8583Client

LOGGER = logging.getLogger(__name__)

class AcquirerService:
    """
    Acquirer service that uses Iso8583Client to send 0200 Authorization Requests
    and waits for the corresponding 0210 Response via STAN (DE11) correlation.
    Supports TLS and distributed STAN generation via Redis.
    """
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        
        # Setup TLS if required
        self.ssl_context = None
        if os.environ.get("BANK_USE_TLS") == "true":
            self.ssl_context = ssl.create_default_context()
            if os.environ.get("BANK_TLS_INSECURE") == "true":
                self.ssl_context.check_hostname = False
                self.ssl_context.verify_mode = ssl.CERT_NONE
        
        self.client = Iso8583Client(host, port, length_header_size=2, ssl_context=self.ssl_context)
        self.client.on_message_received = self._handle_incoming_message
        
        # Pending requests mapped by STAN (Systems Trace Audit Number - DE11)
        self._pending_requests: Dict[str, asyncio.Future] = {}
        
        # Distributed STAN generation via Redis
        redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
        self.redis = redis.from_url(redis_url)

    async def start(self):
        """Starts the background connection task."""
        asyncio.create_task(self.client.connect())
        # Wait briefly for connection to establish
        for _ in range(10):
            if self.client._connected:
                break
            await asyncio.sleep(0.1)

    async def stop(self):
        await self.client.disconnect()
        await self.redis.close()

    async def _get_next_stan(self) -> str:
        """Fetch the next STAN securely from Redis to avoid collisions between gateway instances."""
        try:
            stan_int = await self.redis.incr("global:stan")
            if stan_int > 999999:
                await self.redis.set("global:stan", 1)
                stan_int = 1
            return f"{stan_int:06d}"
        except Exception as e:
            LOGGER.error(f"Failed to get STAN from Redis: {e}. Falling back to random (unsafe for high load).")
            import random
            return f"{random.randint(1, 999999):06d}"

    async def _handle_incoming_message(self, msg_bytes: bytes):
        """Callback invoked by Iso8583Client when a message arrives."""
        try:
            msg_dict = iso8583.loads(msg_bytes)
            mti = msg_dict.get("MTI")
            stan = str(msg_dict.get("DE11", "")).zfill(6)
            
            LOGGER.debug(f"Received MTI={mti}, STAN={stan}")
            
            if stan in self._pending_requests:
                future = self._pending_requests.pop(stan)
                if not future.done():
                    future.set_result(msg_dict)
            else:
                LOGGER.warning(f"Unmatched or unhandled message: MTI={mti}, STAN={stan}")
                
        except Exception as e:
            LOGGER.error(f"Failed to process incoming message: {e}")

    async def authorize_payment(self, pan: str, amount: float, expiry: str) -> Dict[str, Any]:
        """
        Sends an 0200 Authorization Request and waits for the 0210 response.
        """
        if not self.client._connected:
            raise ConnectionError("Acquirer client is not connected to the bank network.")

        stan = await self._get_next_stan()
        
        # Amount format: typically 12 numeric characters (cents)
        amount_cents = int(amount * 100)
        
        request_dict = {
            "MTI": "0200",
            "DE2": pan,                 # Primary Account Number
            "DE3": "000000",            # Processing Code (00=Purchase)
            "DE4": amount_cents,        # Amount
            "DE11": int(stan),          # STAN
            "DE14": expiry,             # Expiry Date (YYMM)
            "DE49": "840",              # Currency (840 = USD)
        }

        future = asyncio.get_running_loop().create_future()
        self._pending_requests[stan] = future

        try:
            msg_bytes = iso8583.dumps(request_dict)
            LOGGER.info(f"Sending 0200 Request STAN={stan}, Amount={amount}")
            await self.client.send(msg_bytes)
            
            # Wait for response (timeout 10s)
            response = await asyncio.wait_for(future, timeout=10.0)
            return response
            
        except asyncio.TimeoutError:
            self._pending_requests.pop(stan, None)
            LOGGER.error(f"Timeout waiting for 0210 response for STAN={stan}")
            raise TimeoutError("Bank network did not respond in time.")
        except Exception as e:
            self._pending_requests.pop(stan, None)
            raise e

    async def reverse_payment(self, original_stan: str, pan: str, amount: float) -> bool:
        """
        Sends an 0400 Reversal Request to undo a previously approved 0200 transaction.
        Waits for 0410 confirmation in production.
        """
        if not self.client._connected:
            LOGGER.error("Cannot send reversal: Acquirer client is disconnected.")
            return False

        stan = await self._get_next_stan()
        amount_cents = int(amount * 100)
        
        request_dict = {
            "MTI": "0400",
            "DE2": pan,
            "DE3": "000000",
            "DE4": amount_cents,
            "DE11": int(stan),
            "DE49": "840",
            "DE90": f"0200{original_stan:0>6}00000000000000000000000000000000",
        }

        future = asyncio.get_running_loop().create_future()
        self._pending_requests[stan] = future

        try:
            msg_bytes = iso8583.dumps(request_dict)
            LOGGER.info(f"Sending 0400 Reversal Request STAN={stan} for original STAN={original_stan}")
            await self.client.send(msg_bytes)
            
            # Production: Wait for 0410 confirmation
            response = await asyncio.wait_for(future, timeout=10.0)
            action_code = str(response.get("DE39", ""))
            
            if action_code == "00":
                LOGGER.info(f"Reversal confirmed by bank! STAN={stan}")
                return True
            else:
                LOGGER.error(f"Bank rejected reversal! Code: {action_code}")
                return False
                
        except asyncio.TimeoutError:
            self._pending_requests.pop(stan, None)
            LOGGER.error(f"Timeout waiting for 0410 reversal confirmation for STAN={stan}")
            return False
        except Exception as e:
            self._pending_requests.pop(stan, None)
            LOGGER.error(f"Failed to send 0400 Reversal: {e}")
            return False
