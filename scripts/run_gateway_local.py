#!/usr/bin/env python3
"""
Run the real gateway locally, without Postgres, Redis, Vault or a chain.

The container stack (``make sim``) is the faithful way to exercise this
service. It needs Podman, and Podman is not always there — on a bare
workstation, or in CI for a quick check. This script covers that gap: it runs
the actual FastAPI application against SQLite and an in-process Redis double,
so the frontend has something real to talk to.

What is real here: the FastAPI app and its middlewares, Pydantic validation,
libfin's ISO 8583 encoding, the TCP dialogue with the acquirer, the database
and the transaction state machine.

What is not: the store (SQLite), Redis (fakeredis), the acquirer
(tests/simulator/bank_server.py) and the chain — there is none, so payments
stop at ``FIAT_AUTHORIZED``. Settlement needs ``make sim``, which brings up a
local chain and a Celery worker.

Never point this at a real acquirer or a real card. It listens in clear HTTP
and holds no secret properly.

Usage:
    # in one terminal, the simulated acquirer
    python tests/simulator/bank_server.py

    # in another, the gateway
    python scripts/run_gateway_local.py --port 8100

    # and the form, relaying to it
    GATEWAY_API_KEY=simulation-api-key-not-a-secret \\
      python frontend/serve.py --gateway http://127.0.0.1:8100

Requires the gateway extra: ``pip install -e '.[gateway,test]'``.
"""

import argparse
import os
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))


def _configure(db_path: str, api_key: str) -> None:
    """Set the environment before ``gateway.config`` snapshots it — it reads once."""
    defaults = {
        "GATEWAY_MODE": "simulation",
        "ENVIRONMENT": "development",
        "DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        "AUTO_CREATE_SCHEMA": "true",
        "BANK_HOST": "127.0.0.1",
        "BANK_PORT": "9000",
        "BANK_ECHO_INTERVAL_SEC": "0",
        "ACQUIRER_TERMINAL_ID": "TERM0001",
        "ACQUIRER_MERCHANT_ID": "SIMULATOR000001",
        "PAN_ENCRYPTION_KEY": "local-only-pan-encryption-key",
        "GATEWAY_API_KEY": api_key,
        "CORS_ORIGINS": "*",
        "RATE_LIMIT_PER_MINUTE": "1000",
        "AMOUNT_MIN": "0.01",
        # Anvil's published account #1. Worthless on any real network, which is
        # the point: the gateway refuses payments it could not settle, so it
        # needs a key it can load — not one that owns anything.
        "WEB3_PRIVATE_KEY": (
            "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
        ),
    }
    for name, value in defaults.items():
        os.environ.setdefault(name, value)


def _patch_redis() -> None:
    """Swap Redis for an in-process double, before any gateway module loads.

    Several of them build a client at import time — the circuit breaker, the
    PAN vault, the STAN allocator. Patching afterwards would leave those
    holding real sockets, which is why this runs first and why the gateway
    imports live inside ``main`` rather than at the top of the file.
    """
    import fakeredis
    import redis
    import redis.asyncio

    server = fakeredis.FakeServer()
    redis.from_url = lambda *a, **kw: fakeredis.FakeRedis(server=server)
    redis.Redis.from_url = classmethod(
        lambda cls, *a, **kw: fakeredis.FakeRedis(server=server)
    )
    redis.asyncio.from_url = lambda *a, **kw: fakeredis.aioredis.FakeRedis(server=server)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--port", type=int, default=8100, help="port to listen on (default: 8100)")
    parser.add_argument(
        "--database",
        default=str(REPO / ".local-gateway.db"),
        help="SQLite file to use (default: .local-gateway.db at the repo root)",
    )
    parser.add_argument(
        "--api-key",
        default="simulation-api-key-not-a-secret",
        help="value the gateway will expect in X-API-Key",
    )
    args = parser.parse_args()

    _configure(args.database, args.api_key)
    try:
        _patch_redis()
        import uvicorn
    except ImportError as exc:
        print(
            f"  Missing dependency: {exc.name}. Install the extras with:\n"
            "      pip install -e '.[gateway,test]'",
            file=sys.stderr,
        )
        return 1

    from gateway.api import app

    print(f"  Gateway        : http://127.0.0.1:{args.port}")
    print(f"  Base           : {args.database}")
    print(f"  Clé d'API      : {args.api_key}")
    print("  Acquéreur      : 127.0.0.1:9000 — lancez tests/simulator/bank_server.py")
    print("  Chaîne         : aucune, les paiements s'arrêtent à FIAT_AUTHORIZED")
    print("  Ceci n'est PAS un environnement de production.\n")

    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
