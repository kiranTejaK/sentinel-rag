"""Unit and integration tests for document chunk ingestion endpoints."""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.db.session import get_async_session
from app.main import app


@pytest.mark.asyncio
async def test_batch_ingest_documents_success() -> None:
    """Tests successful batch document ingestion with mocked database session."""
    mock_session = AsyncMock()
    # Mocking session.scalars to simulate returning list of UUIDs
    mock_id_1 = uuid.uuid4()
    mock_id_2 = uuid.uuid4()

    mock_scalars_result = MagicMock()
    mock_scalars_result.all.return_value = [mock_id_1, mock_id_2]
    mock_session.scalars.return_value = mock_scalars_result
    mock_session.commit = AsyncMock()

    async def override_get_async_session():
        yield mock_session

    app.dependency_overrides[get_async_session] = override_get_async_session

    try:
        payload = {
            "tenant_id": "tenant_test_org",
            "chunks": [
                {
                    "document_name": "quarterly_earnings.pdf",
                    "chunk_text": "Net income for Q2 grew 18% driven by enterprise cloud adoption.",
                    "metadata_json": {"page": 2, "section": "Financials"},
                    "embedding": [0.01] * 1536,
                },
                {
                    "document_name": "quarterly_earnings.pdf",
                    "chunk_text": "R&D investments centered around hybrid search and vector databases.",
                    "metadata_json": {"page": 3, "section": "Operations"},
                    "embedding": [0.02] * 1536,
                },
            ],
        }

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/api/v1/documents/batch", json=payload)
            assert response.status_code == 201
            data = response.json()
            assert data["status"] == "success"
            assert data["tenant_id"] == "tenant_test_org"
            assert data["inserted_count"] == 2
            assert len(data["chunk_ids"]) == 2
            assert str(mock_id_1) in data["chunk_ids"]
            assert str(mock_id_2) in data["chunk_ids"]
            assert "duration_ms" in data
            assert mock_session.commit.called
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_batch_ingest_validation_failure_empty_chunks() -> None:
    """Verifies that an empty chunks list fails schema validation with 422."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/documents/batch",
            json={"tenant_id": "tenant_xyz", "chunks": []},
        )
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_batch_ingest_validation_failure_invalid_embedding() -> None:
    """Verifies that invalid embedding dimension fails schema validation."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/documents/batch",
            json={
                "tenant_id": "tenant_xyz",
                "chunks": [
                    {
                        "document_name": "test.txt",
                        "chunk_text": "sample text",
                        "metadata_json": {},
                        "embedding": [0.1, 0.2],  # only 2 dimensions instead of 1536
                    }
                ],
            },
        )
        assert response.status_code == 422
