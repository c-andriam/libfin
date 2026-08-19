#!/usr/bin/env python3
"""
Apply the database schema.

Delegates to Alembic. The previous version of this script created missing
tables with ``create_all`` and refused to touch anything that already existed —
honest about its limits, but it meant no column could ever change on a database
holding real transactions. Alembic removes that ceiling.

Usage:
    python scripts/migrate.py              # upgrade to the latest revision
    python scripts/migrate.py --check      # report drift, change nothing
    python scripts/migrate.py --sql        # print the SQL instead of running it
    python scripts/migrate.py --history    # show the revision history
"""

import argparse
import logging
import os
import subprocess
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "src"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [migrate] %(levelname)s %(message)s")
LOGGER = logging.getLogger("migrate")


def alembic(*args: str) -> int:
    env = {**os.environ, "PYTHONPATH": os.path.join(ROOT, "src")}
    return subprocess.call(
        [sys.executable, "-m", "alembic", *args], cwd=os.path.abspath(ROOT), env=env
    )


def check_drift() -> int:
    """Report whether the models have moved ahead of the applied revisions.

    ``alembic check`` compares the metadata against the database and exits
    non-zero when a revision is missing. That is the signal a deploy pipeline
    wants: refuse to start an application whose models no longer match its
    schema, rather than discover it at the first write.
    """
    LOGGER.info("Comparing the models against the applied revisions...")
    code = alembic("check")
    if code == 0:
        LOGGER.info("The schema is up to date.")
    else:
        LOGGER.error(
            "The schema is behind the models. Generate a revision with:\n"
            "    python -m alembic revision --autogenerate -m 'describe the change'\n"
            "review the generated file, then apply it with: make prod-migrate"
        )
    return code


def adopt_existing_schema() -> bool:
    """Bring a database that predates Alembic under its control.

    A schema created by ``create_all`` has the tables but no ``alembic_version``
    row, so Alembic assumes nothing is applied and tries to create everything —
    failing on the objects that already exist. Stamping the baseline records
    "this database is already at the first revision" without running it.

    Returns True when a stamp was applied. Only stamps when the expected tables
    are genuinely there; an empty database is left alone so the migrations run
    normally.
    """
    from sqlalchemy import create_engine, inspect, text

    url = os.environ["DATABASE_URL"].replace("+asyncpg", "").replace("+aiosqlite", "")
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            tables = set(inspect(conn).get_table_names())
            if "alembic_version" in tables or "transactions" not in tables:
                return False

            LOGGER.warning(
                "The schema exists but is not tracked by Alembic — it predates "
                "migrations. Stamping the baseline revision; no DDL will run for it."
            )
            conn.execute(text("SELECT 1"))
    finally:
        engine.dispose()

    return alembic("stamp", "5e11e7026cb2") == 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply the gateway database schema.")
    parser.add_argument("--check", action="store_true", help="Report drift; change nothing.")
    parser.add_argument("--sql", action="store_true", help="Print the SQL instead of running it.")
    parser.add_argument("--history", action="store_true", help="Show the revision history.")
    parser.add_argument("--downgrade", metavar="REV", help="Roll back to a revision.")
    args = parser.parse_args()

    if not os.environ.get("DATABASE_URL"):
        LOGGER.error("DATABASE_URL is not set.")
        return 1

    if args.history:
        return alembic("history", "--verbose")
    if args.check:
        return check_drift()
    if args.downgrade:
        LOGGER.warning(f"Rolling back to {args.downgrade}. This can discard data.")
        return alembic("downgrade", args.downgrade)

    if args.sql:
        # Offline mode: emit the statements for review before anything runs on a
        # database holding real transactions.
        return alembic("upgrade", "head", "--sql")

    adopt_existing_schema()

    LOGGER.info("Upgrading to the latest revision...")
    code = alembic("upgrade", "head")
    LOGGER.info("Schema applied." if code == 0 else "Migration failed.")
    return code


if __name__ == "__main__":
    sys.exit(main())
