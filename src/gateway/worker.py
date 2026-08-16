import asyncio
import logging
import os
from celery import Celery
from gateway.crypto_service import CryptoService, ERC20_ABI
from gateway.acquirer import AcquirerService
from gateway.database import engine, async_session
from gateway.models import Transaction, TransactionStatus
from gateway.circuit_breaker import web3_circuit_breaker
from sqlalchemy import select

LOGGER = logging.getLogger(__name__)

# Celery Configuration
CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0")
celery_app = Celery("gateway_worker", broker=CELERY_BROKER_URL)

celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_default_retry_delay=10,
    worker_prefetch_multiplier=1
)

crypto_service = CryptoService()
# Acquirer service for reversals (using env vars)
BANK_HOST = os.environ.get("BANK_HOST", "127.0.0.1")
BANK_PORT = int(os.environ.get("BANK_PORT", "9000"))
acquirer_service = AcquirerService(host=BANK_HOST, port=BANK_PORT)

async def _process_crypto_async(tx_id: int, target_wallet: str, amount_fiat: float, stan: str, task):
    """Async core of the crypto transfer to be run within Celery"""
    async with async_session() as session:
        tx = await session.get(Transaction, tx_id)
        if not tx:
            LOGGER.error(f"Transaction {tx_id} not found.")
            return

        try:
            LOGGER.info(f"Worker: Starting crypto transfer for Tx ID: {tx_id}")
            ERC20_TOKEN_ADDRESS = os.environ.get("ERC20_TOKEN_ADDRESS", "0xdAC17F958D2ee523a2206206994597C13D831ec7")
            
            tx_hash = await crypto_service.transfer_erc20_token(
                token_address=ERC20_TOKEN_ADDRESS,
                to_address=target_wallet,
                amount_fiat=amount_fiat,
                exchange_rate=1.0
            )
            
            tx.crypto_tx_hash = tx_hash
            tx.status = TransactionStatus.CRYPTO_SENT
            await session.commit()
            web3_circuit_breaker.record_success()
            LOGGER.info(f"Worker: Tx {tx_id} successfully converted to crypto. Hash: {tx_hash}")

        except Exception as e:
            LOGGER.error(f"Worker: Crypto transfer failed for Tx {tx_id}: {e}")
            web3_circuit_breaker.record_failure()
            
            # For network/transient errors, we should retry before reversing
            if "Cannot connect to Web3" in str(e) or "Timeout" in str(e):
                raise e # Let Celery retry mechanisms catch this
                
            tx.status = TransactionStatus.CRYPTO_FAILED
            tx.error_message = str(e)
            await session.commit()
            
            # TRIGGER REVERSAL
            LOGGER.warning(f"Worker: Initiating Reversal 0400 for STAN {stan}")
            if not acquirer_service.client._connected:
                await acquirer_service.start()
            
            reversal_success = await acquirer_service.reverse_payment(
                original_stan=stan,
                pan=tx.masked_pan, # Use masked PAN for compliance
                amount=amount_fiat
            )
            
            if reversal_success:
                tx.status = TransactionStatus.REVERSED
                LOGGER.info(f"Worker: Reversal for Tx {tx_id} successful.")
            else:
                LOGGER.critical(f"Worker: FATAL Reversal failed for Tx {tx_id}. Manual intervention required!")
                
            await session.commit()

@celery_app.task(bind=True, name="process_crypto_transfer", max_retries=5)
def process_crypto_transfer(self, tx_id: int, target_wallet: str, amount_fiat: float, stan: str, pan: str = None):
    """
    Celery task that acts as a wrapper to run the async crypto logic.
    Raw PAN is no longer passed for PCI-DSS compliance.
    """
    try:
        asyncio.run(_process_crypto_async(tx_id, target_wallet, amount_fiat, stan, self))
    except Exception as exc:
        LOGGER.warning(f"Task process_crypto_transfer failed, retrying... ({self.request.retries}/5)")
        raise self.retry(exc=exc, countdown=2 ** self.request.retries)
