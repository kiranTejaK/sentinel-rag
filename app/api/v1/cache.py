"""FastAPI router for Redis Semantic Cache endpoints."""

import time
from typing import Annotated

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, status

from app.core.config import settings
from app.core.logging import logger
from app.core.redis import get_redis_client
from app.schemas.cache import (
    CacheEvaluateRequest,
    CacheEvaluateResponse,
    CacheInvalidateRequest,
    CacheInvalidateResponse,
    CacheSetRequest,
    CacheSetResponse,
)
from app.services.cache import RedisSemanticCache

router = APIRouter(prefix="/cache", tags=["Semantic Cache"])


@router.post(
    "/evaluate",
    response_model=CacheEvaluateResponse,
    status_code=status.HTTP_200_OK,
    summary="Evaluate Semantic Cache Hit/Miss & Benchmark Latency",
    description=(
        "Serializes query embedding, searches Redis cache keys in isolated tenant namespace, "
        "and evaluates cosine distance against strict threshold (<= 0.08 distance, >= 0.92 similarity). "
        "Returns sub-20ms lookup performance metrics."
    ),
)
async def evaluate_semantic_cache(
    request: CacheEvaluateRequest,
    redis: Annotated[aioredis.Redis, Depends(get_redis_client)],
) -> CacheEvaluateResponse:
    """Evaluates query embedding against the Redis semantic cache layer."""
    start_time = time.perf_counter()
    cache_service = RedisSemanticCache(redis)

    result = await cache_service.get_or_lookup(
        tenant_id=request.tenant_id,
        query=request.query,
        query_vector=request.query_vector,
        threshold=request.threshold,
    )

    stored = False
    if (
        result.status == "CACHE_MISS"
        and request.store_if_miss
        and request.prompt_response is not None
    ):
        await cache_service.set(
            tenant_id=request.tenant_id,
            query=request.query,
            query_vector=request.query_vector,
            response=request.prompt_response,
            metadata=request.metadata,
        )
        stored = True

    total_latency_ms = round((time.perf_counter() - start_time) * 1000.0, 3)

    logger.info(
        "cache_evaluation_completed",
        tenant_id=request.tenant_id,
        status=result.status,
        similarity=result.similarity,
        distance=result.distance,
        lookup_latency_ms=result.lookup_latency_ms,
        total_latency_ms=total_latency_ms,
        stored=stored,
    )

    return CacheEvaluateResponse(
        status="CACHE_HIT" if result.status == "CACHE_HIT" else "CACHE_MISS",
        query=request.query,
        tenant_id=request.tenant_id,
        similarity=result.similarity,
        distance=result.distance,
        threshold=result.threshold,
        cached_query=result.cached_query,
        cached_response=result.cached_response,
        matched_key=result.matched_key,
        lookup_latency_ms=result.lookup_latency_ms,
        stored=stored,
    )


@router.post(
    "/set",
    response_model=CacheSetResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Store Query Embedding and Generation in Semantic Cache",
)
async def set_semantic_cache(
    request: CacheSetRequest,
    redis: Annotated[aioredis.Redis, Depends(get_redis_client)],
) -> CacheSetResponse:
    """Manually stores a vector embedding and LLM response into Redis semantic cache."""
    cache_service = RedisSemanticCache(redis)
    key = await cache_service.set(
        tenant_id=request.tenant_id,
        query=request.query,
        query_vector=request.query_vector,
        response=request.response,
        metadata=request.metadata,
        ttl_seconds=request.ttl_seconds,
    )
    ttl = request.ttl_seconds or settings.SEMANTIC_CACHE_TTL_SECONDS
    return CacheSetResponse(
        status="stored",
        key=key,
        tenant_id=request.tenant_id,
        ttl_seconds=ttl,
    )


@router.post(
    "/invalidate",
    response_model=CacheInvalidateResponse,
    status_code=status.HTTP_200_OK,
    summary="Invalidate All Cached Vectors for Tenant",
)
async def invalidate_tenant_cache(
    request: CacheInvalidateRequest,
    redis: Annotated[aioredis.Redis, Depends(get_redis_client)],
) -> CacheInvalidateResponse:
    """Evicts all cached semantic vectors belonging to a specific tenant namespace."""
    cache_service = RedisSemanticCache(redis)
    count = await cache_service.invalidate_tenant(request.tenant_id)
    return CacheInvalidateResponse(
        status="invalidated",
        tenant_id=request.tenant_id,
        keys_evicted=count,
    )
