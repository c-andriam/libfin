"""
Celery worker: fiat has already been captured, so this side owes the customer
either crypto or a refund. Every branch below ends in one of those two.

Design points that were bugs before:

  * **The task carries only a transaction id.** Card data never enters a broker
    payload; the worker reads what it needs from the database and the PAN vault.
  * **The chain is consulted before any reversal.** A transfer whose receipt we
    failed to read may well have landed. Reversing the fiat on top of a
    successful transfer pays the customer twice.
  * **Running out of retries triggers a reversal.** Previously the task simply
    died on the last attempt, leaving the cardholder debited with nothing sent.
  * **The acquirer link is built inside the task's own event loop.** A module-level
    connection belongs to whatever loop first touched it; ``asyncio.run`` closes
    that loop, and the next reversal would write to a dead socket.
"""

import asyncio
import logging
from decimal import Decimal

from celery import Celery
from celery.schedules import crontab  # noqa: F401  (available for custom schedules)

from gateway.acquirer import AcquirerService
from gateway.circuit_breaker import web3_circuit_breaker
from gateway.config import settings
from gateway.crypto_service import CryptoService, GasPriceTooHigh, InsufficientFunds
from gateway.database import async_session
from gateway.models import Transaction, TransactionStatus
from gateway.pan_vault import get_pan_vault

LOGGER = logging.getLogger(__name__)

celery_app = Celery("gateway_worker", broker=settings.celery_broker_url)

#: Longest a single transfer attempt can legitimately take: waiting for the
#: receipt, then for confirmations, plus room for the acquirer round trip.
_MAX_TASK_SECONDS = (
    settings.web3_receipt_timeout_sec + settings.web3_confirmation_timeout_sec + 120
)

celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_default_retry_delay=10,
    broker_connection_retry_on_startup=True,
    result_expires=3600,
    timezone="UTC",
    broker_transport_options={
        # With a Redis broker, `acks_late` alone does not recover a killed
        # worker: the task stays invisible until the visibility timeout expires,
        # and Celery's default is one hour. A customer whose fiat was captured
        # would wait that long for their crypto. Sized just above the longest a
        # task can legitimately run — shorter would redeliver tasks that are
        # still working, longer would strand them after a crash.
        #
        # Redelivery is safe here because process_crypto_transfer is
        # idempotent: it reads any hash already persisted and asks the chain
        # what happened before doing anything.
        "visibility_timeout": _MAX_TASK_SECONDS,
    },
    # A task stuck beyond its own budget is killed rather than held forever.
    task_soft_time_limit=_MAX_TASK_SECONDS,
    task_time_limit=_MAX_TASK_SECONDS + 60,
    beat_schedule={
        "reconcile-transactions": {
            "task": "reconcile_transactions",
            "schedule": float(settings.reconciliation_interval_sec),
        }
    },
)

MAX_TRANSFER_RETRIES = 5
MAX_REVERSAL_RETRIES = 10
#: How many times a stuck transfer may be re-priced before we give up and
#: refund. Each replacement raises the fee, so this bounds the spend.
MAX_REPLACEMENTS = 3

#: Errors worth retrying: the network was in the way, the request itself is fine.
_TRANSIENT_MARKERS = (
    "connection",
    "timeout",
    "timed out",
    "temporarily unavailable",
    "too many requests",
    "429",
    "502",
    "503",
    "504",
    "nonce too low",
    "replacement transaction underpriced",
)


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, InsufficientFunds):
        return False
    if isinstance(exc, GasPriceTooHigh):
        # Fees come back down. Retrying is right; paying any price is not.
        return True
    if isinstance(exc, (ConnectionError, TimeoutError, asyncio.TimeoutError)):
        return True
    message = str(exc).lower()
    return any(marker in message for marker in _TRANSIENT_MARKERS)


def _crypto_service() -> CryptoService:
    """Built per task: a Celery prefork child must not share Redis sockets."""
    return CryptoService()


# ---------------------------------------------------------------------------
# Reversal
# ---------------------------------------------------------------------------


async def _reverse_transaction(tx_id: int, reason: str) -> bool:
    """Refund the cardholder for a transaction whose crypto leg failed.

    Returns whether the acquirer confirmed the reversal. Safe to call twice: a
    transaction already in a terminal state is left alone.
    """
    pan_vault = get_pan_vault()
    acquirer = AcquirerService()

    try:
        async with async_session() as session:
            tx = await session.get(Transaction, tx_id)
            if tx is None:
                LOGGER.error(f"Reversal requested for unknown transaction {tx_id}.")
                return False

            if tx.status in (TransactionStatus.REVERSED, TransactionStatus.CRYPTO_SENT):
                LOGGER.info(f"Transaction {tx_id} is already settled ({tx.status.value}).")
                return tx.status is TransactionStatus.REVERSED

            # Last line of defence against paying twice: if the transfer is on
            # the chain after all, keep the money where it is.
            if tx.crypto_tx_hash:
                crypto = _crypto_service()
                try:
                    onchain = await crypto.get_onchain_status(tx.crypto_tx_hash)
                    if onchain == "success":
                        LOGGER.warning(
                            f"Transaction {tx_id} is confirmed on-chain "
                            f"({tx.crypto_tx_hash}); cancelling the reversal."
                        )
                        tx.mark_completed(TransactionStatus.CRYPTO_SENT)
                        tx.error_message = None
                        await session.commit()
                        await pan_vault.purge(tx_id)
                        return False
                    if onchain == "pending":
                        LOGGER.warning(
                            f"Transaction {tx_id} is still pending on-chain; "
                            "postponing the reversal."
                        )
                        return False
                finally:
                    await crypto.close()

            pan = await pan_vault.retrieve(tx_id)
            if not pan:
                tx.status = TransactionStatus.REVERSAL_FAILED
                tx.error_message = f"{reason}; no PAN available for the reversal"[:512]
                tx.reversal_attempts = (tx.reversal_attempts or 0) + 1
                await session.commit()
                LOGGER.critical(
                    f"MANUAL ACTION REQUIRED — transaction {tx_id} (STAN {tx.stan}, "
                    f"{tx.amount}) cannot be reversed automatically."
                )
                return False

            amount = Decimal(str(tx.amount))
            stan = tx.stan
            rrn = tx.rrn
            sent_at = tx.created_at

        # The acquirer call happens outside the session: it takes seconds and
        # must not hold a database connection open.
        await acquirer.start()
        outcome = await acquirer.reverse_payment(
            original_stan=stan,
            pan=pan,
            amount=amount,
            original_sent_at=sent_at,
            original_rrn=rrn,
        )

        async with async_session() as session:
            tx = await session.get(Transaction, tx_id)
            tx.reversal_attempts = (tx.reversal_attempts or 0) + 1
            tx.reversal_stan = outcome.get("stan") or tx.reversal_stan

            if outcome["success"]:
                tx.mark_completed(TransactionStatus.REVERSED)
                tx.error_message = f"Reversed: {reason}"[:512]
                LOGGER.info(f"Transaction {tx_id} reversed; the cardholder has been refunded.")
            else:
                tx.status = TransactionStatus.REVERSAL_FAILED
                tx.error_message = (
                    f"{reason}; reversal refused ({outcome.get('action_code')})"
                )[:512]
                LOGGER.critical(
                    f"MANUAL ACTION REQUIRED — reversal refused for transaction {tx_id} "
                    f"(STAN {stan}, {amount})."
                )
            await session.commit()

        if outcome["success"]:
            await pan_vault.purge(tx_id)
        return bool(outcome["success"])

    finally:
        await acquirer.stop()


# ---------------------------------------------------------------------------
# Crypto transfer
# ---------------------------------------------------------------------------


async def _process_crypto(tx_id: int, attempts_left: int) -> str:
    """Run one attempt. Returns ``sent``, ``retry``, ``reversed`` or ``noop``."""
    pan_vault = get_pan_vault()
    crypto = _crypto_service()

    try:
        async with async_session() as session:
            tx = await session.get(Transaction, tx_id)
            if tx is None:
                LOGGER.error(f"Transaction {tx_id} not found.")
                return "noop"

            if tx.status in (
                TransactionStatus.CRYPTO_SENT,
                TransactionStatus.REVERSED,
                TransactionStatus.FIAT_DECLINED,
            ):
                LOGGER.info(f"Transaction {tx_id} already settled ({tx.status.value}).")
                return "noop"

            existing_hash = tx.crypto_tx_hash
            existing_nonce = tx.crypto_nonce
            existing_units = int(tx.crypto_amount_units) if tx.crypto_amount_units else None
            replacements = tx.crypto_replacements or 0
            amount = Decimal(str(tx.amount))
            target_wallet = tx.target_wallet

        # A previous attempt may have broadcast before dying. Never broadcast a
        # second transfer for the same transaction without checking.
        if existing_hash:
            status = await crypto.get_onchain_status(existing_hash)
            LOGGER.info(f"Transaction {tx_id} already has hash {existing_hash} ({status}).")
            if status == "success":
                await _mark_sent(tx_id)
                await pan_vault.purge(tx_id)
                web3_circuit_breaker.record_success()
                return "sent"
            if status == "pending":
                try:
                    await crypto.await_confirmation(existing_hash)
                    await _mark_sent(tx_id)
                    await pan_vault.purge(tx_id)
                    web3_circuit_breaker.record_success()
                    return "sent"
                except TimeoutError as exc:
                    # Stuck, not failing: priced below the market and sitting in
                    # the mempool. It can neither be delivered nor reversed
                    # (the reversal guard rightly refuses to refund something
                    # still in flight), so the only way out is to replace it at
                    # the same nonce with higher fees.
                    if (
                        existing_nonce is not None
                        and existing_units
                        and replacements < MAX_REPLACEMENTS
                        and attempts_left > 0
                    ):
                        LOGGER.warning(
                            f"Transfer {existing_hash} is stuck; replacing at nonce "
                            f"{existing_nonce} (attempt {replacements + 1}/{MAX_REPLACEMENTS})."
                        )
                        try:
                            new_hash = await crypto.replace_stuck_transfer(
                                token_address=settings.erc20_token_address,
                                to_address=target_wallet,
                                token_units=existing_units,
                                nonce=existing_nonce,
                            )
                            await _record_replacement(tx_id, new_hash)
                            return "retry"
                        except Exception as replace_exc:
                            LOGGER.error(
                                f"Could not replace the stuck transfer for {tx_id}: {replace_exc}"
                            )
                    LOGGER.warning(f"Pending transfer {existing_hash} not confirmed yet: {exc}")
                    return "retry" if attempts_left > 0 else await _fail(tx_id, str(exc))
                except Exception as exc:
                    LOGGER.warning(f"Pending transfer {existing_hash} not confirmed yet: {exc}")
                    return "retry" if attempts_left > 0 else await _fail(tx_id, str(exc))
            # "failed" or "unknown": fall through and try again with a new nonce.
            LOGGER.warning(f"Previous transfer {existing_hash} did not land; retrying.")

        tx_hash, units, nonce = await crypto.broadcast_erc20_transfer(
            token_address=settings.erc20_token_address,
            to_address=target_wallet,
            amount_fiat=amount,
        )

        # Persist before confirming: a crash from here on must not orphan a
        # transfer that is already on the chain.
        async with async_session() as session:
            tx = await session.get(Transaction, tx_id)
            tx.crypto_tx_hash = tx_hash
            tx.crypto_amount_units = units
            tx.crypto_nonce = nonce
            await session.commit()

        await crypto.await_confirmation(tx_hash)
        await _mark_sent(tx_id)
        await pan_vault.purge(tx_id)
        web3_circuit_breaker.record_success()
        LOGGER.info(f"Transaction {tx_id} settled on-chain: {tx_hash}")
        return "sent"

    except Exception as exc:
        LOGGER.error(f"Crypto transfer failed for transaction {tx_id}: {exc}")
        web3_circuit_breaker.record_failure()

        if _is_transient(exc) and attempts_left > 0:
            crypto.rotate_rpc()
            return "retry"

        return await _fail(tx_id, str(exc))

    finally:
        await crypto.close()


async def _record_replacement(tx_id: int, new_hash: str) -> None:
    """Point the transaction at the replacement. The old hash can never mine."""
    async with async_session() as session:
        tx = await session.get(Transaction, tx_id)
        if tx is not None:
            tx.crypto_tx_hash = new_hash
            tx.crypto_replacements = (tx.crypto_replacements or 0) + 1
            await session.commit()


async def _mark_sent(tx_id: int) -> None:
    async with async_session() as session:
        tx = await session.get(Transaction, tx_id)
        if tx is not None:
            tx.mark_completed(TransactionStatus.CRYPTO_SENT)
            tx.error_message = None
            await session.commit()


async def _fail(tx_id: int, reason: str) -> str:
    """Record the failure and refund the cardholder."""
    async with async_session() as session:
        tx = await session.get(Transaction, tx_id)
        if tx is not None and tx.status not in (
            TransactionStatus.CRYPTO_SENT,
            TransactionStatus.REVERSED,
        ):
            tx.status = TransactionStatus.CRYPTO_FAILED
            tx.error_message = reason[:512]
            await session.commit()

    reversed_ok = await _reverse_transaction(tx_id, reason=f"crypto transfer failed: {reason}")
    return "reversed" if reversed_ok else "reversal_failed"


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


@celery_app.task(bind=True, name="process_crypto_transfer", max_retries=MAX_TRANSFER_RETRIES)
def process_crypto_transfer(self, tx_id: int, **_legacy):
    """Deliver the crypto leg of a transaction whose fiat leg was approved.

    ``**_legacy`` swallows arguments from tasks queued by an older build (which
    passed the PAN and the amount); they are deliberately ignored.
    """
    attempts_left = max(0, MAX_TRANSFER_RETRIES - self.request.retries)
    outcome = asyncio.run(_process_crypto(tx_id, attempts_left))

    if outcome == "retry":
        countdown = min(2 ** self.request.retries, 300)
        LOGGER.warning(
            f"Retrying transaction {tx_id} in {countdown}s "
            f"({self.request.retries + 1}/{MAX_TRANSFER_RETRIES})."
        )
        raise self.retry(countdown=countdown)

    if outcome == "reversal_failed":
        # Hand it to the dedicated task so the retry schedule is its own.
        retry_reversal.apply_async(args=[tx_id], countdown=60)

    return outcome


@celery_app.task(bind=True, name="retry_reversal", max_retries=MAX_REVERSAL_RETRIES)
def retry_reversal(self, tx_id: int):
    """Keep trying to refund a cardholder whose reversal was not accepted."""
    ok = asyncio.run(_reverse_transaction(tx_id, reason="scheduled reversal retry"))
    if ok:
        return "reversed"

    if self.request.retries < MAX_REVERSAL_RETRIES:
        raise self.retry(countdown=min(60 * (2 ** self.request.retries), 3600))

    LOGGER.critical(
        f"Transaction {tx_id} could not be reversed after {MAX_REVERSAL_RETRIES} attempts. "
        "It needs a manual refund."
    )
    return "manual_intervention_required"


@celery_app.task(name="reconcile_transactions")
def reconcile_transactions():
    """Periodic safety net for transactions no callback ever completed."""
    from gateway.reconciliation import reconcile

    anomalies = asyncio.run(reconcile())
    return {"anomalies": len(anomalies)}
