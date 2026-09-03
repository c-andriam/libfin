#!/usr/bin/env python3
"""
Mint a temporary session for the link-management console.

Run this from the backend only — there is deliberately no HTTP endpoint that
does the same thing. The token it prints substitutes for GATEWAY_API_KEY on
``/links`` alone (see MANAGEMENT_PATHS in gateway/api.py) and expires on its
own; nothing needs to be revoked by hand.

Usage:
    python scripts/connect.py                                # bare token, 240 minutes
    python scripts/connect.py --base-url https://example.com  # ready-to-open link
    python scripts/connect.py --ttl 900                        # shorter-lived

Make targets:
    make connect [TTL=14400]       # against production
    make sim-connect [TTL=14400]   # against the simulation stack
"""

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from gateway.connect_sessions import create  # noqa: E402

#: 240 minutes. Long enough that an operator working through the console does
#: not get logged out mid-task; short enough that a session left in a chat
#: log or a shared clipboard is not a standing credential.
DEFAULT_TTL_SEC = 240 * 60


def main() -> int:
    parser = argparse.ArgumentParser(description="Mint a temporary link-console session.")
    parser.add_argument(
        "--ttl",
        type=int,
        default=int(os.environ.get("CONNECT_SESSION_TTL_SEC", str(DEFAULT_TTL_SEC))),
        help=f"Session lifetime in seconds (default: {DEFAULT_TTL_SEC}, "
             "or $CONNECT_SESSION_TTL_SEC).",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("CONNECT_BASE_URL", ""),
        help="Origin serving links.html. When given, prints a ready-to-open URL "
             "instead of a bare token.",
    )
    args = parser.parse_args()

    if args.ttl <= 0:
        parser.error("--ttl must be positive.")

    token = create(args.ttl)
    expires = datetime.now(timezone.utc) + timedelta(seconds=args.ttl)

    print("")
    print(f"  Session temporaire créée — expire à {expires.strftime('%H:%M:%S UTC')} ({args.ttl}s).")
    print("")
    if args.base_url:
        url = f"{args.base_url.rstrip('/')}/links.html#connect={token}"
        print(f"  {url}")
    else:
        print(f"  Jeton : {token}")
        print("  Ouvrez .../links.html#connect=<jeton>, ou collez-le dans « Connexion ».")
    print("")
    print("  Quiconque l'utilise peut lister, activer/désactiver et supprimer des")
    print("  liens de paiement jusqu'à l'expiration ci-dessus. Ne le transmettez")
    print("  qu'à la personne qui doit s'en servir.")
    print("")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
