#!/usr/bin/env python3
"""
Apply the database schema as an explicit, one-shot step.

Production runs with ``AUTO_CREATE_SCHEMA=false`` so that several API workers
starting at once cannot race each other creating tables, and so that schema
changes are something a person decides to do rather than a side effect of a
deploy.

Scope: this creates what is missing and reports what exists. It does **not**
alter or drop anything. Introduce Alembic before the first change to a column
that already holds data — ``pip install alembic`` is already in the image, and
``alembic init`` against ``gateway.models.Base`` is the natural next step.

Usage:
    python scripts/migrate.py            # create missing tables
    python scripts/migrate.py --check    # report only, exit 1 if out of date
"""

import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sqlalchemy import inspect  # noqa: E402

from gateway.database import engine  # noqa: E402
from gateway.models import Base  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [migrate] %(levelname)s %(message)s")
LOGGER = logging.getLogger("migrate")


async def _existing_tables() -> set:
    async with engine.begin() as conn:
        return set(await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names()))


async def migrate(check_only: bool) -> int:
    expected = set(Base.metadata.tables)
    existing = await _existing_tables()
    missing = expected - existing

    LOGGER.info(f"Expected tables: {sorted(expected)}")
    LOGGER.info(f"Existing tables: {sorted(existing)}")

    if not missing:
        LOGGER.info("The schema is up to date.")
        await engine.dispose()
        return 0

    if check_only:
        LOGGER.error(f"Missing tables: {sorted(missing)}")
        await engine.dispose()
        return 1

    LOGGER.info(f"Creating: {sorted(missing)}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    LOGGER.info("Schema applied.")

    await engine.dispose()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Apply the gateway database schema.")
    parser.add_argument("--check", action="store_true", help="Report only; do not create anything.")
    args = parser.parse_args()
    sys.exit(asyncio.run(migrate(args.check)))
