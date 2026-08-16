"""
ERC-20 transfer service.

Three properties matter here, and each one corresponds to a way the previous
implementation could lose money:

  * **One nonce, many processes.** Gunicorn runs several API workers and Celery
    several more; an in-process ``asyncio.Lock`` does not serialise them, so two
    transfers could claim the same nonce and one would silently replace the
    other. Nonces are now allocated under a Redis lock.
  * **Broadcasting and confirming are separate steps.** The caller persists the
    hash the moment it is broadcast, so a crash while waiting for confirmations
    can never orphan a transaction that is already on-chain.
  * **Nothing waits forever.** Every wait is bounded, so a stalled chain cannot
    pin a worker slot indefinitely.
"""

import asyncio
import logging
import secrets
import time
from decimal import ROUND_DOWN, Decimal
from typing import Optional, Tuple

import redis.asyncio as redis
from web3 import Web3

from gateway.config import settings

LOGGER = logging.getLogger(__name__)

# Minimal ERC-20 ABI: transfer, decimals, balanceOf.
ERC20_ABI = [
    {
        "constant": False,
        "inputs": [{"name": "_to", "type": "address"}, {"name": "_value", "type": "uint256"}],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "payable": False,
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "payable": False,
        "stateMutability": "view",
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "payable": False,
        "stateMutability": "view",
        "type": "function",
    },
]

#: How far the cached nonce may legitimately run ahead of the chain's own
#: count. A handful of transactions can be in flight at once; a hundred means
#: the cache and the chain disagree about history.
_MAX_NONCE_GAP = 50

_NONCE_LOCK_KEY = "web3:nonce:lock:{address}"
_NONCE_VALUE_KEY = "web3:nonce:next:{address}"

_RELEASE_LOCK_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


class InsufficientFunds(RuntimeError):
    """The hot wallet cannot cover the transfer. Never retry blindly."""


class CryptoService:
    def __init__(
        self,
        rpc_urls: Optional[list] = None,
        private_key: Optional[str] = None,
        redis_url: Optional[str] = None,
    ):
        self.rpc_urls = list(rpc_urls or settings.web3_rpc_urls)
        if not self.rpc_urls:
            raise ValueError("At least one Web3 RPC URL is required.")

        self._rpc_index = 0
        self.w3 = self._build_provider(self.rpc_urls[0])
        self._select_working_rpc()

        self._redis_url = redis_url or settings.redis_url
        self._redis: Optional[redis.Redis] = None
        self._decimals_cache: dict = {}

        self.private_key = private_key or self._fetch_key_from_vault() or settings.web3_private_key
        if self.private_key:
            self.account = self.w3.eth.account.from_key(self.private_key)
            LOGGER.info(f"CryptoService ready. Hot wallet: {self.account.address}")
        else:
            self.account = None
            LOGGER.warning("No private key available (Vault or env). Crypto transfers will fail.")

    # ── RPC plumbing ────────────────────────────────────────────────────────

    @staticmethod
    def _build_provider(url: str) -> Web3:
        return Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 30}))

    @property
    def active_rpc(self) -> str:
        return self.rpc_urls[self._rpc_index]

    def _select_working_rpc(self) -> bool:
        """Point ``self.w3`` at the first responsive provider. Rotates on failure."""
        for offset in range(len(self.rpc_urls)):
            index = (self._rpc_index + offset) % len(self.rpc_urls)
            candidate = self._build_provider(self.rpc_urls[index])
            try:
                if candidate.is_connected():
                    if index != self._rpc_index or self.w3 is None:
                        LOGGER.info(f"Using Web3 RPC: {self.rpc_urls[index]}")
                    self._rpc_index = index
                    self.w3 = candidate
                    # Called once from __init__ before private_key is assigned;
                    # getattr keeps that first call from raising into the
                    # handler below, where it would masquerade as an
                    # unreachable RPC and hide the real state of the network.
                    key = getattr(self, "private_key", None)
                    if key:
                        self.account = self.w3.eth.account.from_key(key)
                    return True
            except Exception as exc:
                LOGGER.warning(f"RPC {self.rpc_urls[index]} unreachable: {exc}")

        LOGGER.error("No Web3 RPC provider is reachable.")
        return False

    def rotate_rpc(self) -> bool:
        """Move to the next provider after a failure on the current one."""
        if len(self.rpc_urls) > 1:
            LOGGER.warning(f"Rotating away from RPC {self.active_rpc}")
            self._rpc_index = (self._rpc_index + 1) % len(self.rpc_urls)
        return self._select_working_rpc()

    def is_connected(self) -> bool:
        try:
            return self.w3.is_connected()
        except Exception:
            return False

    def ensure_connected(self) -> None:
        if not self.is_connected() and not self.rotate_rpc():
            raise ConnectionError("Cannot connect to any Web3 RPC.")

    def verify_chain(self) -> None:
        """Refuse to sign against the wrong network."""
        if not settings.web3_chain_id:
            return
        actual = self.w3.eth.chain_id
        if actual != settings.web3_chain_id:
            raise ConnectionError(
                f"RPC reports chain id {actual}, expected {settings.web3_chain_id}. "
                "Refusing to sign — check WEB3_RPC_URL."
            )

    def _fetch_key_from_vault(self) -> Optional[str]:
        if not settings.vault_addr or not settings.vault_token:
            return None
        try:
            import hvac

            client = hvac.Client(url=settings.vault_addr, token=settings.vault_token)
            if not client.is_authenticated():
                LOGGER.warning("Vault rejected the token; falling back to the environment.")
                return None
            secret = client.secrets.kv.v2.read_secret_version(
                path="gateway/web3", mount_point="secret", raise_on_deleted_version=True
            )
            return secret["data"]["data"].get("private_key")
        except Exception as exc:
            LOGGER.warning(f"Could not read the Web3 key from Vault: {exc}")
            return None

    # ── Distributed nonce allocation ────────────────────────────────────────

    @property
    def redis(self) -> redis.Redis:
        if self._redis is None:
            self._redis = redis.from_url(self._redis_url)
        return self._redis

    async def _acquire_nonce_lock(self, timeout_sec: float = 30.0) -> str:
        key = _NONCE_LOCK_KEY.format(address=self.account.address)
        token = secrets.token_hex(16)
        deadline = time.monotonic() + timeout_sec
        delay = 0.05
        while time.monotonic() < deadline:
            # The TTL bounds the damage if a holder dies mid-broadcast.
            if await self.redis.set(key, token, nx=True, ex=120):
                return token
            await asyncio.sleep(delay)
            delay = min(delay * 2, 1.0)
        raise TimeoutError("Timed out waiting for the nonce lock; another transfer is in flight.")

    async def _release_nonce_lock(self, token: str) -> None:
        key = _NONCE_LOCK_KEY.format(address=self.account.address)
        try:
            await self.redis.eval(_RELEASE_LOCK_LUA, 1, key, token)
        except Exception as exc:
            LOGGER.warning(f"Could not release the nonce lock cleanly: {exc}")

    async def _next_nonce(self) -> int:
        """Highest of the chain's pending nonce and our own bookkeeping.

        The chain's ``pending`` count lags when several transactions are
        broadcast back to back, so we remember what we handed out last.
        """
        chain_nonce = await asyncio.to_thread(
            self.w3.eth.get_transaction_count, self.account.address, "pending"
        )
        key = _NONCE_VALUE_KEY.format(address=self.account.address)
        try:
            cached = await self.redis.get(key)
            if cached is not None:
                cached_nonce = int(cached)
                # Sanity bound. The cache is meant to cover the short window
                # where the chain has not yet counted a just-broadcast
                # transaction. A gap wider than that means the cache is stale
                # relative to the chain — a restored Redis backup, or a test
                # chain that was reset. Trusting it then produces a nonce gap,
                # and the transfer sits in the mempool forever: broadcast, never
                # mined, impossible to reverse because it is still "pending".
                if cached_nonce - chain_nonce > _MAX_NONCE_GAP:
                    LOGGER.error(
                        f"Cached nonce {cached_nonce} is {cached_nonce - chain_nonce} ahead of "
                        f"the chain's {chain_nonce}; discarding the cache and trusting the chain."
                    )
                    await self.redis.delete(key)
                    return chain_nonce
                return max(chain_nonce, cached_nonce)
        except Exception as exc:
            LOGGER.warning(f"Nonce cache unreadable ({exc}); trusting the chain.")
        return chain_nonce

    async def _remember_nonce(self, nonce: int) -> None:
        key = _NONCE_VALUE_KEY.format(address=self.account.address)
        try:
            # Expire well after any transaction could still be pending.
            await self.redis.set(key, nonce + 1, ex=3600)
        except Exception as exc:
            LOGGER.warning(f"Could not persist the next nonce ({exc}).")

    # ── Token helpers ───────────────────────────────────────────────────────

    async def get_decimals(self, token_address: str) -> int:
        token_address = self.w3.to_checksum_address(token_address)
        if token_address not in self._decimals_cache:
            contract = self.w3.eth.contract(address=token_address, abi=ERC20_ABI)
            self._decimals_cache[token_address] = await asyncio.to_thread(
                contract.functions.decimals().call
            )
        return self._decimals_cache[token_address]

    async def token_balance(self, token_address: str) -> int:
        token_address = self.w3.to_checksum_address(token_address)
        contract = self.w3.eth.contract(address=token_address, abi=ERC20_ABI)
        return await asyncio.to_thread(
            contract.functions.balanceOf(self.account.address).call
        )

    async def compute_token_units(
        self, token_address: str, amount_fiat: Decimal, exchange_rate: Optional[Decimal] = None
    ) -> int:
        """Convert a fiat amount to the token's smallest unit.

        Rounds *down*: the gateway never sends more than it was paid for.
        """
        rate = Decimal(str(exchange_rate if exchange_rate is not None else settings.exchange_rate))
        if rate <= 0:
            raise ValueError("Exchange rate must be positive.")
        decimals = await self.get_decimals(token_address)
        units = (Decimal(str(amount_fiat)) / rate) * (Decimal(10) ** decimals)
        return int(units.to_integral_value(rounding=ROUND_DOWN))

    def _fee_parameters(self) -> dict:
        """EIP-1559 fees, falling back to legacy gas price on chains without them."""
        latest = self.w3.eth.get_block("latest")
        base_fee = latest.get("baseFeePerGas")
        if base_fee is None:
            return {"gasPrice": self.w3.eth.gas_price}
        priority = self.w3.to_wei(2, "gwei")
        try:
            priority = max(priority, self.w3.eth.max_priority_fee)
        except Exception:
            pass
        # 2x headroom covers a few blocks of base-fee growth.
        return {"maxFeePerGas": base_fee * 2 + priority, "maxPriorityFeePerGas": priority}

    @staticmethod
    def _raw_bytes(signed_tx) -> bytes:
        """web3 v7 renamed ``rawTransaction`` to ``raw_transaction``."""
        raw = getattr(signed_tx, "raw_transaction", None)
        if raw is None:
            raw = getattr(signed_tx, "rawTransaction", None)
        if raw is None:
            raise RuntimeError("Signed transaction has no raw payload (unexpected web3 version).")
        return raw

    # ── Transfer ────────────────────────────────────────────────────────────

    async def broadcast_erc20_transfer(
        self,
        token_address: str,
        to_address: str,
        amount_fiat: Decimal,
        exchange_rate: Optional[Decimal] = None,
    ) -> Tuple[str, int]:
        """Sign and broadcast a transfer. Returns ``(tx_hash, token_units)``.

        Returns as soon as the transaction is accepted by the node so the caller
        can persist the hash before waiting for confirmations.
        """
        if not self.account:
            raise ValueError("Crypto service has no private key configured.")

        self.ensure_connected()
        self.verify_chain()

        token_address = self.w3.to_checksum_address(token_address)
        to_address = self.w3.to_checksum_address(to_address)
        token_units = await self.compute_token_units(token_address, amount_fiat, exchange_rate)
        if token_units <= 0:
            raise ValueError(f"Computed token amount is zero for {amount_fiat} fiat.")

        balance = await self.token_balance(token_address)
        if balance < token_units:
            # Retrying will not conjure funds; the caller must reverse the fiat.
            raise InsufficientFunds(
                f"Hot wallet holds {balance} units, needs {token_units}. Top up the wallet."
            )

        contract = self.w3.eth.contract(address=token_address, abi=ERC20_ABI)
        lock_token = await self._acquire_nonce_lock()
        try:
            nonce = await self._next_nonce()
            tx_params = {
                "nonce": nonce,
                "from": self.account.address,
                "chainId": self.w3.eth.chain_id,
                **self._fee_parameters(),
            }
            call = contract.functions.transfer(to_address, token_units)
            tx = await asyncio.to_thread(call.build_transaction, tx_params)

            # A little headroom over the node's estimate: some tokens charge fees
            # on transfer and land just above the estimate.
            tx["gas"] = int(tx.get("gas", 100_000) * 1.2)

            signed_tx = self.w3.eth.account.sign_transaction(tx, self.private_key)
            tx_hash = await asyncio.to_thread(
                self.w3.eth.send_raw_transaction, self._raw_bytes(signed_tx)
            )
            await self._remember_nonce(nonce)
        finally:
            await self._release_nonce_lock(lock_token)

        tx_hash_hex = self.w3.to_hex(tx_hash)
        LOGGER.info(
            f"ERC-20 transfer broadcast: hash={tx_hash_hex} nonce={nonce} units={token_units}"
        )
        return tx_hash_hex, token_units

    async def await_confirmation(self, tx_hash: str, confirmations: Optional[int] = None):
        """Wait for the receipt, then for N further blocks. Bounded on both ends."""
        required = settings.web3_confirmations if confirmations is None else confirmations

        receipt = await asyncio.to_thread(
            self.w3.eth.wait_for_transaction_receipt,
            tx_hash,
            settings.web3_receipt_timeout_sec,
        )
        if receipt.status != 1:
            raise RuntimeError(f"Transaction reverted on-chain: {tx_hash}")

        if required <= 0:
            return receipt

        target_block = receipt.blockNumber + required
        deadline = time.monotonic() + settings.web3_confirmation_timeout_sec
        LOGGER.info(
            f"Transaction {tx_hash} mined in block {receipt.blockNumber}; "
            f"waiting for {required} confirmations."
        )
        while time.monotonic() < deadline:
            current = await asyncio.to_thread(lambda: self.w3.eth.block_number)
            if current >= target_block:
                # Re-read the receipt: a reorg could have moved or dropped it.
                final = await asyncio.to_thread(self.w3.eth.get_transaction_receipt, tx_hash)
                if final.status != 1:
                    raise RuntimeError(f"Transaction reverted after reorg: {tx_hash}")
                LOGGER.info(f"Transaction {tx_hash} confirmed.")
                return final
            await asyncio.sleep(2)

        raise TimeoutError(
            f"Transaction {tx_hash} was mined but did not reach {required} confirmations in time."
        )

    async def transfer_erc20_token(
        self,
        token_address: str,
        to_address: str,
        amount_fiat: Decimal,
        exchange_rate: Optional[Decimal] = None,
    ) -> str:
        """Broadcast and confirm in one call. Convenience wrapper for scripts."""
        tx_hash, _ = await self.broadcast_erc20_transfer(
            token_address, to_address, amount_fiat, exchange_rate
        )
        await self.await_confirmation(tx_hash)
        return tx_hash

    # ── Verification (used before any reversal) ─────────────────────────────

    async def get_onchain_status(self, tx_hash: str) -> str:
        """One of ``success``, ``failed``, ``pending``, ``unknown``.

        The reversal path depends on this: reversing fiat for a transfer that
        actually landed would pay the customer twice.
        """
        if not tx_hash:
            return "unknown"
        try:
            receipt = await asyncio.to_thread(self.w3.eth.get_transaction_receipt, tx_hash)
            if receipt is None:
                return "pending"
            return "success" if receipt.status == 1 else "failed"
        except Exception:
            # No receipt yet: is it at least known to the mempool?
            try:
                tx = await asyncio.to_thread(self.w3.eth.get_transaction, tx_hash)
                return "pending" if tx is not None else "unknown"
            except Exception:
                return "unknown"

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None
