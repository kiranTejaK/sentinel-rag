"""Hybrid Search API Routes.

Exposes endpoints for combined dense semantic vector retrieval and sparse full-text lexical
search, merged and re-ranked using Reciprocal Rank Fusion (RRF).
"""

import time
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import logger
from app.db.session import get_async_session
from app.repositories.search import SearchRepository
from app.schemas.search import (
    HybridSearchRequest,
    HybridSearchResponse,
    HybridSearchResultItem,
)
from app.services.rrf import fuse_hybrid_candidates

router = APIRouter(prefix="/search", tags=["Hybrid Search"])


@router.post(
    "/hybrid",
    response_model=HybridSearchResponse,
    status_code=status.HTTP_200_OK,
    summary="Hybrid Vector & Full-Text Search with RRF Fusion",
    description=(
        "Executes a dual-retrieval pipeline against PostgreSQL 16: "
        "1. Dense vector candidate retrieval using pgvector cosine distance (<=>) and HNSW. "
        "2. Sparse lexical candidate retrieval using websearch_to_tsquery and ts_rank_cd over GIN. "
        "Combines and re-ranks top candidates using Reciprocal Rank Fusion (RRF, k=60)."
    ),
)
async def hybrid_search(
    payload: HybridSearchRequest,
    session: Annotated[AsyncSession, Depends(get_async_session)],
) -> HybridSearchResponse:
    """Executes hybrid retrieval combining dense vector similarity and sparse lexical search."""
    start_time = time.perf_counter()

    repo = SearchRepository(session)
    dense_candidates, sparse_candidates = await repo.search_hybrid_candidates(
        tenant_id=payload.tenant_id,
        query_text=payload.query,
        query_vector=payload.query_vector,
        dense_limit=payload.dense_limit,
        sparse_limit=payload.sparse_limit,
    )

    fused_results = fuse_hybrid_candidates(
        dense_candidates=dense_candidates,
        sparse_candidates=sparse_candidates,
        k=payload.rrf_k,
        top_k=payload.top_k,
    )

    duration_ms = round((time.perf_counter() - start_time) * 1000.0, 2)

    logger.info(
        "hybrid_search_executed",
        tenant_id=payload.tenant_id,
        dense_candidates_count=len(dense_candidates),
        sparse_candidates_count=len(sparse_candidates),
        returned_count=len(fused_results),
        duration_ms=duration_ms,
    )

    items = [
        HybridSearchResultItem(
            id=res.chunk.id,
            tenant_id=res.chunk.tenant_id,
            document_name=res.chunk.document_name,
            chunk_text=res.chunk.chunk_text,
            metadata_json=res.chunk.metadata_json,
            fused_score=round(res.fused_score, 6),
            dense_score=round(res.dense_score, 6) if res.dense_score is not None else None,
            dense_rank=res.dense_rank,
            sparse_score=round(res.sparse_score, 6) if res.sparse_score is not None else None,
            sparse_rank=res.sparse_rank,
        )
        for res in fused_results
    ]

    return HybridSearchResponse(
        status="success",
        query=payload.query,
        tenant_id=payload.tenant_id,
        total_results=len(items),
        results=items,
        duration_ms=duration_ms,
    )
