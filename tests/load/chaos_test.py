#!/usr/bin/env python3
"""
Fault injection against the running simulation stack.

The happy path has been exercised plenty. What has not is the set of branches
that decide whether a cardholder gets their money back — and those only run
when something is broken. So this script breaks things on purpose, one at a
time, and asserts the invariant that matters in each case:

    *fiat captured  =>  crypto delivered  OR  cardholder refunded  OR  a
    CRITICAL log line naming a human to fix it.*

Anything else is a silent loss.

Run against a live `make sim` stack, from the host (it drives podman):

    python tests/load/chaos_test.py                # every scenario
    python tests/load/chaos_test.py --only chain-down
    python tests/load/chaos_test.py --list
"""

import argparse
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE_URL = "https://localhost:8443"
API_KEY = "simulation-api-key-not-a-secret"
WALLET = "0x742d35Cc6634C0532925a3b844Bc454e4438f44e"
TOKEN = "0x5FbDB2315678afecb367f032d93F642f64180aa3"
HOT_WALLET = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
ANVIL_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
NETWORK = "libfin_backend-net"
FOUNDRY = "ghcr.io/foundry-rs/foundry:latest"

APPROVED_PAN = "4111111111111111"
REVERSAL_REJECT_PAN = "4000000000000036"

#: States meaning the customer was served or made whole.
SETTLED = {"CRYPTO_SENT", "REVERSED", "FIAT_DECLINED"}
#: States meaning money is owed and a human must act. Acceptable as an outcome
#: only when the system also shouts about it.
NEEDS_HUMAN = {"REVERSAL_FAILED", "FIAT_UNKNOWN"}

GREEN, RED, YELLOW, BLUE, OFF = "\033[32m", "\033[31m", "\033[33m", "\033[34m", "\033[0m"


# ── Plumbing ────────────────────────────────────────────────────────────────


def podman(*args: str, check: bool = False) -> str:
    """Run podman and return stdout **and** stderr combined.

    Celery writes its log records to stderr, so `podman logs` replays them
    there. Returning stdout alone made every alert check come back empty and
    accuse a correctly-behaving system of staying silent.
    """
    result = subprocess.run(
        ["podman", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=180,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"podman {' '.join(args)} failed: {result.stdout.strip()}")
    return result.stdout.strip()


def _cast(*args: str) -> str:
    out = podman("run", "--rm", "--network", NETWORK, "--entrypoint", "cast", FOUNDRY, *args)
    lines = [line for line in out.splitlines() if not line.startswith("Warning")]
    return lines[-1].strip() if lines else ""


def rpc() -> list:
    return ["--rpc-url", "http://gateway-anvil:8545"]


def token_balance(address: str) -> int:
    raw = _cast("call", TOKEN, "balanceOf(address)(uint256)", address, *rpc())
    return int(raw.split()[0].replace(",", "")) if raw else 0


def http(method: str, path: str, body: dict = None, headers: dict = None) -> tuple:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    request = urllib.request.Request(
        BASE_URL + path,
        data=json.dumps(body).encode() if body else None,
        method=method,
        headers={"X-API-Key": API_KEY, "Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, context=context, timeout=60) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return exc.code, {"raw": raw.decode("utf-8", "replace")[:200]}
    except Exception as exc:
        return 0, {"error": str(exc)}


def pay(pan: str = APPROVED_PAN, amount: str = "5.00", key: str = None) -> tuple:
    return http(
        "POST",
        "/pay",
        {
            "pan": pan,
            "expiry": "3012",
            "cvv": "123",
            "amount": amount,
            "target_wallet": WALLET,
        },
        {"Idempotency-Key": key or f"chaos-{time.time()}"},
    )


def wait_for_state(tx_id: int, timeout: int = 180) -> dict:
    """Poll until the transaction leaves the in-flight states."""
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        _, last = http("GET", f"/transaction/{tx_id}")
        if last.get("status") in SETTLED | NEEDS_HUMAN:
            return last
        time.sleep(3)
    return last


def wait_healthy(container: str, timeout: int = 120) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = podman("inspect", container, "--format", "{{.State.Status}}")
        if status == "running":
            health = podman("inspect", container, "--format", "{{.State.Health.Status}}")
            if health in ("", "<no value>", "healthy"):
                return True
        time.sleep(3)
    return False


def wait_api(timeout: int = 120) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, _ = http("GET", "/health")
        if status == 200:
            return True
        time.sleep(2)
    return False


def reset_circuit_breaker() -> None:
    """Close the breaker between scenarios.

    Each scenario deliberately causes failures, which is exactly what opens the
    breaker — correct behaviour, but it makes the next scenario fail for the
    wrong reason. Isolating here keeps every result about the fault under test.
    """
    podman(
        "exec", "gateway-api-sim", "python", "-c",
        "import sys; sys.path.insert(0, '/app/src');"
        "from gateway.circuit_breaker import web3_circuit_breaker;"
        "web3_circuit_breaker.reset()",
    )


def critical_logs(container: str, tx_id: int, timeout: int = 30) -> list:
    """Operator alerts naming this specific transaction.

    Two things this had to learn the hard way. It matches on the transaction id
    rather than a ``--since`` window, because container logs are stamped in
    local time while the harness computed UTC, so the filter silently matched
    nothing and made a passing system look broken. And it waits: log capture is
    asynchronous with respect to the database commit, so the state endpoint can
    report REVERSAL_FAILED a moment before the alert is readable. That lag is
    real, and worth remembering when wiring alerting — an alert pipeline is
    never synchronous with the state it describes.
    """
    deadline = time.time() + timeout
    while True:
        out = podman("logs", container)
        matches = [
            line
            for line in out.splitlines()
            if "MANUAL ACTION REQUIRED" in line and f"transaction {tx_id}" in line
        ]
        if matches or time.time() >= deadline:
            return matches
        time.sleep(2)


# ── Scenarios ───────────────────────────────────────────────────────────────


class Result:
    def __init__(self, name: str):
        self.name = name
        self.problems: list = []
        self.notes: list = []

    def check(self, condition: bool, message: str) -> bool:
        if not condition:
            self.problems.append(message)
        return condition

    def note(self, message: str) -> None:
        self.notes.append(message)

    @property
    def passed(self) -> bool:
        return not self.problems


def scenario_chain_down() -> Result:
    """The chain is unreachable after the card was charged: refund the customer."""
    result = Result("chain-down")
    podman("stop", "gateway-anvil-sim")
    try:
        status, body = pay(amount="5.00")
        result.check(status == 202, f"Expected the fiat leg to be approved, got {status}: {body}")
        if status != 202:
            return result

        tx_id = body["transaction_id"]
        final = wait_for_state(tx_id, timeout=240)
        result.note(f"transaction {tx_id} ended {final.get('status')}")

        result.check(
            final.get("status") in ("REVERSED", "REVERSAL_FAILED"),
            f"Fiat was captured and no crypto could be sent, so the cardholder must be "
            f"refunded. State is {final.get('status')}.",
        )
        result.check(
            not final.get("crypto_tx_hash"),
            "A transaction hash exists even though the chain was unreachable.",
        )
    finally:
        podman("start", "gateway-anvil-sim")
        wait_healthy("gateway-anvil-sim")
    return result


def scenario_bank_down() -> Result:
    """The acquirer is unreachable: refuse up front, debit nothing."""
    result = Result("bank-down")
    before = token_balance(WALLET)
    podman("stop", "gateway-bank-sim")
    try:
        status, body = pay(amount="5.00")
        result.note(f"HTTP {status}")
        result.check(
            status in (502, 503, 504),
            f"With the acquirer down the request must be refused, got {status}: {body}",
        )
        result.check(
            token_balance(WALLET) == before,
            "Tokens moved even though the acquirer was unreachable.",
        )
    finally:
        podman("start", "gateway-bank-sim")
        time.sleep(5)
    return result


def scenario_insufficient_funds() -> Result:
    """The hot wallet is empty: do not retry forever, refund instead."""
    result = Result("insufficient-funds")
    balance = token_balance(HOT_WALLET)
    result.note(f"hot wallet held {balance} units; draining")

    # Move the float out of the way, then put it back afterwards.
    if balance:
        _cast(
            "send", TOKEN, "transfer(address,uint256)",
            "0x000000000000000000000000000000000000dEaD", str(balance),
            "--private-key", "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d",
            *rpc(),
        )
    try:
        status, body = pay(amount="5.00")
        result.check(status == 202, f"Fiat should still be approved, got {status}")
        if status != 202:
            return result

        tx_id = body["transaction_id"]
        started = time.time()
        final = wait_for_state(tx_id, timeout=180)
        elapsed = time.time() - started
        result.note(f"transaction {tx_id} ended {final.get('status')} after {elapsed:.0f}s")

        result.check(
            final.get("status") in ("REVERSED", "REVERSAL_FAILED"),
            f"An empty wallet cannot be fixed by retrying; the cardholder must be "
            f"refunded. State is {final.get('status')}.",
        )
        # InsufficientFunds is classified non-transient, so it must not burn
        # through the retry schedule before giving up.
        result.check(
            elapsed < 120,
            f"Took {elapsed:.0f}s to give up — insufficient funds should not be retried.",
        )
    finally:
        _cast(
            "send", TOKEN, "mint(address,uint256)", HOT_WALLET, str(max(balance, 10**15)),
            "--private-key", ANVIL_KEY, *rpc(),
        )
        result.note(f"hot wallet refilled to {token_balance(HOT_WALLET)} units")
    return result


def scenario_reversal_refused() -> Result:
    """The bank refuses the refund: do not go quiet, name a human."""
    result = Result("reversal-refused")

    # This card makes the simulator approve the sale, then refuse its reversal.
    status, body = pay(pan=REVERSAL_REJECT_PAN, amount="5.00")
    result.check(status == 202, f"Expected approval for the scenario card, got {status}: {body}")
    if status != 202:
        return result
    tx_id = body["transaction_id"]

    podman("stop", "gateway-anvil-sim")
    try:
        final = wait_for_state(tx_id, timeout=240)
        result.note(f"transaction {tx_id} ended {final.get('status')}")

        result.check(
            final.get("status") == "REVERSAL_FAILED",
            f"A refused reversal must be recorded as REVERSAL_FAILED, got "
            f"{final.get('status')}.",
        )
        alerts = critical_logs("gateway-worker-sim", tx_id)
        result.check(
            bool(alerts),
            "Money is owed to a cardholder and nothing logged MANUAL ACTION REQUIRED.",
        )
        if alerts:
            result.note(f"{len(alerts)} operator alert(s) raised")
    finally:
        podman("start", "gateway-anvil-sim")
        wait_healthy("gateway-anvil-sim")
    return result


def scenario_worker_killed() -> Result:
    """The worker dies mid-transfer: recover without paying twice."""
    result = Result("worker-killed")
    before = token_balance(WALLET)

    status, body = pay(amount="5.00")
    result.check(status == 202, f"Expected approval, got {status}")
    if status != 202:
        return result
    tx_id = body["transaction_id"]

    # Kill hard, during the broadcast/confirmation window.
    time.sleep(1.5)
    podman("kill", "gateway-worker-sim")
    result.note("worker killed mid-transfer")
    podman("start", "gateway-worker-sim")
    if not wait_healthy("gateway-worker-sim", timeout=180):
        result.note("worker did not report healthy again in time")

    # Recovery has two paths and the timeout must cover the slower of them:
    # Celery redelivers once the broker's visibility timeout expires, and the
    # reconciliation sweep requeues anything still unsettled. Both are bounded
    # by the longest a transfer attempt can legitimately take.
    final = wait_for_state(tx_id, timeout=int(os.environ.get("CHAOS_RECOVERY_TIMEOUT", "1000")))
    result.note(f"transaction {tx_id} ended {final.get('status')}")

    result.check(
        final.get("status") in SETTLED | NEEDS_HUMAN,
        f"Transaction left in flight after the worker came back: {final.get('status')}.",
    )

    delivered = token_balance(WALLET) - before
    if final.get("status") == "CRYPTO_SENT":
        result.check(
            delivered == 5_000_000,
            f"Expected exactly 5.00 delivered, wallet moved by {delivered} units — "
            "a double send would show up here.",
        )
    elif final.get("status") in ("REVERSED", "REVERSAL_FAILED"):
        result.check(
            delivered == 0,
            f"Transaction was refunded but {delivered} units were still delivered.",
        )
    return result


def scenario_redis_down() -> Result:
    """Redis is gone: refuse payments rather than accept what cannot be tracked."""
    result = Result("redis-down")
    before = token_balance(WALLET)
    podman("stop", "gateway-redis-sim")
    try:
        status, body = pay(amount="5.00")
        result.note(f"HTTP {status}")
        # Nonces, STANs and the staged PAN all live in Redis. Accepting a
        # payment without them means a transfer that cannot be reversed.
        result.check(
            status != 202,
            "A payment was accepted with Redis down: no STAN, no nonce, and no "
            "staged PAN means it could not be reversed if the transfer failed.",
        )
        result.check(
            token_balance(WALLET) == before,
            "Tokens moved while Redis was down.",
        )
    finally:
        podman("start", "gateway-redis-sim")
        wait_healthy("gateway-redis-sim")
        wait_api()
    return result


SCENARIOS = {
    "bank-down": scenario_bank_down,
    "chain-down": scenario_chain_down,
    "insufficient-funds": scenario_insufficient_funds,
    "reversal-refused": scenario_reversal_refused,
    "worker-killed": scenario_worker_killed,
    "redis-down": scenario_redis_down,
}


# ── Driver ──────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description="Fault injection for the simulation stack.")
    parser.add_argument("--only", action="append", choices=sorted(SCENARIOS), help="Run a subset.")
    parser.add_argument("--list", action="store_true", help="List scenarios and exit.")
    args = parser.parse_args()

    if args.list:
        for name, fn in sorted(SCENARIOS.items()):
            print(f"  {name:22} {(fn.__doc__ or '').strip().splitlines()[0]}")
        return 0

    if not wait_api(30):
        print(f"{RED}The gateway is not answering at {BASE_URL}. Start it with `make sim`.{OFF}")
        return 2

    selected = args.only or list(SCENARIOS)
    results = []

    print("=" * 70)
    print(" Fault injection — invariant: fiat captured => crypto sent, refunded,")
    print("                              or a human is told")
    print("=" * 70)

    for name in selected:
        print(f"\n{BLUE}▶ {name}{OFF}")
        started = time.time()
        try:
            reset_circuit_breaker()
            result = SCENARIOS[name]()
        except Exception as exc:
            result = Result(name)
            result.problems.append(f"The scenario itself blew up: {type(exc).__name__}: {exc}")
        results.append(result)

        for note in result.notes:
            print(f"    {note}")
        if result.passed:
            print(f"  {GREEN}PASS{OFF} ({time.time() - started:.0f}s)")
        else:
            print(f"  {RED}FAIL{OFF} ({time.time() - started:.0f}s)")
            for problem in result.problems:
                print(f"    {RED}- {problem}{OFF}")

    print("\n" + "=" * 70)
    failed = [r for r in results if not r.passed]
    if failed:
        print(f"{RED} {len(failed)}/{len(results)} scenario(s) failed: "
              f"{', '.join(r.name for r in failed)}{OFF}")
        return 1
    print(f"{GREEN} All {len(results)} scenario(s) held the invariant.{OFF}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
