import logging
import os
import sys

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from gateway.config import settings
from gateway.models import Base

LOGGER = logging.getLogger(__name__)


def _running_under_celery() -> bool:
    """Whether this process is a Celery worker or beat scheduler.

    It matters because a pooled asyncpg connection belongs to the event loop
    that opened it. The API keeps one loop for the life of the process and
    pools happily; a Celery task runs under a fresh ``asyncio.run`` loop every
    time, and reusing a pooled connection across those loops raises
    "attached to a different loop" — in the middle of a reversal, which is the
    worst possible place to discover it.
    """
    if os.environ.get("GATEWAY_DB_NULLPOOL", "").lower() in ("1", "true", "yes"):
        return True
    argv0 = os.path.basename(sys.argv[0]) if sys.argv else ""
    return argv0.startswith("celery") or "celery" in " ".join(sys.argv[:2])


engine_kwargs: dict = {"echo": False}

if _running_under_celery():
    # One connection per session, opened and closed inside the task's own loop.
    engine_kwargs["poolclass"] = NullPool
    LOGGER.info("Celery process detected: using NullPool for the database engine.")
else:
    engine_kwargs["pool_pre_ping"] = True
    if settings.is_production:
        engine_kwargs.update(
            {
                "pool_size": 20,
                "max_overflow": 10,
                "pool_timeout": 30,
                # Recycle below the usual 1h idle timeout of managed Postgres.
                "pool_recycle": 1800,
            }
        )

connect_args: dict = {}
if settings.database_ssl and settings.database_url.startswith("postgresql+asyncpg"):
    # asyncpg spells it "ssl", not "sslmode"; True means "require".
    connect_args["ssl"] = True

if connect_args:
    engine_kwargs["connect_args"] = connect_args

engine = create_async_engine(settings.database_url, **engine_kwargs)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    """Create the schema when auto-creation is enabled.

    In production this is a no-op: several API workers start at once and
    concurrent ``create_all`` calls race, and silent schema drift is worse than
    an explicit migration step. Set ``AUTO_CREATE_SCHEMA=true`` only for
    simulation and tests.
    """
    if not settings.auto_create_schema:
        LOGGER.info("AUTO_CREATE_SCHEMA disabled — expecting the schema to be migrated already.")
        return

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session():
    async with async_session() as session:
        yield session
