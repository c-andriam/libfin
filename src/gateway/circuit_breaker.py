import time
import logging
import os
from enum import Enum
import redis

LOGGER = logging.getLogger(__name__)

class CircuitState(Enum):
    CLOSED = "CLOSED"      # Normal operation
    OPEN = "OPEN"          # Failing, fast reject
    HALF_OPEN = "HALF_OPEN" # Testing if recovered

class Web3CircuitBreaker:
    def __init__(self, failure_threshold: int = 3, recovery_timeout_sec: int = 30):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout_sec
        redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
        
        # Use sync Redis since these methods are called synchronously in FastAPI/Celery
        self.redis = redis.from_url(redis_url)

    @property
    def state(self) -> CircuitState:
        try:
            state_str = self.redis.get("circuit_breaker:state")
            if state_str:
                return CircuitState(state_str.decode('utf-8'))
            return CircuitState.CLOSED
        except Exception as e:
            LOGGER.error(f"Redis error getting CB state: {e}")
            return CircuitState.CLOSED

    @state.setter
    def state(self, new_state: CircuitState):
        try:
            self.redis.set("circuit_breaker:state", new_state.value)
        except Exception as e:
            LOGGER.error(f"Redis error setting CB state: {e}")

    @property
    def failure_count(self) -> int:
        try:
            count = self.redis.get("circuit_breaker:failure_count")
            return int(count) if count else 0
        except Exception:
            return 0

    @property
    def last_failure_time(self) -> float:
        try:
            t = self.redis.get("circuit_breaker:last_failure_time")
            return float(t) if t else 0.0
        except Exception:
            return 0.0

    def record_failure(self):
        try:
            count = self.redis.incr("circuit_breaker:failure_count")
            self.redis.set("circuit_breaker:last_failure_time", time.time())
            
            current_state = self.state
            if current_state == CircuitState.CLOSED and count >= self.failure_threshold:
                self.state = CircuitState.OPEN
                LOGGER.critical("WEB3 CIRCUIT BREAKER OPENED! Failing fast on all new requests.")
            elif current_state == CircuitState.HALF_OPEN:
                self.state = CircuitState.OPEN
                LOGGER.critical("WEB3 CIRCUIT BREAKER RE-OPENED! (Failed during test ping)")
        except Exception as e:
            LOGGER.error(f"Redis error in CB record_failure: {e}")

    def record_success(self):
        try:
            if self.state == CircuitState.HALF_OPEN:
                LOGGER.info("WEB3 CIRCUIT BREAKER CLOSED! Network recovered.")
            self.state = CircuitState.CLOSED
            self.redis.set("circuit_breaker:failure_count", 0)
        except Exception as e:
            LOGGER.error(f"Redis error in CB record_success: {e}")

    def can_execute(self) -> bool:
        current_state = self.state
        if current_state == CircuitState.CLOSED:
            return True
        if current_state == CircuitState.OPEN:
            if time.time() - self.last_failure_time > self.recovery_timeout:
                self.state = CircuitState.HALF_OPEN
                LOGGER.info("WEB3 CIRCUIT BREAKER HALF_OPEN: Allowing one request to test network.")
                return True
            return False
        if current_state == CircuitState.HALF_OPEN:
            return True
        return True

web3_circuit_breaker = Web3CircuitBreaker()
