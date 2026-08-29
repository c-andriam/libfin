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

from celery import Celery, signals
from celery.schedules import crontab  # noqa: F401  (available for custom schedules)

from gateway.acquirer import AcquirerService
from gateway.circuit_breaker import web3_circuit_breaker
from gateway.config import settings
from gateway.crypto_service import CryptoService, GasPriceTooHigh, InsufficientFunds
from gateway.database import async_session
from gateway.models import Transaction, TransactionStatus
from gateway.observability import configure_logging, correlation_scope
from gateway.pan_vault import get_pan_vault

LOGGER = logging.getLogger(__name__)


@signals.setup_logging.connect
def _use_our_logging(**_kwargs):
    """Stop Celery replacing the structured handler with its own."""
    configure_logging(settings.log_level, structured=settings.log_json)


celery_app = Celery("gateway_worker", broker=settings.celery_broker_url)

#: Longest a single transfer attempt can legitimately take: waiting for the
#: receipt, then for confirmations, plus room for the acquirer round trip.
_MAX_TASK_SECONDS = (
    settings.web3_receipt_timeout_sec + settings.web3_confirmation_timeout_sec + 120
)

#: Longest a task is ever scheduled into the future. retry_reversal backs off
#: to an hour, which is the largest of them.
_MAX_RETRY_COUNTDOWN = 3600

#: The Redis broker treats a message as lost once this elapses and delivers it
#: again. It therefore has to exceed *both* how long a task runs and how far
#: ahead a task is ever scheduled — a countdown longer than the visibility
#: timeout means the message is redelivered while still waiting for its own ETA,
#: and each redelivery is itself redelivered.
#:
#: Sized at execution time plus the longest countdown, doubled. Getting this
#: wrong is not a slow degradation: with a 3600s retry backoff against a 900s
#: visibility timeout, one stuck reversal multiplied into 65,000 queued
#: messages and starved every real payment behind them.
_VISIBILITY_TIMEOUT = (_MAX_TASK_SECONDS + _MAX_RETRY_COUNTDOWN) * 2

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
        "visibility_timeout": _VISIBILITY_TIMEOUT,
    },
    # A task stuck beyond its own budget is killed rather than held forever.
    task_soft_time_limit=_MAX_TASK_SECONDS,
    task_time_limit=_MAX_TASK_SECONDS + 60,
    beat_schedule={
        "reconcile-transactions": {
            "task": "reconcile_transactions",
            "schedule": float(settings.reconciliation_interval_sec),
        },
        "apply-retention": {
            "task": "apply_retention",
            "schedule": float(settings.retention_interval_sec),
        },
        # Frequent, because every unpublished row is a payment nobody has acted
        # on yet. This is the durability guarantee behind the immediate publish
        # the API attempts.
        "publish-outbox": {
            "task": "publish_outbox",
            "schedule": float(settings.outbox_relay_interval_sec),
        },
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
# Releasing and taking the money
# ---------------------------------------------------------------------------


async def _release_funds(tx_id: int, reason: str) -> bool:
    """Undo the fiat leg, by whichever route the transaction's state allows.

    A held authorisation is voided — nothing was debited, so nothing is owed
    and a refusal is merely untidy. A captured purchase must be reversed, and a
    refusal there means a cardholder is out of pocket. Same intent, very
    different stakes; the caller should not have to know which applies.
    """
    async with async_session() as session:
        tx = await session.get(Transaction, tx_id)
        if tx is None:
            LOGGER.error(f"Release requested for unknown transaction {tx_id}.")
            return False
        held = tx.status is TransactionStatus.FIAT_AUTHORIZED

    return await (_void_authorization(tx_id, reason) if held else _reverse_transaction(tx_id, reason))


async def _void_authorization(tx_id: int, reason: str) -> bool:
    """Release a hold. The cheap failure path: no debit ever happened."""
    pan_vault = get_pan_vault()
    acquirer = AcquirerService()

    try:
        async with async_session() as session:
            tx = await session.get(Transaction, tx_id)
            if tx is None:
                return False
            if tx.status is not TransactionStatus.FIAT_AUTHORIZED:
                LOGGER.info(f"Transaction {tx_id} is {tx.status.value}; nothing to void.")
                return False

            # Same guard the reversal path has, and for the same reason: a
            # transfer that is on the chain, or still sitting in the mempool
            # where it may yet be mined, must not have its funding released.
            # Doing so hands the customer their crypto and gives back the money
            # for it. Observed exactly that way — a hold voided against a
            # transfer that was pending, not failed.
            if tx.crypto_tx_hash:
                crypto = _crypto_service()
                try:
                    onchain = await crypto.get_onchain_status(tx.crypto_tx_hash)
                finally:
                    await crypto.close()

                if onchain == "success":
                    LOGGER.warning(
                        f"Transaction {tx_id} is confirmed on-chain ({tx.crypto_tx_hash}); "
                        "capturing instead of releasing the hold."
                    )
                    return False
                if onchain == "pending":
                    LOGGER.warning(
                        f"Transaction {tx_id} has a transfer still pending on-chain "
                        f"({tx.crypto_tx_hash}); the hold stays in place until it "
                        "resolves. Releasing it now would give the crypto away."
                    )
                    return False
                if onchain == "unknown":
                    # The node did not answer, so nothing is known about this
                    # transfer. Treating silence as failure is what turns a
                    # network outage into a refund for crypto that was in fact
                    # delivered — money out twice, unrecoverable. Hold the hold.
                    LOGGER.warning(
                        f"Transaction {tx_id}: the chain could not be reached about "
                        f"{tx.crypto_tx_hash}. The hold stays until the node answers."
                    )
                    return False

            pan = await pan_vault.retrieve(tx_id)
            amount, stan, rrn, sent_at = (
                Decimal(str(tx.amount)), tx.stan, tx.rrn, tx.created_at,
            )

        if not pan:
            # Unlike a reversal, this is survivable without a human: an
            # unvoided hold expires by itself, typically within a week.
            LOGGER.warning(
                f"No stored PAN for transaction {tx_id}; the hold cannot be voided "
                "explicitly and will expire on its own."
            )
            async with async_session() as session:
                tx = await session.get(Transaction, tx_id)
                tx.transition_to(TransactionStatus.AUTH_VOIDED)
                tx.error_message = f"{reason}; hold left to expire"[:512]
                await session.commit()
            await pan_vault.purge(tx_id)
            return True

        await acquirer.start()
        outcome = await acquirer.void_authorization(
            original_stan=stan, pan=pan, amount=amount,
            original_sent_at=sent_at, original_rrn=rrn,
        )

        async with async_session() as session:
            tx = await session.get(Transaction, tx_id)
            tx.reversal_stan = outcome.get("stan") or tx.reversal_stan
            # Either way the hold is gone: the acquirer released it, or it
            # expires. No debit occurred, so this is terminal and costless.
            tx.transition_to(TransactionStatus.AUTH_VOIDED)
            tx.error_message = (
                f"Authorisation voided: {reason}"
                if outcome["success"]
                else f"{reason}; void refused ({outcome.get('action_code')}), hold expires"
            )[:512]
            await session.commit()

        await pan_vault.purge(tx_id)
        LOGGER.info(
            f"Transaction {tx_id}: hold released, no money was ever taken from the card."
        )
        return True

    finally:
        await acquirer.stop()


async def _capture_funds(tx_id: int) -> bool:
    """Turn the hold into a debit, now that the crypto is confirmed.

    Called only after the transfer is on-chain. A refusal here is revenue lost
    rather than a customer wronged — the opposite failure to a refused reversal,
    and the reason this ordering is worth the change.
    """
    pan_vault = get_pan_vault()
    acquirer = AcquirerService()

    try:
        async with async_session() as session:
            tx = await session.get(Transaction, tx_id)
            if tx is None:
                return False
            if tx.status is not TransactionStatus.FIAT_AUTHORIZED:
                LOGGER.info(f"Transaction {tx_id} is {tx.status.value}; no capture needed.")
                return tx.status in (
                    TransactionStatus.FIAT_CAPTURED, TransactionStatus.CRYPTO_SENT,
                )

            pan = await pan_vault.retrieve(tx_id)
            amount, stan, rrn, sent_at, auth_code = (
                Decimal(str(tx.amount)), tx.stan, tx.rrn, tx.created_at, tx.auth_code,
            )
            previous_capture_stan = tx.capture_stan
            capture_attempts = tx.capture_attempts or 0

        # A capture that reached the acquirer while this process died before
        # recording it would leave the row still FIAT_AUTHORIZED — and a retry
        # would take the money a second time. The stored STAN is the evidence
        # that an attempt was already made.
        if previous_capture_stan:
            LOGGER.critical(
                f"MANUAL ACTION REQUIRED — transaction {tx_id} already has a capture "
                f"in flight (STAN {previous_capture_stan}, {capture_attempts} attempt(s)). "
                "Refusing to send another: if the first one succeeded, a second would "
                "charge the cardholder twice. Reconcile against the acquirer's "
                "settlement file, then clear capture_stan to allow a retry."
            )
            return False

        if not pan:
            LOGGER.critical(
                f"MANUAL ACTION REQUIRED — transaction {tx_id} (STAN {stan}, {amount}) "
                "was delivered but its authorisation cannot be captured: no stored PAN. "
                "This is uncollected revenue."
            )
            return False

        await acquirer.start()

        # Allocate and persist the trace number before the message leaves. This
        # is the same discipline the chain side already follows by writing the
        # transaction hash before waiting for confirmations: record the intent,
        # then act, so a crash in between is recoverable rather than invisible.
        capture_stan = await acquirer.next_stan()
        async with async_session() as session:
            tx = await session.get(Transaction, tx_id)
            tx.capture_stan = capture_stan
            tx.capture_attempts = (tx.capture_attempts or 0) + 1
            await session.commit()

        outcome = await acquirer.capture(
            original_stan=stan, pan=pan, amount=amount,
            original_sent_at=sent_at, original_rrn=rrn, auth_code=auth_code,
            stan=capture_stan,
        )

        if outcome["success"]:
            async with async_session() as session:
                tx = await session.get(Transaction, tx_id)
                tx.transition_to(TransactionStatus.FIAT_CAPTURED)
                await session.commit()
            await pan_vault.purge(tx_id)
            return True

        # A refusal is a definite answer: the money was not taken, so the STAN
        # is cleared and a retry may send a fresh one. Only an *unanswered*
        # capture leaves it set, because that is the case where the acquirer's
        # state is unknown.
        if outcome.get("action_code") not in ("TIMEOUT", "SEND_ERROR", "LINK_DOWN"):
            async with async_session() as session:
                tx = await session.get(Transaction, tx_id)
                tx.capture_stan = None
                await session.commit()

        LOGGER.critical(
            f"MANUAL ACTION REQUIRED — capture refused for transaction {tx_id} "
            f"(STAN {stan}, {amount}, code {outcome.get('action_code')}). The crypto "
            "is delivered; this is revenue to collect by hand."
        )
        return False

    finally:
        await acquirer.stop()


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

            if tx.status in (
                TransactionStatus.REVERSED,
                TransactionStatus.CRYPTO_SENT,
                TransactionStatus.AUTH_VOIDED,
            ):
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
                    if onchain == "unknown":
                        # Same rule as the void path: an unreachable node is not
                        # evidence the transfer failed, and reversing on it repays
                        # a cardholder who already has the crypto.
                        LOGGER.warning(
                            f"Transaction {tx_id}: the chain could not be reached "
                            "about this transfer; postponing the reversal."
                        )
                        return False
                finally:
                    await crypto.close()

            pan = await pan_vault.retrieve(tx_id)
            if not pan:
                tx.transition_to(TransactionStatus.REVERSAL_FAILED)
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
                tx.transition_to(TransactionStatus.REVERSAL_FAILED)
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
    """Run one attempt. Returns ``sent``, ``retry``, ``reversed`` or ``noop``.

    Note what this does *not* do: discard the stored PAN. That decision belongs
    to whoever knows the transaction is finished — _mark_sent once the funds are
    captured, or the release paths once the hold is gone.
    """
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
                TransactionStatus.AUTH_VOIDED,
            ):
                LOGGER.info(f"Transaction {tx_id} already settled ({tx.status.value}).")
                return "noop"

            # The rate agreed with the customer. Delivery uses this and never
            # a fresh lookup: the amount was fixed when they were quoted.
            locked_rate = Decimal(str(tx.exchange_rate)) if tx.exchange_rate else None
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
                web3_circuit_breaker.record_success()
                return "sent"
            if status == "pending":
                try:
                    await crypto.await_confirmation(existing_hash)
                    await _mark_sent(tx_id)
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
                        # A gap below this nonce is the more likely cause, and
                        # replacing does nothing about it: the transaction is
                        # stuck because a lower nonce was never broadcast, not
                        # because it is underpriced. Fill the hole first.
                        gap = await crypto.nonce_gap()
                        if gap:
                            LOGGER.warning(
                                f"Transfer {existing_hash} is stuck behind a nonce gap of "
                                f"{gap}; filling it before repricing."
                            )
                            filled = await crypto.fill_nonce_gap()
                            if filled:
                                # Give the fillers a chance to mine, then let the
                                # normal retry re-examine the transfer.
                                return "retry" if attempts_left > 0 else "noop"

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
                                attempt=replacements + 1,
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

        if locked_rate is None:
            # Only possible for a transaction created before rates were locked.
            LOGGER.warning(
                f"Transaction {tx_id} has no locked rate; falling back to the "
                "configured constant. This should not happen for new payments."
            )

        tx_hash, units, nonce = await crypto.broadcast_erc20_transfer(
            token_address=settings.erc20_token_address,
            to_address=target_wallet,
            amount_fiat=amount,
            exchange_rate=locked_rate,
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


async def _mark_sent(tx_id: int) -> bool:
    """Record delivery, capturing the funds first when a hold is outstanding.

    Returns whether the transaction fully settled. It owns the decision to
    discard the stored PAN, because that card number is what a capture needs:
    the callers used to purge unconditionally right after calling this, so a
    refused capture destroyed the only means of ever collecting the money —
    delivered crypto, uncollectable revenue, and nothing left to retry with.

    Order matters here too. The capture is attempted before the transaction is
    called settled, so a refusal stays visible instead of being buried under a
    CRYPTO_SENT that looks complete.
    """
    async with async_session() as session:
        tx = await session.get(Transaction, tx_id)
        if tx is None:
            return
        needs_capture = tx.status is TransactionStatus.FIAT_AUTHORIZED

    if needs_capture and not await _capture_funds(tx_id):
        # The crypto is delivered but the money was not collected. The hold is
        # still open, so this is retried rather than written off — the record
        # stays FIAT_AUTHORIZED, which is the truth, and the stored PAN is kept
        # because the retry cannot build a 0220 without it.
        LOGGER.critical(
            f"MANUAL ACTION REQUIRED — transaction {tx_id} delivered but not captured. "
            "The hold is still open; the capture will be retried."
        )
        return False

    async with async_session() as session:
        tx = await session.get(Transaction, tx_id)
        if tx is None:
            return False
        if tx.can_transition_to(TransactionStatus.CRYPTO_SENT):
            tx.transition_to(TransactionStatus.CRYPTO_SENT)
            tx.error_message = None
            await session.commit()
        else:
            LOGGER.error(
                f"Transaction {tx_id} is {tx.status.value} and cannot be marked "
                "delivered; the crypto is on-chain and the record disagrees."
            )
            return False

    # Settled: the card number is no longer needed by anything.
    await get_pan_vault().purge(tx_id)
    return True


async def _fail(tx_id: int, reason: str) -> str:
    """Give the money back, by whichever route this transaction's state allows.

    With a hold outstanding this releases it and nothing is owed. With a
    purchase already captured it is a reversal, and a refusal there is the
    expensive case this ordering exists to avoid.
    """
    async with async_session() as session:
        tx = await session.get(Transaction, tx_id)
        if tx is None:
            return "noop"
        held = tx.status is TransactionStatus.FIAT_AUTHORIZED

        if not held and tx.can_transition_to(TransactionStatus.CRYPTO_FAILED):
            tx.transition_to(TransactionStatus.CRYPTO_FAILED)
            tx.error_message = reason[:512]
            await session.commit()

    if held:
        await _void_authorization(tx_id, reason=f"crypto transfer failed: {reason}")
        return "voided"

    reversed_ok = await _reverse_transaction(tx_id, reason=f"crypto transfer failed: {reason}")
    return "reversed" if reversed_ok else "reversal_failed"


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


@celery_app.task(bind=True, name="process_crypto_transfer", max_retries=MAX_TRANSFER_RETRIES)
def process_crypto_transfer(self, tx_id: int, correlation_id: str = None, **_legacy):
    """Deliver the crypto leg of a transaction whose fiat leg was approved.

    ``correlation_id`` comes from the API request that created the payment, so
    its log lines and this task's share one identifier. ``**_legacy`` swallows
    arguments from tasks queued by an older build (which passed the PAN and the
    amount); they are deliberately ignored.
    """
    with correlation_scope(correlation_id, tx_id):
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
            retry_reversal.apply_async(
                kwargs={"tx_id": tx_id, "correlation_id": correlation_id}, countdown=60
            )

        return outcome


@celery_app.task(bind=True, name="retry_reversal", max_retries=MAX_REVERSAL_RETRIES)
def retry_reversal(self, tx_id: int, correlation_id: str = None):
    """Keep trying to refund a cardholder whose reversal was not accepted."""
    with correlation_scope(correlation_id, tx_id):
        ok = asyncio.run(_reverse_transaction(tx_id, reason="scheduled reversal retry"))
        if ok:
            return "reversed"

        if self.request.retries < MAX_REVERSAL_RETRIES:
            raise self.retry(countdown=min(60 * (2 ** self.request.retries), 3600))

        LOGGER.critical(
            f"Transaction {tx_id} could not be reversed after {MAX_REVERSAL_RETRIES} "
            "attempts. It needs a manual refund."
        )
        return "manual_intervention_required"


@celery_app.task(name="publish_outbox")
def publish_outbox_task():
    """Move anything still waiting from the outbox to the broker."""
    from gateway.outbox import publish_pending, purge_published

    with correlation_scope():
        counts = asyncio.run(publish_pending())
        # Cheap, and keeps the relay's index from growing without bound.
        if counts["published"] == 0:
            asyncio.run(purge_published())
        return counts


@celery_app.task(name="apply_retention")
def apply_retention_task():
    """Enforce the retention horizons on a schedule.

    Retention that depends on someone remembering to run a script is not a
    policy, it is an intention.
    """
    from gateway.retention import apply_retention

    with correlation_scope():
        return asyncio.run(apply_retention())


@celery_app.task(name="reconcile_transactions")
def reconcile_transactions():
    """Periodic safety net for transactions no callback ever completed."""
    from gateway.reconciliation import reconcile

    with correlation_scope():
        anomalies = asyncio.run(reconcile())
        return {"anomalies": len(anomalies)}
