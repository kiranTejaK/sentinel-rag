"""Unified Resilient LLM Router using LiteLLM.

Features:
- Primary Provider: Groq (ultra-low latency Llama-3-70B / 8B).
- Fallback Provider: OpenAI (gpt-4o-mini) or Mistral.
- Circuit Breaker Protection: Fails fast to fallback when primary is OPEN.
- Tenacity Resilience Engine: Exponential backoff with full jitter on HTTP 429/503.
- Token & Cost Analytics: tiktoken token counting, per-request cost, and cumulative dollars saved.
"""

import time
from dataclasses import dataclass
from typing import Any

import litellm
import redis.asyncio as aioredis

from app.core.circuit_breaker import CircuitBreaker, CircuitState
from app.core.config import settings
from app.core.logging import logger
from app.services.resilience import execute_with_resilience
from app.services.token_analytics import (
    TokenAnalyticsTracker,
    count_messages_tokens,
    count_text_tokens,
)

# Suppress verbose litellm telemetry in production
litellm.telemetry = False
litellm.suppress_debug_info = True


@dataclass(slots=True)
class RouterExecutionResult:
    """Outcome of an LLM routing invocation."""

    content: str
    provider: str
    model_used: str
    primary_model: str
    fallback_model: str
    fallback_triggered: bool
    fallback_reason: str | None
    circuit_breaker_state: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float
    latency_ms: float
    raw_response: dict[str, Any] | None = None


class ResilientLLMRouter:
    """Enterprise multi-provider LLM router with automatic failover and circuit breaking."""

    def __init__(
        self,
        redis_client: aioredis.Redis | None = None,
        primary_model: str | None = None,
        fallback_model: str | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        analytics_tracker: TokenAnalyticsTracker | None = None,
    ) -> None:
        self.primary_model = primary_model or settings.DEFAULT_PRIMARY_MODEL
        self.fallback_model = fallback_model or settings.DEFAULT_FALLBACK_MODEL_1
        self.tertiary_model = settings.DEFAULT_FALLBACK_MODEL_2

        self.circuit_breaker = circuit_breaker or CircuitBreaker(
            name="groq_primary_circuit",
            failure_threshold=settings.CIRCUIT_BREAKER_FAILURE_THRESHOLD,
            recovery_timeout=float(settings.CIRCUIT_BREAKER_RECOVERY_TIME_SECONDS),
        )
        self.analytics = analytics_tracker or TokenAnalyticsTracker(redis_client)

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tenant_id: str = "default",
        temperature: float = 0.7,
        max_tokens: int = 1024,
        preferred_model: str | None = None,
        **kwargs: Any,
    ) -> RouterExecutionResult:
        """Executes a chat completion request with circuit breaker protection and fallback.

        Flow:
        1. Check Circuit Breaker for primary provider.
        2. If OPEN: Fail fast directly to fallback provider (0ms network delay).
        3. If CLOSED / HALF_OPEN: Execute primary model with tenacity exponential backoff + jitter.
        4. If primary succeeds: Record circuit success, calculate cost, return.
        5. If primary exhausts retries: Trip circuit breaker, failover to fallback model.
        """
        start_time = time.perf_counter()
        target_primary = preferred_model or self.primary_model

        can_probe = await self.circuit_breaker.can_execute()
        current_state = self.circuit_breaker.state

        logger.info(
            "llm_router_invocation",
            tenant_id=tenant_id,
            primary_model=target_primary,
            fallback_model=self.fallback_model,
            circuit_state=current_state.value,
            can_probe=can_probe,
        )

        # FAST PATH: Circuit is OPEN -> Fail fast to fallback immediately!
        if not can_probe or current_state == CircuitState.OPEN:
            logger.warning(
                "circuit_open_failing_fast_to_fallback",
                tenant_id=tenant_id,
                primary_model=target_primary,
                fallback_model=self.fallback_model,
                seconds_remaining=round(self.circuit_breaker.seconds_remaining_in_open, 2),
            )
            return await self._execute_fallback(
                messages=messages,
                tenant_id=tenant_id,
                fallback_model=self.fallback_model,
                fallback_reason="circuit_breaker_open_fail_fast",
                temperature=temperature,
                max_tokens=max_tokens,
                start_time=start_time,
                **kwargs,
            )

        # NORMAL / CANARY PATH: Attempt primary model with resilience retry
        try:
            response = await execute_with_resilience(
                litellm.acompletion,
                model=target_primary,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=settings.CIRCUIT_BREAKER_TIMEOUT_SECONDS,
                api_key=self._get_api_key(target_primary),
                max_attempts=3,
                multiplier=0.5,
                max_wait=4.0,
                **kwargs,
            )

            # Record success in circuit breaker
            await self.circuit_breaker.record_success()

            latency_ms = round((time.perf_counter() - start_time) * 1000.0, 2)
            content = self._extract_content(response)

            prompt_tokens, completion_tokens = self._extract_tokens(
                response, messages, content, target_primary
            )
            cost_usd = await self.analytics.record_llm_execution(
                tenant_id=tenant_id,
                model_name=target_primary,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )

            provider_name = target_primary.split("/")[0] if "/" in target_primary else "groq"

            return RouterExecutionResult(
                content=content,
                provider=provider_name,
                model_used=target_primary,
                primary_model=target_primary,
                fallback_model=self.fallback_model,
                fallback_triggered=False,
                fallback_reason=None,
                circuit_breaker_state=self.circuit_breaker.state.value,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
                cost_usd=cost_usd,
                latency_ms=latency_ms,
                raw_response=response.model_dump() if hasattr(response, "model_dump") else None,
            )

        except Exception as primary_error:
            # Record failure in circuit breaker
            await self.circuit_breaker.record_failure()

            logger.error(
                "primary_provider_failed_initiating_fallback",
                tenant_id=tenant_id,
                primary_model=target_primary,
                fallback_model=self.fallback_model,
                circuit_state=self.circuit_breaker.state.value,
                error=str(primary_error),
                error_type=primary_error.__class__.__name__,
            )

            return await self._execute_fallback(
                messages=messages,
                tenant_id=tenant_id,
                fallback_model=self.fallback_model,
                fallback_reason=f"primary_exhausted: {primary_error.__class__.__name__}: {primary_error}",
                temperature=temperature,
                max_tokens=max_tokens,
                start_time=start_time,
                **kwargs,
            )

    async def _execute_fallback(
        self,
        messages: list[dict[str, Any]],
        tenant_id: str,
        fallback_model: str,
        fallback_reason: str,
        temperature: float,
        max_tokens: int,
        start_time: float,
        **kwargs: Any,
    ) -> RouterExecutionResult:
        """Executes fallback model with secondary retry protection."""
        try:
            response = await execute_with_resilience(
                litellm.acompletion,
                model=fallback_model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=settings.CIRCUIT_BREAKER_TIMEOUT_SECONDS,
                api_key=self._get_api_key(fallback_model),
                max_attempts=2,
                multiplier=0.5,
                max_wait=3.0,
                **kwargs,
            )
            model_used = fallback_model
        except Exception as fallback_error:
            logger.error(
                "fallback_provider_failed_trying_tertiary",
                tenant_id=tenant_id,
                fallback_model=fallback_model,
                tertiary_model=self.tertiary_model,
                error=str(fallback_error),
            )
            # Try tertiary model
            response = await litellm.acompletion(
                model=self.tertiary_model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=settings.CIRCUIT_BREAKER_TIMEOUT_SECONDS,
                api_key=self._get_api_key(self.tertiary_model),
                **kwargs,
            )
            model_used = self.tertiary_model

        latency_ms = round((time.perf_counter() - start_time) * 1000.0, 2)
        content = self._extract_content(response)

        prompt_tokens, completion_tokens = self._extract_tokens(
            response, messages, content, model_used
        )
        cost_usd = await self.analytics.record_llm_execution(
            tenant_id=tenant_id,
            model_name=model_used,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

        provider_name = model_used.split("/")[0] if "/" in model_used else "openai"

        return RouterExecutionResult(
            content=content,
            provider=provider_name,
            model_used=model_used,
            primary_model=self.primary_model,
            fallback_model=fallback_model,
            fallback_triggered=True,
            fallback_reason=fallback_reason,
            circuit_breaker_state=self.circuit_breaker.state.value,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            raw_response=response.model_dump() if hasattr(response, "model_dump") else None,
        )

    @staticmethod
    def _extract_content(response: Any) -> str:
        """Safely extracts text string from LiteLLM ModelResponse."""
        if hasattr(response, "choices") and response.choices:
            choice = response.choices[0]
            if hasattr(choice, "message") and hasattr(choice.message, "content"):
                return choice.message.content or ""
            if isinstance(choice, dict):
                return choice.get("message", {}).get("content", "")
        return str(response)

    @staticmethod
    def _extract_tokens(
        response: Any,
        messages: list[dict[str, Any]],
        content: str,
        model_name: str,
    ) -> tuple[int, int]:
        """Extracts token counts from provider usage or computes with tiktoken."""
        prompt_tokens = 0
        completion_tokens = 0

        if hasattr(response, "usage") and response.usage:
            prompt_tokens = getattr(response.usage, "prompt_tokens", 0) or 0
            completion_tokens = getattr(response.usage, "completion_tokens", 0) or 0

        # Fallback to tiktoken calculation if provider reported 0
        if prompt_tokens <= 0:
            prompt_tokens = count_messages_tokens(messages, model_name)
        if completion_tokens <= 0:
            completion_tokens = count_text_tokens(content, model_name)

        return prompt_tokens, completion_tokens

    @staticmethod
    def _get_api_key(model_name: str) -> str | None:
        """Retrieves provider API key from environment / settings."""
        lower = model_name.lower()
        if "groq" in lower:
            return settings.GROQ_API_KEY
        if "openai" in lower:
            return settings.OPENAI_API_KEY
        if "mistral" in lower:
            return settings.MISTRAL_API_KEY
        if "anthropic" in lower:
            return settings.ANTHROPIC_API_KEY
        return None


_global_router: ResilientLLMRouter | None = None


def get_llm_router(redis_client: Any = None) -> ResilientLLMRouter:
    """Returns singleton ResilientLLMRouter instance to maintain circuit breaker state."""
    global _global_router
    if _global_router is None:
        _global_router = ResilientLLMRouter(redis_client=redis_client)
    elif redis_client is not None and _global_router.analytics.redis is None:
        _global_router.analytics.redis = redis_client
    return _global_router
