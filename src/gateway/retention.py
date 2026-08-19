"""
Data retention.

Card numbers were never stored in full, but everything around them was kept
indefinitely: amounts, destination wallets, authorisation codes, timestamps.
Together those identify a person's purchase history as surely as the PAN would,
and keeping them forever is a liability under both PCI-DSS scope rules and data
protection law — the obligation is to keep what you need for as long as you can
justify, and no longer.

Two horizons, because two different rules apply:

  * **Redaction** at the shorter horizon strips the fields that identify a
    person — masked PAN, destination wallet — while keeping the row. Amounts,
    states and timestamps survive, so financial history and reconciliation
    remain intact. This is what most retention obligations actually require.
  * **Deletion** at the longer horizon removes the row entirely, once the
    financial record-keeping period your regulator sets has elapsed.

Nothing is touched while a transaction is unsettled or owes a cardholder money,
whatever the horizon says.
"""

import logging
from datetime import timedelta
from typing import Dict

from sqlalchemy import select

from gateway.config import settings
from gateway.database import async_session
from gateway.models import (
    MANUAL_INTERVENTION_STATES,
    UNSETTLED_STATES,
    Transaction,
    utcnow,
)

LOGGER = logging.getLogger(__name__)

#: Placeholders left in redacted rows, so a reader can tell "redacted" from
#: "never recorded" — the difference matters when investigating a dispute.
REDACTED_PAN = "REDACTED"
REDACTED_WALLET = "0x" + "0" * 40

#: A row in one of these states is either still in flight or owes someone money.
#: Retention never touches it, however old it is: deleting the record of a debt
#: does not settle the debt.
_PROTECTED = set(UNSETTLED_STATES) | set(MANUAL_INTERVENTION_STATES)


async def apply_retention(dry_run: bool = False) -> Dict[str, int]:
    """Redact and delete according to the configured horizons.

    Returns the counts affected. Safe to run repeatedly: redaction is
    idempotent, and rows already redacted are skipped by the query.
    """
    counts = {"redacted": 0, "deleted": 0, "protected": 0}

    redact_before = utcnow() - timedelta(days=settings.retention_redact_days)
    delete_before = utcnow() - timedelta(days=settings.retention_delete_days)

    async with async_session() as session:
        # ── Rows old enough to redact, but still within the record period ────
        redactable = (
            select(Transaction)
            .where(
                Transaction.created_at < redact_before,
                Transaction.masked_pan != REDACTED_PAN,
                Transaction.status.notin_(list(_PROTECTED)),
            )
            .limit(settings.retention_batch_size)
        )
        rows = (await session.execute(redactable)).scalars().all()

        for tx in rows:
            if dry_run:
                continue
            tx.masked_pan = REDACTED_PAN
            tx.target_wallet = REDACTED_WALLET
            tx.auth_code = None
            tx.error_message = None
        counts["redacted"] = len(rows)

        # ── Rows past the record-keeping period ──────────────────────────────
        deletable = (
            select(Transaction)
            .where(
                Transaction.created_at < delete_before,
                Transaction.status.notin_(list(_PROTECTED)),
            )
            .limit(settings.retention_batch_size)
        )
        old = (await session.execute(deletable)).scalars().all()

        for tx in old:
            if dry_run:
                continue
            await session.delete(tx)
        counts["deleted"] = len(old)

        # ── What retention deliberately left alone ───────────────────────────
        protected = (
            select(Transaction)
            .where(
                Transaction.created_at < redact_before,
                Transaction.status.in_(list(_PROTECTED)),
            )
        )
        counts["protected"] = len((await session.execute(protected)).scalars().all())

        if not dry_run:
            await session.commit()

    verb = "would redact" if dry_run else "redacted"
    LOGGER.info(
        f"Retention: {verb} {counts['redacted']}, "
        f"{'would delete' if dry_run else 'deleted'} {counts['deleted']}, "
        f"{counts['protected']} left alone because they are unsettled or owe a refund."
    )
    if counts["protected"]:
        LOGGER.warning(
            f"{counts['protected']} transaction(s) are older than the redaction horizon "
            "but still unsettled. Retention will not clear them; a human has to."
        )

    return counts


async def redact_transaction(tx_id: int) -> bool:
    """Redact one transaction on request.

    The mechanism behind an erasure request. Refuses while the transaction is
    unsettled — a request to be forgotten does not cancel a debt in either
    direction, and the row has to survive until the money is settled.
    """
    async with async_session() as session:
        tx = await session.get(Transaction, tx_id)
        if tx is None:
            LOGGER.warning(f"Redaction requested for unknown transaction {tx_id}.")
            return False

        if tx.status in _PROTECTED:
            LOGGER.warning(
                f"Refusing to redact transaction {tx_id}: it is {tx.status.value}, "
                "so it is either still in flight or owes a refund."
            )
            return False

        tx.masked_pan = REDACTED_PAN
        tx.target_wallet = REDACTED_WALLET
        tx.auth_code = None
        tx.error_message = None
        await session.commit()

    LOGGER.info(f"Transaction {tx_id} redacted on request.")
    return True


async def retention_report() -> Dict[str, int]:
    """What retention would do, without doing it."""
    return await apply_retention(dry_run=True)


if __name__ == "__main__":
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(description="Apply the data retention policy.")
    parser.add_argument("--dry-run", action="store_true", help="Report only.")
    parser.add_argument("--redact", type=int, metavar="TX_ID", help="Redact one transaction.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [retention] %(levelname)s %(message)s")

    if args.redact:
        raise SystemExit(0 if asyncio.run(redact_transaction(args.redact)) else 1)
    asyncio.run(apply_retention(dry_run=args.dry_run))
