"""Resilience engine providing exponential backoff with full jitter using Tenacity.

Targets transient HTTP 429 (Rate Limit), 503 (Service Unavailable), 502 (Bad Gateway),
504 (Gateway Timeout), and network socket drops while failing fast on permanent client errors (400, 401, 403).
"""

from collections.abc import Callable
from typing import Any

import httpx
from litellm.exceptions import (
    APIConnectionError,
    InternalServerError,
    RateLimitError,
    ServiceUnavailableError,
    Timeout,
)
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception,
    stop_after_attempt,
    wait_random_exponential,
)

from app.core.logging import logger


def is_transient_error(exc: BaseException) -> bool:
    """Classifies exceptions as transient (retryable) vs permanent (fatal/fail-fast).

    Retryable:
    - HTTP 429 (Too Many Requests / Rate Limiting)
    - HTTP 503 (Service Unavailable / Capacity Exhaustion)
    - HTTP 502 / 504 (Bad Gateway / Upstream Gateway Timeout)
    - Network drops, timeouts, socket resets

    Non-Retryable:
    - HTTP 400 (Invalid Request / Schema Mismatch)
    - HTTP 401 (Unauthorized / Invalid API Key)
    - HTTP 403 (Forbidden / Tier Quota Disallowed)
    - HTTP 404 (Model Not Found)
    """
    if isinstance(
        exc,
        RateLimitError
        | ServiceUnavailableError
        | InternalServerError
        | APIConnectionError
        | Timeout
        | TimeoutError
        | ConnectionError
        | httpx.TimeoutException
        | httpx.NetworkError,
    ):
        return True

    # Check for HTTPStatusError or generic status_code attribute
    status_code = getattr(exc, "status_code", None)
    if status_code in (429, 502, 503, 504):
        return True

    # Inspect error message strings for provider rate limit / overload hints
    msg = str(exc).lower()
    transient_indicators = [
        "rate limit",
        "too many requests",
        "overloaded",
        "service unavailable",
        "503",
        "429",
        "connection reset",
        "timed out",
    ]
    return any(indicator in msg for indicator in transient_indicators)


def create_resilient_retryer(
    max_attempts: int = 3,
    multiplier: float = 0.5,
    max_wait: float = 5.0,
    before_sleep_hook: Callable[[RetryCallState], None] | None = None,
) -> AsyncRetrying:
    """Creates an AsyncRetrying instance configured with Exponential Backoff and Full Jitter.

    Mathematical Mechanics:
    T_sleep ~ Uniform(0, min(max_wait, multiplier * 2^attempt))
    Eliminates the thundering herd problem under provider-wide rate limit degradation.
    """

    def default_before_sleep(retry_state: RetryCallState) -> None:
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        logger.warning(
            "transient_error_detected_retrying",
            attempt=retry_state.attempt_number,
            max_attempts=max_attempts,
            next_action="exponential_backoff_with_full_jitter",
            error=str(exc),
            error_type=exc.__class__.__name__ if exc else "Unknown",
        )

    return AsyncRetrying(
        retry=retry_if_exception(is_transient_error),
        wait=wait_random_exponential(multiplier=multiplier, max=max_wait),
        stop=stop_after_attempt(max_attempts),
        before_sleep=before_sleep_hook or default_before_sleep,
        reraise=True,
    )


async def execute_with_resilience(
    coro_func: Callable[..., Any],
    *args: Any,
    max_attempts: int = 3,
    multiplier: float = 0.5,
    max_wait: float = 5.0,
    **kwargs: Any,
) -> Any:
    """Executes an async callable wrapped in exponential backoff + jitter retry."""
    retryer = create_resilient_retryer(
        max_attempts=max_attempts,
        multiplier=multiplier,
        max_wait=max_wait,
    )
    async for attempt in retryer:
        with attempt:
            return await coro_func(*args, **kwargs)
