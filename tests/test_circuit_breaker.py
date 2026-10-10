"""Unit tests for State-Machine Circuit Breaker pattern."""

import time

import pytest

from app.core.circuit_breaker import CircuitBreaker, CircuitState


@pytest.mark.asyncio
async def test_circuit_breaker_initial_state_closed() -> None:
    """Verifies that circuit breaker starts in CLOSED state."""
    cb = CircuitBreaker(name="test_circuit", failure_threshold=5, recovery_timeout=60.0)
    assert cb.state == CircuitState.CLOSED
    assert cb.consecutive_failures == 0
    assert await cb.can_execute() is True


@pytest.mark.asyncio
async def test_circuit_breaker_trips_to_open_after_threshold_failures() -> None:
    """Verifies that 5 consecutive failures trip the circuit to OPEN."""
    cb = CircuitBreaker(name="test_circuit", failure_threshold=5, recovery_timeout=60.0)

    for i in range(4):
        await cb.record_failure()
        assert cb.state == CircuitState.CLOSED
        assert cb.consecutive_failures == i + 1
        assert await cb.can_execute() is True

    # 5th failure trips the circuit
    await cb.record_failure()
    assert cb.state == CircuitState.OPEN
    assert cb.consecutive_failures == 5
    assert await cb.can_execute() is False
    assert cb.seconds_remaining_in_open > 0.0


@pytest.mark.asyncio
async def test_circuit_breaker_success_resets_failure_count() -> None:
    """Verifies that a success in CLOSED state resets consecutive failure count."""
    cb = CircuitBreaker(name="test_circuit", failure_threshold=5, recovery_timeout=60.0)

    for _ in range(3):
        await cb.record_failure()
    assert cb.consecutive_failures == 3

    await cb.record_success()
    assert cb.consecutive_failures == 0
    assert cb.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_circuit_breaker_half_open_probe_after_timeout() -> None:
    """Verifies transition from OPEN to HALF_OPEN after recovery timeout."""
    cb = CircuitBreaker(name="test_circuit", failure_threshold=3, recovery_timeout=1.0)

    # Trip breaker
    for _ in range(3):
        await cb.record_failure()
    assert cb.state == CircuitState.OPEN
    assert await cb.can_execute() is False

    # Simulate elapsed time beyond recovery timeout
    cb._last_failure_time = time.time() - 2.0  # 2s > 1s recovery timeout

    assert await cb.can_execute() is True
    assert cb.state == CircuitState.HALF_OPEN

    # Canary success recovers circuit to CLOSED
    await cb.record_success()
    assert cb.state == CircuitState.CLOSED
    assert cb.consecutive_failures == 0


@pytest.mark.asyncio
async def test_circuit_breaker_half_open_probe_failure_re_trips() -> None:
    """Verifies that a failed probe in HALF_OPEN trips immediately back to OPEN."""
    cb = CircuitBreaker(name="test_circuit", failure_threshold=3, recovery_timeout=60.0)

    cb._state = CircuitState.HALF_OPEN
    assert await cb.can_execute() is True

    # Canary probe fails
    await cb.record_failure()
    assert cb.state == CircuitState.OPEN
    assert await cb.can_execute() is False


@pytest.mark.asyncio
async def test_circuit_breaker_manual_reset_and_trip() -> None:
    """Verifies manual reset and trip overrides."""
    cb = CircuitBreaker(name="test_circuit", failure_threshold=5, recovery_timeout=60.0)

    await cb.trip()
    assert cb.state == CircuitState.OPEN
    assert await cb.can_execute() is False

    await cb.reset()
    assert cb.state == CircuitState.CLOSED
    assert cb.consecutive_failures == 0
    assert await cb.can_execute() is True
