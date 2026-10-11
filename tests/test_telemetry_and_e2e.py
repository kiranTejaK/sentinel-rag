"""End-to-end integration and telemetry test suite for SentinelRAG Gateway (Day 6).

Covers:
1. Full Cache Miss -> Hybrid Search -> LLM Generation -> Cache Write cycle.
2. Cache Hit returning correct cached payload in < 25ms.
3. Rate Limiter triggering HTTP 429 when limits are breached.
4. Mocked Primary Provider Failure triggering Circuit Breaker and Fallback routing.
5. Prometheus /metrics endpoint exposition with latency histogram, cache counters, and token counters.
"""

import time
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from litellm.exceptions import ServiceUnavailableError
from litellm.types.utils import Choices, Message, ModelResponse, Usage

from app.core.circuit_breaker import CircuitBreaker, CircuitState
from app.core.redis import get_redis_client
from app.db.session import get_async_session
from app.main import app
from app.models.document import DocumentChunk
from app.repositories.search import DenseCandidate, SparseCandidate
from app.services.cache import SemanticLookupResult
from app.services.router import ResilientLLMRouter, get_llm_router


def _build_mock_llm_response(
    content: str, model: str = "groq/llama-3.3-70b-versatile"
) -> ModelResponse:
    """Helper to mock LiteLLM ModelResponse."""
    return ModelResponse(
        id=f"resp_{uuid.uuid4().hex[:12]}",
        choices=[Choices(message=Message(content=content, role="assistant"))],
        created=int(time.time()),
        model=model,
        usage=Usage(prompt_tokens=42, completion_tokens=35, total_tokens=77),
    )


def _setup_mock_redis() -> AsyncMock:
    from unittest.mock import MagicMock

    mock_redis = AsyncMock()
    pipe = MagicMock()
    pipe.execute = AsyncMock(return_value=[0, 0, 0, 0, 0, 0, 0])
    mock_redis.pipeline = lambda: pipe
    mock_redis.get = AsyncMock(return_value="0.0")
    return mock_redis


@pytest.mark.asyncio
async def test_full_cache_miss_hybrid_search_llm_cycle() -> None:
    """1. Test full Cache Miss -> Hybrid Search -> LLM Generation -> Cache Write cycle."""
    mock_redis = _setup_mock_redis()
    mock_session = AsyncMock()

    app.dependency_overrides[get_redis_client] = lambda: mock_redis
    app.dependency_overrides[get_async_session] = lambda: mock_session

    cache_miss = SemanticLookupResult(
        status="CACHE_MISS",
        similarity=0.45,
        distance=0.55,
        threshold=0.08,
        cached_query=None,
        cached_response=None,
        matched_key=None,
        lookup_latency_ms=1.8,
    )

    test_chunk = DocumentChunk(
        id=uuid.uuid4(),
        tenant_id="tenant_telemetry",
        document_name="architecture_spec.pdf",
        chunk_text="SentinelRAG utilizes reciprocal rank fusion for dense and sparse retrieval.",
        metadata_json={},
        embedding=[0.02] * 1536,
    )

    dense_candidates = [DenseCandidate(chunk=test_chunk, distance=0.05, similarity_score=0.95, rank=1)]
    sparse_candidates = [SparseCandidate(chunk=test_chunk, lexical_score=0.88, rank=1)]

    generated_text = "SentinelRAG successfully combined pgvector and tsvector via RRF."
    mock_resp = _build_mock_llm_response(generated_text, model="groq/llama-3.3-70b-versatile")

    try:
        with patch(
            "app.api.v1.chat.RedisSemanticCache.get_or_lookup",
            new_callable=AsyncMock,
        ) as mock_lookup, patch(
            "app.api.v1.chat.SearchRepository.search_hybrid_candidates",
            new_callable=AsyncMock,
        ) as mock_search, patch(
            "litellm.acompletion",
            new_callable=AsyncMock,
        ) as mock_llm, patch(
            "app.api.v1.chat.RedisSemanticCache.set",
            new_callable=AsyncMock,
        ) as mock_cache_set:
            mock_lookup.return_value = cache_miss
            mock_search.return_value = (dense_candidates, sparse_candidates)
            mock_llm.return_value = mock_resp

            payload = {
                "messages": [{"role": "user", "content": "Explain SentinelRAG hybrid retrieval."}],
                "tenant_id": "tenant_telemetry",
                "model": "groq/llama-3.3-70b-versatile",
                "use_cache": True,
                "use_retrieval": True,
                "query_vector": [0.02] * 1536,
                "top_k_chunks": 3,
            }

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post("/api/v1/chat/completions", json=payload)

            assert response.status_code == 200
            data = response.json()

            # Verify complete pipeline execution
            assert data["telemetry"]["cache_hit"] is False
            assert data["telemetry"]["provider"] == "groq"
            assert data["telemetry"]["retrieval_chunks_used"] >= 1
            assert data["choices"][0]["message"]["content"] == generated_text
            assert mock_lookup.call_count == 1
            assert mock_search.call_count == 1
            assert mock_llm.call_count == 1
            assert mock_cache_set.call_count == 1  # Verified Cache Write
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_cache_hit_returns_payload_under_25ms() -> None:
    """2. Test Cache Hit returning correct cached payload in < 25ms."""
    mock_redis = _setup_mock_redis()
    mock_session = AsyncMock()

    app.dependency_overrides[get_redis_client] = lambda: mock_redis
    app.dependency_overrides[get_async_session] = lambda: mock_session

    cached_content = "Instant ultra-fast answer retrieved directly from Redis cache."
    cached_lookup = SemanticLookupResult(
        status="CACHE_HIT",
        similarity=0.99,
        distance=0.01,
        threshold=0.08,
        cached_query="What is the speed of semantic caching?",
        cached_response=cached_content,
        matched_key="cache:tenant_speed:hash_001",
        lookup_latency_ms=0.8,
    )

    try:
        with patch(
            "app.api.v1.chat.RedisSemanticCache.get_or_lookup",
            new_callable=AsyncMock,
        ) as mock_lookup, patch(
            "litellm.acompletion",
            new_callable=AsyncMock,
        ) as mock_llm:
            mock_lookup.return_value = cached_lookup

            payload = {
                "messages": [{"role": "user", "content": "What is the speed of semantic caching?"}],
                "tenant_id": "tenant_speed",
                "use_cache": True,
                "use_retrieval": False,
            }

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                t_start = time.perf_counter()
                response = await client.post("/api/v1/chat/completions", json=payload)
                elapsed_ms = (time.perf_counter() - t_start) * 1000.0

            assert response.status_code == 200
            data = response.json()

            assert data["telemetry"]["cache_hit"] is True
            assert data["telemetry"]["provider"] == "semantic_cache"
            assert data["choices"][0]["message"]["content"] == cached_content
            assert mock_llm.call_count == 0  # Zero LLM invocations

            # In-memory execution latency is sub-25ms
            assert data["telemetry"]["latency_ms"] < 25.0
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_rate_limiter_triggers_http_429() -> None:
    """3. Test Rate Limiter triggering HTTP 429 when limits are breached."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Mock the underlying limiter to simulate token bucket exhaustion
        with patch(
            "app.core.rate_limiter.TokenBucketLimiter.check",
            new_callable=AsyncMock,
        ) as mock_check:
            from app.core.rate_limiter import RateLimitResult

            mock_check.return_value = RateLimitResult(
                allowed=False,
                remaining=0,
                retry_after=5,
                limit=10,
            )

            headers = {"X-API-Key": "test-exhausted-key"}
            response = await client.post(
                "/api/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "Hello"}]},
                headers=headers,
            )

            assert response.status_code == 429
            assert response.headers.get("retry-after") == "5"
            assert response.headers.get("x-ratelimit-remaining") == "0"
            problem_data = response.json()
            assert problem_data["status"] == 429
            assert "Rate limit quota exceeded" in problem_data["detail"]


@pytest.mark.asyncio
async def test_mocked_primary_failure_triggers_circuit_breaker_and_fallback() -> None:
    """4. Test Mocked Primary Provider Failure triggering Circuit Breaker and Fallback routing."""
    mock_redis = _setup_mock_redis()
    mock_session = AsyncMock()

    app.dependency_overrides[get_redis_client] = lambda: mock_redis
    app.dependency_overrides[get_async_session] = lambda: mock_session

    cache_miss = SemanticLookupResult(
        status="CACHE_MISS",
        similarity=0.3,
        distance=0.7,
        threshold=0.08,
        cached_query=None,
        cached_response=None,
        matched_key=None,
        lookup_latency_ms=1.1,
    )

    fallback_content = "OpenAI fallback response delivered seamlessly."
    mock_fallback_resp = _build_mock_llm_response(fallback_content, model="openai/gpt-4o-mini")

    # Custom circuit breaker with failure threshold of 1 for fast test assertion
    test_cb = CircuitBreaker(name="test_cb", failure_threshold=1, recovery_timeout=60.0)
    test_router = ResilientLLMRouter(
        redis_client=mock_redis,
        primary_model="groq/llama-3.3-70b-versatile",
        fallback_model="openai/gpt-4o-mini",
        circuit_breaker=test_cb,
    )

    app.dependency_overrides[get_llm_router] = lambda: test_router

    try:
        with patch(
            "app.api.v1.chat.RedisSemanticCache.get_or_lookup",
            new_callable=AsyncMock,
        ) as mock_lookup, patch(
            "litellm.acompletion",
            new_callable=AsyncMock,
        ) as mock_llm:
            mock_lookup.return_value = cache_miss

            # Primary attempts 3 retries (all 503), then 4th call is fallback (OpenAI)
            mock_llm.side_effect = [
                ServiceUnavailableError("Groq 503 Service Unavailable 1", model="groq/llama-3.3-70b-versatile", llm_provider="groq"),
                ServiceUnavailableError("Groq 503 Service Unavailable 2", model="groq/llama-3.3-70b-versatile", llm_provider="groq"),
                ServiceUnavailableError("Groq 503 Service Unavailable 3", model="groq/llama-3.3-70b-versatile", llm_provider="groq"),
                mock_fallback_resp,
            ]

            payload = {
                "messages": [{"role": "user", "content": "Explain circuit breakers during outages."}],
                "tenant_id": "tenant_resilience",
                "model": "groq/llama-3.3-70b-versatile",
                "use_cache": True,
                "use_retrieval": False,
            }

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post("/api/v1/chat/completions", json=payload)

            assert response.status_code == 200
            data = response.json()

            # Verify failover to secondary provider
            assert data["telemetry"]["fallback_triggered"] is True
            assert data["telemetry"]["provider"] == "openai"
            assert data["telemetry"]["model"] == "openai/gpt-4o-mini"
            assert data["choices"][0]["message"]["content"] == fallback_content
            # Circuit breaker tripped to OPEN
            assert test_cb.state == CircuitState.OPEN
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_prometheus_metrics_endpoint() -> None:
    """5. Verify Prometheus /metrics endpoint exposes histograms and counters."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Trigger an endpoint to ensure metrics are recorded
        await client.get("/healthz")

        # Fetch Prometheus metrics
        response = await client.get("/metrics")
        assert response.status_code == 200
        text = response.text

        # Verify presence of all required metric identifiers
        assert "http_request_duration_seconds" in text
        assert "cache_operations_total" in text
        assert "tokens_consumed_total" in text
        assert "cost_saved_usd_total" in text
