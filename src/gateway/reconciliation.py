"""
Reconciliation: the safety net for transactions that no callback finished.

Anything that debits a cardholder and then relies on an in-process callback to
deliver value will eventually drop one — the worker is killed, the node stalls,
the broker loses a message. This job runs on a schedule, compares the database
against the chain, and *acts*: it requeues what can still be delivered and
reverses what cannot.
"""

import asyncio
import logging
from datetime import timedelta
from typing import Dict, List

from sqlalchemy import select

from gateway.config import settings
from gateway.crypto_service import CryptoService
from gateway.database import async_session, init_db
from gateway.models import Transaction, TransactionStatus, utcnow

LOGGER = logging.getLogger("reconciliation")


async def reconcile(act: bool = True) -> List[str]:
    """Compare recorded state with reality. Returns the anomalies found.

    With ``act=False`` nothing is queued or written — useful for a dry run
    before a production launch.
    """
    await init_db()

    anomalies: List[str] = []
    counters: Dict[str, int] = {"verified": 0, "requeued": 0, "reversed": 0, "manual": 0}
    crypto = CryptoService()

    try:
        # ── Transfers we believe succeeded: confirm them on-chain ───────────
        async with async_session() as session:
            sent = (
                await session.execute(
                    select(Transaction).where(
                        Transaction.status == TransactionStatus.CRYPTO_SENT
                    )
                )
            ).scalars().all()

            for tx in sent:
                if not tx.crypto_tx_hash:
                    anomalies.append(
                        f"Transaction {tx.id} is CRYPTO_SENT with no transaction hash."
                    )
                    continue

                status = await crypto.get_onchain_status(tx.crypto_tx_hash)
                if status == "success":
                    counters["verified"] += 1
                elif status == "pending":
                    anomalies.append(
                        f"Transaction {tx.id} ({tx.crypto_tx_hash}) is still pending on-chain."
                    )
                elif status == "unknown":
                    # "unknown" is not an answer about the transfer; it is the
                    # absence of one. get_onchain_status returns it whenever the
                    # RPC could not be reached at all, which is indistinguishable
                    # here from a hash the chain has never heard of. Reversing on
                    # it refunds cardholders whose crypto was delivered — the one
                    # error this job exists to prevent, committed by the job
                    # itself. An unreachable node is a reason to ask again later
                    # and to wake a human if it persists, never to move money.
                    anomalies.append(
                        f"Transaction {tx.id} could not be checked on-chain "
                        f"({tx.crypto_tx_hash}): the node did not answer. Left as "
                        f"CRYPTO_SENT; it will be re-checked on the next run."
                    )
                    counters["manual"] += 1
                else:
                    # Only "failed" reaches here: a receipt exists and its status
                    # is 0. That is the chain stating the transfer did not happen.
                    anomalies.append(
                        f"Transaction {tx.id} is marked sent but the chain says '{status}' "
                        f"for {tx.crypto_tx_hash}."
                    )
                    if act:
                        tx.transition_to(TransactionStatus.CRYPTO_FAILED)
                        tx.error_message = f"Reconciliation: chain reports {status}."
                        await session.commit()
                        _queue_reversal(tx.id)
                        counters["reversed"] += 1

        # ── Fiat captured but nothing delivered: requeue ─────────────────────
        # Never call a transaction stale while a worker could still be working
        # on it, or every slow confirmation gets a redundant duplicate task.
        # The floor is the longest a single attempt can legitimately take.
        longest_attempt = (
            settings.web3_receipt_timeout_sec + settings.web3_confirmation_timeout_sec + 120
        )
        stale_seconds = max(settings.stale_transaction_minutes * 60, longest_attempt)
        cutoff = utcnow() - timedelta(seconds=stale_seconds)
        async with async_session() as session:
            stale = (
                await session.execute(
                    select(Transaction).where(
                        Transaction.status.in_(
                            [
                                TransactionStatus.FIAT_APPROVED,
                                # A hold with nothing delivered against it is
                                # the same problem, and expires if ignored.
                                TransactionStatus.FIAT_AUTHORIZED,
                            ]
                        ),
                        Transaction.created_at < cutoff,
                    )
                )
            ).scalars().all()

            for tx in stale:
                anomalies.append(
                    f"Transaction {tx.id} (STAN {tx.stan}, {tx.amount}) has been "
                    f"FIAT_APPROVED since {tx.created_at} with nothing delivered."
                )
                if act:
                    _queue_transfer(tx.id)
                    counters["requeued"] += 1

        # ── Authorisations whose outcome we never learned ────────────────────
        async with async_session() as session:
            unknown = (
                await session.execute(
                    select(Transaction).where(
                        Transaction.status == TransactionStatus.FIAT_UNKNOWN
                    )
                )
            ).scalars().all()

            for tx in unknown:
                anomalies.append(
                    f"Transaction {tx.id} (STAN {tx.stan}) never got an authorisation "
                    "response; the cardholder may have been debited."
                )
                if act:
                    _queue_reversal(tx.id)
                    counters["reversed"] += 1

        # ── Money owed to a cardholder that no retry will fix ────────────────
        async with async_session() as session:
            failed = (
                await session.execute(
                    select(Transaction).where(
                        Transaction.status == TransactionStatus.REVERSAL_FAILED
                    )
                )
            ).scalars().all()

            for tx in failed:
                counters["manual"] += 1
                anomalies.append(
                    f"MANUAL REFUND OWED — transaction {tx.id} (STAN {tx.stan}, "
                    f"{tx.amount} {tx.currency}, card {tx.masked_pan}) after "
                    f"{tx.reversal_attempts} reversal attempts."
                )

    finally:
        await crypto.close()

    LOGGER.info(
        "Reconciliation: %s verified on-chain, %s requeued, %s reversals queued, "
        "%s awaiting a manual refund, %s anomalies.",
        counters["verified"],
        counters["requeued"],
        counters["reversed"],
        counters["manual"],
        len(anomalies),
    )
    for anomaly in anomalies:
        LOGGER.critical(f"  >> {anomaly}")
    if counters["manual"]:
        LOGGER.critical(
            f"{counters['manual']} transaction(s) owe a cardholder money and need a human."
        )

    return anomalies


def _queue_transfer(tx_id: int) -> None:
    try:
        from gateway.worker import process_crypto_transfer

        process_crypto_transfer.apply_async(kwargs={"tx_id": tx_id})
        LOGGER.info(f"Requeued the crypto transfer for transaction {tx_id}.")
    except Exception as exc:
        LOGGER.error(f"Could not requeue transaction {tx_id}: {exc}")


def _queue_reversal(tx_id: int) -> None:
    try:
        from gateway.worker import retry_reversal

        retry_reversal.apply_async(args=[tx_id])
        LOGGER.info(f"Queued a reversal for transaction {tx_id}.")
    except Exception as exc:
        LOGGER.error(f"Could not queue a reversal for transaction {tx_id}: {exc}")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [reconciliation] %(levelname)s %(message)s"
    )
    asyncio.run(reconcile())
