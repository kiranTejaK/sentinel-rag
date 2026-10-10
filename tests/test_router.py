"""Unit tests for ResilientLLMRouter with LiteLLM and Circuit Breaker."""

from unittest.mock import AsyncMock, patch

import pytest
from litellm.exceptions import ServiceUnavailableError
from litellm.types.utils import Choices, Message, ModelResponse, Usage

from app.core.circuit_breaker import CircuitBreaker, CircuitState
from app.services.router import ResilientLLMRouter


def _build_mock_response(content: str, model: str) -> ModelResponse:
    """Helper to construct LiteLLM ModelResponse."""
    return ModelResponse(
        id="resp_123",
        choices=[Choices(message=Message(content=content, role="assistant"))],
        created=1700000000,
        model=model,
        usage=Usage(prompt_tokens=20, completion_tokens=30, total_tokens=50),
    )


@pytest.mark.asyncio
async def test_router_primary_model_success() -> None:
    """Verifies successful completion via primary Groq provider."""
    cb = CircuitBreaker(failure_threshold=5, recovery_timeout=60.0)
    router = ResilientLLMRouter(
        primary_model="groq/llama-3.3-70b-versatile",
        fallback_model="openai/gpt-4o-mini",
        circuit_breaker=cb,
    )

    mock_resp = _build_mock_response("Groq generated completion", "groq/llama-3.3-70b-versatile")

    with patch("litellm.acompletion", new_callable=AsyncMock) as mock_litellm:
        mock_litellm.return_value = mock_resp

        messages = [{"role": "user", "content": "What is SentinelRAG?"}]
        result = await router.complete(messages=messages, tenant_id="tenant_1")

        assert result.content == "Groq generated completion"
        assert result.fallback_triggered is False
        assert result.model_used == "groq/llama-3.3-70b-versatile"
        assert result.provider == "groq"
        assert result.circuit_breaker_state == "CLOSED"
        assert result.cost_usd > 0.0
        assert cb.consecutive_failures == 0


@pytest.mark.asyncio
async def test_router_fallback_triggered_on_primary_failure() -> None:
    """Verifies that primary failure triggers failover to fallback model."""
    cb = CircuitBreaker(failure_threshold=5, recovery_timeout=60.0)
    router = ResilientLLMRouter(
        primary_model="groq/llama-3.3-70b-versatile",
        fallback_model="openai/gpt-4o-mini",
        circuit_breaker=cb,
    )

    fallback_resp = _build_mock_response("OpenAI fallback completion", "openai/gpt-4o-mini")

    with patch("litellm.acompletion", new_callable=AsyncMock) as mock_litellm:
        # 3 failures exhaust primary retries; 4th call (fallback) succeeds
        mock_litellm.side_effect = [
            ServiceUnavailableError("Groq unavailable 1", model="llama-3", llm_provider="groq"),
            ServiceUnavailableError("Groq unavailable 2", model="llama-3", llm_provider="groq"),
            ServiceUnavailableError("Groq unavailable 3", model="llama-3", llm_provider="groq"),
            fallback_resp,
        ]

        messages = [{"role": "user", "content": "Query"}]
        result = await router.complete(messages=messages, tenant_id="tenant_1")

        assert result.content == "OpenAI fallback completion"
        assert result.fallback_triggered is True
        assert result.model_used == "openai/gpt-4o-mini"
        assert result.provider == "openai"
        assert cb.consecutive_failures == 1


@pytest.mark.asyncio
async def test_router_fails_fast_when_circuit_breaker_is_open() -> None:
    """Verifies that an OPEN circuit breaker bypasses primary provider with 0 network calls."""
    cb = CircuitBreaker(failure_threshold=2, recovery_timeout=60.0)
    # Trip circuit breaker
    await cb.trip()
    assert cb.state == CircuitState.OPEN

    router = ResilientLLMRouter(
        primary_model="groq/llama-3.3-70b-versatile",
        fallback_model="openai/gpt-4o-mini",
        circuit_breaker=cb,
    )

    fallback_resp = _build_mock_response("Fast-fail fallback response", "openai/gpt-4o-mini")

    with patch("litellm.acompletion", new_callable=AsyncMock) as mock_litellm:
        mock_litellm.return_value = fallback_resp

        messages = [{"role": "user", "content": "Query"}]
        result = await router.complete(messages=messages, tenant_id="tenant_1")

        # Primary must NOT have been called!
        assert mock_litellm.call_count == 1
        assert mock_litellm.call_args.kwargs["model"] == "openai/gpt-4o-mini"

        assert result.content == "Fast-fail fallback response"
        assert result.fallback_triggered is True
        assert result.fallback_reason == "circuit_breaker_open_fail_fast"
        assert result.circuit_breaker_state == "OPEN"
