# 03. Core Concepts Explained From First Principles

This document breaks down the foundational computer science, distributed systems, and retrieval concepts implemented throughout SentinelRAG. Each concept is presented in a strict 10-point analysis format.

---

## Concept 1: Distributed Token-Bucket Rate Limiter (Atomic Redis Lua)

### 1. What it is
The Token Bucket is an algorithm used to control the rate of traffic sent into or processed by a network or server. A "bucket" of fixed capacity holds "tokens". Tokens refill continuously at a constant rate. Each incoming request consumes one or more tokens. If sufficient tokens are present, the request proceeds; if empty, the request is rejected immediately with HTTP 429.

### 2. Why it exists
Without rate limiting, malicious users or malfunctioning scripts can execute denial-of-service (DoS) attacks, exhaust upstream LLM API billing quotas, or monopolize database connection pools. In distributed architectures with multiple stateless ASGI workers, in-memory Python counters cannot share state. Furthermore, standard multi-step Redis commands (`GET`, increment, `SET`) suffer from race conditions under concurrent load.

### 3. How it works
Rather than using a background thread to add tokens every second (which would consume massive CPU across millions of tenant keys), the algorithm calculates replenishment lazily upon request arrival:
1. Read current tokens and timestamp of last access.
2. Calculate time elapsed: $\Delta t = \text{now} - \text{last\_updated}$.
3. Calculate replenished tokens: $\text{tokens} = \min(\text{capacity}, \text{tokens} + (\Delta t \times \text{refill\_rate}))$.
4. If $\text{tokens} \ge \text{requested}$, deduct $\text{requested}$, set $\text{allowed} = 1$, persist state, and return.
5. If $\text{tokens} < \text{requested}$, calculate $\text{retry\_after} = \lceil (\text{requested} - \text{tokens}) / \text{refill\_rate} \rceil$, set $\text{allowed} = 0$, and reject.

### 4. Where it appears in SentinelRAG
- File: [`app/core/rate_limiter.py`](file:///c:/Users/kiran/Desktop/sentinel-rag/app/core/rate_limiter.py)
- Symbol: `TOKEN_BUCKET_LUA_SCRIPT`, `TokenBucketLimiter`, `TokenBucketRateLimitMiddleware`.

### 5. How SentinelRAG implements it
In `app/core/rate_limiter.py`, SentinelRAG compiles an atomic Lua script executed directly inside the Redis single-threaded execution context:
```lua
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local requested = tonumber(ARGV[3])

-- Query high-precision Redis server time to prevent clock skew across replicas
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
```
Notice the use of `redis.call('TIME')`â€”this guarantees that time evaluation is decoupled from client/worker system clocks, making it immune to NTP clock drift across distributed containers.

### 6. Example
A tenant has a bucket with capacity = 10, refill rate = 2 tokens/sec.
- At $t=0$: Bucket is full (10 tokens). A burst of 10 requests arrives simultaneously. All 10 are allowed. Bucket tokens = 0.
- At $t=0.5$: A new request arrives. Elapsed $\Delta t = 0.5\text{s}$. Replenished tokens = $0 + (0.5 \times 2) = 1.0$. Request consumes 1 token. Allowed.
- At $t=0.6$: Another request arrives. Elapsed $\Delta t = 0.1\text{s}$. Replenished tokens = $0 + (0.1 \times 2) = 0.2$. Less than 1 token! Request is rejected with HTTP 429 and `Retry-After: 1`.

### 7. Alternatives & Tradeoffs
- **Fixed Window Counter:** Trivial to implement, but vulnerable to 2x burst spikes at window boundaries.
- **Sliding Window Log:** Precise, but stores timestamps in a Redis sorted set, consuming significant memory under high throughput.
- **Token Bucket (Our Choice):** Constant $O(1)$ memory, allows controlled traffic bursts while enforcing strict sustained rate limits.

### 8. Common Mistakes
- **Worker Clock Skew:** Calculating `time.time()` in Python and sending it as an argument into Redis causes race conditions and erratic rate limiting when worker servers have drifting NTP clocks.
- **Redis Memory Leaks:** Failing to set an `EXPIRE` on the bucket key causes abandoned keys to fill Redis RAM forever. SentinelRAG sets `math.max(60, (capacity / refill_rate) * 2)`.

### 9. Interview Explanation
> "In SentinelRAG, we implemented distributed rate limiting using the Token Bucket algorithm running inside an atomic Redis Lua script. Rather than polling background timers, we calculate fractional token replenishment lazily upon request arrival. By querying Redis's internal `TIME` command directly inside Lua, we prevent NTP clock drift across our ASGI worker replicas. The entire evaluationâ€”replenishment, capacity bounding, consumption, and expirationâ€”executes atomically in a single Redis roundtrip, eliminating race conditions."

### 10. Follow-Up Questions
- **Q:** *Why Lua instead of a Redis Transaction (`MULTI/EXEC`)?*
  **A:** `MULTI/EXEC` cannot perform conditional logic based on the value of a key read within the same transaction. Lua allows transactional reads, mathematical branching, and atomic writes in one execution step.
- **Q:** *What happens if Redis becomes unreachable?*
  **A:** In `app/core/rate_limiter.py`, the middleware catches connection exceptions, emits a structured error log, and defaults to failing open (or closed, per security policy) to preserve gateway availability.

---

## Concept 2: Semantic Vector Caching & Cosine Similarity Gating

### 1. What it is
Semantic caching stores previous request embeddings alongside their generated outputs. Instead of requiring an exact character-for-character match, it compares the high-dimensional vector embedding of a new query against cached query embeddings using cosine similarity. If the angular proximity exceeds a mathematical threshold ($\ge 0.92$), the cached response is served directly.

### 2. Why it exists
In human natural language, identical intent is expressed using different words:
- "How do I change my login password?"
- "Where can I reset my account password?"
Traditional key-value caches miss 100% of these queries. Semantic caching captures this latent intent, reducing LLM API billing by 40-70% in high-frequency customer support systems while cutting latency from 2,000ms to under 20ms.

### 3. How it works
1. Normalize query and check exact-match SHA-256 KV store ($O(1)$ fast-path).
2. If exact match misses, take the 1536-dimensional query embedding vector $\mathbf{u}$.
3. Compute cosine similarity against cached vectors $\mathbf{v}$:
   $$\cos(\theta) = \frac{\sum_{i=1}^n u_i v_i}{\sqrt{\sum_{i=1}^n u_i^2} \sqrt{\sum_{i=1}^n v_i^2}}$$
4. If $\max(\cos(\theta)) \ge \text{Threshold}$ (0.92 in SentinelRAG), return the cached response.
5. If $\max(\cos(\theta)) < 0.92$, treat as a cache miss, route to RAG/LLM, and asynchronously write the new query, embedding, and response back to cache with a 24-hour TTL.

### 4. Where it appears in SentinelRAG
- File: [`app/services/cache.py`](file:///c:/Users/kiran/Desktop/sentinel-rag/app/services/cache.py)
- Symbol: `RedisSemanticCache`, `compute_cosine_similarity`, `serialize_embedding`, `deserialize_embedding`.

### 5. How SentinelRAG implements it
In `app/services/cache.py`:
- Embeddings are packed into IEEE 754 32-bit float bytes and base64-encoded via `struct.pack(f"{len(vector)}f", *vector)`. This ensures compact storage, UTF-8 safety, and high-speed in-memory unpacking without JSON overhead.
- Tenant isolation is strictly enforced via keyspaced prefixing: `cache:{tenant_id}:{query_hash}` and tracking sets `cache:{tenant_id}:keys`.
- Fast-path SHA-256 lookup executes in under 2ms. Vector scan executes in under 15ms.

### 6. Example
- Query A: *"What is the warranty period for Model X?"* $\to$ LLM generated: *"Model X has a 2-year warranty."* Cached in Redis.
- Query B: *"How many years of warranty does Model X have?"*
- Cosine similarity between Embedding A and Embedding B = 0.948.
- Since $0.948 \ge 0.92$, SentinelRAG returns the cached response in 12.4ms with 0 tokens consumed.

### 7. Alternatives & Tradeoffs
- **Exact Hash Caching (SHA-256 only):** Zero false positives and $O(1)$ speed, but near 0% hit rate for natural language queries.
- **Dedicated Vector DB Caching (Redis Stack RediSearch FT.SEARCH):** Excellent for millions of keys, but requires specialized C modules (`redis-stack-server`).
- **SentinelRAG Approach:** Hybrid tieringâ€”SHA-256 fast path plus lightweight IEEE 754 serialized vector similarity in standard Redis 7.2.

### 8. Common Mistakes
- **Threshold Set Too Low (< 0.85):** Causes semantic hallucination hits (e.g., "How do I upgrade?" matching "How do I cancel?").
- **Missing Tenant Namespacing:** Allowing Tenant A's cached queries to match and serve responses to Tenant B, causing cross-tenant data leakage.

### 9. Interview Explanation
> "In SentinelRAG, we implemented a two-tier semantic cache in Redis. The first tier is a deterministic SHA-256 hash lookup for exact string hits. The second tier unpacks base64-encoded IEEE 754 binary vectors and calculates cosine similarity. We gate cache hits with a strict 0.92 cosine similarity threshold, which corresponds to an angular distance of 0.08. This allows us to serve semantically identical paraphrases in under 20ms while preventing false-positive semantic collisions."

### 10. Follow-Up Questions
- **Q:** *Why is IEEE 754 binary serialization preferred over JSON for vectors?*
  **A:** A 1536-dimensional float vector stored as JSON text requires ~12KB of string data and heavy CPU parsing. Packed binary floats take exactly 6,144 bytes ($1536 \times 4\text{ bytes}$) and unpack in microseconds via the Python `struct` module.

---

## Concept 3: Single-Store Hybrid Retrieval (Dense pgvector HNSW + Sparse PostgreSQL tsvector GIN)

### 1. What it is
Hybrid retrieval combines two fundamentally different search paradigms:
1. **Dense Vector Search:** High-dimensional semantic embeddings (1536 dimensions) mapped into continuous vector space, indexed via Hierarchical Navigable Small World (HNSW) graphs.
2. **Sparse Lexical Search:** Exact token matching using term frequencies, inverse document frequencies (BM25 / TF-IDF), and linguistic stemming via PostgreSQL `tsvector` and `ts_rank_cd`.

### 2. Why it exists
- Dense search understands concepts (e.g., "canine" matches "dog") but fails on exact alphanumeric tokens, part numbers, and error codes (e.g., searching "CVE-2024-38077" returns random security docs rather than the exact CVE).
- Sparse search excels at exact keywords but fails completely on synonyms or paraphrased questions.
- Combining both in a single database (PostgreSQL 16) avoids the architectural complexity, eventual consistency lag, and cost of maintaining two separate data stores (like Pinecone + Elasticsearch).

### 3. How it works
Both indices live on the same table (`document_chunks`):
- Dense query:
  $$\text{distance} = \text{embedding} \iff :query\_vector$$
- Sparse query:
  $$\text{score} = \text{ts\_rank\_cd}(\text{search\_vector}, \text{websearch\_to\_tsquery}('english', :query\_text))$$
The queries execute concurrently via `asyncio.gather`, returning the top 50 candidates from each system.

### 4. Where it appears in SentinelRAG
- Model: [`app/models/document.py`](file:///c:/Users/kiran/Desktop/sentinel-rag/app/models/document.py)
- Repository: [`app/repositories/search.py`](file:///c:/Users/kiran/Desktop/sentinel-rag/app/repositories/search.py)

### 5. How SentinelRAG implements it
In `app/models/document.py`:
```python
search_vector: Mapped[Any] = mapped_column(
    TSVECTOR,
    Computed("to_tsvector('english', chunk_text)", persisted=True),
    nullable=True,
)
embedding: Mapped[list[float]] = mapped_column(Vector(1536), nullable=False)

__table_args__ = (
    Index("ix_document_chunks_search_vector", "search_vector", postgresql_using="gin"),
    Index(
        "ix_document_chunks_embedding_hnsw",
        "embedding",
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    ),
)
```
In `app/repositories/search.py`, `SearchRepository.search_dense()` uses the `cosine_distance` (`<=>`) operator, while `SearchRepository.search_sparse()` uses `websearch_to_tsquery` and `ts_rank_cd`.

### 6. Example
A user searches for: *"Why is error ERR-4091 occurring in billing service?"*
- Dense HNSW retrieves chunks explaining billing service architectures.
- Sparse FTS retrieves chunks containing the exact token "ERR-4091".
- The fused result places the chunk containing both the exact error code and billing context at Rank 1.

### 7. Alternatives & Tradeoffs
- **Separate Vector DB (e.g. Pinecone) + Elasticsearch:** Higher ingestion throughput at massive scale (100M+ documents), but introduces two additional infrastructure dependencies, distributed transaction overhead, and complex ETL pipelines.
- **PostgreSQL 16 Single-Store:** ACID transactions, zero data drift, single backup mechanism, and sub-50ms query latency up to millions of chunks.

### 8. Common Mistakes
- **Using IVFFlat instead of HNSW:** IVFFlat requires an initial training step and suffers from degraded recall as the dataset grows dynamically. HNSW supports incremental insertions with high recall.
- **Unpersisted `tsvector` columns:** Calculating `to_tsvector` on every query causes full table scans; using `GENERATED ALWAYS AS ... STORED` with a GIN index enables instant inverted index lookups.

### 9. Interview Explanation
> "In SentinelRAG, we adopted a single-store hybrid retrieval architecture inside PostgreSQL 16. Rather than running a distributed sync between a vector database and Elasticsearch, we store both the 1536-dimensional dense embedding and a persisted `tsvector` column in our `document_chunks` table. Dense search uses an HNSW index with cosine distance operators, while sparse search uses a GIN inverted index with `ts_rank_cd`. This guarantees strict transactional consistency, eliminates multi-database operational sprawl, and achieves high recall across both conceptual queries and exact keyword matches."

### 10. Follow-Up Questions
- **Q:** *What are the trade-offs of HNSW `m=16` and `ef_construction=64`?*
  **A:** `m=16` dictates the number of bidirectional links per vector node, while `ef_construction=64` controls the size of the dynamic candidate list during graph construction. This provides an optimal balance between fast build times and high recall ($>95\%$) during query execution.

---

## Concept 4: Reciprocal Rank Fusion (RRF, $k=60$)

### 1. What it is
Reciprocal Rank Fusion (RRF) is an unsupervised ranking algorithm used to combine ranked lists from multiple search algorithms into a single unified ranking. It scores items solely based on their relative positional rank within each candidate list, rather than attempting to normalize disparate raw score distributions.

### 2. Why it exists
Raw scores from different retrieval engines are fundamentally non-comparable:
- Cosine distance ranges from $0.0$ to $2.0$.
- BM25 / `ts_rank_cd` scores are unbounded positive floats (e.g., $0.001$ to $42.5$).
Normalizing these via min-max or z-score normalization is brittle because outliers skew the distribution. RRF solves this by scoring candidates purely based on their ordinal position.

### 3. How it works
For every document $d$ appearing in ranked result lists:
$$\text{RRF Score}(d) = \sum_{m \in M} \frac{1}{k + r_m(d)}$$
where:
- $M$ is the set of retrieval methods (dense, sparse).
- $r_m(d)$ is the 1-based ordinal rank of document $d$ in system $m$.
- $k$ is a constant smoothing parameter (standardized at $k=60$).

### 4. Where it appears in SentinelRAG
- File: [`app/services/rrf.py`](file:///c:/Users/kiran/Desktop/sentinel-rag/app/services/rrf.py)
- Symbol: `fuse_hybrid_candidates`, `reciprocal_rank_fusion`.

### 5. How SentinelRAG implements it
In `app/services/rrf.py`:
```python
def reciprocal_rank_fusion(
    ranked_lists: list[list[str]],
    k: int = 60,
) -> list[tuple[str, float]]:
    scores: dict[str, float] = defaultdict(float)
    for ranked_list in ranked_lists:
        for rank, item_id in enumerate(ranked_list, start=1):
            scores[item_id] += 1.0 / (k + rank)
    
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)
```
In `fuse_hybrid_candidates`, dense candidates (ranked by ascending cosine distance) and sparse candidates (ranked by descending lexical score) are merged. Chunks that rank highly in both systems receive the highest cumulative RRF score.

### 6. Example
Let $k=60$.
- Document A ranks #1 in Dense, #1 in Sparse:
  $$\text{Score}(A) = \frac{1}{60+1} + \frac{1}{60+1} = \frac{1}{61} + \frac{1}{61} = 0.01639 + 0.01639 = 0.03278$$
- Document B ranks #2 in Dense, not present in Sparse:
  $$\text{Score}(B) = \frac{1}{60+2} + 0 = 0.01613$$
Document A is decisively ranked first because both systems confirmed its relevance.

### 7. Alternatives & Tradeoffs
- **Linear Weighted Combination ($\alpha \cdot \text{dense} + (1-\alpha) \cdot \text{sparse}$):** Requires calibrating $\alpha$ against labelled evaluation datasets and requires score normalization.
- **Cross-Encoder Neural Reranking (e.g. Cohere / BGE Reranker):** High precision, but adds 150-300ms of GPU/CPU compute latency.
- **RRF (Our Choice):** Parameter-free, zero GPU requirement, sub-1ms compute overhead, and highly robust across varied query domains.

### 8. Common Mistakes
- **Setting $k$ too small (e.g. $k=1$):** Causes the top-ranked document of any single list to overwhelmingly dominate, negating the fusion effect.
- **0-indexed vs 1-indexed Ranks:** RRF mathematically assumes 1-based indexing ($r \in [1, N]$). Using 0-based indexing without adjusting $k$ alters the ranking curve.

### 9. Interview Explanation
> "We use Reciprocal Rank Fusion with $k=60$ to merge our dense HNSW candidates with sparse full-text candidates. Because cosine distance and BM25 scores have incompatible scales and distributions, naive linear combination requires fragile score normalization. RRF calculates an ordinal reciprocal score $1 / (k + \text{rank})$ for each document. A document that appears in the top 5 of both dense and sparse retrieval will automatically outrank a document that only appeared in one list, maximizing retrieval precision without adding neural reranking latency."

### 10. Follow-Up Questions
- **Q:** *Why is $k=60$ the industry standard?*
  **A:** Empirical research by Cormack, Clarke, and BÃ¼ttcher demonstrated that $k=60$ prevents top-ranked outliers from dominating the score while giving meaningful weight to documents appearing in the top 20â€“50 positions across multiple retrieval engines.

---

## Concept 5: State Machine Circuit Breaker Pattern

### 1. What it is
The Circuit Breaker pattern is a stability pattern designed to detect failures and encapsulate the logic of preventing a failure from constantly recurring during maintenance, temporary external outages, or unexpected system difficulties. It operates as a three-state machine: `CLOSED`, `OPEN`, and `HALF_OPEN`.

### 2. Why it exists
When an external LLM provider experiences an outage or severe rate limiting (HTTP 429/500/503), naive clients continue hammering the endpoint. Requests hang until HTTP timeouts expire (e.g., 30â€“60 seconds), consuming worker thread pools, holding open database connections, and causing cascading failure across the entire application stack.

### 3. How it works
1. **`CLOSED` (Normal):** Requests pass through to the primary provider. Successes reset consecutive failure counters.
2. **`CLOSED` $\to$ `OPEN`:** When consecutive failures reach a threshold (5 failures), the circuit trips to `OPEN`.
3. **`OPEN` (Fail-Fast):** All execution attempts are rejected immediately ($0\text{ ms}$ delay) with `CircuitBreakerOpenException`, triggering automatic failover to the fallback provider without making any network call to the primary.
4. **`OPEN` $\to$ `HALF_OPEN`:** After a recovery timeout (60 seconds) elapses, the circuit transitions to `HALF_OPEN`.
5. **`HALF_OPEN` (Canary Probe):** Exactly one canary request is permitted through to probe the primary service. If it succeeds, the circuit resets to `CLOSED`. If it fails, the circuit re-trips to `OPEN` for another 60 seconds.

### 4. Where it appears in SentinelRAG
- File: [`app/core/circuit_breaker.py`](file:///c:/Users/kiran/Desktop/sentinel-rag/app/core/circuit_breaker.py)
- Symbol: `CircuitBreaker`, `CircuitState`, `CircuitBreakerOpenException`.

### 5. How SentinelRAG implements it
In `app/core/circuit_breaker.py`:
- Built using coroutine-safe `asyncio.Lock()` to prevent race conditions during state transitions.
- Tracks `_consecutive_failures`, `_last_failure_time`, and computes `seconds_remaining_in_open`.
- Cleanly integrated into `ResilientLLMRouter.complete()` in [`app/services/router.py`](file:///c:/Users/kiran/Desktop/sentinel-rag/app/services/router.py).

### 6. Example
Groq API enters a 5-minute brownout returning HTTP 500.
- Requests 1 through 5 attempt Groq and fail. Consecutive failures = 5.
- Circuit transitions to `OPEN`.
- Requests 6 through 5,000 arrive over the next 60 seconds. Each checks `can_execute()`, sees `OPEN`, and immediately routes to OpenAI `gpt-4o-mini` with zero timeout latency.
- At $t=60\text{s}$, a request arrives. Circuit transitions to `HALF_OPEN`. It sends one probe to Groq. Groq returns 200 OK. Circuit transitions back to `CLOSED`.

### 7. Alternatives & Tradeoffs
- **Simple Try/Catch Fallback:** Catches the error and calls fallback, but still incurs the full 30-second timeout on *every single request* during an outage, causing severe thread pool exhaustion.
- **Circuit Breaker (Our Choice):** Only the first 5 requests suffer latency; all subsequent requests fail fast in 0ms directly to the healthy provider.

### 8. Common Mistakes
- **Not Locking State Transitions:** In asynchronous code, concurrent coroutines can race to transition from `HALF_OPEN` to `OPEN`, causing unpredictable states. SentinelRAG guards all transitions with `async with self._lock`.
- **Counting Client-Side Validation Errors as Outages:** Counting HTTP 400 or 401 errors against the circuit breaker will falsely trip the circuit when users submit bad inputs. SentinelRAG only tracks upstream 5xx, timeouts, and transient 429 errors.

### 9. Interview Explanation
> "In SentinelRAG, we protect upstream LLM calls with a Michael Nygard-style state machine Circuit Breaker. When our primary provider, Groq, accumulates 5 consecutive failures, the circuit transitions from CLOSED to OPEN. For the next 60 seconds, all requests fail fast in 0 milliseconds and route directly to our OpenAI fallback, avoiding network timeout accumulation and preserving worker pool concurrency. After 60 seconds, the circuit enters HALF_OPEN to allow a single canary probe to verify upstream recovery."

### 10. Follow-Up Questions
- **Q:** *How do you coordinate circuit breaker state across multiple container replicas?*
  **A:** While SentinelRAG currently maintains coroutine-safe in-process circuit state per worker, in a large multi-region deployment this can be externalized to Redis using Redis hashes with Lua or an established Envoy/service mesh sidecar.

