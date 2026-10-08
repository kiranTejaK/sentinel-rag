"""Redis Semantic Cache Service for Sub-20ms Vector Lookup.

Provides:
- Exact-match SHA-256 KV fast-path lookup
- Vector cosine similarity exploration over serialized embeddings
- Strict threshold evaluation (cosine similarity >= 0.92, distance <= 0.08)
- Configurable TTL (24 hours) and tenant namespace isolation (cache:{tenant_id}:{hash})
"""

import base64
import hashlib
import json
import math
import struct
import time
from dataclasses import dataclass
from typing import Any

import redis.asyncio as aioredis

from app.core.config import settings
from app.core.logging import logger


@dataclass(slots=True)
class SemanticLookupResult:
    """Outcome of a semantic cache evaluation."""

    status: str  # "CACHE_HIT" or "CACHE_MISS"
    similarity: float | None
    distance: float | None
    threshold: float
    cached_query: str | None
    cached_response: str | None
    matched_key: str | None
    lookup_latency_ms: float
    metadata: dict[str, Any] | None = None


def serialize_embedding(vector: list[float]) -> str:
    """Serializes a float vector into base64-encoded IEEE 754 32-bit floats.
    Compact, UTF-8 safe for Redis decode_responses=True, and fast to unpack.
    """
    raw_bytes = struct.pack(f"{len(vector)}f", *vector)
    return base64.b64encode(raw_bytes).decode("ascii")


def deserialize_embedding(serialized: str) -> list[float]:
    """Deserializes base64-encoded binary string back to float vector."""
    raw_bytes = base64.b64decode(serialized.encode("ascii"))
    count = len(raw_bytes) // 4
    return list(struct.unpack(f"{count}f", raw_bytes))


def compute_cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    """Computes cosine similarity between two float vectors.
    Returns a value between -1.0 and 1.0.
    """
    if len(vec_a) != len(vec_b):
        return 0.0

    dot_product = 0.0
    norm_a_sq = 0.0
    norm_b_sq = 0.0

    for a, b in zip(vec_a, vec_b, strict=False):
        dot_product += a * b
        norm_a_sq += a * a
        norm_b_sq += b * b

    if norm_a_sq <= 0.0 or norm_b_sq <= 0.0:
        return 0.0

    denom = math.sqrt(norm_a_sq) * math.sqrt(norm_b_sq)
    sim = dot_product / denom
    return max(-1.0, min(1.0, sim))


class RedisSemanticCache:
    """Production Semantic Cache service backed by Redis 7."""

    def __init__(self, redis_client: aioredis.Redis) -> None:
        self.redis = redis_client
        self.default_threshold = settings.SEMANTIC_CACHE_THRESHOLD
        self.default_ttl = settings.SEMANTIC_CACHE_TTL_SECONDS

    @staticmethod
    def compute_query_hash(query: str) -> str:
        """Computes deterministic normalized SHA-256 digest for exact-match fast path."""
        normalized = query.strip().lower()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @staticmethod
    def get_cache_key(tenant_id: str, query_hash: str) -> str:
        """Returns isolated tenant cache key."""
        return f"cache:{tenant_id}:{query_hash}"

    @staticmethod
    def get_index_key(tenant_id: str) -> str:
        """Returns isolated tenant key tracking set."""
        return f"cache:{tenant_id}:keys"

    async def get_or_lookup(
        self,
        tenant_id: str,
        query: str,
        query_vector: list[float],
        threshold: float | None = None,
    ) -> SemanticLookupResult:
        """Evaluates semantic similarity of query against cached vectors in tenant namespace.
        1. Fast-path: Check exact query hash match (O(1)).
        2. Semantic-path: Pipelined scan of cached vectors calculating cosine distance.
        Strict threshold: distance <= threshold (similarity >= 1.0 - threshold).
        """
        start_time = time.perf_counter()
        effective_threshold = threshold if threshold is not None else self.default_threshold
        query_hash = self.compute_query_hash(query)
        exact_key = self.get_cache_key(tenant_id, query_hash)

        # 1. Exact-match Fast Path
        try:
            exact_data = await self.redis.hgetall(exact_key)
            if exact_data and "response" in exact_data:
                latency_ms = round((time.perf_counter() - start_time) * 1000.0, 3)
                logger.info(
                    "semantic_cache_exact_hit",
                    tenant_id=tenant_id,
                    key=exact_key,
                    latency_ms=latency_ms,
                )
                meta = json.loads(exact_data.get("metadata", "{}"))
                return SemanticLookupResult(
                    status="CACHE_HIT",
                    similarity=1.0,
                    distance=0.0,
                    threshold=effective_threshold,
                    cached_query=exact_data.get("query", query),
                    cached_response=exact_data.get("response"),
                    matched_key=exact_key,
                    lookup_latency_ms=latency_ms,
                    metadata=meta,
                )
        except Exception as exc:
            logger.error("semantic_cache_exact_lookup_error", error=str(exc), tenant_id=tenant_id)

        # 2. Semantic Similarity Scan across Tenant Keys
        index_key = self.get_index_key(tenant_id)
        try:
            candidate_keys = await self.redis.smembers(index_key)
            if not candidate_keys:
                latency_ms = round((time.perf_counter() - start_time) * 1000.0, 3)
                return SemanticLookupResult(
                    status="CACHE_MISS",
                    similarity=None,
                    distance=None,
                    threshold=effective_threshold,
                    cached_query=None,
                    cached_response=None,
                    matched_key=None,
                    lookup_latency_ms=latency_ms,
                )

            # Pipeline HGETALL for all candidate keys
            pipeline = self.redis.pipeline(transaction=False)
            keys_list = list(candidate_keys)
            for k in keys_list:
                pipeline.hgetall(k)
            entries = await pipeline.execute()

            best_similarity = -1.0
            best_entry: dict[str, str] | None = None
            best_key: str | None = None
            expired_keys: list[str] = []

            for k, entry in zip(keys_list, entries, strict=False):
                if not entry or "embedding" not in entry:
                    expired_keys.append(k)
                    continue

                try:
                    cached_vec = deserialize_embedding(entry["embedding"])
                    sim = compute_cosine_similarity(query_vector, cached_vec)
                    if sim > best_similarity:
                        best_similarity = sim
                        best_entry = entry
                        best_key = k
                except Exception as parse_err:
                    logger.warning("corrupt_cache_embedding", key=k, error=str(parse_err))
                    expired_keys.append(k)

            # Lazy eviction of dead keys from tenant index set
            if expired_keys:
                try:
                    await self.redis.srem(index_key, *expired_keys)
                except Exception as srem_err:
                    logger.warning("failed_to_clean_expired_keys", error=str(srem_err))

            latency_ms = round((time.perf_counter() - start_time) * 1000.0, 3)

            # Calculate distance and evaluate threshold
            if best_similarity >= -1.0 and best_entry is not None:
                best_distance = round(1.0 - best_similarity, 5)
                best_similarity = round(best_similarity, 5)

                # Strict threshold logic: distance <= threshold (e.g. <= 0.08)
                if best_distance <= effective_threshold:
                    logger.info(
                        "semantic_cache_hit",
                        tenant_id=tenant_id,
                        key=best_key,
                        similarity=best_similarity,
                        distance=best_distance,
                        threshold=effective_threshold,
                        latency_ms=latency_ms,
                    )
                    meta = json.loads(best_entry.get("metadata", "{}"))
                    return SemanticLookupResult(
                        status="CACHE_HIT",
                        similarity=best_similarity,
                        distance=best_distance,
                        threshold=effective_threshold,
                        cached_query=best_entry.get("query"),
                        cached_response=best_entry.get("response"),
                        matched_key=best_key,
                        lookup_latency_ms=latency_ms,
                        metadata=meta,
                    )

                logger.info(
                    "semantic_cache_miss_distance_exceeded",
                    tenant_id=tenant_id,
                    best_distance=best_distance,
                    best_similarity=best_similarity,
                    threshold=effective_threshold,
                    latency_ms=latency_ms,
                )
                return SemanticLookupResult(
                    status="CACHE_MISS",
                    similarity=best_similarity,
                    distance=best_distance,
                    threshold=effective_threshold,
                    cached_query=None,
                    cached_response=None,
                    matched_key=None,
                    lookup_latency_ms=latency_ms,
                )

            return SemanticLookupResult(
                status="CACHE_MISS",
                similarity=None,
                distance=None,
                threshold=effective_threshold,
                cached_query=None,
                cached_response=None,
                matched_key=None,
                lookup_latency_ms=latency_ms,
            )

        except Exception as exc:
            latency_ms = round((time.perf_counter() - start_time) * 1000.0, 3)
            logger.error("semantic_cache_lookup_failed", error=str(exc), tenant_id=tenant_id)
            return SemanticLookupResult(
                status="CACHE_MISS",
                similarity=None,
                distance=None,
                threshold=effective_threshold,
                cached_query=None,
                cached_response=None,
                matched_key=None,
                lookup_latency_ms=latency_ms,
            )

    async def set(
        self,
        tenant_id: str,
        query: str,
        query_vector: list[float],
        response: str,
        metadata: dict[str, Any] | None = None,
        ttl_seconds: int | None = None,
    ) -> str:
        """Stores a query embedding and LLM generation into Redis hash with TTL and tenant isolation."""
        ttl = ttl_seconds if ttl_seconds is not None else self.default_ttl
        query_hash = self.compute_query_hash(query)
        cache_key = self.get_cache_key(tenant_id, query_hash)
        index_key = self.get_index_key(tenant_id)

        serialized_embedding = serialize_embedding(query_vector)
        metadata_str = json.dumps(metadata or {})

        mapping = {
            "query": query,
            "embedding": serialized_embedding,
            "response": response,
            "metadata": metadata_str,
            "created_at": str(time.time()),
        }

        pipeline = self.redis.pipeline(transaction=True)
        pipeline.hset(cache_key, mapping=mapping)
        pipeline.expire(cache_key, ttl)
        pipeline.sadd(index_key, cache_key)
        # Ensure tenant index key also has TTL if not renewed
        pipeline.expire(index_key, ttl * 2)
        await pipeline.execute()

        logger.info(
            "semantic_cache_stored",
            tenant_id=tenant_id,
            key=cache_key,
            ttl=ttl,
        )
        return cache_key

    async def invalidate_tenant(self, tenant_id: str) -> int:
        """Evicts all cached semantic vectors and metadata belonging to a tenant."""
        index_key = self.get_index_key(tenant_id)
        candidate_keys = await self.redis.smembers(index_key)

        evicted_count = 0
        if candidate_keys:
            pipeline = self.redis.pipeline(transaction=True)
            for k in candidate_keys:
                pipeline.delete(k)
            pipeline.delete(index_key)
            results = await pipeline.execute()
            # Sum deleted keys (last result is deletion of index_key)
            evicted_count = sum(r for r in results[:-1] if isinstance(r, int))

        logger.info(
            "semantic_cache_tenant_invalidated",
            tenant_id=tenant_id,
            evicted_keys=evicted_count,
        )
        return evicted_count
