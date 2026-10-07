"""create hybrid search indexes for embedding and full-text search

Revision ID: 2026_10_07_0002
Revises: 2026_09_29_0001
Create Date: 2026-10-07 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2026_10_07_0002"
down_revision: str | None = "2026_09_29_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Functional GIN Index on to_tsvector('english', chunk_text)
    # Accelerates websearch_to_tsquery and ts_rank_cd full-text queries
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_document_chunks_chunk_text_tsv_gin
        ON document_chunks
        USING gin (to_tsvector('english', chunk_text));
        """
    )

    # 2. HNSW Index on embedding vector column
    # Distance metric: vector_cosine_ops (<=> cosine distance operator)
    # Parameters: m=16 (bidirectional links per node), ef_construction=64 (search depth at build time)
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_document_chunks_embedding_hnsw
        ON document_chunks
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64);
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_document_chunks_chunk_text_tsv_gin;")
    # Drop HNSW index only if explicitly created in this revision
    op.execute("DROP INDEX IF EXISTS ix_document_chunks_embedding_hnsw;")
