"""SentinelRAG Core Service Layer."""

from app.services.cache import (
    RedisSemanticCache,
    SemanticLookupResult,
    compute_cosine_similarity,
    deserialize_embedding,
    serialize_embedding,
)
from app.services.rrf import (
    FusedSearchResult,
    fuse_hybrid_candidates,
    reciprocal_rank_fusion,
)

__all__ = [
    "FusedSearchResult",
    "RedisSemanticCache",
    "SemanticLookupResult",
    "compute_cosine_similarity",
    "deserialize_embedding",
    "fuse_hybrid_candidates",
    "reciprocal_rank_fusion",
    "serialize_embedding",
]

