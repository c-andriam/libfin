#!/usr/bin/env python3
"""
Run the real gateway locally in PayMeGate (hosted-checkout) mode.

This is the reproducible launcher for a live PayMeGate run. It reads the real
credentials from the gitignored ``.env`` at the repo root (PAYMEGATE_API_KEY,
PAYMEGATE_WEBHOOK_SECRET, …) and starts the actual FastAPI application against
SQLite and an in-process Redis double.

Unlike ``run_gateway_local.py`` (the ISO 8583 simulator flow), this connects to
the real PayMeGate API: creating an order returns a genuine hosted-checkout
URL, and PayMeGate's signed ``order.paid`` webhook delivers the crypto
settlement. The tunnel/return URL must be a public HTTPS endpoint.

Usage:
    python scripts/run_paymegate_local.py [--port 8100] [--database ...]

Environment:
    Reads ``.env`` at the repo root for secrets. Requires a ``.env`` (or the
    individual PAYMEGATE_* variables in the environment).

Never commit the .env. It holds live merchant credentials.
"""

import argparse
import os
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))


def _load_dotenv() -> None:
    """Load KEY=VALUE pairs from the repo-root .env into os.environ.

    A minimal parser (no external dependency): skips blank lines and full-line
    comments, strips an optional leading ``export ``, and ignores trailing
    comments. Existing environment variables win (setdefault semantics).
    """
    env_path = REPO / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _configure() -> None:
    """Ensure the local-run defaults are in place before config snapshots them."""
    defaults = {
        "GATEWAY_MODE": "simulation",
        "ENVIRONMENT": "development",
        "ACQUIRER": "paymegate",
        "AUTO_CREATE_SCHEMA": "true",
        "CORS_ORIGINS": "*",
        "RATE_LIMIT_PER_MINUTE": "1000",
        "AMOUNT_MIN": "0.01",
        "AMOUNT_MAX": "10000.00",
    }
    for name, value in defaults.items():
        os.environ.setdefault(name, value)


def _patch_redis() -> None:
    """Swap Redis for an in-process double before any gateway module loads."""
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
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument(
        "--database",
        default=str(pathlib.Path.home() / ".local-gateway-paymegate.db"),
        help="SQLite file (default: ~/.local-gateway-paymegate.db)",
    )
    args = parser.parse_args()

    _load_dotenv()
    _configure()
    os.environ.setdefault(
        "DATABASE_URL", f"sqlite+aiosqlite:///{args.database}"
    )

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

    print(f"  Gateway  : http://127.0.0.1:{args.port}")
    print(f"  Base     : {args.database}")
    print("  Mode     : PayMeGate (hosted checkout, live API)")
    print("  Ceci est configuré pour des essais locaux — ne pas exposer sans HTTPS/TLS.\n")

    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
