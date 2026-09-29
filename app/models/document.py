"""DocumentChunk ORM Model.
Stores raw text chunks, JSONB metadata, 1536-dimensional embeddings,
and automatically generated TSVector search vectors for hybrid retrieval.
"""

from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import Computed, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class DocumentChunk(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """DocumentChunk entity representation in PostgreSQL 16 + pgvector.

    Attributes:
        id: Unique UUIDv4 identifier.
        tenant_id: Multi-tenant partitioning identifier (B-Tree indexed).
        document_name: Source document name or file path.
        chunk_text: Textual body of the chunk.
        metadata_json: Arbitrary structured metadata (GIN indexed JSONB).
        embedding: 1536-dimensional dense vector representation (HNSW indexed).
        search_vector: Stored generated tsvector column for sparse lexical BM25/FTS search.
        created_at: Timezone-aware UTC timestamp.
        updated_at: Timezone-aware UTC timestamp.
    """

    __tablename__ = "document_chunks"

    tenant_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
        doc="Tenant or organization identifier for multi-tenant data isolation",
    )
    document_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        doc="Human-readable name or file identifier of the source document",
    )
    chunk_text: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        doc="Extracted textual content of the chunk",
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
        doc="Arbitrary structured metadata (page number, author, source URL, tags)",
    )
    embedding: Mapped[list[float]] = mapped_column(
        Vector(1536),
        nullable=False,
        doc="1536-dimensional dense embedding vector",
    )
    search_vector: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', chunk_text)", persisted=True),
        nullable=True,
        doc="PostgreSQL generated column computed from chunk_text for full-text search",
    )

    __table_args__ = (
        Index("ix_document_chunks_metadata_json", "metadata_json", postgresql_using="gin"),
        Index("ix_document_chunks_search_vector", "search_vector", postgresql_using="gin"),
        Index(
            "ix_document_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<DocumentChunk(id={self.id}, tenant_id='{self.tenant_id}', "
            f"document='{self.document_name}', chunk_length={len(self.chunk_text)})>"
        )
