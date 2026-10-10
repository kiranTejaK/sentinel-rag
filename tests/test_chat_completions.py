"""Integration and unit tests for unified POST /api/v1/chat/completions endpoint."""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from litellm.types.utils import Choices, Message, ModelResponse, Usage

from app.core.redis import get_redis_client
from app.db.session import get_async_session
from app.main import app
from app.models.document import DocumentChunk
from app.repositories.search import DenseCandidate, SparseCandidate
from app.services.cache import SemanticLookupResult


def _mock_llm_response(content: str, model: str = "groq/llama-3.3-70b-versatile") -> ModelResponse:
    return ModelResponse(
        id="resp_456",
        choices=[Choices(message=Message(content=content, role="assistant"))],
        created=1700000000,
        model=model,
        usage=Usage(prompt_tokens=40, completion_tokens=25, total_tokens=65),
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
async def test_chat_completions_cache_hit_bypasses_llm() -> None:
    """Verifies that a semantic cache hit returns cached response with dollars saved and zero LLM calls."""
    mock_redis = _setup_mock_redis()
    mock_session = AsyncMock()

    app.dependency_overrides[get_redis_client] = lambda: mock_redis
    app.dependency_overrides[get_async_session] = lambda: mock_session

    cached_lookup = SemanticLookupResult(
        status="CACHE_HIT",
        similarity=0.98,
        distance=0.02,
        threshold=0.08,
        cached_query="What is SentinelRAG?",
        cached_response="SentinelRAG is an enterprise AI gateway with semantic caching.",
        matched_key="cache:tenant_a:abc123hash",
        lookup_latency_ms=1.2,
    )

    try:
        with patch(
            "app.api.v1.chat.RedisSemanticCache.get_or_lookup",
            new_callable=AsyncMock,
        ) as mock_get_or_lookup, patch("litellm.acompletion", new_callable=AsyncMock) as mock_llm:
            mock_get_or_lookup.return_value = cached_lookup

            payload = {
                "messages": [{"role": "user", "content": "What is SentinelRAG?"}],
                "tenant_id": "tenant_a",
                "use_cache": True,
                "use_retrieval": False,
            }

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post("/api/v1/chat/completions", json=payload)

            assert response.status_code == 200
            data = response.json()

            assert data["telemetry"]["cache_hit"] is True
            assert data["telemetry"]["provider"] == "semantic_cache"
            assert data["telemetry"]["cost_usd"] == 0.0
            assert data["telemetry"]["dollars_saved"] > 0.0
            assert data["choices"][0]["message"]["content"] == cached_lookup.cached_response
            assert mock_llm.call_count == 0  # LLM never called!

    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_chat_completions_cache_miss_routes_to_llm() -> None:
    """Verifies that a cache miss forwards to LLM and returns complete response with usage and cost."""
    mock_redis = _setup_mock_redis()
    mock_session = AsyncMock()

    app.dependency_overrides[get_redis_client] = lambda: mock_redis
    app.dependency_overrides[get_async_session] = lambda: mock_session

    cache_miss = SemanticLookupResult(
        status="CACHE_MISS",
        similarity=0.60,
        distance=0.40,
        threshold=0.08,
        cached_query=None,
        cached_response=None,
        matched_key=None,
        lookup_latency_ms=2.1,
    )

    mock_resp = _mock_llm_response("Groq generated response about circuit breakers.")

    try:
        with patch(
            "app.api.v1.chat.RedisSemanticCache.get_or_lookup",
            new_callable=AsyncMock,
        ) as mock_lookup, patch(
            "app.api.v1.chat.RedisSemanticCache.set",
            new_callable=AsyncMock,
        ) as mock_set, patch(
            "litellm.acompletion",
            new_callable=AsyncMock,
        ) as mock_llm:
            mock_lookup.return_value = cache_miss
            mock_llm.return_value = mock_resp

            payload = {
                "messages": [{"role": "user", "content": "Tell me about circuit breakers."}],
                "tenant_id": "tenant_b",
                "use_cache": True,
                "use_retrieval": False,
                "query_vector": [0.05] * 1536,
            }

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post("/api/v1/chat/completions", json=payload)

            assert response.status_code == 200
            data = response.json()

            assert data["telemetry"]["cache_hit"] is False
            assert data["telemetry"]["provider"] == "groq"
            assert data["telemetry"]["cost_usd"] > 0.0
            assert data["choices"][0]["message"]["content"] == "Groq generated response about circuit breakers."
            assert mock_set.call_count == 1  # Stored to cache

    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_chat_completions_with_hybrid_retrieval() -> None:
    """Verifies retrieval integration when use_retrieval is True."""
    mock_redis = _setup_mock_redis()
    mock_session = AsyncMock()

    app.dependency_overrides[get_redis_client] = lambda: mock_redis
    app.dependency_overrides[get_async_session] = lambda: mock_session

    cache_miss = SemanticLookupResult(
        status="CACHE_MISS",
        similarity=None,
        distance=None,
        threshold=0.08,
        cached_query=None,
        cached_response=None,
        matched_key=None,
        lookup_latency_ms=0.5,
    )

    chunk_1 = DocumentChunk(
        id=uuid.uuid4(),
        tenant_id="tenant_c",
        document_name="architecture_handbook.pdf",
        chunk_text="SentinelRAG uses pgvector HNSW indexing for dense retrieval.",
        metadata_json={},
        embedding=[0.01] * 1536,
    )

    dense_candidates = [DenseCandidate(chunk=chunk_1, distance=0.1, similarity_score=0.9, rank=1)]
    sparse_candidates = [SparseCandidate(chunk=chunk_1, lexical_score=0.95, rank=1)]
    mock_resp = _mock_llm_response("According to your architecture handbook, SentinelRAG uses pgvector.")

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
        ) as mock_llm:
            mock_lookup.return_value = cache_miss
            mock_search.return_value = (dense_candidates, sparse_candidates)
            mock_llm.return_value = mock_resp

            payload = {
                "messages": [{"role": "user", "content": "How does SentinelRAG retrieve documents?"}],
                "tenant_id": "tenant_c",
                "use_cache": True,
                "use_retrieval": True,
                "query_vector": [0.01] * 1536,
                "top_k_chunks": 2,
            }

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post("/api/v1/chat/completions", json=payload)

            assert response.status_code == 200
            data = response.json()

            assert data["telemetry"]["retrieval_chunks_used"] >= 1
            assert mock_search.call_count == 1
            assert mock_llm.call_count == 1

    finally:
        app.dependency_overrides.clear()
