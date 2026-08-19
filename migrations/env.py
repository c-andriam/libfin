"""
Alembic environment.

Reads the database URL from ``DATABASE_URL`` rather than alembic.ini, so a
production password never lands in a tracked file, and the migration runs
against exactly the database the application uses.

The async driver is swapped for its sync equivalent here: Alembic's runner is
synchronous, and a migration is a one-shot administrative command with no
reason to carry the application's async machinery.
"""

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from gateway.models import Base  # noqa: E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _sync_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Migrations run against the same database "
            "the application uses; export it or run this through `make prod-migrate`."
        )
    return url.replace("+asyncpg", "").replace("+aiosqlite", "")


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of executing it.

    Worth having: a DBA reviewing what is about to run on a production database
    is a normal control, and `--sql` is how they get it.
    """
    context.configure(
        url=_sync_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(_sync_url(), poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Detect column type and default changes, not only added or dropped
            # columns — a silently widened Numeric is exactly the sort of drift
            # that matters when the column holds money.
            compare_type=True,
            # Server defaults are managed on PostgreSQL only: SQLite has no
            # ALTER COLUMN, so the migrations that set them skip it there.
            # Comparing anyway makes every SQLite check report drift that no
            # migration can resolve — and a gate that always fails is a gate
            # nobody reads.
            compare_server_default=connection.dialect.name == "postgresql",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
