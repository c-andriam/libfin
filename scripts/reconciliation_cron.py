#!/usr/bin/env python3
"""
Reconciliation Cron Script
--------------------------
This script is designed to be run periodically (e.g., via crontab every hour)
to verify the integrity of all transactions in the system.

It cross-references:
  1. Our PostgreSQL database (source of truth for fiat state)
  2. The blockchain (source of truth for crypto state)

Any discrepancy triggers a CRITICAL log alert.

Usage:
    python scripts/reconciliation_cron.py

Crontab example (every hour):
    0 * * * * /path/to/venv/bin/python /path/to/scripts/reconciliation_cron.py
"""

import asyncio
import logging
import os
import sys

# Add the src directory to the Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from sqlalchemy import select
from gateway.database import async_session, init_db
from gateway.models import Transaction, TransactionStatus
from gateway.crypto_service import CryptoService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [RECONCILIATION] %(levelname)s %(message)s"
)
LOGGER = logging.getLogger("reconciliation")

crypto_service = CryptoService()

async def reconcile():
    """
    Main reconciliation logic:
    1. Find all transactions marked CRYPTO_SENT in the DB.
    2. For each, verify on-chain that the tx_hash exists and is confirmed.
    3. Find all transactions stuck in FIAT_APPROVED (worker may have crashed).
    4. Report anomalies.
    """
    await init_db()

    anomalies = []
    stale_count = 0
    verified_count = 0

    async with async_session() as session:
        # ── Phase 1: Verify CRYPTO_SENT transactions on-chain ──────────
        stmt = select(Transaction).where(Transaction.status == TransactionStatus.CRYPTO_SENT)
        result = await session.execute(stmt)
        sent_transactions = result.scalars().all()

        LOGGER.info(f"Phase 1: Verifying {len(sent_transactions)} CRYPTO_SENT transactions on-chain...")

        for tx in sent_transactions:
            if not tx.crypto_tx_hash:
                anomalies.append(
                    f"ANOMALY: Tx ID={tx.id} marked CRYPTO_SENT but has no crypto_tx_hash!"
                )
                continue

            try:
                if not crypto_service.is_connected():
                    LOGGER.warning("Web3 RPC not reachable. Skipping on-chain verification.")
                    break

                receipt = await asyncio.to_thread(
                    crypto_service.w3.eth.get_transaction_receipt, tx.crypto_tx_hash
                )

                if receipt is None:
                    anomalies.append(
                        f"ANOMALY: Tx ID={tx.id} hash={tx.crypto_tx_hash} NOT FOUND on-chain!"
                    )
                elif receipt.status != 1:
                    anomalies.append(
                        f"ANOMALY: Tx ID={tx.id} hash={tx.crypto_tx_hash} FAILED on-chain (status=0)!"
                    )
                else:
                    verified_count += 1

            except Exception as e:
                anomalies.append(
                    f"ANOMALY: Tx ID={tx.id} hash={tx.crypto_tx_hash} verification error: {e}"
                )

        # ── Phase 2: Detect stale FIAT_APPROVED transactions ──────────
        # These are transactions where the fiat was debited but the Celery
        # worker never completed the crypto transfer (crash, timeout, etc.)
        from datetime import datetime, timedelta, timezone
        stale_cutoff = datetime.now(timezone.utc) - timedelta(minutes=15)

        stmt = select(Transaction).where(
            Transaction.status == TransactionStatus.FIAT_APPROVED,
            Transaction.created_at < stale_cutoff
        )
        result = await session.execute(stmt)
        stale_transactions = result.scalars().all()
        stale_count = len(stale_transactions)

        for tx in stale_transactions:
            anomalies.append(
                f"STALE: Tx ID={tx.id} STAN={tx.stan} stuck in FIAT_APPROVED since {tx.created_at}. "
                f"Amount={tx.amount} USD. Manual intervention or re-queue required."
            )

    # ── Report ─────────────────────────────────────────────────────────
    LOGGER.info("=" * 60)
    LOGGER.info("RECONCILIATION REPORT")
    LOGGER.info("=" * 60)
    LOGGER.info(f"  On-chain verified (CRYPTO_SENT): {verified_count}")
    LOGGER.info(f"  Stale FIAT_APPROVED (>15min):    {stale_count}")
    LOGGER.info(f"  Total anomalies:                 {len(anomalies)}")

    if anomalies:
        LOGGER.critical("ANOMALIES DETECTED:")
        for a in anomalies:
            LOGGER.critical(f"  >> {a}")
    else:
        LOGGER.info("  ✅ No anomalies detected. All systems nominal.")

    LOGGER.info("=" * 60)
    return anomalies

if __name__ == "__main__":
    asyncio.run(reconcile())
