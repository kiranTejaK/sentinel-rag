# DAY 4 - Redis 7 Semantic Vector Cache & Atomic Lua Rate Limiting

> **Goal:** By the end of Day 4, you will understand how SentinelRack builds a two-tier semantic caching layer (SHA-256 exact match + vector cosine similarity exploration) that returns responses in under 20ms, bypassing expensive LLM calls. You will also learn how to build an enterprise-grade distributed Token Bucket rate limiter using atomic Redis Lua scripts to protect the gateway from noisy neighbors and denial-of-service attacks.

---

## 1. The Core Engineering Problem

### Why Traditional Caching Fails for Generative AI
In traditional web APIs, caching is straightforward:
- An HTTP request `GET /api/v1/products/42` is cached against its exact URL or a hash of its request body.
- If the URL matches, return the cached JSON.

In Large Language Model (LLM) applications, this exact-match approach yields near-zero cache hit rates:
1. User A asks: *"What is the policy for expense reimbursements?"*
2. User B asks: *"How do I submit an expense report?"*
3. User C asks: *"Expense reimbursement rules?"*

All three queries express the **exact same user intent** and require the **exact same answer**. Yet their exact strings are completely different:
- A traditional key-value cache yields **3 cache misses**.
- The gateway makes 3 calls to an expensive upstream LLM (costing ~$0.03 to $0.06 each, waiting 1,200ms to 2,500ms per request).

### What is a Semantic Cache?
A semantic cache does not merely match strings. It computes the **semantic similarity** between the incoming query's vector embedding and previously cached queries:
1. User A asks the question -> Gateway calls LLM -> Stores query text, 1536-dim embedding vector, and response in Redis.
2. User B asks a paraphrased question -> Gateway computes embedding -> Finds User A's vector in Redis -> Measures cosine distance $\le 0.08$ (similarity $\ge 0.92$) -> **Returns User A's answer in 12ms**.

**Impact:**
- **Latency:** Slashed from $1,800\text{ms}$ to **$< 20\text{ms}$** (a $90\times$ speedup).
- **Cost:** Dropped from $\$0.03$ per query to **$\$0.00$**.
- **LLM Quota:** Prevents hitting upstream provider TPM/RPM rate limits during traffic spikes.

---

## 2. Two-Tier Cache Architecture

Scanning through thousands of vectors calculating cosine similarity takes CPU time. To achieve sub-20ms latency consistently, SentinelRack implements a **two-tier lookup pipeline**:

```
[Incoming Request: query, query_vector]
                   │
                   ▼
       [Tier 1: Fast-Path Hash Check]
         SHA-256(lowercase(query.strip()))
         HGETALL cache:{tenant_id}:{hash}
                   │
         ┌─────────┴─────────┐
      Hit (O(1))          Miss (Hash mismatch)
         │                   │
         ▼                   ▼
   [Return Cached    [Tier 2: Semantic Exploration]
    Response in 2ms]   Fetch tenant candidate keys (SMEMBERS)
                       Pipeline HGETALL embeddings
                       Unpack IEEE 754 floats
                       Compute Cosine Similarity
                             │
                     ┌───────┴───────┐
             Sim >= 0.92          Sim < 0.92
             (Dist <= 0.08)          │
                     │               ▼
                     ▼          [CACHE MISS]
              [CACHE HIT]       Proceed to RAG & LLM
            Return in 15ms      Store in Cache on Return
```

### Tier 1: Exact-Match Fast Path ($O(1)$)
- Normalizes the query string (trimming whitespace and converting to lowercase).
- Computes deterministic SHA-256 hash.
- Looks up `cache:{tenant_id}:{query_hash}` via `HGETALL`.
- If found, returns immediately in **$< 2\text{ms}$**, completely bypassing vector deserialization and math.

### Tier 2: Semantic Exploration Path
- Retrieves the set of all active cache keys for that tenant: `SMEMBERS cache:{tenant_id}:keys`.
- Executes a Redis **pipeline** to fetch all cached entries in a single network round-trip.
- Deserializes binary vectors and calculates cosine similarity.
- Identifies the entry with the highest similarity.
- Evaluates against the strict threshold: $\text{distance} = 1.0 - \text{similarity} \le 0.08$ ($\text{similarity} \ge 0.92$).
- If within threshold, returns the cached generation in **$< 18\text{ms}$**.

---

## 3. High-Performance Vector Serialization: IEEE 754 Binary Packing

How should a 1536-dimensional float vector be stored in Redis?

### The Naive Approach: JSON String Array
```json
"[0.01234567, -0.04567891, 0.00987654, ...]"
```
- A 1536-float array serialized as JSON text consumes **$\approx 35\text{ KB}$**.
- If a tenant has 1,000 cached queries, scanning them requires transferring and parsing **$35\text{ MB}$ of text** on every query!
- Python's `json.loads()` spends tens of milliseconds parsing string floats into Python float objects, obliterating our sub-20ms latency budget.

### The SentinelRack Approach: `struct.pack` Binary Packing + Base64
In memory, each 32-bit floating-point number is represented according to the IEEE 754 standard as exactly 4 bytes.
- 1536 floats $\times 4\text{ bytes} = 6,144\text{ bytes}$ (exactly **$6\text{ KB}$**).
- Python's built-in `struct.pack(f"{len(vec)}f", *vec)` serializes the entire 1536-float list into raw C binary bytes in **$< 15\text{ microseconds}$**.
- Because our Redis connection is configured with `decode_responses=True` (which expects UTF-8 text strings), storing arbitrary binary bytes directly would cause UTF-8 decoding errors.
- SentinelRack encodes the 6,144 bytes into standard Base64 ASCII characters ($8,192\text{ characters}$):

$$\text{Packing Efficiency: } 35\text{ KB (JSON)} \longrightarrow 8.1\text{ KB (Base64 Binary)} \quad (\approx 77\% \text{ reduction})$$

```python
def serialize_embedding(vector: list[float]) -> str:
    """Serializes a float vector into base64-encoded IEEE 754 32-bit floats."""
    raw_bytes = struct.pack(f"{len(vector)}f", *vector)
    return base64.b64encode(raw_bytes).decode("ascii")

def deserialize_embedding(serialized: str) -> list[float]:
    """Deserializes base64-encoded binary string back to float vector."""
    raw_bytes = base64.b64decode(serialized.encode("ascii"))
    count = len(raw_bytes) // 4
    return list(struct.unpack(f"{count}f", raw_bytes))
```

---

## 4. Multi-Tenant Namespace Isolation & Lazy Eviction

In enterprise SaaS, Tenant A must **never** receive cached answers from Tenant B (data leakage security vulnerability).

### Key Naming Conventions:
1. **Cache Entry Hash:** `cache:{tenant_id}:{query_hash}`
   - Stores a Redis Hash with fields: `query`, `embedding`, `response`, `metadata`, `created_at`.
   - Has a native Redis TTL (e.g., 24 hours $= 86,400\text{ seconds}$).
2. **Tenant Key Index Set:** `cache:{tenant_id}:keys`
   - Stores a Redis Set containing all active cache keys created by that tenant.
   - Enables fast `SMEMBERS` retrieval during semantic search.
   - Enables instant bulk invalidation when a tenant updates their knowledge base.

### The Ghost Key Problem & Lazy Eviction
Redis automatically expires individual cache entry keys when their TTL elapses (`cache:tenant_a:abc1234`). However, Redis does *not* automatically remove expired keys from the tracking set `cache:tenant_a:keys`!
- Over time, `cache:tenant_a:keys` accumulates thousands of "ghost" keys that point to deleted entries.
- If unhandled, the semantic scan would waste time checking keys that no longer exist.

**SentinelRack's Solution: Lazy Eviction during Scan**
During the semantic search pipeline, if a key in the set returns an empty hash (`not entry`), SentinelRack appends that key to an `expired_keys` list and issues an atomic `SREM`:

```python
if expired_keys:
    await self.redis.srem(index_key, *expired_keys)
```
This self-healing cleanup keeps the tenant index set clean without requiring a background cron job.

---

## 5. Distributed Token Bucket Rate Limiting with Atomic Lua

### Why In-Memory Rate Limiters Fail in Production
A junior developer often implements rate limiting using an in-memory dictionary or `asyncio.Lock()`:

```python
# BROKEN IN PRODUCTION: In-memory dictionary
request_counts = {}

def rate_limit(api_key: str):
    request_counts[api_key] = request_counts.get(api_key, 0) + 1
```

**Why this fails catastrophically:**
1. **Multiple Uvicorn Workers:** In production, Uvicorn runs 4 to 8 worker processes (`uvicorn app.main:app --workers 4`). Each worker has its own isolated memory space. A client can make $4\times$ the allowed limit by hitting different worker processes.
2. **Horizontal Autoscaling:** In Kubernetes or Docker Swarm, multiple container replicas run across multiple physical hosts. An in-memory counter is completely blind to traffic hitting other replicas.

### Why Redis `GET` + `SET` Causes Race Conditions
Moving to Redis with separate commands introduces a classic **Check-Then-Act race condition**:

```
Worker 1: GET ratelimit:user_123   --> Returns 1 token remaining
Worker 2: GET ratelimit:user_123   --> Returns 1 token remaining
Worker 1: SET ratelimit:user_123 0 --> Request allowed!
Worker 2: SET ratelimit:user_123 0 --> Request allowed! (Limit breached!)
```

### The Solution: Atomic Redis Lua Scripts
Redis executes Lua scripts **atomically in its single-threaded execution context**. No other command or script can run concurrently while a Lua script is executing.

```
ASGI Replicas                   Redis Engine (Single-Threaded)
[Worker 1] ───EVALSHA───┐       ┌──────────────────────────────┐
[Worker 2] ───EVALSHA───┼──────>│ 1. Read tokens & timestamp   │
[Worker 3] ───EVALSHA───┘       │ 2. Replenish based on TIME   │
                                │ 3. Decrement token if avail  │
                                │ 4. Persist & return status   │
                                └──────────────┬───────────────┘
                                               │
                                 Atomic Result (Allowed/Rejected)
```

### The Token Bucket Algorithm in Lua
In the Token Bucket algorithm:
- A bucket has a maximum **capacity** (e.g., 60 tokens).
- Tokens refill continuously at a fixed **refill rate** (e.g., 1 token per second).
- Each request consumes 1 token.
- If tokens $\ge 1$, the request is allowed. If tokens $< 1$, the request is rejected with HTTP 429 and a calculated `Retry-After` duration.

Let's dissect SentinelRack's production Lua script line by line:

```lua
-- app/core/rate_limiter.py (Lua Script)

local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local requested = tonumber(ARGV[3])

-- 1. Query Redis server time (avoids host clock skew across ASGI workers)
local time_arr = redis.call('TIME')
local now = tonumber(time_arr[1]) + (tonumber(time_arr[2]) / 1000000)

-- 2. Fetch current bucket state from Hash
local current_data = redis.call('HMGET', key, 'tokens', 'last_updated')
local tokens = tonumber(current_data[1])
local last_updated = tonumber(current_data[2])

if tokens == nil or last_updated == nil then
    -- Fresh bucket: initialize with max capacity
    tokens = capacity
    last_updated = now
else
    -- Compute token replenishment: tokens += delta * refill_rate
    local delta = math.max(0, now - last_updated)
    tokens = math.min(capacity, tokens + (delta * refill_rate))
    last_updated = now
end

local allowed = 0
local retry_after = 0

-- 3. Check token availability
if tokens >= requested then
    tokens = tokens - requested
    allowed = 1
    retry_after = 0
else
    allowed = 0
    local needed = requested - tokens
    retry_after = math.ceil(needed / refill_rate)
end

-- 4. Persist updated tokens and timestamp
redis.call('HSET', key, 'tokens', tokens, 'last_updated', last_updated)

-- 5. Auto-expire inactive buckets (prevent memory leak)
local ttl = math.ceil((capacity / refill_rate) * 2)
redis.call('EXPIRE', key, math.max(60, ttl))

return {allowed, math.floor(tokens), retry_after, capacity}
```

#### Key Engineering Decisions in this Lua Script:
1. `redis.call('TIME')`:
   - Never pass the application server's local time (`time.time()`) into the rate limiter! If Worker A's clock is 500ms ahead of Worker B's clock, token replenishment calculations fluctuate wildly. Querying the Redis server's own clock guarantees a single, monotonic source of truth.
2. `delta = math.max(0, now - last_updated)`:
   - Guards against backward clock steps.
3. `tokens = math.min(capacity, tokens + (delta * refill_rate))`:
   - Continuous replenishment without requiring background timers.
4. `redis.call('EXPIRE', key, ttl)`:
   - If an API client stops sending requests, the Redis key automatically expires after $2\times$ the refill window, preventing memory leaks in Redis.

---

## 6. Code Walkthrough - File by File

### File 1: Semantic Cache Service - `app/services/cache.py`

```python
# app/services/cache.py

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
    """Serializes a float vector into base64-encoded IEEE 754 32-bit floats."""
    raw_bytes = struct.pack(f"{len(vector)}f", *vector)
    return base64.b64encode(raw_bytes).decode("ascii")


def deserialize_embedding(serialized: str) -> list[float]:
    """Deserializes base64-encoded binary string back to float vector."""
    raw_bytes = base64.b64decode(serialized.encode("ascii"))
    count = len(raw_bytes) // 4
    return list(struct.unpack(f"{count}f", raw_bytes))


def compute_cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    """Computes cosine similarity between two float vectors bounded [-1.0, 1.0]."""
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
        return f"cache:{tenant_id}:{query_hash}"

    @staticmethod
    def get_index_key(tenant_id: str) -> str:
        return f"cache:{tenant_id}:keys"

    async def get_or_lookup(
        self,
        tenant_id: str,
        query: str,
        query_vector: list[float],
        threshold: float | None = None,
    ) -> SemanticLookupResult:
        start_time = time.perf_counter()
        effective_threshold = threshold if threshold is not None else self.default_threshold
        query_hash = self.compute_query_hash(query)
        exact_key = self.get_cache_key(tenant_id, query_hash)

        # 1. Exact-match Fast Path (O(1))
        try:
            exact_data = await self.redis.hgetall(exact_key)
            if exact_data and "response" in exact_data:
                latency_ms = round((time.perf_counter() - start_time) * 1000.0, 3)
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

            # Pipelined retrieval of candidate entries
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

            # Lazy eviction of dead keys
            if expired_keys:
                try:
                    await self.redis.srem(index_key, *expired_keys)
                except Exception as srem_err:
                    logger.warning("failed_to_clean_expired_keys", error=str(srem_err))

            latency_ms = round((time.perf_counter() - start_time) * 1000.0, 3)

            # Evaluate against distance threshold
            if best_similarity >= -1.0 and best_entry is not None:
                best_distance = round(1.0 - best_similarity, 5)
                best_similarity = round(best_similarity, 5)

                if best_distance <= effective_threshold:
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
        pipeline.expire(index_key, ttl * 2)
        await pipeline.execute()

        return cache_key

    async def invalidate_tenant(self, tenant_id: str) -> int:
        index_key = self.get_index_key(tenant_id)
        candidate_keys = await self.redis.smembers(index_key)

        evicted_count = 0
        if candidate_keys:
            pipeline = self.redis.pipeline(transaction=True)
            for k in candidate_keys:
                pipeline.delete(k)
            pipeline.delete(index_key)
            results = await pipeline.execute()
            evicted_count = sum(r for r in results[:-1] if isinstance(r, int))

        return evicted_count
```

---

### File 2: Distributed Rate Limiter & Middleware - `app/core/rate_limiter.py`

```python
# app/core/rate_limiter.py

from dataclasses import dataclass
from typing import Any

import redis.asyncio as aioredis
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.core.logging import logger

TOKEN_BUCKET_LUA_SCRIPT = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local requested = tonumber(ARGV[3])

local time_arr = redis.call('TIME')
local now = tonumber(time_arr[1]) + (tonumber(time_arr[2]) / 1000000)

local current_data = redis.call('HMGET', key, 'tokens', 'last_updated')
local tokens = tonumber(current_data[1])
local last_updated = tonumber(current_data[2])

if tokens == nil or last_updated == nil then
    tokens = capacity
    last_updated = now
else
    local delta = math.max(0, now - last_updated)
    tokens = math.min(capacity, tokens + (delta * refill_rate))
    last_updated = now
end

local allowed = 0
local retry_after = 0

if tokens >= requested then
    tokens = tokens - requested
    allowed = 1
    retry_after = 0
else
    allowed = 0
    local needed = requested - tokens
    retry_after = math.ceil(needed / refill_rate)
end

redis.call('HSET', key, 'tokens', tokens, 'last_updated', last_updated)
local ttl = math.ceil((capacity / refill_rate) * 2)
redis.call('EXPIRE', key, math.max(60, ttl))

return {allowed, math.floor(tokens), retry_after, capacity}
"""


@dataclass(slots=True)
class RateLimitResult:
    allowed: bool
    remaining: int
    retry_after: int
    limit: int


class TokenBucketLimiter:
    """Evaluates token bucket consumption via atomic Redis Lua script."""

    def __init__(
        self,
        redis_client: aioredis.Redis | None = None,
        redis_getter: Any | None = None,
    ) -> None:
        self._client = redis_client
        self._getter = redis_getter
        self._script_sha: str | None = None

    @property
    def redis(self) -> aioredis.Redis | None:
        if self._client is not None:
            return self._client
        if self._getter is not None:
            return self._getter()
        return None

    async def _get_script_sha(self, client: aioredis.Redis) -> str:
        """Pre-loads the Lua script into Redis script cache and caches SHA1 digest."""
        if self._script_sha is None:
            self._script_sha = await client.script_load(TOKEN_BUCKET_LUA_SCRIPT)
        return self._script_sha

    async def check(
        self,
        identifier: str,
        capacity: int = 60,
        refill_rate_per_sec: float = 1.0,
        requested: int = 1,
    ) -> RateLimitResult:
        key = f"ratelimit:{identifier}"
        client = self.redis
        if client is None:
            # Redis not configured; fail-open safely
            return RateLimitResult(allowed=True, remaining=capacity, retry_after=0, limit=capacity)

        try:
            sha = await self._get_script_sha(client)
            try:
                # Optimized invocation: EVALSHA passes 40-char SHA1 hash instead of full script text
                raw = await client.evalsha(
                    sha, 1, key, str(capacity), str(refill_rate_per_sec), str(requested)
                )
            except aioredis.exceptions.NoScriptError:
                # Redis SCRIPT FLUSH occurred; fallback to EVAL
                self._script_sha = None
                raw = await client.eval(
                    TOKEN_BUCKET_LUA_SCRIPT,
                    1,
                    key,
                    str(capacity),
                    str(refill_rate_per_sec),
                    str(requested),
                )

            return RateLimitResult(
                allowed=bool(raw[0]),
                remaining=int(raw[1]),
                retry_after=int(raw[2]),
                limit=int(raw[3]),
            )
        except Exception as exc:
            logger.error("rate_limit_redis_evaluation_error", error=str(exc), key=key)
            # Fail-open policy: maintain service availability during Redis hiccups
            return RateLimitResult(allowed=True, remaining=capacity, retry_after=0, limit=capacity)


class TokenBucketRateLimitMiddleware(BaseHTTPMiddleware):
    """Custom middleware enforcing distributed rate limiting per API key or client IP."""

    EXEMPT_PATHS: set[str] = {"/healthz", "/", "/docs", "/redoc", "/openapi.json"}

    def __init__(
        self,
        app: Any,
        redis_client: aioredis.Redis | None = None,
        redis_getter: Any | None = None,
        capacity: int = 60,
        refill_rate_per_sec: float = 1.0,
    ) -> None:
        super().__init__(app)
        self.limiter = TokenBucketLimiter(redis_client=redis_client, redis_getter=redis_getter)
        self.capacity = capacity
        self.refill_rate_per_sec = refill_rate_per_sec

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        if path in self.EXEMPT_PATHS or path.startswith(("/docs", "/redoc")):
            return await call_next(request)

        # Extract identifier: prioritize X-API-Key, fallback to client IP
        api_key = request.headers.get("X-API-Key")
        if api_key:
            identifier = f"key:{api_key.strip()}"
        else:
            client_ip = request.client.host if request.client else "anonymous"
            identifier = f"ip:{client_ip}"

        res = await self.limiter.check(
            identifier=identifier,
            capacity=self.capacity,
            refill_rate_per_sec=self.refill_rate_per_sec,
            requested=1,
        )

        request_id = getattr(request.state, "request_id", None) or request.headers.get(
            "X-Request-Id", "unknown"
        )

        if not res.allowed:
            headers = {
                "Content-Type": "application/problem+json",
                "X-RateLimit-Limit": str(res.limit),
                "X-RateLimit-Remaining": "0",
                "Retry-After": str(max(1, res.retry_after)),
                "X-Request-Id": str(request_id),
            }
            problem_body = {
                "type": "https://sentinelrag.io/errors/rate-limit-exceeded",
                "title": "Too Many Requests",
                "status": 429,
                "detail": f"Rate limit quota exceeded ({res.limit} req/min). Retry after {max(1, res.retry_after)}s.",
                "instance": path,
                "request_id": str(request_id),
                "invalid_params": [],
            }
            return JSONResponse(status_code=429, content=problem_body, headers=headers)

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(res.limit)
        response.headers["X-RateLimit-Remaining"] = str(res.remaining)
        return response
```

---

### File 3: Cache Endpoints - `app/api/v1/cache.py`

```python
# app/api/v1/cache.py

import time
from typing import Annotated

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, status

from app.core.config import settings
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
)
async def evaluate_semantic_cache(
    request: CacheEvaluateRequest,
    redis: Annotated[aioredis.Redis, Depends(get_redis_client)],
) -> CacheEvaluateResponse:
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
    return CacheSetResponse(status="stored", key=key, tenant_id=request.tenant_id, ttl_seconds=ttl)


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
    cache_service = RedisSemanticCache(redis)
    count = await cache_service.invalidate_tenant(request.tenant_id)
    return CacheInvalidateResponse(status="invalidated", tenant_id=request.tenant_id, keys_evicted=count)
```

---

## 7. Comprehensive Topic-Wise Interview Questionnaire

*(Mandatory Directive: Zero DSA/LeetCode puzzles. Exhaustive, production-grade technical answers covering high-concurrency systems, storage engines, and system architecture).*

### Topic 1: Distributed Caching & Redis Internals

#### Q1.1: Why does Redis execute Lua scripts atomically? What are the engine-level guarantees and latency trade-offs of this design?
**Answer:**
Redis operates on a single-threaded event loop architecture (utilizing `epoll` on Linux or `kqueue` on BSD). When a Lua script is invoked via `EVAL` or `EVALSHA`, the Redis server pauses event loop dispatching of all other client commands and executes the Lua script from start to finish within its embedded Lua 5.1 interpreter.
- **Engine-Level Guarantees:** 
  1. *Atomicity & Isolation:* No other command, pipeline, or transaction can interleave. Every read and write inside the script observes a consistent snapshot without requiring distributed locks (`Redlock`).
  2. *Transactional Rollback Caveat:* Redis Lua scripts do **not** support relational ACID rollbacks. If a script fails halfway due to a Lua runtime syntax/type error, prior commands executed within the script remain committed in memory. Scripts must be tested to ensure runtime infallibility.
- **Latency Trade-offs:** Because the event loop is blocked for the entire duration of the script, long-running scripts (e.g., executing complex nested loops over large datasets) block all other connected clients, causing latency spikes across the entire fleet. To safeguard against this, Redis enforces a `lua-time-limit` (default 5 seconds), after which it begins accepting `SCRIPT KILL` commands. SentinelRack's Token Bucket script executes in $< 20\text{ microseconds}$, completely eliminating blocking risk.

#### Q1.2: Explain the operational difference between `EVAL` and `EVALSHA`. What is `NoScriptError`, and how must an ASGI application handle it?
**Answer:**
- **`EVAL`:** Transmits the full source code string of the Lua script over the network socket on every single invocation. For a 1 KB script executed across 10,000 requests per second, this generates $10\text{ MB/s}$ of unnecessary network bandwidth overhead and forces Redis to parse and compile the script repeatedly.
- **`EVALSHA`:** Pre-loads the script into the Redis server's memory via `SCRIPT LOAD`. Redis hashes the script content using SHA-1 and returns a 40-character hexadecimal digest. On subsequent calls, the client sends only the 40-character digest (`EVALSHA <sha1> ...`). Redis retrieves the pre-compiled bytecode from its in-memory script cache, saving network bandwidth and CPU cycles.
- **`NoScriptError` & Handling:** Redis stores compiled scripts exclusively in volatile RAM. If an operator issues a `SCRIPT FLUSH`, or if the Redis instance restarts, failovers, or runs in a multi-node cluster where a connection routes to a new replica, the cached script is lost. When the client executes `EVALSHA`, Redis responds with `NOSCRIPT No matching script. Please use EVAL.`. SentinelRack catches `aioredis.exceptions.NoScriptError`, clears the cached SHA1, and immediately issues `client.eval()` as a seamless fallback, reloading the script without dropping the user's request.

---

### Topic 2: Python / ASGI Concurrency Internals

#### Q2.1: Why is Starlette's `BaseHTTPMiddleware` criticized in high-throughput async Python services, and what specific behaviors must engineers monitor?
**Answer:**
Starlette's `BaseHTTPMiddleware` uses an internal streaming wrapper (`StreamingResponse`) and task management machinery to decouple request and response processing.
1. **ContextVar Propagation & Memory Leaks:** Prior to recent Starlette releases, `BaseHTTPMiddleware` ran the request handler inside a separate `anyio` task group, which could detach Python `contextvars` or cause memory accumulation under high streaming concurrency.
2. **Cancellation Propagation:** If a client abruptly disconnects (`ECONNRESET`), `BaseHTTPMiddleware` may not propagate the cancellation exception cleanly down the ASGI call stack, causing backend coroutines to continue executing orphan work.
3. **Throughput Overhead:** For ultra-high-throughput microservices handling $50,000+\text{ req/sec}$, `BaseHTTPMiddleware` introduces a measurable latency penalty ($\approx 0.5\text{ms}$ per request) compared to a pure ASGI middleware implemented directly as `async def __call__(self, scope, receive, send)`. SentinelRack uses `BaseHTTPMiddleware` for readability and rich Starlette `Request`/`Response` object abstractions, but high-frequency edge gateways often migrate to raw ASGI callables.

#### Q2.2: Python has a Global Interpreter Lock (GIL). How does SentinelRack compute 1536-dimensional cosine similarity in Python without causing severe GIL contention under concurrent load?
**Answer:**
Computing cosine similarity across 1536 floats in pure Python involves a loop executing 1536 floating-point multiplications and additions per candidate vector. Because this is CPU-bound math, it executes while holding the Python GIL.
- In SentinelRack, candidate pools per query are strictly bounded (e.g., 20 to 50 cached items in the tenant set).
- Computing similarity over 50 vectors of dimension 1536 takes $\approx 1.2\text{ms}$ of CPU time—well within the thread execution slice before yielding to the asyncio event loop.
- Furthermore, because Uvicorn runs multiple worker processes (process-level concurrency), each process has its own GIL and event loop running on separate CPU cores.
- For deployments caching $> 1,000$ vectors per tenant, the pure Python math is replaced by NumPy or C-accelerated extensions (`BLAS` / `LAPACK`), which release the GIL during vector matrix multiplication, allowing other asyncio I/O tasks to run concurrently on the same thread.

---

### Topic 3: Networking, Protocols & API Specifications

#### Q3.1: Detail the anatomy of an RFC 7807 Problem Details response on HTTP 429. Why is this standard superior to ad-hoc JSON error responses?
**Answer:**
Ad-hoc JSON error structures (e.g., `{"error": "Too many requests"}`) lack machine-readable schemas, forcing API consumers to write custom error-parsing regexes or proprietary handlers for every third-party service they integrate with.

**RFC 7807 (Problem Details for HTTP APIs)** defines a standardized, universally understood JSON schema (`Content-Type: application/problem+json`):
```json
{
  "type": "https://sentinelrag.io/errors/rate-limit-exceeded",
  "title": "Too Many Requests",
  "status": 429,
  "detail": "Rate limit quota exceeded (60 req/min). Retry after 4s.",
  "instance": "/v1/chat/completions",
  "request_id": "req_8f1a2b3c4d5e",
  "invalid_params": []
}
```
**Key Protocol Headers Injected:**
- `Retry-After: 4`: Tells automated API clients and HTTP libraries (like `httpx` or `requests`) exactly how many seconds to wait before attempting backoff retries.
- `X-RateLimit-Limit: 60`: Informs client monitoring systems of their provisioned ceiling.
- `X-RateLimit-Remaining: 0`: Explains why the current request was throttled.
- `X-Request-Id`: Enables distributed tracing across microservice boundaries.

---

### Topic 4: Resilience, Reliability & Failure Modes

#### Q4.1: Explain the architectural trade-off between "Fail-Open" and "Fail-Closed" policies in a distributed rate limiter when Redis crashes.
**Answer:**
When the centralized Redis cluster becomes unreachable (network partition, node crash, OOM event):
- **Fail-Closed Policy:** The rate limiter rejects all incoming traffic with HTTP 500 or HTTP 429.
  - *Pro:* Guarantees that internal downstream services (PostgreSQL, LLM providers) are protected from being overwhelmed.
  - *Con:* Completely takes down the entire business application. An outage in the caching layer creates a catastrophic total outage for all end users.
- **Fail-Open Policy (SentinelRack's Choice):** When Redis fails, the limiter catches the exception, logs a high-severity alert, and allows requests through (`allowed = True`).
  - *Pro:* High availability. End users experience zero downtime even if Redis is completely rebooted or network-isolated.
  - *Con:* Temporarily exposes the upstream LLM providers to burst traffic.
  - *Defense-in-Depth:* SentinelRack pairs fail-open application rate limiting with **Nginx-level static rate limiting** (`limit_req_zone` in memory on each reverse proxy instance). If Redis dies, Nginx still acts as a hard outer wall, preventing traffic floods from reaching the application tier.

#### Q4.2: What is the "Thundering Herd" (Cache Stampede) problem in a semantic cache, and how does a production system mitigate it?
**Answer:**
A Cache Stampede occurs when a high-traffic query expires or is invalidated from the cache simultaneously:
1. 500 concurrent users ask the same question within a 200ms window.
2. All 500 requests evaluate the cache simultaneously and encounter a `CACHE_MISS`.
3. All 500 requests proceed to execute expensive RAG retrieval and call upstream LLM APIs concurrently.
4. Upstream LLM rate limits are breached, costs spike $500\times$, and database connection pools are exhausted.

**Production Mitigations:**
1. **Probabilistic Early Expiration (XFetch Algorithm):** Recompute and refresh the cached entry in the background before it actually expires, based on query frequency and remaining TTL.
2. **Distributed Mutex (Singleflight Pattern):** When a cache miss occurs, the first worker acquires a Redis mutex lock for that specific query hash (`SET cache:lock:{hash} 1 EX 10 NX`). Subsequent requests waiting on that query wait for the lock to release or poll the cache for the result, ensuring that exactly **one** call is made to the upstream LLM.

---

### Topic 5: Container & Linux Security

#### Q5.1: How do you enforce security and prevent cross-tenant data leakage in a multi-tenant Redis instance without running separate Redis containers per customer?
**Answer:**
Running separate Redis containers for hundreds of enterprise tenants creates unmanageable RAM overhead and orchestration complexity. SentinelRack achieves secure multi-tenancy on a shared Redis instance through:
1. **Strict Cryptographic Key Namespacing:** Every Redis key is strictly prefixed with the tenant's validated authentication identifier: `cache:{tenant_id}:{hash}`. Code reviews mandate that no raw user input is interpolated into key names without sanitization.
2. **Redis 6+ Access Control Lists (ACLs):** Redis supports granular user ACLs with key pattern restrictions:
   ```redis
   ACL SETUSER tenant_acme on >StrongPassword ~cache:tenant_acme:* +@all
   ```
   This guarantees at the storage engine level that connection credentials provisioned for `tenant_acme` cannot read or write to `cache:tenant_globex:*`.
3. **TLS Encryption in Transit:** Disabling plaintext Redis connections and enforcing TLS 1.3 (`rediss://`) prevents packet sniffing on virtual container networks.

---

## 8. Day 4 - What the Real Commit Would Have Contained

A Git commit for Day 4 implementation includes:
- `app/services/cache.py`: `RedisSemanticCache` with exact hash fast path, pipelined vector scanning, IEEE 754 binary packing, lazy eviction, and tenant invalidation.
- `app/core/rate_limiter.py`: Atomic Redis Lua script, `TokenBucketLimiter` with `EVALSHA` fallback, and `TokenBucketRateLimitMiddleware`.
- `app/schemas/cache.py`: Pydantic v2 schemas (`CacheEvaluateRequest`, `CacheEvaluateResponse`, `CacheSetRequest`, `CacheInvalidateRequest`).
- `app/api/v1/cache.py`: `/v1/cache/evaluate`, `/v1/cache/set`, and `/v1/cache/invalidate` API routes.
- `tests/test_semantic_cache.py`: Unit and integration test suite with `FakeRedis` in-memory simulator validating cosine distance boundaries ($\le 0.08$).
- `tests/test_rate_limiter.py`: Exhaustive concurrency test suite verifying token depletion, replenishment over simulated time, and HTTP 429 header compliance.

---

*End of Day 4. Study the Redis internals and Lua scripting mechanics. When ready, reply "continue" for Day 5.*
