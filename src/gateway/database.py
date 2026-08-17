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
elif settings.database_url.startswith("sqlite"):
    # SQLite has no meaningful pool; leave its defaults alone.
    engine_kwargs["pool_pre_ping"] = True
else:
    # The same pool everywhere. Sizing these only in production meant the
    # simulation ran on SQLAlchemy's defaults (5 + 10) and could not reproduce
    # a production pool exhaustion — the stack has to be faithful on the axes
    # that fail under load, or it is not a rehearsal.
    engine_kwargs.update(
        {
            "pool_pre_ping": True,
            "pool_size": settings.db_pool_size,
            "max_overflow": settings.db_max_overflow,
            "pool_timeout": settings.db_pool_timeout,
            # Recycle below the usual 1h idle timeout of managed Postgres.
            "pool_recycle": 1800,
        }
    )

connect_args: dict = {}
if settings.database_ssl and settings.database_url.startswith("postgresql+asyncpg"):
    # asyncpg takes an SSLContext, and `ssl=True` is shorthand for full
    # verification — which rejects the self-signed certificate the bundled
    # Postgres presents. Build the context explicitly so the trust decision is
    # visible rather than inherited from a default.
    import ssl as _ssl

    mode = settings.database_ssl_mode
    if mode == "verify-full":
        context = _ssl.create_default_context(cafile=settings.database_ssl_ca_file or None)
        LOGGER.info("Database TLS: encrypted and verified against the configured CA.")
    elif mode == "require":
        # Encrypted, certificate not verified. Defensible only when the peer is
        # reachable solely over a private network you control — which is the
        # case for the bundled Postgres on the internal network. Point at a
        # managed database and you want verify-full with its CA.
        context = _ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = _ssl.CERT_NONE
        LOGGER.warning(
            "Database TLS: encrypted but NOT verified (DATABASE_SSL_MODE=require). "
            "Use verify-full with DATABASE_SSL_CA_FILE for a managed database."
        )
    else:
        context = None
        LOGGER.warning(f"Unknown DATABASE_SSL_MODE={mode!r}; connecting without TLS.")

    if context is not None:
        connect_args["ssl"] = context

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
