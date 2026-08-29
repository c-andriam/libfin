"""
Temporary access to the link-management console.

``/links`` and its siblings disclose destination wallets and can delete a
merchant's payment links, so — unlike every other route — they are never
authenticated by the gateway's permanent API key alone; see
``MANAGEMENT_PATHS`` in ``gateway.api``. Handing an operator that permanent
key just so they can open the console would mean it now lives in a browser
indefinitely, which is the exposure this module exists to avoid.

``make connect`` mints a one-off, time-limited token instead: an operator
with shell access to the backend runs it and hands the resulting link to
whoever needs the console. There is no HTTP endpoint that creates one — only
:func:`create` does, called from ``scripts/connect.py`` — so a leaked
frontend is never enough to get in.
"""

import secrets
from typing import Optional

from gateway.config import settings
from gateway.redis_client import async_client, sync_client

#: Redis key prefix. A bare EXPIRE-backed marker is all a session is: there is
#: nothing to look up beyond "does this token still exist".
_KEY_PREFIX = "connect_session:"

_async_redis = None


def _client():
    global _async_redis
    if _async_redis is None:
        _async_redis = async_client()
    return _async_redis


def create(ttl_seconds: int, redis_url: Optional[str] = None) -> str:
    """Mint a new session, valid for ``ttl_seconds``.

    Synchronous and built on its own connection: the only caller is the
    ``connect`` CLI, which has no running event loop to share one with.
    """
    token = secrets.token_urlsafe(32)
    client = sync_client(redis_url or settings.redis_url)
    try:
        client.set(_KEY_PREFIX + token, "1", ex=ttl_seconds)
    finally:
        client.close()
    return token


async def verify(token: str) -> bool:
    """Whether ``token`` names a session that has not expired.

    Existence is the whole check — Redis' own TTL is what makes the session
    temporary, so there is no separate expiry to compare against and nothing
    to clean up once it lapses.
    """
    if not token:
        return False
    return bool(await _client().exists(_KEY_PREFIX + token))
