"""Unit tests for Token & Cost Analytics Tracker."""

import pytest

from app.services.token_analytics import (
    TokenAnalyticsTracker,
    calculate_request_cost,
    count_messages_tokens,
    count_text_tokens,
    get_model_rates,
)


def test_count_text_tokens_accuracy() -> None:
    """Verifies that tiktoken accurately counts tokens for plain strings."""
    text = "SentinelRAG provides sub-20ms latency and high resilience."
    tokens = count_text_tokens(text, model_name="gpt-4o-mini")
    assert tokens > 0
    assert isinstance(tokens, int)


def test_count_messages_tokens_overhead() -> None:
    """Verifies chat message formatting tokens."""
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Explain circuit breakers in distributed systems."},
    ]
    tokens = count_messages_tokens(messages, model_name="gpt-4o-mini")
    # Must be greater than raw text tokens due to chat delimiters
    raw_tokens = count_text_tokens(
        "You are a helpful assistant. Explain circuit breakers in distributed systems."
    )
    assert tokens > raw_tokens


def test_model_rates_resolution() -> None:
    """Verifies pricing resolution for Groq and OpenAI models."""
    groq_rates = get_model_rates("groq/llama-3.3-70b-versatile")
    assert groq_rates["prompt"] == 0.59
    assert groq_rates["completion"] == 0.79

    openai_rates = get_model_rates("openai/gpt-4o-mini")
    assert openai_rates["prompt"] == 0.15
    assert openai_rates["completion"] == 0.60


def test_calculate_request_cost() -> None:
    """Verifies request cost calculation per 1M token rates."""
    # 1,000 prompt tokens on groq/llama-3.3-70b-versatile = (1000 / 1_000_000) * 0.59 = $0.00059
    # 1,000 completion tokens = (1000 / 1_000_000) * 0.79 = $0.00079
    cost = calculate_request_cost("groq/llama-3.3-70b-versatile", 1000, 1000)
    assert cost == pytest.approx(0.00138, rel=1e-5)


@pytest.mark.asyncio
async def test_token_analytics_tracker_dollars_saved() -> None:
    """Verifies cumulative dollars saved tracking when served from cache."""
    tracker = TokenAnalyticsTracker(redis_client=None)

    saved = await tracker.record_cache_hit_savings(
        tenant_id="tenant_alpha",
        prompt_tokens=500,
        completion_tokens=500,
        baseline_model="openai/gpt-4o-mini",
    )
    assert saved > 0.0

    cumulative = await tracker.get_cumulative_dollars_saved("tenant_alpha")
    assert cumulative == pytest.approx(saved, rel=1e-5)

    metrics = await tracker.get_summary_metrics()
    assert metrics["cache_hits"] == 1
    assert metrics["dollars_saved"] > 0.0
