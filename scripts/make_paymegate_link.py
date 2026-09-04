#!/usr/bin/env python3
"""
Create a PayMeGate hosted-checkout link to send to a friend/customer.

Runs a real ``POST /pay`` against the locally running gateway, which creates a
genuine PayMeGate order and returns a ``checkout_url``. Send that URL to the
payer: they open it in a browser and pay with their card on PayMeGate's page.
The proceeds (USDT/USDC) land in the wallet configured on the PayMeGate
account — NOT in the ``--wallet`` value, which is only stored locally.

This is a REAL payment flow. When the payer completes checkout, real money
moves to the configured wallet. Do not run this lightly.

Usage:
    python scripts/make_paymegate_link.py --amount 50 --currency USD \
        --email friend@example.com [--wallet 0x...]

Reads from the environment (or the repo-root .env via run helpers):
    GATEWAY_URL       default http://127.0.0.1:8100
    GATEWAY_API_KEY   default local-paymegate-test-key
"""

import argparse
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))


def load_dotenv() -> None:
    """Minimal KEY=VALUE loader for the repo-root .env (setdefault semantics)."""
    env_path = REPO / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"'))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--amount", required=True, type=str)
    parser.add_argument("--currency", default="USD")
    parser.add_argument("--email", default="customer@example.com")
    parser.add_argument("--wallet", default=None)
    parser.add_argument("--name", default=None)
    args = parser.parse_args()

    load_dotenv()
    gw = os.environ.get("GATEWAY_URL", "http://127.0.0.1:8100")
    key = os.environ.get("GATEWAY_API_KEY", "local-paymegate-test-key")
    wallet = args.wallet or os.environ.get(
        "PAYMEGATE_WALLET", "0x813146d4650743D3D633c50f43Ed114f37e2101A"
    )

    payload = {
        "pan": "4111111111111111",  # placeholder only; PayMeGate processes the card
        "expiry": "3012",
        "cvv": "123",
        "amount": args.amount,
        "currency": args.currency,
        "target_wallet": wallet,
        "customer_email": args.email,
    }
    if args.name:
        payload["customer_name"] = args.name

    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{gw}/pay",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-API-Key": key,
            "Idempotency-Key": f"link-{os.getpid()}-{os.urandom(4).hex()}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            result = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        sys.stderr.write(f"HTTP {exc.code}: {exc.read().decode(errors='replace')}\n")
        return 1
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"Gateway unreachable at {gw}: {exc}\nIs the gateway running?\n")
        return 1

    if result.get("status") != "PENDING":
        sys.stderr.write(f"Unexpected response: {json.dumps(result)}\n")
        return 1

    print("\n=== SÉNDEZ CE LIEN AU PAIEUR ===")
    print(result["checkout_url"])
    print()
    print(f"  Commande :  {result.get('order_uuid')}")
    print(f"  Montant  :  {result.get('fiat_amount')} {args.currency}")
    print(f"  Verify   :  GET {gw}/transaction/{result.get('transaction_id')}")
    print("\nL'argent arrive sur le wallet configuré sur le compte PayMeGate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
