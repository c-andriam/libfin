"""
Redis-backed circuit breaker shared by the API and the Celery workers.

The breaker exists to stop the gateway from debiting fiat it cannot honour
on-chain. That makes its failure mode important: if Redis is unreachable the
previous implementation reported CLOSED and let every payment through, which is
precisely the wrong answer during an infrastructure incident. In production the
breaker now fails *closed*.
"""

import logging
import time
from enum import Enum
from typing import Optional

import redis

from gateway.config import settings

LOGGER = logging.getLogger(__name__)

_STATE_KEY = "circuit_breaker:web3:state"
_FAILURES_KEY = "circuit_breaker:web3:failures"
_LAST_FAILURE_KEY = "circuit_breaker:web3:last_failure"
_PROBE_KEY = "circuit_breaker:web3:probe"

# Record a failure, stamp the time and open the circuit once the threshold is
# reached — in one round trip so concurrent workers cannot interleave.
_RECORD_FAILURE_LUA = """
local failures = redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], ARGV[3])
redis.call('SET', KEYS[2], ARGV[1])
local state = redis.call('GET', KEYS[3])
if state == 'HALF_OPEN' or failures >= tonumber(ARGV[2]) then
    redis.call('SET', KEYS[3], 'OPEN')
    return 1
end
return 0
"""


class CircuitState(Enum):
    CLOSED = "CLOSED"       # Normal operation
    OPEN = "OPEN"           # Failing, fast reject
    HALF_OPEN = "HALF_OPEN"  # One probe in flight


class CircuitBreakerUnavailable(RuntimeError):
    """Raised when the breaker cannot reach Redis and is configured fail-closed."""


class Web3CircuitBreaker:
    def __init__(
        self,
        failure_threshold: Optional[int] = None,
        recovery_timeout_sec: Optional[int] = None,
        fail_closed: Optional[bool] = None,
        redis_url: Optional[str] = None,
    ):
        # Explicit None checks: a threshold or timeout of 0 is a legitimate
        # setting, and `x or default` would silently discard it.
        self.failure_threshold = (
            settings.circuit_breaker_threshold if failure_threshold is None else failure_threshold
        )
        self.recovery_timeout = (
            settings.circuit_breaker_recovery_sec
            if recovery_timeout_sec is None
            else recovery_timeout_sec
        )
        self.fail_closed = (
            settings.circuit_breaker_fail_closed if fail_closed is None else fail_closed
        )
        # Short timeouts: the breaker sits on the request path and must never be
        # the thing that makes the request hang.
        self.redis = redis.from_url(
            redis_url or settings.redis_url,
            socket_timeout=2,
            socket_connect_timeout=2,
        )
        self._record_failure = self.redis.register_script(_RECORD_FAILURE_LUA)
        # Counters live slightly longer than the recovery window so a burst of
        # unrelated failures spread over hours never adds up to an open circuit.
        self._failure_ttl = max(self.recovery_timeout * 4, 60)

    # ── State access ────────────────────────────────────────────────────────

    @property
    def state(self) -> CircuitState:
        raw = self.redis.get(_STATE_KEY)
        if not raw:
            return CircuitState.CLOSED
        try:
            return CircuitState(raw.decode("utf-8"))
        except ValueError:
            return CircuitState.CLOSED

    @state.setter
    def state(self, new_state: CircuitState) -> None:
        self.redis.set(_STATE_KEY, new_state.value)

    @property
    def failure_count(self) -> int:
        raw = self.redis.get(_FAILURES_KEY)
        return int(raw) if raw else 0

    @property
    def last_failure_time(self) -> float:
        raw = self.redis.get(_LAST_FAILURE_KEY)
        return float(raw) if raw else 0.0

    # ── Transitions ─────────────────────────────────────────────────────────

    def record_failure(self) -> None:
        try:
            opened = self._record_failure(
                keys=[_FAILURES_KEY, _LAST_FAILURE_KEY, _STATE_KEY],
                args=[time.time(), self.failure_threshold, self._failure_ttl],
            )
            if opened:
                LOGGER.critical(
                    "WEB3 CIRCUIT BREAKER OPENED — rejecting new payments to protect fiat funds."
                )
        except Exception as exc:
            # Losing a failure record biases towards staying closed, so shout.
            LOGGER.error(f"Circuit breaker could not record a failure: {exc}")

    def record_success(self) -> None:
        try:
            pipe = self.redis.pipeline()
            pipe.set(_STATE_KEY, CircuitState.CLOSED.value)
            pipe.delete(_FAILURES_KEY)
            pipe.delete(_PROBE_KEY)
            pipe.execute()
        except Exception as exc:
            LOGGER.error(f"Circuit breaker could not record a success: {exc}")

    def can_execute(self) -> bool:
        """Whether a new payment may be accepted.

        Blocking call — from async code use :func:`can_execute_async`.
        """
        try:
            state = self.state
        except Exception as exc:
            if self.fail_closed:
                LOGGER.critical(
                    f"Circuit breaker state unreadable ({exc}); refusing payments (fail-closed)."
                )
                return False
            LOGGER.error(f"Circuit breaker state unreadable ({exc}); allowing (fail-open).")
            return True

        if state is CircuitState.CLOSED:
            return True

        if state is CircuitState.OPEN:
            if time.time() - self.last_failure_time <= self.recovery_timeout:
                return False
            # Recovery window elapsed: let exactly one caller through to probe
            # the network. SET NX gives us that without a lock.
            try:
                won_probe = self.redis.set(
                    _PROBE_KEY, "1", nx=True, ex=max(self.recovery_timeout, 5)
                )
            except Exception:
                return not self.fail_closed
            if won_probe:
                self.state = CircuitState.HALF_OPEN
                LOGGER.info("WEB3 CIRCUIT BREAKER HALF_OPEN — probing the network with one request.")
                return True
            return False

        # HALF_OPEN: the probe is already in flight, everyone else waits.
        return False

    async def can_execute_async(self) -> bool:
        """Async-safe variant: Redis I/O is synchronous, so run it off-loop."""
        import asyncio

        return await asyncio.to_thread(self.can_execute)

    def reset(self) -> None:
        """Force the breaker closed. Used by tests and by operators."""
        try:
            self.redis.delete(_STATE_KEY, _FAILURES_KEY, _LAST_FAILURE_KEY, _PROBE_KEY)
        except Exception as exc:
            LOGGER.error(f"Circuit breaker reset failed: {exc}")

    def health(self) -> dict:
        try:
            return {
                "state": self.state.value,
                "failures": self.failure_count,
                "reachable": True,
            }
        except Exception as exc:
            return {"state": "unknown", "reachable": False, "error": type(exc).__name__}


web3_circuit_breaker = Web3CircuitBreaker()
