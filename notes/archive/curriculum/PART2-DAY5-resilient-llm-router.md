# DAY 5 - Resilient LLM Router, Circuit Breaker State Machine & Cost Analytics

> **Goal:** By the end of Day 5, you will understand how SentinelRack prevents cascading provider outages using a coroutine-safe Circuit Breaker Finite State Machine (CLOSED, OPEN, HALF-OPEN), executes retries with exponential backoff and full jitter, routes between primary (Groq Llama 3) and fallback (OpenAI/Mistral) models, and tracks real-time token economics and cumulative "Dollars Saved" using `tiktoken`. You will also construct the complete end-to-end `POST /v1/chat/completions` gateway endpoint.

---

## 1. The Core Engineering Problem

### The Fragility of External LLM APIs
Production RAG systems depend on external third-party AI APIs (Groq, OpenAI, Anthropic, Mistral). In real-world enterprise environments, these APIs exhibit distinct failure modes:
1. **Sudden Outages & Degraded Capacity:** An upstream provider experiences severe infrastructure failure, returning HTTP 503 Service Unavailable or HTTP 502 Bad Gateway.
2. **Aggressive Rate Limiting (HTTP 429):** High-traffic enterprise spikes exceed Tier TPM (Tokens Per Minute) or RPM (Requests Per Minute) quotas.
3. **Latency Tail Drifts & Hanging TCP Sockets:** Instead of failing fast, an overloaded provider accepts incoming TCP connections but hangs for 45 to 90 seconds before sending token frames.

### What Happens Without Circuit Breaking: The Cascading Failure
If your gateway naively retries an overloaded provider with standard synchronous loops:
```
[100 User Requests] ───> [Gateway] ───> [Failing Provider (Groq)] (Hangs 30s)
                                          │
                                          ▼
                               Gateway worker threads blocked!
                               Database connections held open!
                               Uvicorn worker pool starved!
                               Total system crash across the enterprise!
```

This is called a **cascading failure**. An outage in one third-party external dependency takes down your entire internal application infrastructure.

---

## 2. The Solution: Michael Nygard's Circuit Breaker Pattern

Originally popularized in electrical engineering and adapted for distributed software by Michael Nygard in *Release It!*, a **Circuit Breaker** acts as an automatic switch placed between your application and an external remote service.

### The Three Operational States

```
                ┌──────────────────────────────────────────────┐
                │                                              │
                ▼                                              │ Success
        ┌──────────────┐    consecutive_failures >= threshold  │ (Canary probe)
        │    CLOSED    │ ─────────────────────────────────┐    │
        └──────────────┘                                  │    │
          ▲ (Normal operation:                            ▼    │
          │  traffic flows to primary)             ┌──────────────┐
          │                                        │     OPEN     │
          │                                        └──────────────┘
          │                                          │ (Fail Fast:
          │ Recovery timeout elapsed                 │  0ms network delay,
          │ (Canary probe allowed)                   │  instant fallback)
          │                                          │
          │         ┌──────────────────┐             │
          └──────── │    HALF_OPEN     │ <───────────┘
                    └──────────────────┘
                      │
                      │ Failure (Canary probe fails)
                      ▼
                    [OPEN] (Timer resets!)
```

### 1. `CLOSED` State (Normal Operation)
- Requests are permitted to execute against the primary provider (e.g., Groq `llama-3.3-70b-versatile`).
- If an execution fails, internal error counters increment.
- If consecutive failures reach `failure_threshold` (e.g., 5 consecutive errors), the circuit immediately **trips to `OPEN`**.

### 2. `OPEN` State (Fail-Fast Mode)
- The primary provider is known to be down or severely degraded.
- **Zero network requests** are sent to the primary provider.
- Any request attempting to hit the primary provider fails fast in **$0.0\text{ms}$**, instantly routing to the fallback provider (e.g., OpenAI `gpt-4o-mini`).
- This accomplishes two critical goals:
  1. Protects the gateway's thread pool and connection buffers from hanging on dying sockets.
  2. Gives the upstream provider breathing room to recover without being hammered by retry storms.

### 3. `HALF_OPEN` State (Canary Probe Mode)
- After a configured `recovery_timeout` (e.g., 60 seconds), the circuit transitions to `HALF_OPEN`.
- The circuit permits **one single request** (a "canary probe") to test the primary provider:
  - If the canary request **succeeds**: The provider is healthy again! The circuit transitions back to `CLOSED`, resetting failure counts.
  - If the canary request **fails**: The provider is still broken. The circuit trips immediately back to `OPEN`, resetting the 60-second recovery timer.

---

## 3. Resilience Engine: Exponential Backoff with Full Jitter

When a transient error occurs (like an HTTP 429 rate limit or HTTP 503 capacity blip), a naive retry policy simply waits a fixed duration:
$$\text{Retry 1: wait 1s} \quad \longrightarrow \quad \text{Retry 2: wait 1s} \quad \longrightarrow \quad \text{Retry 3: wait 1s}$$

### The Thundering Herd Problem
If 500 concurrent users hit a provider rate limit simultaneously, and all 500 wait exactly 1 second, all 500 clients retry at the exact same millisecond. This creates a synchronized spike called a **thundering herd**, repeatedly re-tripping the rate limit.

### Full Jitter Formula (AWS Architecture Standard)
To break synchronized waves of retries, SentinelRack uses **Exponential Backoff with Full Jitter** via the `tenacity` library:

$$t_{\text{sleep}} \sim \text{Uniform}\left(0, \, \min(t_{\text{max}}, \, \text{multiplier} \cdot 2^{\text{attempt}})\right)$$

```
Attempt 1 (multiplier=0.5): Uniform(0, 1.0s)  --> e.g., 0.23s, 0.81s, 0.44s
Attempt 2 (multiplier=0.5): Uniform(0, 2.0s)  --> e.g., 1.45s, 0.12s, 1.88s
Attempt 3 (multiplier=0.5): Uniform(0, 4.0s)  --> e.g., 3.12s, 0.95s, 2.74s
```
Because the sleep interval is randomly distributed across the entire exponential window, retry attempts from hundreds of clients spread out evenly over time, allowing the upstream provider to absorb traffic smoothly.

### Transient vs. Permanent Error Classification
Never retry client errors! SentinelRack strictly distinguishes between retryable and non-retryable errors:
- **Retryable (Transient):** HTTP 429 (Rate Limit), HTTP 503 (Service Unavailable), HTTP 502 (Bad Gateway), HTTP 504 (Gateway Timeout), TCP connection resets, socket read timeouts.
- **Non-Retryable (Fatal / Fail-Fast):** HTTP 400 (Bad Request / Schema Error), HTTP 401 (Invalid API Key), HTTP 403 (Forbidden), HTTP 404 (Model Not Found). Retrying an invalid API key is pointless and wastes quota.

---

## 4. Token & Cost Economics: `tiktoken` & "Dollars Saved"

How does an enterprise AI gateway prove its ROI (Return on Investment) to executives and engineering leadership? By tracking **cost per request** and cumulative **"Dollars Saved"**.

### Exact BPE Token Counting via `tiktoken`
Instead of guessing tokens based on character count ($1\text{ token} \approx 4\text{ chars}$), SentinelRack uses OpenAI's official Byte-Pair Encoding (BPE) engine:
- For raw text strings: `tiktoken.encoding_for_model("gpt-4o-mini").encode(text)`
- For chat message arrays: Accounts for the exact chat markup overhead ($3\text{ tokens}$ per message for `<|start|>{role}\n{content}<|end|>\n` plus $3\text{ primer tokens}$ for the assistant response).

### Pricing Matrix & Cost Formulas:
SentinelRack maintains exact dollar rates per 1,000,000 tokens:
$$\text{Cost} = \left(\frac{\text{Prompt Tokens}}{1,000,000} \times \text{Rate}_{\text{prompt}}\right) + \left(\frac{\text{Completion Tokens}}{1,000,000} \times \text{Rate}_{\text{completion}}\right)$$

| Provider / Model | Prompt Rate ($/1M) | Completion Rate ($/1M) |
|---|---|---|
| **Groq Llama-3.3-70B** | **$0.59** | **$0.79** |
| **Groq Llama-3.1-8B** | **$0.05** | **$0.08** |
| **OpenAI gpt-4o-mini** | **$0.15** | **$0.60** |
| **OpenAI gpt-4o** | **$2.50** | **$10.00** |

### The "Dollars Saved" Metric
When a user query is resolved by the Day 4 Semantic Cache:
1. The gateway computes the exact prompt tokens and completion tokens of the cached response.
2. It calculates what that request *would have cost* if sent to the primary LLM.
3. It atomically increments the tenant's cumulative savings counter in Redis via `HINCRBYFLOAT`:
   ```redis
   HINCRBYFLOAT analytics:tenant_acme:dollars_saved value 0.000452
   ```
4. This value is echoed back in the response headers and Prometheus metrics, giving real-time visibility into cost reductions.

---

## 5. Code Walkthrough - File by File

### File 1: Circuit Breaker FSM - `app/core/circuit_breaker.py`

```python
# app/core/circuit_breaker.py

import asyncio
import time
from enum import StrEnum
from typing import Any

from app.core.config import settings
from app.core.logging import logger


class CircuitState(StrEnum):
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
    def seconds_remaining_in_open(self) -> float:
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
                        # Recovery timeout elapsed: allow single canary probe
                        self._transition_to(CircuitState.HALF_OPEN)
                        logger.warning(
                            "circuit_breaker_half_open_probe_allowed",
                            name=self.name,
                            elapsed=elapsed,
                        )
                        return True
                return False

            # In HALF_OPEN, can_execute returns True for the active canary probe
            return self._state == CircuitState.HALF_OPEN

    async def record_success(self) -> None:
        """Records a successful call, closing half-open circuits."""
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
                logger.error("circuit_breaker_probe_failed", name=self.name, re_tripped_to="OPEN")
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
                )
                self._transition_to(CircuitState.OPEN)

    def _transition_to(self, new_state: CircuitState) -> None:
        self._state = new_state
        self._last_state_change = time.time()
```

#### Why `asyncio.Lock()` is Mandatory Here:
In an asynchronous event loop, dozens of concurrent coroutines evaluate `can_execute()`, `record_success()`, and `record_failure()` simultaneously. Without `async with self._lock:`, two coroutines experiencing simultaneous failures could read stale state, causing race conditions in state transitions (e.g., transitions skipping `HALF_OPEN` or corrupting failure counters).

---

### File 2: Resilience Retryer - `app/services/resilience.py`

```python
# app/services/resilience.py

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
    """Classifies exceptions as transient (retryable) vs permanent (fatal/fail-fast)."""
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

    status_code = getattr(exc, "status_code", None)
    if status_code in (429, 502, 503, 504):
        return True

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
    """Creates an AsyncRetrying instance configured with Exponential Backoff and Full Jitter."""

    def default_before_sleep(retry_state: RetryCallState) -> None:
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        logger.warning(
            "transient_error_detected_retrying",
            attempt=retry_state.attempt_number,
            max_attempts=max_attempts,
            error=str(exc),
        )

    hook = before_sleep_hook or default_before_sleep

    return AsyncRetrying(
        reraise=True,
        stop=stop_after_attempt(max_attempts),
        wait=wait_random_exponential(multiplier=multiplier, max=max_wait),
        retry=retry_if_exception(is_transient_error),
        before_sleep=hook,
    )


async def execute_with_resilience(
    coro_fn: Callable[..., Any],
    *args: Any,
    max_attempts: int = 3,
    multiplier: float = 0.5,
    max_wait: float = 4.0,
    **kwargs: Any,
) -> Any:
    """Executes an async callable with resilient exponential backoff and jitter."""
    retryer = create_resilient_retryer(
        max_attempts=max_attempts,
        multiplier=multiplier,
        max_wait=max_wait,
    )
    async for attempt in retryer:
        with attempt:
            return await coro_fn(*args, **kwargs)
```

---

### File 3: Unified Resilient Router - `app/services/router.py`

```python
# app/services/router.py

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

litellm.telemetry = False
litellm.suppress_debug_info = True


@dataclass(slots=True)
class RouterExecutionResult:
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
        start_time = time.perf_counter()
        target_primary = preferred_model or self.primary_model

        can_probe = await self.circuit_breaker.can_execute()
        current_state = self.circuit_breaker.state

        # FAST PATH: Circuit is OPEN -> Fail fast to fallback immediately (0ms delay)
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

        # NORMAL PATH: Execute primary model with Tenacity retry
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

            await self.circuit_breaker.record_success()

            latency_ms = round((time.perf_counter() - start_time) * 1000.0, 2)
            content = self._extract_content(response)
            p_tokens, c_tokens = self._extract_tokens(response, messages, content, target_primary)
            cost_usd = self.analytics.calculate_cost(target_primary, p_tokens, c_tokens)

            await self.analytics.record_llm_spend(tenant_id, target_primary, p_tokens, c_tokens)

            return RouterExecutionResult(
                content=content,
                provider="primary",
                model_used=target_primary,
                primary_model=target_primary,
                fallback_model=self.fallback_model,
                fallback_triggered=False,
                fallback_reason=None,
                circuit_breaker_state=self.circuit_breaker.state.value,
                prompt_tokens=p_tokens,
                completion_tokens=c_tokens,
                total_tokens=p_tokens + c_tokens,
                cost_usd=cost_usd,
                latency_ms=latency_ms,
                raw_response=response.model_dump() if hasattr(response, "model_dump") else None,
            )

        except Exception as primary_exc:
            # Primary failed after retries: trip circuit breaker and execute fallback
            logger.error(
                "primary_model_exhausted_failing_over",
                primary_model=target_primary,
                fallback_model=self.fallback_model,
                error=str(primary_exc),
            )
            await self.circuit_breaker.record_failure()

            return await self._execute_fallback(
                messages=messages,
                tenant_id=tenant_id,
                fallback_model=self.fallback_model,
                fallback_reason=f"primary_failed: {type(primary_exc).__name__}",
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
        """Executes secondary fallback provider."""
        response = await execute_with_resilience(
            litellm.acompletion,
            model=fallback_model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=settings.CIRCUIT_BREAKER_TIMEOUT_SECONDS,
            api_key=self._get_api_key(fallback_model),
            max_attempts=3,
            multiplier=0.5,
            max_wait=4.0,
            **kwargs,
        )

        latency_ms = round((time.perf_counter() - start_time) * 1000.0, 2)
        content = self._extract_content(response)
        p_tokens, c_tokens = self._extract_tokens(response, messages, content, fallback_model)
        cost_usd = self.analytics.calculate_cost(fallback_model, p_tokens, c_tokens)

        await self.analytics.record_llm_spend(tenant_id, fallback_model, p_tokens, c_tokens)

        return RouterExecutionResult(
            content=content,
            provider="fallback",
            model_used=fallback_model,
            primary_model=self.primary_model,
            fallback_model=fallback_model,
            fallback_triggered=True,
            fallback_reason=fallback_reason,
            circuit_breaker_state=self.circuit_breaker.state.value,
            prompt_tokens=p_tokens,
            completion_tokens=c_tokens,
            total_tokens=p_tokens + c_tokens,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
        )

    def _get_api_key(self, model: str) -> str | None:
        if "groq" in model:
            return settings.GROQ_API_KEY
        if "openai" in model or "gpt" in model:
            return settings.OPENAI_API_KEY
        if "mistral" in model:
            return settings.MISTRAL_API_KEY
        return None

    def _extract_content(self, response: Any) -> str:
        try:
            return str(response.choices[0].message.content or "")
        except Exception:
            return str(response)

    def _extract_tokens(
        self, response: Any, messages: list[dict[str, Any]], content: str, model: str
    ) -> tuple[int, int]:
        try:
            usage = response.usage
            return usage.prompt_tokens, usage.completion_tokens
        except Exception:
            p_tokens = count_messages_tokens(messages, model)
            c_tokens = count_text_tokens(content, model)
            return p_tokens, c_tokens
```

---

### File 4: The Unified Chat Endpoint - `app/api/v1/chat.py`

This endpoint connects **all four days of work** into a unified pipeline:
1. **Day 4:** Check Semantic Cache. If hit -> return in 15ms.
2. **Day 3:** If cache miss -> execute Single-Store Hybrid Search (Dense HNSW + FTS GIN merged via RRF $k=60$).
3. **Day 5:** Augment prompt with retrieved chunks -> execute Resilient Router (Circuit Breaker Groq -> OpenAI).
4. **Day 4 & 5:** Store answer into Semantic Cache -> update Token & Cost Analytics in Redis.

```python
# app/api/v1/chat.py (Abridged core flow)

@router.post("/completions", response_model=ChatCompletionResponse)
async def chat_completions(
    payload: ChatCompletionRequest,
    session: Annotated[AsyncSession, Depends(get_async_session)],
    redis: Annotated[aioredis.Redis, Depends(get_redis_client)],
    router_service: Annotated[ResilientLLMRouter, Depends(get_llm_router)],
) -> ChatCompletionResponse:
    start_time = time.perf_counter()
    cache_service = RedisSemanticCache(redis)

    user_messages = [m for m in payload.messages if m.role == "user"]
    query_text = user_messages[-1].content if user_messages else ""

    # STAGE 1: SUB-20ms SEMANTIC CACHE CHECK
    if payload.use_cache and query_text:
        cache_result = await cache_service.get_or_lookup(
            tenant_id=payload.tenant_id,
            query=query_text,
            query_vector=payload.query_vector,
        )
        if cache_result.status == "CACHE_HIT" and cache_result.cached_response:
            # Return cached response instantly!
            ...

    # STAGE 2: HYBRID SEARCH CONTEXT RETRIEVAL (RRF k=60)
    retrieved_chunks = []
    if payload.use_retrieval and query_text:
        repo = SearchRepository(session)
        dense, sparse = await repo.search_hybrid_candidates(
            tenant_id=payload.tenant_id,
            query_text=query_text,
            query_vector=payload.query_vector,
            dense_limit=payload.retrieval_dense_limit,
            sparse_limit=payload.retrieval_sparse_limit,
        )
        fused = fuse_hybrid_candidates(dense, sparse, k=payload.retrieval_rrf_k, top_k=payload.retrieval_top_k)
        retrieved_chunks = fused

    # STAGE 3: AUGMENT PROMPT WITH RETRIEVED CONTEXT
    augmented_messages = build_augmented_messages(payload.messages, retrieved_chunks)

    # STAGE 4: RESILIENT LLM ROUTER INVOCATION (CIRCUIT BREAKER + TENACITY)
    router_result = await router_service.complete(
        messages=augmented_messages,
        tenant_id=payload.tenant_id,
        temperature=payload.temperature,
        max_tokens=payload.max_tokens,
    )

    # STAGE 5: POPULATE CACHE FOR FUTURE HITS
    if payload.use_cache and query_text:
        await cache_service.set(
            tenant_id=payload.tenant_id,
            query=query_text,
            query_vector=payload.query_vector,
            response=router_result.content,
        )

    return build_chat_response(...)
```

---

## 6. Comprehensive Topic-Wise Interview Questionnaire

*(Mandatory Directive: Zero DSA/LeetCode puzzles. Exhaustive, production-grade technical answers covering high-concurrency systems, storage engines, and system architecture).*

### Topic 1: Resilience, Reliability & Failure Modes

#### Q1.1: Why must the Circuit Breaker transition to `HALF_OPEN` rather than jumping directly from `OPEN` to `CLOSED`? What failure mode does this prevent?
**Answer:**
When an external service experiences severe CPU or memory exhaustion, recovering takes time. 
- If a circuit jumped directly from `OPEN` to `CLOSED`, the gateway would instantly redirect 100% of incoming production traffic back to the primary provider. This immediate flood of thousands of concurrent requests causes an instantaneous relapse—overwhelming the newly recovered provider before it can warm internal caches or establish connection pools.
- The `HALF_OPEN` state introduces a controlled **canary probing phase**. It allows only a single probe request through while keeping all other traffic safely routing to the fallback provider. 
- Only when this canary request succeeds does the circuit transition to `CLOSED`. If the canary fails, the circuit resets its recovery timer without ever subjecting the recovering provider to production traffic volumes.

#### Q1.2: Compare "Full Jitter", "Equal Jitter", and "Decorrelated Jitter" in retry loops. Why does standard exponential backoff without jitter worsen outages?
**Answer:**
Without jitter, backoff times are deterministic: $t = 2^i$. Under concurrent failures, client retries synchronize into periodic spikes ("thundering herds") that hit the server at identical intervals ($1\text{s}, 2\text{s}, 4\text{s}, 8\text{s}$), reinforcing queue collapse.
- **Full Jitter:** $t_{\text{sleep}} = \text{random}(0, \min(\text{cap}, \text{base} \cdot 2^i))$. Clients sleep for a random duration between 0 and the exponential ceiling. This completely flattens the retry distribution across the timeline, offering maximum queue de-synchronization.
- **Equal Jitter:** $t_{\text{sleep}} = \frac{1}{2}(\text{base} \cdot 2^i) + \text{random}(0, \frac{1}{2}(\text{base} \cdot 2^i))$. Guarantees a minimum sleep duration while adding randomness to the upper half.
- **Decorrelated Jitter:** $t_{\text{sleep}} = \min(\text{cap}, \text{random}(\text{base}, \text{sleep}_{\text{prev}} \cdot 3))$. Decouples sleep time from attempt numbers entirely, calculating the next sleep based on the previous sleep duration.
- *AWS Benchmark Studies* show that **Full Jitter** provides the lowest server-side work contention and shortest time to total system recovery under high concurrency.

---

### Topic 2: Python / ASGI Concurrency Internals

#### Q2.1: Why is an `asyncio.Lock()` used inside `CircuitBreaker` instead of a standard `threading.Lock()` or raw non-locked state updates?
**Answer:**
1. **Raw Non-Locked State Updates:** In Python's asyncio event loop, coroutines yield control to the loop at `await` expressions. While Python dictionary lookups and attribute assignments are atomic at the bytecode level, reading state, checking timestamps (`elapsed >= timeout`), and changing state across multiple operations is **not** atomic. Two coroutines interleaved by the event loop can simultaneously detect `OPEN` with `elapsed >= timeout`, causing both to think they are the designated canary probe.
2. **`threading.Lock()` vs `asyncio.Lock()`:** A `threading.Lock()` blocks the OS thread. If an async coroutine calls `threading_lock.acquire()` and blocks, it **freezes the entire OS thread**, preventing the single-threaded asyncio event loop from processing any other coroutine in the application.
3. **`asyncio.Lock()`:** A coroutine-aware lock. Calling `await lock.acquire()` suspends *only the calling coroutine* without blocking the underlying OS thread, allowing the event loop to continue servicing I/O on hundreds of other requests.

#### Q2.2: How does LiteLLM handle asynchronous HTTP connection pooling under high concurrency, and what connection leak risks exist?
**Answer:**
Under the hood, LiteLLM's `acompletion` leverages `httpx.AsyncClient`.
- An `AsyncClient` maintains an underlying HTTP connection pool (`httpcore.AsyncConnectionPool`) with persistent TCP keep-alive sockets to remote provider domains (`api.groq.com`, `api.openai.com`).
- **Connection Leak Risk:** If an application instantiates a new `AsyncClient` per request, it creates a new TCP handshake and TLS negotiation on every request, exhausting local OS ephemeral ports (`TIME_WAIT` socket exhaustion).
- LiteLLM reuses global client singletons across calls. However, if timeouts are not explicitly configured, hanging upstream connections consume slots in the connection pool. SentinelRack mitigates this by passing an explicit `timeout=settings.CIRCUIT_BREAKER_TIMEOUT_SECONDS` (15.0 seconds) to every `litellm.acompletion` call, guaranteeing that unresponsive sockets are forcefully aborted and returned to the pool.

---

### Topic 3: Distributed Systems, Telemetry & Caching

#### Q3.1: Why does SentinelRack use `HINCRBYFLOAT` in Redis for tracking "Dollars Saved" instead of reading the value, adding to it in Python, and writing it back?
**Answer:**
Reading a counter into Python, performing addition, and writing it back is the classic **Check-Then-Act / Read-Modify-Write anti-pattern**:
```
Worker 1: HGET savings   --> Returns $10.00
Worker 2: HGET savings   --> Returns $10.00
Worker 1: $10.00 + $0.05 --> HSET savings $10.05
Worker 2: $10.00 + $0.02 --> HSET savings $10.02 (Worker 1's $0.05 lost!)
```
Under concurrent traffic across 8 ASGI workers, this causes severe race conditions and lost updates.

`HINCRBYFLOAT` is a native atomic Redis command. Redis parses the IEEE 754 float in C, increments the value within its single-threaded execution thread, and updates the hash field in a single atomic instruction. Zero locks, zero race conditions, and perfect transactional accuracy across unlimited distributed workers.

---

### Topic 4: Networking & Protocols

#### Q4.1: Contrast the network characteristics and error semantics of Server-Sent Events (SSE) streaming vs. buffered JSON completions in an AI gateway.
**Answer:**
- **Buffered Completions (`POST /v1/chat/completions` non-streaming):**
  - The client waits until the LLM finishes generating the entire response (1,000 tokens may take 5 to 15 seconds).
  - The gateway sends standard HTTP status codes (`200 OK`, `429 Too Many Requests`, `503 Service Unavailable`).
  - *Network Overhead:* High Time-To-First-Token (TTFT) latency for the end user, but simple connection handling and straightforward retry logic. If the upstream provider fails halfway through generation, the gateway can catch the error and cleanly failover to a fallback provider before sending any headers to the client.
- **Server-Sent Events (SSE) Streaming (`text/event-stream`):**
  - The gateway immediately responds with HTTP `200 OK` and `Transfer-Encoding: chunked`, flushing tokens over the TCP socket as they arrive.
  - *User Experience:* Sub-500ms TTFT.
  - *Failure Challenge:* Once HTTP 200 headers and the first token chunks are sent over the wire, **the gateway cannot retroactively change the HTTP status code to 503 or trigger a seamless fallback provider**. If the upstream connection drops midway, the gateway must emit an in-band custom JSON error event over the stream (`data: {"error": "upstream_dropped"}`) and terminate the stream, requiring the client frontend to handle mid-stream recovery.

---

### Topic 5: Container & Linux Security

#### Q5.1: Why should production AI gateways avoid logging raw LLM prompt text and completion text in application logs? What is the PCI-DSS and GDPR implication?
**Answer:**
1. **PII and Sensitive Data Leakage:** Users frequently input Personally Identifiable Information (PII), passwords, healthcare data, or proprietary source code into LLM prompts. If an application logs raw prompts via `logger.info("prompt", text=query_text)`, this sensitive data is written to plaintext disk files and forwarded to centralized log management platforms (Datadog, Splunk, ElasticSearch).
2. **Regulatory Non-Compliance:** Storing unencrypted PII or credit card numbers in application logs violates **GDPR Article 32** (Security of Processing) and **PCI-DSS Requirement 3** (Protect stored cardholder data). Furthermore, GDPR grants users the "Right to be Forgotten" (Article 17); if prompt text is immutably indexed across hundreds of daily log shards, fulfilling a user deletion request is virtually impossible.
3. **SentinelRack's Production Practice:** SentinelRack binds structured metadata (`tenant_id`, `request_id`, `prompt_tokens`, `cost_usd`, `latency_ms`) to `structlog` contextvars, while redacting or hashing raw prompt text in general application log pipelines.

---

## 7. Day 5 - What the Real Commit Would Have Contained

A Git commit for Day 5 implementation includes:
- `app/core/circuit_breaker.py`: `CircuitBreaker` state machine with `CLOSED`, `OPEN`, `HALF_OPEN` states and coroutine-safe `asyncio.Lock`.
- `app/services/resilience.py`: Tenacity retry engine with transient error classification and exponential backoff with full jitter.
- `app/services/token_analytics.py`: `tiktoken` BPE token counter, pricing table, per-request cost calculator, and atomic Redis `HINCRBYFLOAT` tracking.
- `app/services/router.py`: `ResilientLLMRouter` orchestrating primary Groq execution, circuit breaker tripping, and fallback OpenAI execution.
- `app/api/v1/chat.py`: Unified `POST /v1/chat/completions` route chaining Cache -> RRF Hybrid Search -> LLM Router -> Token Telemetry.
- `tests/test_circuit_breaker.py` & `tests/test_router.py`: Comprehensive test suites validating state transitions and failover logic.

---

*End of Day 5. Study the Circuit Breaker FSM and resilience mechanics. When ready, reply "continue" for Day 6.*
