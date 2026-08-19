"""
Resilient Redis clients.

Redis restarts. Maintenance, a failover, an OOM kill — and every pooled
connection open at that moment is dead. Without a health check the pool keeps
handing those corpses out, and each one raises "Connection closed by server"
forever. In this system that is not a degraded mode: the circuit breaker cannot
be read, no STAN can be allocated, no PAN can be staged, so the gateway refuses
every payment until someone restarts it by hand.

Observed exactly that way — Redis healthy, a fresh client connecting fine, and
the running API still reporting it unreachable minutes later.

So every client is built here, with:

  * a **health check** before a pooled connection is reused, which is what turns
    a dead connection into a replaced one;
  * **retries** on the errors that a restart produces, so a request that arrives
    mid-restart waits rather than fails;
  * **short timeouts**, because these calls sit on the payment path and must
    never be the thing that makes a request hang.
"""

import logging
from typing import Optional

import redis
import redis.asyncio as aioredis
from redis.backoff import ExponentialBackoff
from redis.exceptions import BusyLoadingError, ConnectionError, TimeoutError
from redis.retry import Retry

from gateway.config import settings

LOGGER = logging.getLogger(__name__)

#: Errors a restart produces, all of which succeed on a new connection.
#: BusyLoadingError matters here: Redis is configured append-only, so after a
#: restart it spends time replaying the log and rejects commands until it is
#: done — which a client without retries reports as a hard failure.
RETRYABLE = (ConnectionError, TimeoutError, BusyLoadingError)

#: Seconds between liveness checks on an idle pooled connection. Well below the
#: time any restart takes, so a stale connection is found before a payment does.
HEALTH_CHECK_INTERVAL = 15

_COMMON = {
    "socket_timeout": 5,
    "socket_connect_timeout": 5,
    "socket_keepalive": True,
    "health_check_interval": HEALTH_CHECK_INTERVAL,
}


def _retry(attempts: int = 3) -> Retry:
    return Retry(ExponentialBackoff(base=0.05, cap=1.0), attempts)


def async_client(url: Optional[str] = None, **overrides) -> aioredis.Redis:
    """An async client that survives a Redis restart."""
    return aioredis.from_url(
        url or settings.redis_url,
        retry=_retry(),
        retry_on_error=list(RETRYABLE),
        **{**_COMMON, **overrides},
    )


def sync_client(url: Optional[str] = None, **overrides) -> redis.Redis:
    """A synchronous client that survives a Redis restart.

    Used by the circuit breaker, which is consulted from both the request path
    and Celery tasks.
    """
    return redis.from_url(
        url or settings.redis_url,
        retry=_retry(),
        retry_on_error=list(RETRYABLE),
        **{**_COMMON, **overrides},
    )
