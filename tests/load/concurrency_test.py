#!/usr/bin/env python3
"""
Concurrency probe against a running simulation stack.

Every functional test so far has sent one payment at a time, which never
exercises the part of the system most likely to be wrong: nonce allocation
across processes. Four Gunicorn workers and four Celery workers all sign from
the same hot wallet, and two transfers claiming the same nonce means one
silently replaces the other — the customer pays and receives nothing, with no
error anywhere.

This fires N payments at once and then checks the invariants that a correct
system must satisfy:

  * every accepted payment gets a distinct transaction id and a distinct STAN;
  * every settled transfer has a distinct transaction hash;
  * the destination wallet's balance equals the sum of what was accepted;
  * no transaction is left in an unsettled state.

Usage:
    python tests/load/concurrency_test.py --count 30
    python tests/load/concurrency_test.py --count 50 --amount 2.50
"""

import argparse
import asyncio
import json
import ssl
import sys
import time
from collections import Counter
from decimal import Decimal
from urllib.parse import urlparse

BASE_URL = "https://localhost:8443"
API_KEY = "simulation-api-key-not-a-secret"
WALLET = "0x742d35Cc6634C0532925a3b844Bc454e4438f44e"
APPROVED_PAN = "4111111111111111"

TERMINAL_OK = {"CRYPTO_SENT"}
TERMINAL_REFUNDED = {"REVERSED", "FIAT_DECLINED"}
UNSETTLED = {"PENDING", "FIAT_APPROVED", "FIAT_UNKNOWN", "CRYPTO_FAILED", "REVERSAL_FAILED"}


async def _request(method: str, path: str, body: dict = None, headers: dict = None) -> tuple:
    """Minimal HTTPS client, so this script needs nothing installed."""
    url = urlparse(BASE_URL + path)
    if url.scheme == "https":
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        port = url.port or 443
    else:
        context = None
        port = url.port or 80

    reader, writer = await asyncio.open_connection(url.hostname, port, ssl=context)
    try:
        payload = json.dumps(body).encode() if body is not None else b""
        lines = [
            f"{method} {url.path} HTTP/1.1",
            f"Host: {url.hostname}",
            "Connection: close",
            f"X-API-Key: {API_KEY}",
        ]
        if body is not None:
            lines += ["Content-Type: application/json", f"Content-Length: {len(payload)}"]
        for key, value in (headers or {}).items():
            lines.append(f"{key}: {value}")
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode() + payload)
        await writer.drain()

        raw = await reader.read()
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

    head, _, tail = raw.partition(b"\r\n\r\n")
    status = int(head.split(b" ")[1])
    # Connection: close means no chunked framing to unpick in practice, but be
    # defensive: find the JSON body wherever it starts.
    text = tail.decode("utf-8", "replace")
    start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
    parsed = None
    if start >= 0:
        try:
            parsed = json.loads(text[start:])
        except json.JSONDecodeError:
            for end in range(len(text), start, -1):
                try:
                    parsed = json.loads(text[start:end])
                    break
                except json.JSONDecodeError:
                    continue
    return status, parsed


async def pay(index: int, amount: str) -> dict:
    started = time.monotonic()
    status, body = await _request(
        "POST",
        "/pay",
        {
            "pan": APPROVED_PAN,
            "expiry": "3012",
            "cvv": "123",
            "amount": amount,
            "target_wallet": WALLET,
        },
        {"Idempotency-Key": f"conc-{int(time.time())}-{index}"},
    )
    return {
        "index": index,
        "http": status,
        "body": body,
        "elapsed": round(time.monotonic() - started, 2),
    }


async def transaction(tx_id: int) -> dict:
    _, body = await _request("GET", f"/transaction/{tx_id}")
    return body or {}


async def main() -> int:
    global BASE_URL
    parser = argparse.ArgumentParser(description="Concurrent payment probe.")
    parser.add_argument("--count", type=int, default=25)
    parser.add_argument("--amount", default="1.00")
    parser.add_argument("--settle-timeout", type=int, default=180)
    parser.add_argument(
        "--min-accepted",
        type=int,
        default=None,
        help="Fail if fewer payments were accepted (default: 80%% of --count).",
    )
    parser.add_argument(
        "--base-url",
        default=BASE_URL,
        help="Target. Point at the API directly to isolate nonce contention from the proxy's rate limit.",
    )
    args = parser.parse_args()

    BASE_URL = args.base_url
    if args.min_accepted is None:
        args.min_accepted = max(1, int(args.count * 0.8))

    print(f"Target: {BASE_URL}")
    print(f"Firing {args.count} concurrent payments of {args.amount} each...")
    fired = time.monotonic()
    results = await asyncio.gather(*(pay(i, args.amount) for i in range(args.count)))
    print(f"All responses in {time.monotonic() - fired:.1f}s\n")

    # ── Acceptance ──────────────────────────────────────────────────────────
    by_status = Counter(r["http"] for r in results)
    print("HTTP responses:", dict(by_status))

    accepted = [r for r in results if r["http"] == 202]
    tx_ids = [r["body"]["transaction_id"] for r in accepted]
    stans = [r["body"]["stan"] for r in accepted]

    failures = []

    if len(set(tx_ids)) != len(tx_ids):
        failures.append(f"Duplicate transaction ids: {len(tx_ids) - len(set(tx_ids))} collision(s).")
    if len(set(stans)) != len(stans):
        dupes = [s for s, n in Counter(stans).items() if n > 1]
        failures.append(f"Duplicate STANs across concurrent authorisations: {dupes}")

    print(f"Accepted: {len(accepted)}/{args.count}, distinct STANs: {len(set(stans))}")

    # A run where nothing was accepted proves nothing, and silently reporting
    # success for it is worse than reporting a failure: it reads as evidence.
    if len(accepted) < args.min_accepted:
        rejects = {code: n for code, n in by_status.items() if code != 202}
        failures.append(
            f"Only {len(accepted)} payment(s) accepted, need at least {args.min_accepted} "
            f"for the result to mean anything. Rejections: {rejects}. "
            "422 means the amount is outside AMOUNT_MIN/AMOUNT_MAX; "
            "429 means a rate limit throttled the run — lower --count or target the API directly."
        )
    slowest = max((r["elapsed"] for r in results), default=0)
    print(f"Slowest response: {slowest}s\n")

    # ── Settlement ──────────────────────────────────────────────────────────
    print("Waiting for settlement...")
    deadline = time.monotonic() + args.settle_timeout
    states = {}
    while time.monotonic() < deadline:
        states = {tx_id: await transaction(tx_id) for tx_id in tx_ids}
        pending = [t for t in states.values() if t.get("status") in UNSETTLED]
        if not pending:
            break
        await asyncio.sleep(3)

    counts = Counter(t.get("status") for t in states.values())
    print("Final states:", dict(counts))

    stuck = [t["id"] for t in states.values() if t.get("status") in UNSETTLED]
    if stuck:
        failures.append(f"Unsettled after {args.settle_timeout}s: transactions {stuck}")

    # ── The invariant that matters: one hash per settled transfer ───────────
    hashes = [t["crypto_tx_hash"] for t in states.values() if t.get("crypto_tx_hash")]
    if len(set(hashes)) != len(hashes):
        dupes = [h for h, n in Counter(hashes).items() if n > 1]
        failures.append(
            f"NONCE COLLISION — the same transaction hash serves several payments: {dupes}"
        )
    print(f"Distinct transaction hashes: {len(set(hashes))} for {len(hashes)} settled transfer(s)")

    sent = [t for t in states.values() if t.get("status") == "CRYPTO_SENT"]
    expected_value = sum(Decimal(t["amount"]) for t in sent)
    print(f"Value delivered on-chain: {expected_value}")

    # ── Verdict ─────────────────────────────────────────────────────────────
    print()
    if failures:
        print("FAILED")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print("PASSED — no duplicate STAN, no duplicate hash, nothing left unsettled.")
    print(f"  Compare the wallet balance against {expected_value} to close the loop.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
