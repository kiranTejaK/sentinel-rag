# PART 1 — Project Reconnaissance

> **What this document is:** A complete technical map of the SentinelRack codebase — written as
> if you are an engineer who just joined a team and needs to understand the whole system before
> touching any code. Read this before Day 1. Re-read it after Day 7. The second read will feel
> completely different.

---

## 1. What SentinelRack Actually Is

SentinelRack is an **AI middleware gateway** — a service that sits between your application and
external AI providers (OpenAI, Groq, Anthropic) and does four things on every single request:

1. **Rate limiting** — prevents any one client from flooding the system
2. **Semantic caching** — recognises when two questions mean the same thing, returns a stored
   answer instantly rather than calling an expensive AI API
3. **Hybrid document retrieval** — fetches the most relevant context from your private document
   store before asking the AI, so the AI answers from your data
4. **Resilient AI routing** — if your primary AI provider fails, automatically switches to a
   backup with zero delay for the end user

The real-world analogy: SentinelRack is like a smart, security-conscious receptionist at the
front desk of an AI company. They check who you are, make sure you are not spamming, look up the
answer in the filing cabinet before bothering the expert, and if the expert is sick, they
immediately route you to the backup expert. All of this happens before you as a user notice anything.

---

## 2. Full Architecture Map

```
EXTERNAL WORLD (Untrusted)
  |
  v  HTTPS port 443
NGINX REVERSE PROXY
  - Terminates TLS
  - Enforces 10 MB body limit
  - IP-level rate limit (30 req/s first wall)
  - Security headers: HSTS, CSP, X-Frame-Options
  Files: nginx/nginx.conf, nginx/conf.d/sentinel.conf
  |
  v  HTTP proxy to port 8000
FASTAPI APPLICATION (Uvicorn ASGI)
  |
  +-- MIDDLEWARE PIPELINE (runs on every request, in order)
  |     1. CORS Middleware
  |     2. TokenBucketRateLimitMiddleware  (Redis Lua, per-tenant)
  |     3. correlation_id_and_timing_middleware (X-Request-Id, Prometheus)
  |
  +-- API ROUTES v1
  |     POST /v1/chat/completions      main orchestration
  |     POST /v1/documents/ingest      batch document ingestion
  |     POST /v1/search/hybrid         standalone search
  |     POST /v1/cache/evaluate        inspect a cache entry
  |     DELETE /v1/cache/invalidate    flush a tenant's cache
  |     GET  /healthz                  liveness + readiness probe
  |     GET  /metrics                  Prometheus scrape endpoint
  |
  +-- DOMAIN SERVICES
  |     RedisSemanticCache     SHA-256 + cosine similarity lookup
  |     ResilientLLMRouter     multi-provider routing + failover
  |     CircuitBreaker         CLOSED / OPEN / HALF-OPEN FSM
  |     execute_with_resilience Tenacity retry wrapper
  |     fuse_hybrid_candidates  RRF rank fusion
  |     TokenAnalyticsTracker   token count + USD cost tracking
  |
  +-- DATA ACCESS
        SearchRepository       dense (pgvector) + sparse (FTS) SQL
        DocumentChunk model    ORM entity with vector + tsvector
  |
  +-------------------+
  |                   |
  v                   v
PostgreSQL 16       Redis 7.2
+ pgvector          - Semantic cache  cache:{tenant}:{hash}
  document_chunks   - Rate-limit buckets ratelimit:tenant:{id}
  HNSW vector idx   - Token analytics counters
  GIN full-text idx
  GIN JSONB idx
                    |
                    v (outbound only)
                 External LLM Providers
                   Groq   (primary)
                   OpenAI (fallback 1)
                   Mistral (fallback 2)
```

---

## 3. Component-by-Component Responsibility

### 3.1 Configuration (app/core/config.py)

**What it does:** Reads environment variables and validates them into a typed Python object.

**Why it matters:** Every module that needs a database URL, API key, or threshold imports the
`settings` singleton from this file. Nothing ever reads `os.environ` directly elsewhere. This
single design choice prevents "works on my machine" bugs where a missing env var silently becomes
`None` and crashes somewhere deep in a service at 2am.

Key abstractions:
- `Settings(BaseSettings)`: Pydantic class that auto-reads from `.env` and system env
- `settings = Settings()`: module-level singleton — loaded once, shared everywhere

---

### 3.2 Application Entrypoint (app/main.py)

**What it does:** Creates the FastAPI application, attaches middleware and routes, and manages
startup/shutdown of database connections.

**Why it matters:** In an async application you cannot connect to PostgreSQL or Redis in a
module-level `__init__` because the async event loop is not running yet. The `lifespan` context
manager is the correct place to open and close these connections.

Key abstractions:
- `lifespan(app)`: asynccontextmanager function that runs on startup (opens DB/Redis pools,
  runs health pings) and on shutdown (closes them cleanly)
- `correlation_id_and_timing_middleware`: generates a request ID, binds it to all logs for
  that request, records how long the request took

---

### 3.3 Database Connection (app/core/database.py)

**What it does:** Creates and holds a SQLAlchemy async engine backed by asyncpg.

**Why it matters:** Database connections are expensive to open. A connection pool opens N
connections once at startup and reuses them. Without pooling every API request would spend
~50ms just opening a TCP connection to PostgreSQL.

Key abstractions:
- `DatabaseManager`: owns the AsyncEngine and the async_sessionmaker
- `db_manager.ping()`: used in /healthz to verify the DB is alive
- `get_async_session()` in app/db/session.py: FastAPI dependency that provides a live
  AsyncSession to route handlers, commits on success, rolls back on error

---

### 3.4 Redis Connection (app/core/redis.py)

**What it does:** Creates and holds an aioredis connection pool.

Key abstractions:
- `RedisManager`: owns the aioredis.ConnectionPool
- `get_redis_client()`: FastAPI dependency injecting a live Redis client into route handlers

---

### 3.5 Rate Limiter (app/core/rate_limiter.py)

**What it does:** Implements the Token Bucket algorithm in a Redis Lua script. Attached as ASGI
middleware so it runs before any route handler.

**Why the middleware layer matters:** The rate limiter can short-circuit a request with HTTP 429
in under 1ms — before Pydantic validation, before DB sessions are opened, before anything
expensive runs.

Key abstractions:
- `TOKEN_BUCKET_LUA_SCRIPT`: Lua code that executes atomically inside Redis — reads current
  tokens, calculates replenishment based on elapsed time, decides allow/deny, writes back state
- `TokenBucketLimiter`: Python class that loads the Lua script via SCRIPT LOAD and calls it
  via EVALSHA
- `TokenBucketRateLimitMiddleware`: Starlette BaseHTTPMiddleware that extracts a tenant ID,
  calls the limiter, and either continues or returns HTTP 429

---

### 3.6 Document Model (app/models/document.py)

**What it does:** Defines the DocumentChunk SQLAlchemy ORM model — a row in document_chunks table.

Key columns:
- `embedding`: Vector(1536) — the pgvector column holding the 1536-float dense embedding
- `search_vector`: TSVECTOR GENERATED ALWAYS AS ... STORED — PostgreSQL automatically maintains
  this column for full-text search with no application code needed
- Both columns have specialized indexes: HNSW for vectors, GIN for full-text

---

### 3.7 Search Repository (app/repositories/search.py)

**What it does:** Executes the actual database queries for dense vector and sparse lexical search.

Key methods:
- `search_dense(tenant_id, query_vector, limit)`: queries using `embedding <=> :query_vector`
  (cosine distance, ascending — lower distance = more similar)
- `search_sparse(tenant_id, query_text, limit)`: queries using `search_vector @@
  websearch_to_tsquery('english', :query)` ranked by `ts_rank_cd`

---

### 3.8 RRF Service (app/services/rrf.py)

**What it does:** Merges the dense and sparse candidate lists into a single ranked list using
Reciprocal Rank Fusion.

**The formula:** Each document gets score = 1 / (60 + its_rank_in_this_list) for each list
it appears in. Scores are summed. Higher total = more relevant.

**Why 60?** It is the empirically validated smoothing constant from the original RRF research
paper (Cormack, Clarke, Buttcher 2009). It prevents the top-ranked result in a single list
from completely dominating when the other list disagrees.

---

### 3.9 Semantic Cache (app/services/cache.py)

**What it does:** Stores query-response pairs in Redis and retrieves them on semantically
similar future queries.

The two-tier lookup:
1. SHA-256 hash of the normalized query -> exact key lookup (O(1), < 2ms)
2. If miss: compute cosine similarity between query embedding and all cached embeddings for
   this tenant -> return if similarity >= 0.92 (distance <= 0.08)

**Why 0.92?** Queries like "How do I reset my password?" and "How can I change my account
password?" score above this threshold and are treated as equivalent.

---

### 3.10 Circuit Breaker (app/core/circuit_breaker.py)

**What it does:** Implements the Michael Nygard Circuit Breaker pattern as a finite state machine.

**Why it exists:** Without a circuit breaker, when Groq is down, every request waits for a
30-second socket timeout before falling back to OpenAI. At 100 req/s that is 3,000 blocked
async workers. With the circuit breaker, the first 5 failures are absorbed; after that all
requests fail-fast in 0ms.

State transitions:
- CLOSED -> OPEN: 5 consecutive failures
- OPEN -> HALF_OPEN: 60 seconds elapsed
- HALF_OPEN -> CLOSED: canary probe succeeds
- HALF_OPEN -> OPEN: canary probe fails

---

### 3.11 Resilience Engine (app/services/resilience.py)

**What it does:** Wraps any async function call in a Tenacity retry loop with exponential
backoff and full jitter.

**Why jitter?** Without randomization, all 1,000 clients that failed at the same time retry
at the same time, causing a thundering herd that crashes the recovering service again.

---

### 3.12 LLM Router (app/services/router.py)

**What it does:** Orchestrates multi-provider LLM execution. Checks the circuit breaker, calls
the primary provider with retries, falls back to secondary providers on failure, tracks token
counts and costs.

Provider hierarchy:
1. Groq llama-3.3-70b-versatile — lowest latency
2. OpenAI gpt-4o-mini — first fallback
3. Mistral mistral-small-latest — last resort

---

### 3.13 Token Analytics (app/services/token_analytics.py)

**What it does:** Counts tokens using tiktoken (the same tokenizer OpenAI uses), calculates
USD cost using a per-model pricing table, and accumulates these in Redis counters.

**Why tiktoken?** LLM providers charge by the token. Accurate cost tracking requires the exact
token count, not an approximation.

---

### 3.14 Chat Completions Endpoint (app/api/v1/chat.py)

**What it does:** The main orchestration point. Runs stages in sequence:
1. Semantic cache check (sub-20ms exit on hit)
2. Hybrid retrieval via SearchRepository + fuse_hybrid_candidates
3. Prompt construction with retrieved context
4. Resilient LLM routing via ResilientLLMRouter
5. Async cache write + Prometheus metric recording

This file is the conductor — it owns no logic itself, only coordinates all the services.

---

### 3.15 Observability (app/core/telemetry.py, app/core/logging.py)

Telemetry: Four Prometheus metrics:
- HTTP_REQUEST_DURATION_SECONDS: histogram of request latencies by endpoint
- CACHE_OPERATIONS_TOTAL: counter of cache hits vs misses by tenant
- TOKENS_CONSUMED_TOTAL: counter of tokens consumed by model and tenant
- COST_SAVED_USD_TOTAL: counter of dollars saved by cache hits

Logging: structlog configured to emit single-line JSON objects. Every log line automatically
includes request_id, tenant_id, route because they are bound to the request context via Python
contextvars.

---

## 4. Request Lifecycle — The Full Movie

What happens when a client sends POST /v1/chat/completions:

```
1. Nginx receives the HTTPS request
   -> checks body size (max 10MB)
   -> applies IP-level rate limit
   -> terminates TLS, forwards to Uvicorn port 8000 as plain HTTP

2. Uvicorn receives the request
   -> passes to FastAPI ASGI pipeline

3. CORS Middleware runs
   -> adds Access-Control headers

4. TokenBucketRateLimitMiddleware runs
   -> extracts X-API-Key header (or client IP as fallback)
   -> runs Lua script in Redis: reads tokens, replenishes, deducts
   -> if depleted: returns HTTP 429 immediately (< 1ms total)
   -> if allowed: sets X-RateLimit headers, continues

5. correlation_id_and_timing_middleware runs
   -> generates X-Request-Id
   -> binds request_id, route, method to structlog contextvars
   -> starts a high-precision timer

6. FastAPI parses and validates the JSON body
   -> ChatCompletionRequest Pydantic model validates all fields

7. chat_completions() handler executes:

   Stage 1 — Cache Check:
   -> extracts user query text from messages list
   -> calls RedisSemanticCache.get_or_lookup()
     -> Tier 1: SHA-256 hash lookup (< 2ms)
     -> Tier 2: cosine similarity scan over tenant cached embeddings
   -> if HIT: builds response, increments Prometheus counters, RETURNS (~15ms total)

   Stage 2 — Hybrid Retrieval (only if use_retrieval=True):
   -> SearchRepository.search_dense() and search_sparse() run sequentially on same session
   -> fuse_hybrid_candidates() merges them via RRF
   -> top 5 chunks become context for the prompt

   Stage 3 — LLM Routing:
   -> ResilientLLMRouter.complete() is called
   -> circuit_breaker.can_execute() checked
     -> if OPEN: skip to fallback immediately
     -> if CLOSED or HALF_OPEN: try primary (Groq) with Tenacity retries
   -> on primary success: record_success(), compute cost, return
   -> on primary failure: record_failure(), try fallback (OpenAI), then Mistral

   Stage 4 — Post-Processing:
   -> write new entry to Redis semantic cache (async, non-blocking)
   -> increment Prometheus counters for tokens, cost, latency
   -> return ChatCompletionResponse with telemetry headers

8. correlation_id_and_timing_middleware resumes
   -> records total duration to Prometheus histogram
   -> adds X-Sentinel-Latency-Ms header

9. Response returned to Nginx, then to client
```

---

## 5. Core Abstractions Inventory

| Abstraction | Pattern | File |
|---|---|---|
| Configuration | Pydantic BaseSettings singleton | app/core/config.py |
| App lifecycle | FastAPI lifespan / asynccontextmanager | app/main.py |
| Rate limiting | Token Bucket via atomic Redis Lua | app/core/rate_limiter.py |
| Semantic cache | SHA-256 + cosine similarity gating | app/services/cache.py |
| Vector search | SQLAlchemy + pgvector cosine distance | app/repositories/search.py |
| Text search | PostgreSQL tsvector + ts_rank_cd | app/repositories/search.py |
| Rank fusion | Reciprocal Rank Fusion k=60 | app/services/rrf.py |
| Provider routing | LiteLLM + strategy pattern | app/services/router.py |
| Fault tolerance | Circuit Breaker FSM + asyncio.Lock | app/core/circuit_breaker.py |
| Retry logic | Tenacity with exponential + jitter | app/services/resilience.py |
| Token counting | tiktoken BPE tokenizer | app/services/token_analytics.py |
| Structured logging | structlog + contextvars | app/core/logging.py |
| Metrics | prometheus_client Histogram/Counter | app/core/telemetry.py |
| DB sessions | SQLAlchemy async sessionmaker | app/db/session.py |
| Dependency injection | FastAPI Depends() | all route files |

---

## 6. Why This Build Order Is Optimal

```
Day 1: Foundation (config, logging, connections, healthz)
  -- everything else imports from Day 1
Day 2: Persistence (ORM model, migrations, session, ingestion API)
  -- search depends on the model and session
Day 3: Retrieval (search repository + RRF + search endpoint)
  -- the cache uses cosine math; the router needs search results
Day 4: Cache + Rate Limiting (Redis semantic cache + Lua rate limiter)
  -- the LLM router reads cached responses first
Day 5: LLM Router (circuit breaker + retry + fallback + token analytics)
  -- the chat endpoint orchestrates all of the above
Day 6: Chat Endpoint + Observability (unified pipeline + Prometheus + dashboard)
  -- once the app works, harden the infrastructure
Day 7: Production Hardening (Docker, Nginx, CI/CD)
```

This order is optimal because:
- Risk reduction: each day can be tested independently before adding the next layer
- Natural dependencies: you cannot write the LLM router without a cache to check first
- Progressive complexity: async, pooling, ORM, vector math, state machines introduced in order
- Runnable at every stage: /healthz works after Day 1, ingestion after Day 2, search after Day 3

---

## 7. Where the Existing Daily Logs Diverge from Reality

### Day 3 and Day 4 — Deepest divergence (spend the most study time here)

**Day 3 logs say:** "hybrid search pipeline — embed query, dense search, sparse search"

**What the code actually does:**
- DenseCandidate and SparseCandidate are typed dataclasses with rank, distance, similarity fields
- search_hybrid_candidates() runs dense and sparse sequentially on the same session
  (not asyncio.gather — socket collision is prevented by sharing the session)
- websearch_to_tsquery handles multi-word queries, boolean operators, phrase matching
  the log just says "full-text search"
- cosine_distance relationship: similarity = max(0.0, 1.0 - distance), clipped at 0.0
- RRF edge cases: empty lists, documents in only one list, tied RRF scores

**Day 4 logs say:** "added semantic cache with Redis" and "added rate limiter"

**What the code actually does:**
- Cache uses TWO completely different mechanisms: SHA-256 exact-match first, then cosine scan
  the log only mentions vector similarity
- Embeddings are serialized as IEEE 754 binary (not JSON) using struct.pack/unpack + base64
  the log never mentions this
- Rate limiter uses a Redis Lua script that runs atomically inside Redis single-threaded engine
- The Lua script uses redis.call('TIME') to avoid NTP clock skew across containers
- Bucket state is lazily calculated (tokens replenish on each request, not on a background timer)
- Cache keys are tenant-namespaced (cache:{tenant_id}:{hash}) to prevent cross-tenant leakage

### Days 1, 2, 5, 6, 7 — Minor but notable gaps

- Day 1: The log does not mention the pre-flight health ping during lifespan startup
  (catches misconfigured DATABASE_URL before the first real request)
- Day 2: The log does not explain that search_vector is a computed column
  (PostgreSQL updates it automatically when chunk_text changes)
- Day 5: The log says "circuit breaker and fallback" without explaining HALF_OPEN
  (the canary probe mechanism that safely tests whether the primary is recovering)
- Day 6: Does not explain that Prometheus scrapes /metrics at a configurable interval,
  or that the Streamlit dashboard reads from Redis (not directly from PostgreSQL)
- Day 7: Does not explain multi-stage Docker build, or why the app runs as UID 10001

---

## 8. Technology List with First-Use Context

| Technology | Role | First appears |
|---|---|---|
| FastAPI | Async web framework | Day 1 |
| Uvicorn | ASGI server that runs FastAPI | Day 1 |
| Pydantic v2 | Settings validation + request/response schemas | Day 1 |
| structlog | Structured JSON logging | Day 1 |
| asyncpg | Async PostgreSQL driver | Day 1 |
| SQLAlchemy 2 async | ORM + query builder over asyncpg | Day 2 |
| Alembic | Database schema migration tool | Day 2 |
| pgvector | PostgreSQL extension for vector math | Day 2 |
| aioredis | Async Redis client | Day 1 (manager) / Day 4 (heavily used) |
| Redis Lua scripting | Atomic server-side scripting | Day 4 |
| LiteLLM | Unified interface to multiple LLM providers | Day 5 |
| Tenacity | Retry library with backoff strategies | Day 5 |
| tiktoken | OpenAI BPE tokenizer | Day 5 |
| prometheus_client | Prometheus metrics exposition | Day 6 |
| Streamlit | Interactive Python dashboard | Day 6 |
| Docker | Container packaging | Day 7 |
| Nginx | Reverse proxy and TLS terminator | Day 7 |
| GitHub Actions | CI/CD pipeline | Day 7 |

---

*End of Part 1. Proceed to Day 1.*
