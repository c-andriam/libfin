#!/usr/bin/env python3
"""
Validate the crypto service against a real public chain.

Anvil is a convenient liar. It mines on a timer, has no fee market, never
reorgs, never rate-limits, and answers every RPC instantly. Code that works
against it can still fail on a real network for reasons that have nothing to do
with logic — a provider that throttles, a base fee that moved between the
estimate and the broadcast, a chain that takes twelve seconds a block instead of
one.

This exercises the parts of ``CryptoService`` that can be checked without
spending anything: chain identification, fee construction against a live fee
market, reading a real ERC-20, provider rotation, and receipt lookup on a real
historical transaction.

Explicitly *not* covered: broadcasting. That needs a funded key, and a faucet
needs a human. Signing and nonce allocation are exercised by the local stack;
what this adds is confidence that the surrounding assumptions survive contact
with a real network.

    python tests/load/testnet_check.py
    WEB3_RPC_URL=https://... python tests/load/testnet_check.py
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

SEPOLIA_CHAIN_ID = 11155111
DEFAULT_RPC = "https://ethereum-sepolia-rpc.publicnode.com"
# LINK on Sepolia: a long-lived, widely-used ERC-20 to read real metadata from.
SEPOLIA_LINK = "0x779877A7B0D9E8603169DdbD7836e478b4624789"

GREEN, RED, YELLOW, BLUE, OFF = "\033[32m", "\033[31m", "\033[33m", "\033[34m", "\033[0m"

problems = []
warnings = []


def ok(message):
    print(f"  {GREEN}[ ok ]{OFF} {message}")


def fail(message):
    print(f"  {RED}[FAIL]{OFF} {message}")
    problems.append(message)


def warn(message):
    print(f"  {YELLOW}[warn]{OFF} {message}")
    warnings.append(message)


def section(title):
    print(f"\n── {title} " + "─" * max(0, 56 - len(title)))


def main() -> int:
    rpc = os.environ.get("WEB3_RPC_URL", DEFAULT_RPC)
    chain_id = int(os.environ.get("WEB3_CHAIN_ID", SEPOLIA_CHAIN_ID))
    token = os.environ.get("ERC20_TOKEN_ADDRESS", SEPOLIA_LINK)

    # Configure before importing: settings snapshots the environment once.
    os.environ["WEB3_RPC_URL"] = rpc
    os.environ["WEB3_CHAIN_ID"] = str(chain_id)
    os.environ["ERC20_TOKEN_ADDRESS"] = token
    os.environ.setdefault("GATEWAY_MODE", "simulation")
    os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6379/0")

    import asyncio

    from gateway.config import settings
    from gateway.crypto_service import CryptoService

    print("=" * 66)
    print(f" Real-chain validation against {rpc}")
    print("=" * 66)

    # ── Connectivity and identity ───────────────────────────────────────────
    section("Connectivity")
    started = time.time()
    try:
        service = CryptoService(rpc_urls=[rpc])
    except Exception as exc:
        fail(f"CryptoService could not be built: {exc}")
        return 1

    if service.is_connected():
        ok(f"Connected in {time.time() - started:.1f}s")
    else:
        fail("The RPC did not answer; nothing else can be checked.")
        return 1

    actual_chain = service.w3.eth.chain_id
    if actual_chain == chain_id:
        ok(f"Chain id {actual_chain}, as configured")
    else:
        fail(f"Chain id is {actual_chain}, configured {chain_id}")

    section("Wrong-chain guard")
    try:
        service.verify_chain()
        ok("verify_chain accepts the configured chain")
    except Exception as exc:
        fail(f"verify_chain rejected the right chain: {exc}")

    original = settings.web3_chain_id
    settings.web3_chain_id = 999999
    try:
        service.verify_chain()
        fail("verify_chain accepted a chain id it should have refused — "
             "a misconfigured RPC could get real transfers signed on the wrong network.")
    except ConnectionError:
        ok("verify_chain refuses to sign against the wrong chain")
    except Exception as exc:
        warn(f"verify_chain raised {type(exc).__name__} rather than ConnectionError: {exc}")
    finally:
        settings.web3_chain_id = original

    # ── Fee market ──────────────────────────────────────────────────────────
    section("Fee market")
    try:
        fees = service._fee_parameters()
        if "maxFeePerGas" in fees:
            max_fee_gwei = fees["maxFeePerGas"] / 1e9
            tip_gwei = fees["maxPriorityFeePerGas"] / 1e9
            base = service.w3.eth.get_block("latest")["baseFeePerGas"] / 1e9
            ok(f"EIP-1559: base {base:.3f} gwei, tip {tip_gwei:.3f}, max {max_fee_gwei:.3f}")

            # A transfer is ~65k gas. Sanity-check what a single one would cost.
            cost_eth = (fees["maxFeePerGas"] * 65000) / 1e18
            print(f"         a 65k-gas transfer would cap at {cost_eth:.6f} ETH")
            if max_fee_gwei > 500:
                warn(f"maxFeePerGas is {max_fee_gwei:.0f} gwei. With no absolute ceiling "
                     "configured, a fee spike is paid in full.")
        else:
            ok(f"Legacy gas price: {fees['gasPrice'] / 1e9:.3f} gwei")
    except Exception as exc:
        fail(f"Fee construction failed against a live fee market: {exc}")

    # ── Real ERC-20 ─────────────────────────────────────────────────────────
    section("Real ERC-20")

    async def read_token():
        decimals = await service.get_decimals(token)
        units = await service.compute_token_units(token, __import__("decimal").Decimal("12.34"))
        return decimals, units

    try:
        decimals, units = asyncio.run(read_token())
        ok(f"decimals() on {token[:10]}… returned {decimals}")
        expected = int(12.34 * (10 ** decimals))
        if units == expected:
            ok(f"12.34 converts to {units} units")
        else:
            fail(f"12.34 converted to {units} units, expected {expected}")
    except Exception as exc:
        fail(f"Could not read the token contract: {exc}")

    # ── Block cadence ───────────────────────────────────────────────────────
    section("Block cadence")
    try:
        first = service.w3.eth.block_number
        print("         sampling block time (up to 40s)...")
        deadline = time.time() + 40
        started = time.time()
        while service.w3.eth.block_number == first and time.time() < deadline:
            time.sleep(2)
        moved = service.w3.eth.block_number - first
        if moved:
            seconds = time.time() - started
            ok(f"Advanced {moved} block(s) in {seconds:.0f}s")
            confirmations = settings.web3_confirmations
            estimate = seconds * confirmations
            print(f"         {confirmations} confirmations ≈ {estimate:.0f}s of customer wait")
            if estimate > settings.web3_confirmation_timeout_sec:
                fail(f"WEB3_CONFIRMATION_TIMEOUT_SEC is "
                     f"{settings.web3_confirmation_timeout_sec}s but {confirmations} "
                     f"confirmations need about {estimate:.0f}s here — every transfer would "
                     "time out and be reversed.")
        else:
            warn("No new block in 40s; the chain or the provider may be stalled.")
    except Exception as exc:
        warn(f"Could not sample block cadence: {exc}")

    # ── Receipt lookup ──────────────────────────────────────────────────────
    section("Receipt lookup")

    async def check_statuses():
        results = {}
        block = service.w3.eth.get_block("latest", full_transactions=True)
        sample = block.transactions[0].hash.hex() if block.transactions else None
        if sample:
            if not sample.startswith("0x"):
                sample = "0x" + sample
            results["real"] = await service.get_onchain_status(sample)
        results["absent"] = await service.get_onchain_status("0x" + "11" * 32)
        return results

    try:
        statuses = asyncio.run(check_statuses())
        if "real" in statuses:
            if statuses["real"] in ("success", "failed"):
                ok(f"A mined transaction reads back as '{statuses['real']}'")
            else:
                fail(f"A mined transaction reads back as '{statuses['real']}' — the reversal "
                     "guard depends on this being accurate.")
        else:
            warn("The latest block was empty; skipped the mined-transaction check.")

        if statuses["absent"] == "unknown":
            ok("An unknown hash reads back as 'unknown'")
        else:
            fail(f"An unknown hash reads back as '{statuses['absent']}', which would make "
                 "the reversal guard treat a nonexistent transfer as in flight.")
    except Exception as exc:
        fail(f"Receipt lookup failed: {exc}")

    # ── Provider rotation ───────────────────────────────────────────────────
    section("Provider rotation")
    try:
        failover = CryptoService(rpc_urls=["http://127.0.0.1:9", rpc])
        if failover.is_connected() and failover.active_rpc == rpc:
            ok("Skipped a dead provider and settled on the working one")
        else:
            fail(f"Failover did not reach the working provider (active: {failover.active_rpc})")

        rotating = CryptoService(rpc_urls=[rpc])
        rotating.rpc_urls = [rpc, "http://127.0.0.1:9"]
        if rotating.rotate_rpc():
            ok("rotate_rpc keeps a working provider when the alternative is dead")
        else:
            fail("rotate_rpc dropped the connection entirely.")
    except Exception as exc:
        fail(f"Provider rotation raised: {exc}")

    # ── Rate limiting ───────────────────────────────────────────────────────
    section("Provider throttling")
    try:
        started = time.time()
        errors = 0
        for _ in range(25):
            try:
                service.w3.eth.block_number
            except Exception:
                errors += 1
        elapsed = time.time() - started
        if errors:
            warn(f"{errors}/25 rapid calls failed in {elapsed:.1f}s — this provider throttles. "
                 "Configure WEB3_RPC_URL_BACKUP and expect rotation under load.")
        else:
            ok(f"25 rapid calls in {elapsed:.1f}s with no rejection")
    except Exception as exc:
        warn(f"Throttling probe failed: {exc}")

    # ── Verdict ─────────────────────────────────────────────────────────────
    print("\n" + "=" * 66)
    print(f"{BLUE} Not covered: broadcasting a transfer. That needs a funded key,{OFF}")
    print(f"{BLUE} and a faucet needs a human. Signing and nonce allocation are{OFF}")
    print(f"{BLUE} covered by the local stack's concurrency test.{OFF}")
    print("=" * 66)

    if problems:
        print(f"{RED} FAILED — {len(problems)} problem(s), {len(warnings)} warning(s).{OFF}")
        return 1
    if warnings:
        print(f"{YELLOW} PASSED with {len(warnings)} warning(s).{OFF}")
    else:
        print(f"{GREEN} PASSED — the service behaves correctly against a real chain.{OFF}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
