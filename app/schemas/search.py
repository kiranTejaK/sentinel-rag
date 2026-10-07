"""Pydantic v2 validation schemas for Hybrid Search endpoints."""

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class HybridSearchRequest(BaseModel):
    """Request payload for the hybrid search endpoint combining vector and full-text search."""

    query: str = Field(
        ...,
        min_length=1,
        description="Raw search query text for sparse lexical search",
        examples=["enterprise cloud security protocols"],
    )
    query_vector: list[float] = Field(
        ...,
        min_length=1536,
        max_length=1536,
        description="1536-dimensional query embedding vector for dense semantic search",
    )
    tenant_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Tenant identifier for multi-tenant data isolation",
        examples=["tenant_acme_corp"],
    )
    top_k: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Final number of re-ranked documents to return",
    )
    dense_limit: int = Field(
        default=50,
        ge=1,
        le=200,
        description="Maximum candidates retrieved from dense vector search",
    )
    sparse_limit: int = Field(
        default=50,
        ge=1,
        le=200,
        description="Maximum candidates retrieved from sparse full-text search",
    )
    rrf_k: int = Field(
        default=60,
        ge=1,
        le=1000,
        description="Smoothing constant k for Reciprocal Rank Fusion",
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "query": "cloud infrastructure compliance",
                "query_vector": [0.01] * 1536,
                "tenant_id": "tenant_acme_corp",
                "top_k": 5,
                "dense_limit": 50,
                "sparse_limit": 50,
                "rrf_k": 60,
            }
        }
    )


class HybridSearchResultItem(BaseModel):
    """Schema for an individual document chunk returned by hybrid search."""

    id: uuid.UUID = Field(..., description="Chunk unique UUID identifier")
    tenant_id: str = Field(..., description="Tenant identifier")
    document_name: str = Field(..., description="Source document name")
    chunk_text: str = Field(..., description="Text content of the retrieved chunk")
    metadata_json: dict[str, Any] = Field(..., description="Arbitrary JSONB metadata")
    fused_score: float = Field(..., description="Calculated Reciprocal Rank Fusion score")
    dense_score: float | None = Field(
        default=None,
        description="Cosine similarity score (1 - distance) from dense retrieval",
    )
    dense_rank: int | None = Field(
        default=None,
        description="1-based rank in dense retrieval candidate pool",
    )
    sparse_score: float | None = Field(
        default=None,
        description="PostgreSQL ts_rank_cd score from sparse retrieval",
    )
    sparse_rank: int | None = Field(
        default=None,
        description="1-based rank in sparse retrieval candidate pool",
    )

    model_config = ConfigDict(from_attributes=True)


class HybridSearchResponse(BaseModel):
    """Response payload returned by the hybrid search endpoint."""

    status: str = Field(default="success", description="Execution status")
    query: str = Field(..., description="Echo of original text query")
    tenant_id: str = Field(..., description="Tenant identifier queried")
    total_results: int = Field(..., description="Count of returned top-K results")
    results: list[HybridSearchResultItem] = Field(
        ...,
        description="Merged, deduplicated, and re-ranked document chunks",
    )
    duration_ms: float = Field(..., description="Server-side execution latency in milliseconds")
