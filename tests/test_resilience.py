"""Unit tests for Tenacity exponential backoff with full jitter resilience engine."""

import pytest
from litellm.exceptions import RateLimitError, ServiceUnavailableError

from app.services.resilience import execute_with_resilience, is_transient_error


def test_is_transient_error_classification() -> None:
    """Verifies that transient HTTP 429/503 and network errors are classified correctly."""
    # Transient errors
    rate_limit_err = RateLimitError("Rate limit exceeded", model="llama-3", llm_provider="groq")
    service_err = ServiceUnavailableError("Overloaded", model="llama-3", llm_provider="groq")
    timeout_err = TimeoutError("Socket timed out")
    conn_err = ConnectionError("Connection reset by peer")

    assert is_transient_error(rate_limit_err) is True
    assert is_transient_error(service_err) is True
    assert is_transient_error(timeout_err) is True
    assert is_transient_error(conn_err) is True

    # Permanent non-transient errors
    assert is_transient_error(ValueError("Invalid argument")) is False
    assert is_transient_error(KeyError("missing_key")) is False


@pytest.mark.asyncio
async def test_execute_with_resilience_succeeds_after_transient_retry() -> None:
    """Verifies successful execution after encountering transient failures."""
    call_count = 0

    async def flaky_call():
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise RateLimitError("HTTP 429: Too Many Requests", model="llama-3", llm_provider="groq")
        return "success_response"

    result = await execute_with_resilience(
        flaky_call, max_attempts=3, multiplier=0.01, max_wait=0.05
    )
    assert result == "success_response"
    assert call_count == 2


@pytest.mark.asyncio
async def test_execute_with_resilience_fails_fast_on_fatal_error() -> None:
    """Verifies non-transient error raises immediately without retrying."""
    call_count = 0

    async def fatal_call():
        nonlocal call_count
        call_count += 1
        raise ValueError("Non-retryable invalid format")

    with pytest.raises(ValueError, match="Non-retryable invalid format"):
        await execute_with_resilience(
            fatal_call, max_attempts=3, multiplier=0.01, max_wait=0.05
        )

    assert call_count == 1
