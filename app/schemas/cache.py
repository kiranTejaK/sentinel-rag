"""Pydantic v2 validation schemas for Semantic Cache endpoints."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class CacheEvaluateRequest(BaseModel):
    """Payload to evaluate query embedding against Redis Semantic Cache."""

    query: str = Field(
        ...,
        min_length=1,
        description="Query prompt text to evaluate or cache",
        examples=["What is the company policy on remote work?"],
    )
    query_vector: list[float] = Field(
        ...,
        min_length=1536,
        max_length=1536,
        description="1536-dimensional embedding vector of the query",
    )
    tenant_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Tenant namespace identifier for isolation",
        examples=["tenant_acme_corp"],
    )
    threshold: float | None = Field(
        default=None,
        ge=0.001,
        le=1.0,
        description="Maximum cosine distance threshold for a cache hit (default from settings: 0.08)",
    )
    prompt_response: str | None = Field(
        default=None,
        description="Optional LLM response to populate into cache if CACHE_MISS occurs",
        examples=["Remote work is permitted up to 3 days per week with manager approval."],
    )
    store_if_miss: bool = Field(
        default=True,
        description="If True and lookup misses, automatically store this query and response in cache",
    )
    metadata: dict[str, Any] | None = Field(
        default=None,
        description="Optional metadata associated with generation (model name, tokens, etc.)",
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "query": "What is the remote work policy?",
                "query_vector": [0.01] * 1536,
                "tenant_id": "tenant_acme_corp",
                "threshold": 0.08,
                "prompt_response": "Employees may work remotely 3 days weekly.",
                "store_if_miss": True,
            }
        }
    )


class CacheEvaluateResponse(BaseModel):
    """Response returned by cache evaluate endpoint with benchmark metrics."""

    status: Literal["CACHE_HIT", "CACHE_MISS"] = Field(
        ..., description="Hit/miss evaluation status"
    )
    query: str = Field(..., description="Echo of evaluated query")
    tenant_id: str = Field(..., description="Tenant namespace identifier")
    similarity: float | None = Field(
        default=None,
        description="Cosine similarity score (0.0 to 1.0) with best matching cached vector",
    )
    distance: float | None = Field(
        default=None,
        description="Cosine distance (1 - similarity) with best matching cached vector",
    )
    threshold: float = Field(
        ...,
        description="Configured distance threshold used for boundary evaluation",
    )
    cached_query: str | None = Field(
        default=None,
        description="Original query text associated with the matched cache entry",
    )
    cached_response: str | None = Field(
        default=None,
        description="Cached generation content returned on CACHE_HIT",
    )
    matched_key: str | None = Field(
        default=None,
        description="Redis key of the matched cache entry",
    )
    lookup_latency_ms: float = Field(
        ...,
        description="Latency in milliseconds taken to perform the vector lookup and similarity check",
    )
    stored: bool = Field(
        default=False,
        description="Whether a new cache entry was created during this evaluation",
    )


class CacheSetRequest(BaseModel):
    """Explicitly store an item in the semantic cache."""

    query: str = Field(..., min_length=1, description="Original query prompt")
    query_vector: list[float] = Field(
        ..., min_length=1536, max_length=1536, description="1536-dimensional query embedding"
    )
    tenant_id: str = Field(..., min_length=1, max_length=64, description="Tenant namespace")
    response: str = Field(..., min_length=1, description="LLM generated answer to cache")
    metadata: dict[str, Any] | None = Field(default=None, description="Generation metadata")
    ttl_seconds: int | None = Field(
        default=None, ge=60, description="Optional custom TTL in seconds (defaults to 86400s / 24h)"
    )


class CacheSetResponse(BaseModel):
    """Confirmation payload for stored cache item."""

    status: str = Field(default="stored", description="Status confirmation")
    key: str = Field(..., description="Redis key used for storage")
    tenant_id: str = Field(..., description="Tenant namespace")
    ttl_seconds: int = Field(..., description="Configured expiration time in seconds")


class CacheInvalidateRequest(BaseModel):
    """Request to evict all cached entries for a given tenant."""

    tenant_id: str = Field(..., min_length=1, max_length=64, description="Tenant to invalidate")


class CacheInvalidateResponse(BaseModel):
    """Confirmation of tenant cache invalidation."""

    status: str = Field(default="invalidated", description="Status confirmation")
    tenant_id: str = Field(..., description="Target tenant")
    keys_evicted: int = Field(..., description="Number of evicted cache keys")
