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
from web3.exceptions import TimeExhausted

from gateway.config import settings
from gateway.redis_client import async_client

LOGGER = logging.getLogger(__name__)

# Minimal ERC-20 ABI: transfer, decimals, balanceOf.
#
# The transfer entry exists in two forms because the largest token this gateway
# is likely to move does not follow the standard. USDT
# (0xdAC17F958D2ee523a2206206994597C13D831ec7) predates ERC-20 being finalised
# and its transfer returns nothing at all — verified against mainnet, where an
# eth_call to it comes back as `0x`, zero bytes, rather than an encoded bool.
#
# Encoding a call does not consult the outputs, so declaring a bool that is
# never returned is harmless while the only thing done with transfer is to
# build a transaction. It stops being harmless the moment anyone adds a
# pre-flight `.call()` to check a transfer would succeed — a natural thing to
# reach for, which would then fail on USDT specifically and on nothing else.
#
# Success is taken from the receipt status either way. A token's own return
# value is not evidence: the ones that return false on failure are exactly the
# ones a caller forgets to check.
_TRANSFER_RETURNS_BOOL = {
    "constant": False,
    "inputs": [{"name": "_to", "type": "address"}, {"name": "_value", "type": "uint256"}],
    "name": "transfer",
    "outputs": [{"name": "", "type": "bool"}],
    "payable": False,
    "stateMutability": "nonpayable",
    "type": "function",
}

_TRANSFER_RETURNS_NOTHING = {**_TRANSFER_RETURNS_BOOL, "outputs": []}

ERC20_ABI = [
    _TRANSFER_RETURNS_BOOL,
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

#: Tokens known to omit the boolean their interface implies. Detected at
#: runtime as well; this list only avoids the probe for the common cases.
NON_STANDARD_TRANSFER_TOKENS = frozenset(
    {
        "0xdac17f958d2ee523a2206206994597c13d831ec7",  # USDT, Ethereum mainnet
        "0xdac17f958d2ee523a2206206994597c13d831ec7".upper(),
    }
)

_NONCE_LOCK_KEY = "web3:nonce:lock:{address}"
_NONCE_VALUE_KEY = "web3:nonce:next:{address}"

_RELEASE_LOCK_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


#: Guards a once-per-process warning; see _fetch_key_from_vault.
_vault_warning_logged = False


class InsufficientFunds(RuntimeError):
    """The hot wallet cannot cover the transfer. Never retry blindly."""


class GasPriceTooHigh(RuntimeError):
    """Fees are above the configured ceiling. Temporary — wait, then refund."""


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

    #: Real calls get room to breathe; a liveness probe must not.
    RPC_CALL_TIMEOUT = 30
    RPC_PROBE_TIMEOUT = 3

    @classmethod
    def _build_provider(cls, url: str, timeout: Optional[int] = None) -> Web3:
        return Web3(
            Web3.HTTPProvider(
                url, request_kwargs={"timeout": timeout or cls.RPC_CALL_TIMEOUT}
            )
        )

    @property
    def active_rpc(self) -> str:
        return self.rpc_urls[self._rpc_index]

    def _select_working_rpc(self) -> bool:
        """Point ``self.w3`` at the first responsive provider. Rotates on failure."""
        for offset in range(len(self.rpc_urls)):
            index = (self._rpc_index + offset) % len(self.rpc_urls)
            # Probe with a short timeout, then keep the full-timeout provider
            # for real work. Probing at the call timeout meant that with an
            # unreachable node, each retry burned 30s before even starting —
            # so a customer waited minutes for a refund the system had already
            # decided to make.
            probe = self._build_provider(self.rpc_urls[index], self.RPC_PROBE_TIMEOUT)
            try:
                if probe.is_connected():
                    candidate = self._build_provider(self.rpc_urls[index])
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
        """Liveness of the active provider, answered quickly."""
        try:
            probe = self._build_provider(self.active_rpc, self.RPC_PROBE_TIMEOUT)
            return probe.is_connected()
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
            # Once per process. A CryptoService is built per Celery task, so
            # warning every time buries the lines that actually matter — and in
            # a payment system, log noise is how a real alert gets missed.
            global _vault_warning_logged
            if not _vault_warning_logged:
                _vault_warning_logged = True
                LOGGER.warning(
                    f"Could not read the Web3 key from Vault ({exc}); "
                    "falling back to the environment. Logged once per process."
                )
            return None

    # ── Distributed nonce allocation ────────────────────────────────────────

    @property
    def redis(self) -> redis.Redis:
        if self._redis is None:
            self._redis = async_client(self._redis_url)
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

    def _abi_for(self, token_address: str) -> list:
        """The ABI matching this token's actual transfer signature."""
        if token_address.lower() in NON_STANDARD_TRANSFER_TOKENS:
            return [_TRANSFER_RETURNS_NOTHING] + ERC20_ABI[1:]
        return ERC20_ABI

    async def transfer_returns_bool(self, token_address: str) -> bool:
        """Ask the chain whether this token's transfer returns anything.

        A zero-value transfer to ourselves is the cheapest probe that reaches
        the same code path a real transfer would, without moving anything or
        costing gas — it is a call, not a transaction.
        """
        token_address = self.w3.to_checksum_address(token_address)
        selector = self.w3.keccak(text="transfer(address,uint256)")[:4]
        payload = (
            selector
            + bytes(12) + bytes.fromhex(self.account.address[2:])
            + (0).to_bytes(32, "big")
        )
        try:
            returned = await asyncio.to_thread(
                self.w3.eth.call,
                {"to": token_address, "from": self.account.address, "data": payload},
            )
            return len(returned) > 0
        except Exception as exc:
            # A revert says nothing about the return type; assume the standard.
            LOGGER.debug(f"Could not probe {token_address} transfer signature: {exc}")
            return True

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

    def _fee_parameters(self, multiplier: Optional[Decimal] = None) -> dict:
        """EIP-1559 fees, falling back to legacy gas price on chains without them.

        Refuses to build fees above ``WEB3_MAX_FEE_GWEI``. A gas spike is not a
        reason to spend more on delivering a transfer than the transfer is
        worth; the caller retries later, and refunds the customer if the
        network never calms down.
        """
        ceiling_wei = int(Decimal(self.w3.to_wei(1, "gwei")) * settings.web3_max_fee_gwei)
        bump = Decimal(multiplier) if multiplier else Decimal(1)

        latest = self.w3.eth.get_block("latest")
        base_fee = latest.get("baseFeePerGas")

        if base_fee is None:
            gas_price = int(Decimal(self.w3.eth.gas_price) * bump)
            if gas_price > ceiling_wei:
                raise GasPriceTooHigh(
                    f"Gas price {gas_price / 1e9:.1f} gwei exceeds the "
                    f"{settings.web3_max_fee_gwei} gwei ceiling."
                )
            return {"gasPrice": gas_price}

        priority = self.w3.to_wei(2, "gwei")
        try:
            priority = max(priority, self.w3.eth.max_priority_fee)
        except Exception:
            pass

        # 2x headroom covers a few blocks of base-fee growth.
        max_fee = int((Decimal(base_fee * 2 + priority)) * bump)
        priority = int(Decimal(priority) * bump)

        if max_fee > ceiling_wei:
            raise GasPriceTooHigh(
                f"maxFeePerGas would be {max_fee / 1e9:.1f} gwei, above the "
                f"{settings.web3_max_fee_gwei} gwei ceiling (base fee "
                f"{base_fee / 1e9:.1f}). Waiting for the fee market to settle."
            )

        return {
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": min(priority, max_fee),
        }

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
    ) -> Tuple[str, int, int]:
        """Sign and broadcast a transfer. Returns ``(tx_hash, token_units, nonce)``.

        Returns as soon as the transaction is accepted by the node so the caller
        can persist the hash before waiting for confirmations. The nonce comes
        back too: replacing a transfer stuck in the mempool means rebroadcasting
        at the *same* nonce with higher fees, and that number is not otherwise
        recoverable once the process that chose it is gone.
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

        contract = self.w3.eth.contract(address=token_address, abi=self._abi_for(token_address))
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
        return tx_hash_hex, token_units, nonce

    async def nonce_gap(self) -> int:
        """How far our next nonce runs ahead of what the chain will accept.

        A transaction signed at nonce N cannot be mined until every nonce below
        it has been. If one was allocated and never broadcast — a worker killed
        between the two, a dropped mempool — the gap is permanent: that
        transaction stalls, and every payment after it queues behind a hole that
        nothing fills on its own. Observed exactly that way, a hot wallet frozen
        at nonce 509 with the chain still expecting 508.

        Returns the number of missing nonces, or 0 when the queue is contiguous.
        """
        chain_nonce = await asyncio.to_thread(
            self.w3.eth.get_transaction_count, self.account.address, "pending"
        )
        key = _NONCE_VALUE_KEY.format(address=self.account.address)
        try:
            cached = await self.redis.get(key)
        except Exception:
            return 0
        if cached is None:
            return 0
        return max(0, int(cached) - chain_nonce)

    async def fill_nonce_gap(self, max_fill: int = 10) -> int:
        """Close a nonce gap with no-op self-transfers, cheapest first.

        Each missing nonce is filled with a zero-value transfer to our own
        address: the smallest transaction that can occupy a slot. It costs gas
        and moves nothing, which is the point — it unblocks everything queued
        behind it.

        Bounded, because a gap large enough to need more than a handful of
        fillers means the nonce bookkeeping is wrong in a way that filling will
        not fix, and a human should look before spending more gas.
        """
        if not self.account:
            raise ValueError("Crypto service has no private key configured.")

        self.ensure_connected()
        chain_nonce = await asyncio.to_thread(
            self.w3.eth.get_transaction_count, self.account.address, "pending"
        )
        gap = await self.nonce_gap()
        if gap == 0:
            return 0

        if gap > max_fill:
            LOGGER.critical(
                f"MANUAL ACTION REQUIRED — nonce gap of {gap} on "
                f"{self.account.address}, beyond the {max_fill} this will fill "
                "automatically. The nonce bookkeeping is wrong; do not simply "
                "raise the limit."
            )
            gap = max_fill

        filled = 0
        for offset in range(gap):
            nonce = chain_nonce + offset
            try:
                tx = {
                    "nonce": nonce,
                    "from": self.account.address,
                    "to": self.account.address,
                    "value": 0,
                    "gas": 21000,
                    "chainId": self.w3.eth.chain_id,
                    **self._fee_parameters(),
                }
                signed = self.w3.eth.account.sign_transaction(tx, self.private_key)
                await asyncio.to_thread(
                    self.w3.eth.send_raw_transaction, self._raw_bytes(signed)
                )
                filled += 1
                LOGGER.warning(f"Filled nonce {nonce} with a no-op to unblock the queue.")
            except Exception as exc:
                # "already known" means the slot is occupied after all, which is
                # the outcome we wanted.
                if "already" in str(exc).lower():
                    filled += 1
                    continue
                LOGGER.error(f"Could not fill nonce {nonce}: {exc}")
                break

        return filled

    async def replace_stuck_transfer(
        self,
        token_address: str,
        to_address: str,
        token_units: int,
        nonce: int,
        attempt: int = 1,
    ) -> str:
        """Rebroadcast at the same nonce with higher fees.

        A transfer priced below the market sits in the mempool indefinitely: it
        is neither delivered nor refundable, because the reversal guard sees it
        as still pending. The only way out is to replace it — same nonce, fees
        raised by at least ~10% or the network rejects the replacement.

        ``attempt`` compounds the fee bump. Without it every retry reprices
        against the same market and produces byte-identical transaction — the
        node answers "transaction already imported" and the transfer stays
        exactly as stuck as before. Each attempt has to bid strictly higher than
        the last, not merely higher than the market.

        Returns the new hash. The old one becomes permanently unmineable, so
        exactly one of the two can ever land.
        """
        if not self.account:
            raise ValueError("Crypto service has no private key configured.")

        self.ensure_connected()
        self.verify_chain()

        token_address = self.w3.to_checksum_address(token_address)
        to_address = self.w3.to_checksum_address(to_address)
        contract = self.w3.eth.contract(address=token_address, abi=ERC20_ABI)

        # No nonce lock here: the nonce is already fixed by the transaction
        # being replaced, so there is nothing to allocate and nothing to race.
        tx_params = {
            "nonce": nonce,
            "from": self.account.address,
            "chainId": self.w3.eth.chain_id,
            **self._fee_parameters(
                multiplier=settings.web3_replacement_multiplier ** max(1, attempt)
            ),
        }
        call = contract.functions.transfer(to_address, token_units)
        tx = await asyncio.to_thread(call.build_transaction, tx_params)
        tx["gas"] = int(tx.get("gas", 100_000) * 1.2)

        signed_tx = self.w3.eth.account.sign_transaction(tx, self.private_key)
        tx_hash = await asyncio.to_thread(
            self.w3.eth.send_raw_transaction, self._raw_bytes(signed_tx)
        )
        tx_hash_hex = self.w3.to_hex(tx_hash)
        LOGGER.warning(f"Replaced a stuck transfer at nonce {nonce}: new hash {tx_hash_hex}")
        return tx_hash_hex

    async def await_confirmation(self, tx_hash: str, confirmations: Optional[int] = None):
        """Wait for the receipt, then for N further blocks. Bounded on both ends."""
        required = settings.web3_confirmations if confirmations is None else confirmations

        try:
            receipt = await asyncio.to_thread(
                self.w3.eth.wait_for_transaction_receipt,
                tx_hash,
                settings.web3_receipt_timeout_sec,
            )
        except TimeExhausted as exc:
            # web3 raises TimeExhausted, which is a Web3Exception and *not* a
            # TimeoutError. Callers reasonably catch TimeoutError to decide a
            # transfer is stuck and needs replacing — and with the wrong type
            # that branch is unreachable. It was: a transfer sat pending at a
            # fixed nonce while the replacement logic written for exactly that
            # case never ran once. Normalised here so the contract of this
            # method matches what its name promises.
            raise TimeoutError(
                f"Transaction {tx_hash} was not mined within "
                f"{settings.web3_receipt_timeout_sec}s."
            ) from exc
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
        tx_hash, _, _ = await self.broadcast_erc20_transfer(
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
