"""Reciprocal Rank Fusion (RRF) Service Module.

Implements the Cormack, Clarke, and Büttcher (2009) Reciprocal Rank Fusion algorithm
for merging and re-ranking disparate search retrieval candidate lists.

Formula:
    RRF_Score(d) = sum_{m in M} (1 / (k + r_m(d)))

Where:
    - M is the set of retrieval modalities (e.g., dense vector search, sparse BM25/FTS).
    - r_m(d) is the 1-based rank of document d in modality m.
    - k is the ranking smoothing constant (default: 60), penalizing low-ranked items
      while preventing dominant outlier scores from skewing rank stability.
"""

from collections.abc import Hashable, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

from app.models.document import DocumentChunk
from app.repositories.search import DenseCandidate, SparseCandidate

T = TypeVar("T", bound=Hashable)

DEFAULT_RRF_K: int = 60


@dataclass(slots=True)
class FusedSearchResult:
    """Represents a fused, deduplicated search candidate with individual component scores."""

    chunk: DocumentChunk
    fused_score: float
    dense_score: float | None = None
    dense_rank: int | None = None
    sparse_score: float | None = None
    sparse_rank: int | None = None


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[T]],
    k: int = DEFAULT_RRF_K,
) -> list[tuple[T, float]]:
    """Generic Reciprocal Rank Fusion across arbitrary ranked lists of identifiers.

    Args:
        rankings: A list of rankings, where each inner list contains unique identifiers
                  ordered from best (rank 1) to worst.
        k: Smoothing constant (default 60). Higher values reduce the score disparity
           between top and middle ranked items.

    Returns:
        List of tuples (item_id, fused_score) sorted in descending order of fused_score.
    """
    if k < 1:
        raise ValueError(f"RRF smoothing constant k must be >= 1, got {k}")

    rrf_scores: dict[T, float] = {}

    for ranking in rankings:
        for rank_idx, item in enumerate(ranking, start=1):
            rrf_scores[item] = rrf_scores.get(item, 0.0) + (1.0 / (k + rank_idx))

    # Sort descending by fused score
    sorted_items = sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)
    return sorted_items


def fuse_hybrid_candidates(
    dense_candidates: list[DenseCandidate],
    sparse_candidates: list[SparseCandidate],
    k: int = DEFAULT_RRF_K,
    top_k: int | None = None,
) -> list[FusedSearchResult]:
    """Merges and deduplicates dense vector and sparse lexical candidates using RRF.

    Combines top-N candidates from dense vector search (pgvector HNSW) and sparse
    lexical search (PostgreSQL TSVector/ts_rank_cd) into a unified, rank-fused list.

    Args:
        dense_candidates: List of DenseCandidate objects (ranked 1..N).
        sparse_candidates: List of SparseCandidate objects (ranked 1..M).
        k: RRF smoothing constant (default 60).
        top_k: Optional limit on the final number of returned results.

    Returns:
        Sorted list of FusedSearchResult objects ordered by descending fused_score.
    """
    if k < 1:
        raise ValueError(f"RRF smoothing constant k must be >= 1, got {k}")

    # Map candidate by chunk UUID to preserve chunk entity and component scores
    chunk_map: dict[Any, DocumentChunk] = {}
    dense_info: dict[Any, tuple[int, float]] = {}  # chunk_id -> (rank, similarity_score)
    sparse_info: dict[Any, tuple[int, float]] = {}  # chunk_id -> (rank, lexical_score)
    fused_scores: dict[Any, float] = {}

    for cand in dense_candidates:
        c_id = cand.chunk.id
        chunk_map[c_id] = cand.chunk
        dense_info[c_id] = (cand.rank, cand.similarity_score)
        fused_scores[c_id] = fused_scores.get(c_id, 0.0) + (1.0 / (k + cand.rank))

    for cand in sparse_candidates:
        c_id = cand.chunk.id
        chunk_map[c_id] = cand.chunk
        sparse_info[c_id] = (cand.rank, cand.lexical_score)
        fused_scores[c_id] = fused_scores.get(c_id, 0.0) + (1.0 / (k + cand.rank))

    # Construct fused candidate objects
    fused_results: list[FusedSearchResult] = []
    for c_id, score in fused_scores.items():
        d_rank, d_score = dense_info.get(c_id, (None, None))
        s_rank, s_score = sparse_info.get(c_id, (None, None))

        fused_results.append(
            FusedSearchResult(
                chunk=chunk_map[c_id],
                fused_score=score,
                dense_score=d_score,
                dense_rank=d_rank,
                sparse_score=s_score,
                sparse_rank=s_rank,
            )
        )

    # Sort primarily by fused_score descending, secondarily by dense similarity / tie-break
    fused_results.sort(
        key=lambda r: (
            r.fused_score,
            r.dense_score if r.dense_score is not None else -1.0,
            r.sparse_score if r.sparse_score is not None else -1.0,
        ),
        reverse=True,
    )

    if top_k is not None:
        fused_results = fused_results[:top_k]

    return fused_results
