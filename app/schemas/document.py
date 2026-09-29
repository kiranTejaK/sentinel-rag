"""Pydantic v2 validation schemas for Document and Chunk ingestion."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ChunkCreate(BaseModel):
    """Schema for a single document chunk within an ingestion batch."""

    document_name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Source document filename or identifier",
        examples=["q3_financial_report.pdf"],
    )
    chunk_text: str = Field(
        ...,
        min_length=1,
        description="Extracted textual content of the chunk",
        examples=["Operating revenue for Q3 increased by 14% year-over-year..."],
    )
    metadata_json: dict[str, Any] = Field(
        default_factory=dict,
        description="Structured key-value metadata attributes (page, author, tags)",
        examples=[{"page": 4, "section": "Executive Summary", "classification": "confidential"}],
    )
    embedding: list[float] = Field(
        ...,
        min_length=1536,
        max_length=1536,
        description="Dense vector embedding of chunk_text (1536 dimensions for text-embedding-3-small)",
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "document_name": "architecture_rfc.md",
                "chunk_text": "SentinelRAG utilizes pgvector HNSW indexing with cosine distance.",
                "metadata_json": {"author": "Database Architect", "version": "1.0"},
                "embedding": [0.0012] * 1536,
            }
        }
    )


class BatchIngestRequest(BaseModel):
    """Schema for the batch document ingestion endpoint."""

    tenant_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Tenant identifier for multi-tenant data segregation",
        examples=["tenant_acme_corp"],
    )
    chunks: list[ChunkCreate] = Field(
        ...,
        min_length=1,
        max_length=1000,
        description="List of document chunks to insert in a single atomic transaction",
    )


class BatchIngestResponse(BaseModel):
    """Response payload returned upon successful batch ingestion."""

    status: str = Field(default="success", description="Ingestion operational status")
    tenant_id: str = Field(..., description="Tenant identifier")
    inserted_count: int = Field(..., description="Total number of chunks successfully inserted")
    chunk_ids: list[uuid.UUID] = Field(..., description="List of generated UUIDv4 primary keys")
    duration_ms: float = Field(..., description="Server-side ingestion latency in milliseconds")


class ChunkResponse(BaseModel):
    """Detailed response schema for an individual document chunk."""

    id: uuid.UUID
    tenant_id: str
    document_name: str
    chunk_text: str
    metadata_json: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
