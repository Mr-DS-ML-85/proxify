"""
Circuit Breaker — Per-strategy-per-domain circuit breaker pattern.
States: CLOSED → OPEN (after N failures) → HALF_OPEN (test after cooldown) → CLOSED.
"""

import asyncio
import time
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from config import config

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    CLOSED = "closed"       # Normal operation — requests flow through
    OPEN = "open"           # Tripped — requests are rejected immediately
    HALF_OPEN = "half_open" # Testing — one request allowed to test recovery


@dataclass
class CircuitRecord:
    """Tracks circuit breaker state for a specific strategy+domain pair."""
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    success_count: int = 0
    last_failure_time: float = 0.0
    last_success_time: float = 0.0
    opened_at: float = 0.0
    half_open_attempts: int = 0


class CircuitBreaker:
    """
    Per-strategy-per-domain circuit breaker.

    When a strategy fails too many times for a domain, the circuit opens,
    preventing further attempts until the cooldown expires. After cooldown,
    one test request is allowed (HALF_OPEN). If it succeeds, the circuit
    closes again; if it fails, it re-opens.
    """

    def __init__(
        self,
        failure_threshold: int = config.CIRCUIT_BREAKER_FAILURE_THRESHOLD,
        cooldown_seconds: int = config.CIRCUIT_BREAKER_COOLDOWN,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._circuits: dict[str, CircuitRecord] = {}
        self._lock = asyncio.Lock()

    def _key(self, strategy: str, domain: str) -> str:
        return f"{strategy}:{domain}"

    def _get_record(self, key: str) -> CircuitRecord:
        if key not in self._circuits:
            self._circuits[key] = CircuitRecord()
        return self._circuits[key]

    async def can_execute(self, strategy: str, domain: str) -> bool:
        """Check if a request can proceed through this strategy for this domain."""
        key = self._key(strategy, domain)
        async with self._lock:
            record = self._get_record(key)

            if record.state == CircuitState.CLOSED:
                return True

            if record.state == CircuitState.OPEN:
                # Check if cooldown has elapsed
                elapsed = time.monotonic() - record.opened_at
                if elapsed >= self._cooldown_seconds:
                    record.state = CircuitState.HALF_OPEN
                    record.half_open_attempts = 0
                    logger.info(
                        f"Circuit {key}: OPEN → HALF_OPEN (cooldown elapsed after {elapsed:.1f}s)"
                    )
                    return True
                return False

            if record.state == CircuitState.HALF_OPEN:
                # Allow only one test request in half-open
                if record.half_open_attempts < 1:
                    record.half_open_attempts += 1
                    return True
                return False

        return False

    async def record_success(self, strategy: str, domain: str) -> None:
        """Record a successful request — may close an open circuit."""
        key = self._key(strategy, domain)
        async with self._lock:
            record = self._get_record(key)
            record.success_count += 1
            record.last_success_time = time.monotonic()

            if record.state == CircuitState.HALF_OPEN:
                record.state = CircuitState.CLOSED
                record.failure_count = 0
                logger.info(f"Circuit {key}: HALF_OPEN → CLOSED (test request succeeded)")
            elif record.state == CircuitState.CLOSED:
                # Reset failure count on success
                record.failure_count = 0

    async def record_failure(self, strategy: str, domain: str) -> None:
        """Record a failed request — may open the circuit."""
        key = self._key(strategy, domain)
        async with self._lock:
            record = self._get_record(key)
            record.failure_count += 1
            record.last_failure_time = time.monotonic()

            if record.state == CircuitState.HALF_OPEN:
                # Test request failed — re-open
                record.state = CircuitState.OPEN
                record.opened_at = time.monotonic()
                logger.warning(f"Circuit {key}: HALF_OPEN → OPEN (test request failed)")

            elif record.state == CircuitState.CLOSED:
                if record.failure_count >= self._failure_threshold:
                    record.state = CircuitState.OPEN
                    record.opened_at = time.monotonic()
                    logger.warning(
                        f"Circuit {key}: CLOSED → OPEN "
                        f"(failures={record.failure_count} >= threshold={self._failure_threshold})"
                    )

    async def get_state(self, strategy: str, domain: str) -> CircuitState:
        """Get the current circuit state."""
        key = self._key(strategy, domain)
        async with self._lock:
            return self._get_record(key).state

    async def get_all_states(self) -> dict[str, dict]:
        """Get all circuit states for monitoring."""
        async with self._lock:
            return {
                key: {
                    "state": record.state.value,
                    "failures": record.failure_count,
                    "successes": record.success_count,
                }
                for key, record in self._circuits.items()
            }

    async def reset(self, strategy: str, domain: str) -> None:
        """Manually reset a circuit to closed state."""
        key = self._key(strategy, domain)
        async with self._lock:
            self._circuits[key] = CircuitRecord()
            logger.info(f"Circuit {key}: manually reset to CLOSED")
