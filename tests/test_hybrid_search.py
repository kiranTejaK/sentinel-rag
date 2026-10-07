"""Unit and integration tests for the Hybrid Search endpoint."""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.db.session import get_async_session
from app.main import app
from app.models.document import DocumentChunk
from app.repositories.search import DenseCandidate, SparseCandidate


@pytest.mark.asyncio
async def test_hybrid_search_endpoint_success() -> None:
    """Tests successful execution of POST /api/v1/search/hybrid."""
    mock_session = AsyncMock()

    async def override_get_async_session():
        yield mock_session

    app.dependency_overrides[get_async_session] = override_get_async_session

    chunk_id_1 = uuid.uuid4()
    chunk_id_2 = uuid.uuid4()

    mock_chunk_1 = DocumentChunk(
        id=chunk_id_1,
        tenant_id="tenant_finance",
        document_name="sec_filing_10k.pdf",
        chunk_text="EBITDA expanded 14% year-over-year.",
        metadata_json={"filing_year": 2026},
        embedding=[0.05] * 1536,
    )
    mock_chunk_2 = DocumentChunk(
        id=chunk_id_2,
        tenant_id="tenant_finance",
        document_name="risk_factors.pdf",
        chunk_text="Operational resilience during cloud outages.",
        metadata_json={"classification": "internal"},
        embedding=[0.02] * 1536,
    )

    dense_candidates = [
        DenseCandidate(chunk=mock_chunk_1, distance=0.15, similarity_score=0.85, rank=1),
        DenseCandidate(chunk=mock_chunk_2, distance=0.25, similarity_score=0.75, rank=2),
    ]
    sparse_candidates = [
        SparseCandidate(chunk=mock_chunk_2, lexical_score=0.92, rank=1),
        SparseCandidate(chunk=mock_chunk_1, lexical_score=0.45, rank=2),
    ]

    try:
        with patch(
            "app.api.v1.search.SearchRepository.search_hybrid_candidates",
            new_callable=AsyncMock,
        ) as mock_search:
            mock_search.return_value = (dense_candidates, sparse_candidates)

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                payload = {
                    "query": "financial resilience EBITDA",
                    "query_vector": [0.01] * 1536,
                    "tenant_id": "tenant_finance",
                    "top_k": 5,
                    "dense_limit": 50,
                    "sparse_limit": 50,
                    "rrf_k": 60,
                }
                response = await client.post("/api/v1/search/hybrid", json=payload)

                assert response.status_code == 200
                data = response.json()
                assert data["status"] == "success"
                assert data["query"] == "financial resilience EBITDA"
                assert data["tenant_id"] == "tenant_finance"
                assert data["total_results"] == 2
                assert len(data["results"]) == 2

                # Both chunks were in dense and sparse at ranks 1 & 2:
                # Chunk 1: 1/(60+1) + 1/(60+2) = 1/61 + 1/62
                # Chunk 2: 1/(60+2) + 1/(60+1) = 1/62 + 1/61
                # Both have equal fused scores; chunk 1 has higher dense_score (0.85 vs 0.75)
                first_result = data["results"][0]
                assert first_result["dense_score"] is not None
                assert first_result["sparse_score"] is not None
                assert first_result["fused_score"] > 0.0
                assert "duration_ms" in data
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_hybrid_search_validation_vector_dimension() -> None:
    """Verifies that vector dimension not equal to 1536 yields HTTP 422."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        payload = {
            "query": "vector dimension test",
            "query_vector": [0.1, 0.2],  # only 2 dimensions
            "tenant_id": "tenant_alpha",
        }
        response = await client.post("/api/v1/search/hybrid", json=payload)
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_hybrid_search_validation_empty_query() -> None:
    """Verifies that an empty query text yields HTTP 422."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        payload = {
            "query": "",
            "query_vector": [0.01] * 1536,
            "tenant_id": "tenant_alpha",
        }
        response = await client.post("/api/v1/search/hybrid", json=payload)
        assert response.status_code == 422
