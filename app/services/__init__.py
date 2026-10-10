"""SentinelRAG Core Service Layer."""

from app.services.cache import (
    RedisSemanticCache,
    SemanticLookupResult,
    compute_cosine_similarity,
    deserialize_embedding,
    serialize_embedding,
)
from app.services.resilience import (
    create_resilient_retryer,
    execute_with_resilience,
    is_transient_error,
)
from app.services.router import ResilientLLMRouter, RouterExecutionResult
from app.services.rrf import (
    FusedSearchResult,
    fuse_hybrid_candidates,
    reciprocal_rank_fusion,
)
from app.services.token_analytics import (
    TokenAnalyticsTracker,
    calculate_request_cost,
    count_messages_tokens,
    count_text_tokens,
)

__all__ = [
    "FusedSearchResult",
    "RedisSemanticCache",
    "ResilientLLMRouter",
    "RouterExecutionResult",
    "SemanticLookupResult",
    "TokenAnalyticsTracker",
    "calculate_request_cost",
    "compute_cosine_similarity",
    "count_messages_tokens",
    "count_text_tokens",
    "create_resilient_retryer",
    "deserialize_embedding",
    "execute_with_resilience",
    "fuse_hybrid_candidates",
    "is_transient_error",
    "reciprocal_rank_fusion",
    "serialize_embedding",
]

