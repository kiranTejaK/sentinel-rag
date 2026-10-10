"""State-Machine Circuit Breaker Pattern for Third-Party LLM Providers.

Implements standard Michael Nygard Circuit Breaker states:
- CLOSED: Normal operation; requests pass through. Failures are tracked.
- OPEN: Fail fast without network latency; immediate failover to fallback providers.
- HALF_OPEN: Cool-down window elapsed; probe primary provider with canary request.

Transitions:
- CLOSED -> OPEN: Upon reaching failure_threshold (e.g. 5 consecutive errors).
- OPEN -> HALF_OPEN: After recovery_timeout (e.g. 60.0 seconds).
- HALF_OPEN -> CLOSED: On successful canary response.
- HALF_OPEN -> OPEN: On canary failure (resets recovery timer).
"""

import asyncio
import time
from enum import StrEnum
from typing import Any

from app.core.config import settings
from app.core.logging import logger


class CircuitState(StrEnum):
    """Circuit breaker operational states."""

    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreakerOpenException(Exception):
    """Raised when an execution attempt is rejected because the circuit is OPEN."""

    def __init__(self, message: str, time_remaining: float, name: str = "circuit") -> None:
        super().__init__(message)
        self.time_remaining = max(0.0, time_remaining)
        self.name = name


class CircuitBreaker:
    """Coroutine-safe State Machine Circuit Breaker."""

    def __init__(
        self,
        name: str = "primary_llm_circuit",
        failure_threshold: int | None = None,
        recovery_timeout: float | None = None,
    ) -> None:
        self.name = name
        self.failure_threshold = (
            failure_threshold
            if failure_threshold is not None
            else settings.CIRCUIT_BREAKER_FAILURE_THRESHOLD
        )
        self.recovery_timeout = (
            recovery_timeout
            if recovery_timeout is not None
            else float(settings.CIRCUIT_BREAKER_RECOVERY_TIME_SECONDS)
        )

        self._state: CircuitState = CircuitState.CLOSED
        self._consecutive_failures: int = 0
        self._last_failure_time: float | None = None
        self._last_state_change: float = time.time()
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def seconds_remaining_in_open(self) -> float:
        """Returns seconds remaining before circuit transitions from OPEN to HALF_OPEN."""
        if self._state != CircuitState.OPEN or self._last_failure_time is None:
            return 0.0
        elapsed = time.time() - self._last_failure_time
        remaining = self.recovery_timeout - elapsed
        return max(0.0, remaining)

    async def can_execute(self) -> bool:
        """Checks if a request is permitted to execute against the protected resource."""
        async with self._lock:
            if self._state == CircuitState.CLOSED:
                return True

            if self._state == CircuitState.OPEN:
                if self._last_failure_time is not None:
                    elapsed = time.time() - self._last_failure_time
                    if elapsed >= self.recovery_timeout:
                        self._transition_to(CircuitState.HALF_OPEN)
                        logger.warning(
                            "circuit_breaker_half_open_probe_allowed",
                            name=self.name,
                            elapsed=elapsed,
                            recovery_timeout=self.recovery_timeout,
                        )
                        return True
                return False

            return self._state == CircuitState.HALF_OPEN

    async def record_success(self) -> None:
        """Records a successful call, resetting failure counts and closing half-open circuits."""
        async with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                logger.info(
                    "circuit_breaker_recovered",
                    name=self.name,
                    previous_state="HALF_OPEN",
                    new_state="CLOSED",
                )
                self._transition_to(CircuitState.CLOSED)
            self._consecutive_failures = 0
            self._last_failure_time = None

    async def record_failure(self) -> None:
        """Records an execution failure and trips circuit if threshold exceeded."""
        async with self._lock:
            self._consecutive_failures += 1
            self._last_failure_time = time.time()

            if self._state == CircuitState.HALF_OPEN:
                # Canary probe failed; trip back to OPEN immediately
                logger.error(
                    "circuit_breaker_probe_failed",
                    name=self.name,
                    re_tripped_to="OPEN",
                    recovery_timeout=self.recovery_timeout,
                )
                self._transition_to(CircuitState.OPEN)
            elif (
                self._state == CircuitState.CLOSED
                and self._consecutive_failures >= self.failure_threshold
            ):
                logger.error(
                    "circuit_breaker_tripped_open",
                    name=self.name,
                    consecutive_failures=self._consecutive_failures,
                    failure_threshold=self.failure_threshold,
                    recovery_timeout=self.recovery_timeout,
                )
                self._transition_to(CircuitState.OPEN)

    def _transition_to(self, new_state: CircuitState) -> None:
        """Internal helper to change state with telemetry timestamp."""
        self._state = new_state
        self._last_state_change = time.time()

    async def reset(self) -> None:
        """Manually forces the circuit breaker back to CLOSED state."""
        async with self._lock:
            self._state = CircuitState.CLOSED
            self._consecutive_failures = 0
            self._last_failure_time = None
            self._last_state_change = time.time()
            logger.info("circuit_breaker_manually_reset", name=self.name)

    async def trip(self) -> None:
        """Manually forces the circuit breaker into OPEN state."""
        async with self._lock:
            self._state = CircuitState.OPEN
            self._consecutive_failures = self.failure_threshold
            self._last_failure_time = time.time()
            self._last_state_change = time.time()
            logger.warning("circuit_breaker_manually_tripped", name=self.name)

    def get_status(self) -> dict[str, Any]:
        """Returns real-time diagnostics of the circuit breaker."""
        return {
            "name": self.name,
            "state": self._state.value,
            "consecutive_failures": self._consecutive_failures,
            "failure_threshold": self.failure_threshold,
            "recovery_timeout_seconds": self.recovery_timeout,
            "seconds_remaining_in_open": round(self.seconds_remaining_in_open, 2),
            "last_failure_time": self._last_failure_time,
            "last_state_change": self._last_state_change,
        }
