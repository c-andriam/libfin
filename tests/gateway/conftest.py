"""
Test rig for the gateway.

Redis is patched out before any gateway module is imported, because several of
them build a client at import time (the circuit breaker, the PAN vault, the STAN
allocator). Patching afterwards would leave those holding real sockets.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

# ── Environment ─────────────────────────────────────────────────────────────
# Set before importing gateway.config, which snapshots the environment once.
os.environ.setdefault("GATEWAY_MODE", "simulation")
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("AUTO_CREATE_SCHEMA", "true")
os.environ.setdefault("BANK_HOST", "127.0.0.1")
os.environ.setdefault("BANK_PORT", "9000")
os.environ.setdefault("BANK_ECHO_INTERVAL_SEC", "0")  # no heartbeat noise in tests
os.environ.setdefault("ACQUIRER_TERMINAL_ID", "TERM0001")
os.environ.setdefault("ACQUIRER_MERCHANT_ID", "SIMULATOR000001")
os.environ.setdefault("PAN_ENCRYPTION_KEY", "test-only-pan-encryption-key")
os.environ.setdefault("AMOUNT_MIN", "0.01")
# The rate limiter keys on the client address, which is the same for every test
# in this file. Raise the ceiling so it does not leak between them.
os.environ.setdefault("RATE_LIMIT_PER_MINUTE", "1000")
# The gateway refuses payments it could not settle, so the test environment
# must represent one that can sign. Anvil's published account #1 — worthless
# on any real network, which is the point.
os.environ.setdefault(
    "WEB3_PRIVATE_KEY",
    "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d",
)

# ── Redis ───────────────────────────────────────────────────────────────────
import fakeredis  # noqa: E402
import redis  # noqa: E402
import redis.asyncio  # noqa: E402

_fake_server = fakeredis.FakeServer()

redis.from_url = lambda *a, **kw: fakeredis.FakeRedis(server=_fake_server)
redis.Redis.from_url = classmethod(lambda cls, *a, **kw: fakeredis.FakeRedis(server=_fake_server))
redis.asyncio.from_url = lambda *a, **kw: fakeredis.aioredis.FakeRedis(server=_fake_server)

import pytest_asyncio  # noqa: E402

from gateway.circuit_breaker import web3_circuit_breaker  # noqa: E402


# ── Fixtures ────────────────────────────────────────────────────────────────


def future_expiry(years: int = 3) -> str:
    """A YYMM expiry that stays valid however long this suite lives."""
    target = datetime.now(timezone.utc) + timedelta(days=365 * years)
    return f"{target.year % 100:02d}{target.month:02d}"


@pytest.fixture
def expiry() -> str:
    return future_expiry()


@pytest.fixture(autouse=True)
def clean_redis():
    """Every test starts with a closed circuit and empty counters."""
    fakeredis.FakeRedis(server=_fake_server).flushall()
    web3_circuit_breaker.reset()
    yield
    fakeredis.FakeRedis(server=_fake_server).flushall()


@pytest_asyncio.fixture(autouse=True)
async def clean_outbox():
    """Empty the outbox before each test.

    The suite shares one in-memory database, so rows written by an earlier test
    would otherwise be counted by a later one — and an assertion like "a
    declined card queues nothing" would fail on someone else's message.
    """
    from sqlalchemy import delete

    from gateway.database import async_session, init_db
    from gateway.models import (
        LiquidityProvider,
        Merchant,
        OutboxMessage,
    )

    await init_db()
    async with async_session() as session:
        await session.execute(delete(OutboxMessage))
        # The prestataire tables share the same in-memory database and their
        # rows accumulate too — a merchant created by one test would collide on
        # its unique wallet with the next.
        await session.execute(delete(Merchant))
        await session.execute(delete(LiquidityProvider))
        await session.commit()
    yield


@pytest_asyncio.fixture
async def bank_server():
    """Run the mock acquirer for the duration of a test."""
    import asyncio

    process = await asyncio.create_subprocess_exec(
        sys.executable,
        os.path.join(os.path.dirname(__file__), "..", "simulator", "bank_server.py"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "SIM_SLOW_DELAY": "2"},
    )
    # Give the listener a moment to bind.
    for _ in range(50):
        await asyncio.sleep(0.05)
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", 9000)
            writer.close()
            break
        except OSError:
            continue

    yield process

    # The simulator may already have exited — a test that kills it, or a crash.
    # terminate() on a reaped process raises ProcessLookupError on Python 3.14,
    # which turns a passing run into 14 teardown errors and fails `make check`.
    if process.returncode is None:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            process.kill()


@pytest_asyncio.fixture
async def gateway_client(bank_server):
    """An HTTP client wired to a live gateway app and the mock acquirer."""
    import httpx

    from gateway.api import acquirer_service, app
    from gateway.database import init_db

    await init_db()
    await acquirer_service.start()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client

    await acquirer_service.stop()
