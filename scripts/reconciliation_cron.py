#!/usr/bin/env python3
"""
Reconciliation entry point for crontab or a one-off operator run.

In the production stack the same logic runs on a schedule inside Celery Beat
(``reconcile_transactions``), so this script is for manual checks and for
deployments that prefer cron.

Usage:
    python scripts/reconciliation_cron.py            # inspect and repair
    python scripts/reconciliation_cron.py --dry-run  # inspect only

Crontab (hourly):
    0 * * * * /path/to/venv/bin/python /path/to/scripts/reconciliation_cron.py
"""

import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from gateway.reconciliation import reconcile  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile transactions against the chain.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report anomalies without requeueing or reversing anything.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [reconciliation] %(levelname)s %(message)s",
    )

    anomalies = asyncio.run(reconcile(act=not args.dry_run))
    # Non-zero exit so a cron wrapper or monitoring check can alert on it.
    return 1 if anomalies else 0


if __name__ == "__main__":
    sys.exit(main())
