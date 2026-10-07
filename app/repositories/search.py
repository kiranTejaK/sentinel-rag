"""Asynchronous Hybrid Search Repository.

Executes dense vector similarity queries using the pgvector <=> cosine distance operator
and sparse full-text lexical queries using PostgreSQL websearch_to_tsquery and ts_rank_cd.
"""

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import DocumentChunk


@dataclass(slots=True)
class DenseCandidate:
    """Represents a candidate retrieved via dense vector similarity."""

    chunk: DocumentChunk
    distance: float
    similarity_score: float
    rank: int


@dataclass(slots=True)
class SparseCandidate:
    """Represents a candidate retrieved via sparse full-text search."""

    chunk: DocumentChunk
    lexical_score: float
    rank: int


class SearchRepository:
    """Repository handling dense semantic and sparse lexical vector retrieval."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def search_dense(
        self,
        tenant_id: str,
        query_vector: list[float],
        limit: int = 50,
    ) -> list[DenseCandidate]:
        """Retrieves top-N dense vector candidates using pgvector cosine distance (<=>).

        Args:
            tenant_id: Multi-tenant partitioning identifier.
            query_vector: 1536-dimensional query embedding vector.
            limit: Maximum candidate pool size (default 50).

        Returns:
            List of DenseCandidate items ranked by ascending cosine distance.
        """
        # DocumentChunk.embedding.cosine_distance generates `embedding <=> :val`
        distance_expr = DocumentChunk.embedding.cosine_distance(query_vector).label("distance")

        stmt = (
            select(DocumentChunk, distance_expr)
            .where(DocumentChunk.tenant_id == tenant_id)
            .order_by(distance_expr.asc())
            .limit(limit)
        )

        result = await self._session.execute(stmt)
        rows: list[tuple[DocumentChunk, float]] = result.all()  # type: ignore[assignment]

        candidates: list[DenseCandidate] = []
        for rank_idx, (chunk, dist) in enumerate(rows, start=1):
            # Cosine distance d in [0, 2]; similarity = max(0.0, 1.0 - d)
            similarity = max(0.0, 1.0 - float(dist))
            candidates.append(
                DenseCandidate(
                    chunk=chunk,
                    distance=float(dist),
                    similarity_score=similarity,
                    rank=rank_idx,
                )
            )

        return candidates

    async def search_sparse(
        self,
        tenant_id: str,
        query_text: str,
        limit: int = 50,
    ) -> list[SparseCandidate]:
        """Retrieves top-N sparse candidates using websearch_to_tsquery and ts_rank_cd.

        Args:
            tenant_id: Multi-tenant partitioning identifier.
            query_text: Raw user query string.
            limit: Maximum candidate pool size (default 50).

        Returns:
            List of SparseCandidate items ranked by descending ts_rank_cd score.
        """
        sanitized_query = query_text.strip()
        if not sanitized_query:
            return []

        ts_query = func.websearch_to_tsquery("english", sanitized_query)
        rank_expr = func.ts_rank_cd(DocumentChunk.search_vector, ts_query).label("rank_score")

        stmt = (
            select(DocumentChunk, rank_expr)
            .where(
                DocumentChunk.tenant_id == tenant_id,
                DocumentChunk.search_vector.op("@@")(ts_query),
            )
            .order_by(rank_expr.desc())
            .limit(limit)
        )

        result = await self._session.execute(stmt)
        rows: list[tuple[DocumentChunk, float]] = result.all()  # type: ignore[assignment]

        candidates: list[SparseCandidate] = []
        for rank_idx, (chunk, rank_score) in enumerate(rows, start=1):
            candidates.append(
                SparseCandidate(
                    chunk=chunk,
                    lexical_score=float(rank_score),
                    rank=rank_idx,
                )
            )

        return candidates

    async def search_hybrid_candidates(
        self,
        tenant_id: str,
        query_text: str,
        query_vector: list[float],
        dense_limit: int = 50,
        sparse_limit: int = 50,
    ) -> tuple[list[DenseCandidate], list[SparseCandidate]]:
        """Retrieves candidate pools sequentially on the session to avoid socket collision."""
        dense_candidates = await self.search_dense(
            tenant_id=tenant_id,
            query_vector=query_vector,
            limit=dense_limit,
        )
        sparse_candidates = await self.search_sparse(
            tenant_id=tenant_id,
            query_text=query_text,
            limit=sparse_limit,
        )
        return dense_candidates, sparse_candidates
