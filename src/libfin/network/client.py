import asyncio
import logging
from typing import Optional, Callable, Awaitable

from libfin.network.protocol import Iso8583Protocol

LOGGER = logging.getLogger(__name__)


class Iso8583Client:
    """
    Asyncio TCP client for ISO8583 networks with auto-reconnect and async dispatch.
    """
    def __init__(self, host: str, port: int, length_header_size: int = 2):
        self.host = host
        self.port = port
        self.protocol = Iso8583Protocol(length_header_size)
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self._connected = False
        self._receive_task = None

        # Callback to handle incoming messages
        self.on_message_received: Optional[Callable[[bytes], Awaitable[None]]] = None

    async def connect(self):
        while not self._connected:
            try:
                self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
                self._connected = True
                LOGGER.info(f"Connected to {self.host}:{self.port}")
                self._receive_task = asyncio.create_task(self._receive_loop())
            except Exception as e:
                LOGGER.error(f"Connection failed to {self.host}:{self.port} - {e}. Retrying in 2s...")
                await asyncio.sleep(2)

    async def _receive_loop(self):
        try:
            while self._connected:
                msg_bytes = await self.protocol.read_message(self.reader)
                if msg_bytes is None:
                    LOGGER.warning("Connection closed by peer.")
                    break

                if self.on_message_received:
                    # Dispatch to engine concurrently
                    asyncio.create_task(self.on_message_received(msg_bytes))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            LOGGER.error(f"Error in receive loop: {e}")
        finally:
            if self._connected:
                self._connected = False
                if self.writer:
                    self.writer.close()
                    try:
                        await self.writer.wait_closed()
                    except Exception:
                        pass
                # Auto-reconnect
                LOGGER.info("Connection lost. Attempting to reconnect...")
                asyncio.create_task(self.connect())

    async def send(self, msg_bytes: bytes):
        if not self._connected or not self.writer:
            raise ConnectionError("Not connected")

        out_msg = self.protocol.pack_message(msg_bytes)
        self.writer.write(out_msg)
        await self.writer.drain()

    async def disconnect(self):
        self._connected = False
        if self._receive_task:
            self._receive_task.cancel()
        if self.writer:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except Exception:
                pass
        LOGGER.info("Disconnected.")
