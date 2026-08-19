"""
Short-lived encrypted storage for card numbers.

A reversal (0400) must carry the *real* PAN in DE2 — an acquirer rejects a
masked one outright, which is why the previous implementation's reversals could
never succeed. But the PAN must not travel as a Celery argument either: broker
payloads are stored in Redis in clear text and show up in task logs and in
flower, which is a PCI-DSS violation.

So the API puts the PAN here, encrypted, keyed by transaction id and with a
short TTL, and hands the worker nothing but the id. Only the reversal path ever
reads it back, and the entry is destroyed as soon as the transaction reaches a
terminal state.

Scope note: this is the pragmatic option when the acquirer offers no
tokenisation. If yours returns a network token, store that instead and drop the
encryption entirely — search for ``store_pan`` to find the two call sites.
"""

import asyncio
import base64
import hashlib
import logging
from typing import Optional

import redis.asyncio as redis
from cryptography.fernet import Fernet, InvalidToken

from gateway.config import ConfigError, settings
from gateway.redis_client import RETRYABLE, async_client

#: Errors that mean "this connection is gone", as opposed to "Redis said no".
_RECOVERABLE = tuple(RETRYABLE)

LOGGER = logging.getLogger(__name__)

_KEY_PREFIX = "pan:"


def _derive_fernet_key(secret: str) -> bytes:
    """Accept either a real Fernet key or any high-entropy secret."""
    try:
        raw = base64.urlsafe_b64decode(secret)
        if len(raw) == 32:
            return secret.encode()
    except Exception:
        pass
    return base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())


def _load_secret() -> Optional[str]:
    """Fetch the PAN encryption secret: Vault first, environment second."""
    import os

    if settings.vault_addr and settings.vault_token:
        try:
            import hvac

            client = hvac.Client(url=settings.vault_addr, token=settings.vault_token)
            if client.is_authenticated():
                secret = client.secrets.kv.v2.read_secret_version(
                    path="gateway/pan", mount_point="secret", raise_on_deleted_version=True
                )
                value = secret["data"]["data"].get("encryption_key")
                if value:
                    LOGGER.info("PAN encryption key loaded from Vault.")
                    return value
        except Exception as exc:
            LOGGER.warning(f"Could not read the PAN encryption key from Vault: {exc}")

    return os.environ.get("PAN_ENCRYPTION_KEY") or None


class PanVault:
    def __init__(self, redis_url: Optional[str] = None, secret: Optional[str] = None):
        self._redis_url = redis_url or settings.redis_url
        self._redis: Optional[redis.Redis] = None
        self._redis_loop = None
        secret = secret or _load_secret()

        if not secret:
            if settings.is_production:
                raise ConfigError(
                    "PAN_ENCRYPTION_KEY is required in production: without it a failed "
                    "crypto transfer cannot be reversed and the cardholder keeps the debit."
                )
            # Simulation: derive a throwaway key so the full path stays testable.
            secret = "simulation-only-pan-key"
            LOGGER.warning("Using a throwaway PAN encryption key (simulation mode).")

        self._fernet = Fernet(_derive_fernet_key(secret))

    @property
    def client(self) -> redis.Redis:
        """A Redis client bound to the event loop that is running right now.

        This object is a process-wide singleton, which suits the API (one loop
        for the process lifetime) but not the Celery worker: every task runs
        under a fresh ``asyncio.run`` loop, and a connection created under a
        previous loop raises "attached to a different loop" on first use. So the
        client is rebuilt whenever the loop changes.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if self._redis is not None and loop is not self._redis_loop:
            # The old client belongs to a closed loop; drop it without awaiting.
            LOGGER.debug("Event loop changed; rebuilding the PAN vault Redis client.")
            self._redis = None

        if self._redis is None:
            self._redis = async_client(self._redis_url)
            self._redis_loop = loop

        return self._redis

    def _discard_client(self) -> None:
        """Drop the cached client so the next call builds a fresh one.

        The library's own retry policy reconnects within a command, but a
        connection killed while idle in the pool of a long-lived process can
        stay poisoned: observed after a Redis restart, where a brand-new client
        connected fine while the running API reported it unreachable for as long
        as it was left running. Rebuilding on error is self-healing, and does
        not depend on the exact reconnection semantics of a library version.
        """
        self._redis = None
        self._redis_loop = None

    async def _call(self, operation: str, *args, **kwargs):
        """Run a Redis command, replacing the client once if it has gone stale."""
        try:
            return await getattr(self.client, operation)(*args, **kwargs)
        except _RECOVERABLE as exc:
            LOGGER.warning(f"Redis {operation} failed ({exc}); rebuilding the client and retrying.")
            self._discard_client()
            return await getattr(self.client, operation)(*args, **kwargs)

    async def ping(self) -> bool:
        return bool(await self._call("ping"))

    async def store(self, tx_id: int, pan: str, ttl_sec: Optional[int] = None) -> None:
        token = self._fernet.encrypt(pan.encode())
        await self._call(
            "setex", f"{_KEY_PREFIX}{tx_id}", ttl_sec or settings.pan_token_ttl_sec, token
        )

    async def retrieve(self, tx_id: int) -> Optional[str]:
        token = await self._call("get", f"{_KEY_PREFIX}{tx_id}")
        if not token:
            LOGGER.error(
                f"No stored PAN for transaction {tx_id}: it expired or was already purged. "
                "A reversal for this transaction must be raised manually."
            )
            return None
        try:
            return self._fernet.decrypt(token).decode()
        except InvalidToken:
            LOGGER.error(
                f"Stored PAN for transaction {tx_id} cannot be decrypted — "
                "the encryption key changed. Manual reversal required."
            )
            return None

    async def purge(self, tx_id: int) -> None:
        try:
            await self._call("delete", f"{_KEY_PREFIX}{tx_id}")
        except Exception as exc:
            LOGGER.warning(f"Could not purge the stored PAN for transaction {tx_id}: {exc}")

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None
            self._redis_loop = None


_pan_vault: Optional[PanVault] = None


def get_pan_vault() -> PanVault:
    global _pan_vault
    if _pan_vault is None:
        _pan_vault = PanVault()
    return _pan_vault
