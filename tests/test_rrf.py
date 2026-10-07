"""Unit tests for the Reciprocal Rank Fusion (RRF) algorithm."""

import uuid

import pytest

from app.models.document import DocumentChunk
from app.repositories.search import DenseCandidate, SparseCandidate
from app.services.rrf import (
    fuse_hybrid_candidates,
    reciprocal_rank_fusion,
)


def test_reciprocal_rank_fusion_generic_formula() -> None:
    """Verifies RRF calculation against known theoretical values."""
    # List 1: ["doc_a", "doc_b"] (rank 1, rank 2)
    # List 2: ["doc_b", "doc_c"] (rank 1, rank 2)
    # k = 60
    # doc_a: 1 / (60 + 1) = 1/61 ~= 0.016393
    # doc_b: 1 / (60 + 2) + 1 / (60 + 1) = 1/62 + 1/61 ~= 0.016129 + 0.016393 = 0.032522
    # doc_c: 1 / (60 + 2) = 1/62 ~= 0.016129
    # Expected order: doc_b, doc_a, doc_c
    list1 = ["doc_a", "doc_b"]
    list2 = ["doc_b", "doc_c"]

    fused = reciprocal_rank_fusion([list1, list2], k=60)

    assert len(fused) == 3
    assert fused[0][0] == "doc_b"
    assert pytest.approx(fused[0][1], rel=1e-4) == (1 / 61 + 1 / 62)
    assert fused[1][0] == "doc_a"
    assert pytest.approx(fused[1][1], rel=1e-4) == (1 / 61)
    assert fused[2][0] == "doc_c"
    assert pytest.approx(fused[2][1], rel=1e-4) == (1 / 62)


def test_reciprocal_rank_fusion_invalid_k() -> None:
    """Ensures invalid smoothing constant raises ValueError."""
    with pytest.raises(ValueError, match="RRF smoothing constant k must be >= 1"):
        reciprocal_rank_fusion([["a"]], k=0)


def test_fuse_hybrid_candidates_overlap_and_metadata() -> None:
    """Tests hybrid fusion with DocumentChunk candidates."""
    id_1 = uuid.uuid4()
    id_2 = uuid.uuid4()
    id_3 = uuid.uuid4()

    chunk_1 = DocumentChunk(
        id=id_1,
        tenant_id="tenant_alpha",
        document_name="doc1.pdf",
        chunk_text="Vector retrieval systems",
        metadata_json={"page": 1},
        embedding=[0.0] * 1536,
    )
    chunk_2 = DocumentChunk(
        id=id_2,
        tenant_id="tenant_alpha",
        document_name="doc2.pdf",
        chunk_text="Full-text search using BM25",
        metadata_json={"page": 2},
        embedding=[0.0] * 1536,
    )
    chunk_3 = DocumentChunk(
        id=id_3,
        tenant_id="tenant_alpha",
        document_name="doc3.pdf",
        chunk_text="Hybrid reciprocal rank fusion",
        metadata_json={"page": 3},
        embedding=[0.0] * 1536,
    )

    # Dense: chunk_1 (rank 1), chunk_2 (rank 2)
    dense = [
        DenseCandidate(chunk=chunk_1, distance=0.1, similarity_score=0.9, rank=1),
        DenseCandidate(chunk=chunk_2, distance=0.2, similarity_score=0.8, rank=2),
    ]

    # Sparse: chunk_2 (rank 1), chunk_3 (rank 2)
    sparse = [
        SparseCandidate(chunk=chunk_2, lexical_score=0.45, rank=1),
        SparseCandidate(chunk=chunk_3, lexical_score=0.30, rank=2),
    ]

    fused = fuse_hybrid_candidates(dense, sparse, k=60, top_k=2)

    # chunk_2 appears in both dense (rank 2) and sparse (rank 1) -> highest RRF score
    assert len(fused) == 2
    assert fused[0].chunk.id == id_2
    assert fused[0].dense_rank == 2
    assert fused[0].dense_score == 0.8
    assert fused[0].sparse_rank == 1
    assert fused[0].sparse_score == 0.45
    assert pytest.approx(fused[0].fused_score, rel=1e-4) == (1 / 62 + 1 / 61)

    # Second result should be chunk_1 (rank 1 in dense: 1/61 > rank 2 in sparse: 1/62)
    assert fused[1].chunk.id == id_1
    assert fused[1].dense_rank == 1
    assert fused[1].sparse_rank is None
    assert fused[1].sparse_score is None


def test_fuse_hybrid_empty_inputs() -> None:
    """Verifies graceful handling of empty candidate lists."""
    fused = fuse_hybrid_candidates([], [])
    assert fused == []
