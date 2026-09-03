#!/usr/bin/env python3
"""
Configure the crypto payout wallet PayMeGate settles to.

In PayMeGate mode the collected fiat is paid out by PayMeGate itself — not by
this gateway — so the receiving wallet is configured directly on PayMeGate's
side via ``PUT /v1/wallet``. This script is a thin, interactive guard around
that call: it checks the address matches the chosen network before anything is
sent, so a T.. address is not silently wired to the every-day EVM field.

Usage:
    PAYMEGATE_API_KEY=pmg_live_... scripts/paymegate_setup.py wallet T.. --network trc20
    PAYMEGATE_API_KEY=pmg_live_... scripts/paymegate_setup.py wallet 0x... --network evm
    PAYMEGATE_API_KEY=pmg_live_... scripts/paymegate_setup.py show

Networks / typical Trust Wallet assets:
    trc20   USDT TRC-20 on TRON   (T.. — the most common for such providers)
    evm     USDC/USDT ERC-20 on Ethereum / Base / BNB / Arbitrum / Avalanche (0x...)

Requires the gateway extra: ``pip install -e '.[gateway]'``. It only talks to
PayMeGate; it does not need Postgres, Redis or a chain.
"""

import argparse
import asyncio
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

#: Net short on arbitrary typing; the two regexes are the real specification.
TRC20_RE = re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$")
EVM_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


def _load_settings() -> dict:
    from gateway.config import settings

    return {
        "api_key": settings.paymegate_api_key,
        "base_url": settings.paymegate_base_url,
    }


def _format_wallet(payload: dict) -> None:
    # The API wraps results in {"success": true, "data": {…}}; some builds also
    # return the wallets map at the top level. Flatten defensively.
    if isinstance(payload, dict):
        data = payload.get("data", payload)
        wallets = data.get("cryptoWallets") or data.get("wallet") or data
    else:
        wallets = payload
    if isinstance(wallets, dict):
        for key, value in wallets.items():
            print(f"  {key:8} {value}")
    else:
        print(f"  {wallets}")


def _run(coro):
    """Drive one client coroutine to completion.

    Every PayMeGateClient method is ``async``. Calling one without awaiting it
    builds a coroutine and discards it: nothing is sent, no exception is raised,
    and the caller goes on to print a success message for a request that never
    left the process. That is exactly what this script did until now — the
    payout wallet was never configured, and it said it was.

    A command-line tool has no event loop of its own, so one is created per call
    rather than making this whole script async for three short commands.
    """
    return asyncio.run(coro)


def cmd_show(args) -> int:
    """Print what PayMeGate currently holds for our wallet."""
    from gateway.paymegate import PayMeGateClient

    client = PayMeGateClient()
    try:
        data = _run(client._request("GET", "/v1/wallet"))
    except Exception as exc:  # noqa: BLE001
        print(f"  Could not read the wallet: {exc}", file=sys.stderr)
        return 1
    print("  Current payout configuration:")
    _format_wallet(data.get("data", data))
    return 0


def cmd_wallet(args) -> int:
    from gateway.paymegate import PayMeGateClient

    wallet = args.wallet
    network = args.network

    if network == "trc20":
        if not TRC20_RE.match(wallet):
            print(
                f"  {wallet!r} is not a valid TRC-20 (TRON) address (T..).",
                file=sys.stderr,
            )
            return 1
        kwargs = {"trc20": wallet}
    elif network == "evm":
        if not EVM_RE.match(wallet):
            print(f"  {wallet!r} is not a valid EVM (0x..) address.", file=sys.stderr)
            return 1
        kwargs = {"evm": wallet}
    else:
        print(f"  Unknown network {network!r}: choose trc20 or evm.", file=sys.stderr)
        return 1

    account = f"Trust Wallet ({network})"
    confirm = input(
        f"  Configure PayMeGate to settle to:\n"
        f"      {account}: {wallet}\n"
        f"  Type 'yes' to confirm: "
    )
    if confirm.strip().lower() != "yes":
        print("  Aborted.")
        return 1

    client = PayMeGateClient()
    try:
        data = _run(client.update_wallet(**kwargs, include_crypto=True))
    except Exception as exc:  # noqa: BLE001
        print(f"  PayMeGate rejected the wallet update: {exc}", file=sys.stderr)
        return 1

    print("  Wallet updated on PayMeGate:")
    _format_wallet(data)
    return 0


def cmd_webhook(args) -> int:
    """Register the callback URL and print the signing secret it returns.

    PayMeGate shows the secret once. It is printed here and nowhere else: not
    logged, not written to a file, because a secret in a log is a secret you
    have to go and delete afterwards.
    """
    from gateway.paymegate import PayMeGateClient

    print(f"  Registering {args.url}")
    print("  PayMeGate will POST order.paid events there, signed with the")
    print("  secret it returns below. That URL must already be reachable")
    print("  from the public internet — PayMeGate calls you, not the reverse.")
    confirm = input("  Type 'yes' to confirm: ")
    if confirm.strip().lower() != "yes":
        print("  Aborted.")
        return 1

    client = PayMeGateClient()
    try:
        data = _run(client.set_webhook(args.url))
    except Exception as exc:  # noqa: BLE001
        print(f"  PayMeGate rejected the webhook update: {exc}", file=sys.stderr)
        return 1

    secret = data.get("secret") or data.get("signingSecret") or ""
    print()
    print("  Webhook registered.")
    if secret:
        print(f"  PAYMEGATE_WEBHOOK_SECRET={secret}")
        print()
        print("  Copy that into your environment now. PayMeGate does not show")
        print("  it again; registering the webhook a second time issues a new")
        print("  one and breaks verification until the new value is deployed.")
    else:
        print("  No secret in the response — read it from the PayMeGate")
        print(f"  dashboard. Raw response: {data}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Configure the crypto payout wallet on PayMeGate."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_wallet = sub.add_parser("wallet", help="point the payout at an address")
    p_wallet.add_argument("wallet", help="the receiving address (T.. or 0x..)")
    p_wallet.add_argument(
        "--network", choices=["trc20", "evm"], required=True,
        help="trc20 for TRON USDT, evm for ERC-20 USDC/USDT",
    )
    p_wallet.set_defaults(func=cmd_wallet)

    p_hook = sub.add_parser("webhook", help="register the callback URL and get its secret")
    p_hook.add_argument("url", help="public HTTPS URL, e.g. https://pay.example.com/webhook/paymegate")
    p_hook.set_defaults(func=cmd_webhook)

    p_show = sub.add_parser("show", help="print the current payout configuration")
    p_show.set_defaults(func=cmd_show)

    args = parser.parse_args()

    try:
        loaded = _load_settings()
    except Exception as exc:  # noqa: BLE001
        print(f"  Configuration error: {exc}", file=sys.stderr)
        return 1

    if not loaded["api_key"]:
        print(
            "  PAYMEGATE_API_KEY is empty. Set it in the environment before running.",
            file=sys.stderr,
        )
        return 1

    print(f"  PayMeGate base: {loaded['base_url']}")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
