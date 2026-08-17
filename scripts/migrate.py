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


async def _inspect_schema() -> dict:
    """Existing tables mapped to their column names."""

    def collect(sync_conn):
        inspector = inspect(sync_conn)
        return {
            table: {column["name"] for column in inspector.get_columns(table)}
            for table in inspector.get_table_names()
        }

    async with engine.begin() as conn:
        return await conn.run_sync(collect)


async def migrate(check_only: bool) -> int:
    existing = await _inspect_schema()
    expected_tables = set(Base.metadata.tables)
    missing_tables = expected_tables - set(existing)

    # Columns added to a model after a table already exists are invisible to
    # create_all — it creates tables, it never alters them. Without this check
    # the application starts happily and fails at the first write, which for
    # this system means mid-payment.
    missing_columns = {}
    for name, table in Base.metadata.tables.items():
        if name in existing:
            absent = {c.name for c in table.columns} - existing[name]
            if absent:
                missing_columns[name] = sorted(absent)

    LOGGER.info(f"Expected tables: {sorted(expected_tables)}")
    LOGGER.info(f"Existing tables: {sorted(existing)}")

    if not missing_tables and not missing_columns:
        LOGGER.info("The schema is up to date.")
        await engine.dispose()
        return 0

    if missing_columns:
        for table, columns in missing_columns.items():
            LOGGER.error(f"Table '{table}' is missing column(s): {', '.join(columns)}")
        LOGGER.error(
            "Adding columns is beyond this script: it creates tables and never alters "
            "them, so it cannot do this without risking the data already there. Use "
            "Alembic, or in a disposable environment recreate the database "
            "(`make sim-down` drops the simulation volumes)."
        )
        await engine.dispose()
        return 1

    if check_only:
        LOGGER.error(f"Missing tables: {sorted(missing_tables)}")
        await engine.dispose()
        return 1

    LOGGER.info(f"Creating: {sorted(missing_tables)}")
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
