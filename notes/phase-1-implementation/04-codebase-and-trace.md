# 04. Codebase File-by-File Technical Guide

This guide systematically dissects every source module, configuration file, and infrastructure script in SentinelRAG.

---

## 1. Application Entrypoint & Ingress Tier

### Module: `app/main.py`
- **File Path:** [`app/main.py`](file:///c:/Users/kiran/Desktop/sentinel-rag/app/main.py)
- **Purpose & Responsibility:** ASGI application factory, lifespan management (database/Redis pool initialization and pre-flight health checks), middleware registration (rate limiting, timing, correlation, CORS), global exception handling, and core diagnostics endpoints (`/healthz`, `/metrics`, `/`).
- **Why It Exists:** Serves as the central nervous system binding all decoupled services, middlewares, and route controllers into a unified Uvicorn-executable ASGI instance.
- **Key Imports:**
  - `FastAPI`, `Request`, `Response`: Web framework core primitives.
  - `lifespan`: Context manager managing application startup and graceful shutdown.
  - `TokenBucketRateLimitMiddleware`: Ingress rate limiter.
  - `db_manager`, `redis_manager`: Subsystem connection managers.
  - `HTTP_REQUEST_DURATION_SECONDS`, `metrics_response`: Prometheus instrumentation.
- **Key Symbols & Functions:**
  - `lifespan(app: FastAPI)`: Sets up structlog, opens asyncpg and aioredis pools, executes pre-warm pings, and cleanly terminates pools upon SIGTERM/SIGINT.
  - `correlation_id_and_timing_middleware(request, call_next)`: Extracts or generates `X-Request-Id` (UUIDv4), binds `request_id`, `route`, and `method` to structlog `contextvars`, measures nanosecond-precision execution latency via `time.perf_counter()`, records the Prometheus duration histogram, and appends `X-Sentinel-Latency-Ms` and `X-Request-Id` headers.
  - `health_check()` (`/healthz`): Runs asynchronous concurrent pings against PostgreSQL and Redis, returning HTTP 200 with latency measurements or HTTP 503 if any subsystem is degraded.
  - `get_metrics()` (`/metrics`): Returns raw Prometheus text format telemetry.
- **Dependencies & Callers:** Initialized directly by Uvicorn. Calls `db_manager`, `redis_manager`, `api_v1_router`, and `telemetry`.
- **Security Implications:** Strips docs endpoints (`/docs`, `/redoc`) in production mode (`settings.ENVIRONMENT == "production"`). Enforces strict CORS headers and header exposure limits.
- **Validated By:** [`tests/test_healthz.py`](file:///c:/Users/kiran/Desktop/sentinel-rag/tests/test_healthz.py), [`tests/test_telemetry_and_e2e.py`](file:///c:/Users/kiran/Desktop/sentinel-rag/tests/test_telemetry_and_e2e.py).
- **Related Daily Logs:** Day 1 (ASGI setup & healthz), Day 6 (Telemetry middleware).
- **Interview Answer:**
  > "`app/main.py` orchestrates the ASGI application lifespan. During startup, it pre-warms our asyncpg and Redis connection pools with health checks before accepting ingress traffic. It registers our atomic token-bucket middleware and a timing middleware that binds correlation IDs to structlog contextvars, ensuring that every downstream log across all async coroutines carries the same request ID."

---

### Module: `app/core/config.py`
- **File Path:** [`app/core/config.py`](file:///c:/Users/kiran/Desktop/sentinel-rag/app/core/config.py)
- **Purpose & Responsibility:** Type-safe configuration management and environment variable validation using Pydantic v2 `BaseSettings`.
- **Why It Exists:** Eliminates runtime crashes caused by missing or malformed environment variables. Loads `.env` or system environment variables with strict typing.
- **Key Symbols:**
  - `Settings(BaseSettings)`:
    - Networking: `HOST`, `PORT`, `ENVIRONMENT`, `LOG_LEVEL`.
    - PostgreSQL: `POSTGRES_SERVER`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, `POSTGRES_PORT`, `SQLALCHEMY_DATABASE_URI`.
    - Redis: `REDIS_HOST`, `REDIS_PORT`, `REDIS_PASSWORD`, `REDIS_DB`, `REDIS_URL`.
    - Cache & Rate Limiting: `SEMANTIC_CACHE_THRESHOLD` (0.08 distance), `SEMANTIC_CACHE_TTL_SECONDS` (86400s), `RATE_LIMIT_DEFAULT_CAPACITY` (60), `RATE_LIMIT_DEFAULT_REFILL_RATE_PER_SEC` (1.0).
    - LLM Mesh: `GROQ_API_KEY`, `OPENAI_API_KEY`, `DEFAULT_PRIMARY_MODEL`, `DEFAULT_FALLBACK_MODEL_1`, `DEFAULT_FALLBACK_MODEL_2`.
    - Circuit Breaker: `CIRCUIT_BREAKER_FAILURE_THRESHOLD` (5), `CIRCUIT_BREAKER_RECOVERY_TIME_SECONDS` (60).
  - `settings = Settings()`: Singleton instantiated at import time.
- **Security Implications:** Sensitive secrets (passwords, API keys) are masked in string representations and never logged in plain text.
- **Validated By:** Loaded by all test suites and modules.

---

### Module: `app/core/rate_limiter.py`
- **File Path:** [`app/core/rate_limiter.py`](file:///c:/Users/kiran/Desktop/sentinel-rag/app/core/rate_limiter.py)
- **Purpose & Responsibility:** Multi-tenant distributed rate limiting using atomic Redis Lua scripts.
- **Why It Exists:** Guarantees zero race conditions across multi-process ASGI workers without database locks or NTP clock drift vulnerabilities.
- **Key Symbols:**
  - `TOKEN_BUCKET_LUA_SCRIPT`: Embedded Lua script executing `redis.call('TIME')`, fractional token replenishment, token deduction, and TTL setting.
  - `RateLimitResult`: Dataclass holding `allowed`, `remaining`, `retry_after`, and `limit`.
  - `TokenBucketLimiter`: Manages script compilation (SHA-1 hashing via `SCRIPT LOAD`) and evaluation via `EVALSHA`.
  - `TokenBucketRateLimitMiddleware`: Starlette `BaseHTTPMiddleware` that extracts tenant ID (or IP), queries limiter, and returns HTTP 429 with RFC-standard `Retry-After` header when depleted.
- **Validated By:** [`tests/test_rate_limiter.py`](file:///c:/Users/kiran/Desktop/sentinel-rag/tests/test_rate_limiter.py).
- **Interview Answer:**
  > "We execute our token-bucket algorithm inside an atomic Redis Lua script using Redis's internal `TIME` command. This ensures atomic check-and-decrement in a single network roundtrip while making token replenishment completely immune to clock drift across our distributed worker containers."

---

### Module: `app/core/circuit_breaker.py`
- **File Path:** [`app/core/circuit_breaker.py`](file:///c:/Users/kiran/Desktop/sentinel-rag/app/core/circuit_breaker.py)
- **Purpose & Responsibility:** State-machine circuit breaker protecting external LLM provider calls.
- **Why It Exists:** Prevents cascading thread/coroutine pool exhaustion during upstream provider outages by failing fast in 0ms.
- **Key Symbols:**
  - `CircuitState(StrEnum)`: `CLOSED`, `OPEN`, `HALF_OPEN`.
  - `CircuitBreakerOpenException`: Exception raised when execution is denied.
  - `CircuitBreaker`: Coroutine-safe state machine managing transitions via `asyncio.Lock()`.
    - `can_execute()`: Evaluates state and transitions `OPEN` to `HALF_OPEN` when `recovery_timeout` has elapsed.
    - `record_success()`: Resets consecutive failure counters and transitions `HALF_OPEN` to `CLOSED`.
    - `record_failure()`: Increments failure counter and transitions `CLOSED` or `HALF_OPEN` to `OPEN` if threshold is reached.
- **Validated By:** [`tests/test_circuit_breaker.py`](file:///c:/Users/kiran/Desktop/sentinel-rag/tests/test_circuit_breaker.py).
- **Interview Answer:**
  > "Our Circuit Breaker implements Michael Nygard's three-state model using an asyncio Lock. When an upstream provider accumulates 5 consecutive failures, it trips to OPEN for 60 seconds, allowing all subsequent calls to fail fast in 0ms to our fallback provider. When the timeout expires, it enters HALF_OPEN to allow a single canary probe."

---

## 2. Persistence & Data Access Tier

### Module: `app/models/document.py`
- **File Path:** [`app/models/document.py`](file:///c:/Users/kiran/Desktop/sentinel-rag/app/models/document.py)
- **Purpose & Responsibility:** SQLAlchemy ORM model definition for `document_chunks`.
- **Key Symbols:**
  - `DocumentChunk(Base, UUIDPrimaryKeyMixin, TimestampMixin)`:
    - `tenant_id`: Multi-tenant partition string (B-tree index).
    - `document_name`: Source file name.
    - `chunk_text`: Raw extracted text chunk.
    - `metadata_json`: JSONB metadata (GIN indexed).
    - `embedding`: `Vector(1536)` (HNSW indexed with `m=16`, `ef_construction=64`, and `vector_cosine_ops`).
    - `search_vector`: `TSVECTOR` generated column persisted via `Computed("to_tsvector('english', chunk_text)", persisted=True)` (GIN indexed).
- **Validated By:** [`tests/test_document_ingestion.py`](file:///c:/Users/kiran/Desktop/sentinel-rag/tests/test_document_ingestion.py), [`alembic/versions/*`](file:///c:/Users/kiran/Desktop/sentinel-rag/alembic/versions).

---

### Module: `app/repositories/search.py`
- **File Path:** [`app/repositories/search.py`](file:///c:/Users/kiran/Desktop/sentinel-rag/app/repositories/search.py)
- **Purpose & Responsibility:** Asynchronous data access repository executing dense vector and sparse lexical queries.
- **Key Symbols:**
  - `DenseCandidate`: Holds `chunk`, `distance`, `similarity_score`, `rank`.
  - `SparseCandidate`: Holds `chunk`, `lexical_score`, `rank`.
  - `SearchRepository`:
    - `search_dense(tenant_id, query_vector, limit=50)`: Executes `DocumentChunk.embedding.cosine_distance(query_vector)` ordered ascending.
    - `search_sparse(tenant_id, query_text, limit=50)`: Executes `websearch_to_tsquery('english', query_text)` matched against `search_vector` ordered descending by `ts_rank_cd`.
- **Validated By:** [`tests/test_hybrid_search.py`](file:///c:/Users/kiran/Desktop/sentinel-rag/tests/test_hybrid_search.py).

---

## 3. Services & Domain Logic Tier

### Module: `app/services/cache.py`
- **File Path:** [`app/services/cache.py`](file:///c:/Users/kiran/Desktop/sentinel-rag/app/services/cache.py)
- **Purpose & Responsibility:** Redis Semantic Vector Cache implementation.
- **Key Symbols:**
  - `serialize_embedding(vector)` / `deserialize_embedding(str)`: Encodes float vectors into base64 IEEE 754 32-bit float streams via `struct.pack/unpack`.
  - `compute_cosine_similarity(vec_a, vec_b)`: Computes normalized dot product between two vectors.
  - `RedisSemanticCache`:
    - `get_or_lookup(tenant_id, query, query_vector)`: Evaluates exact SHA-256 match first, then scans tenant keys for cosine similarity $\ge 0.92$.
    - `set_cache(tenant_id, query, response, query_vector, ttl)`: Stores response, serialized embedding, and query text with a 24-hour TTL.
    - `invalidate_tenant(tenant_id)`: Flushes all cache keys belonging to a specific tenant.
- **Validated By:** [`tests/test_semantic_cache.py`](file:///c:/Users/kiran/Desktop/sentinel-rag/tests/test_semantic_cache.py).

---

### Module: `app/services/router.py`
- **File Path:** [`app/services/router.py`](file:///c:/Users/kiran/Desktop/sentinel-rag/app/services/router.py)
- **Purpose & Responsibility:** Resilient multi-provider LLM router utilizing LiteLLM, Circuit Breaker, and Tenacity.
- **Key Symbols:**
  - `RouterExecutionResult`: Dataclass holding content, model used, fallback status, tokens, cost, and latency.
  - `ResilientLLMRouter`:
    - `complete(messages, tenant_id, ...)`: Coordinates primary (Groq) and fallback (OpenAI) execution. If circuit is `OPEN`, bypasses primary immediately. If primary fails after retries, records failure and executes fallback.
- **Validated By:** [`tests/test_router.py`](file:///c:/Users/kiran/Desktop/sentinel-rag/tests/test_router.py), [`tests/test_chat_completions.py`](file:///c:/Users/kiran/Desktop/sentinel-rag/tests/test_chat_completions.py).

---

### Module: `app/services/rrf.py`
- **File Path:** [`app/services/rrf.py`](file:///c:/Users/kiran/Desktop/sentinel-rag/app/services/rrf.py)
- **Purpose & Responsibility:** Reciprocal Rank Fusion implementation for hybrid search ranking.
- **Key Symbols:**
  - `reciprocal_rank_fusion(ranked_lists, k=60)`: Computes sum of $1 / (k + \text{rank})$ across arbitrary lists of identifiers.
  - `fuse_hybrid_candidates(dense_candidates, sparse_candidates, k=60, limit=10)`: Merges `DenseCandidate` and `SparseCandidate` instances into a unified ranked list.
- **Validated By:** [`tests/test_rrf.py`](file:///c:/Users/kiran/Desktop/sentinel-rag/tests/test_rrf.py).

---

### Module: `app/services/token_analytics.py`
- **File Path:** [`app/services/token_analytics.py`](file:///c:/Users/kiran/Desktop/sentinel-rag/app/services/token_analytics.py)
- **Purpose & Responsibility:** High-precision token counting using `tiktoken`, per-request financial cost calculation, and cumulative financial savings tracking.
- **Key Symbols:**
  - `count_text_tokens(text, model)` / `count_messages_tokens(messages, model)`: Computes exact token consumption including ChatML metadata framing tokens.
  - `calculate_request_cost(model, prompt_tokens, completion_tokens)`: Computes exact USD cost using a built-in provider pricing matrix (Groq, OpenAI, Anthropic, Mistral).
  - `TokenAnalyticsTracker`: Records cumulative tokens consumed and cumulative dollars saved in Redis via `HINCRBYFLOAT`.
- **Validated By:** [`tests/test_token_analytics.py`](file:///c:/Users/kiran/Desktop/sentinel-rag/tests/test_token_analytics.py).

---

## 4. API Controllers & Routes Tier

### Module: `app/api/v1/chat.py`
- **File Path:** [`app/api/v1/chat.py`](file:///c:/Users/kiran/Desktop/sentinel-rag/app/api/v1/chat.py)
- **Purpose & Responsibility:** Unified chat completions gateway controller orchestrating Cache $\to$ Hybrid Retrieval $\to$ Resilient Routing $\to$ Async Telemetry.
- **Key Symbols:**
  - `chat_completions(payload, session, redis, router_service)`:
    1. Checks semantic cache. If hit, returns immediately (sub-20ms).
    2. If miss and retrieval enabled, queries `SearchRepository` and fuses via `fuse_hybrid_candidates`.
    3. Invokes `ResilientLLMRouter.complete()` with context-augmented prompt.
    4. Asynchronously writes to Redis cache and records Prometheus and Redis analytics.
- **Validated By:** [`tests/test_chat_completions.py`](file:///c:/Users/kiran/Desktop/sentinel-rag/tests/test_chat_completions.py), [`tests/test_telemetry_and_e2e.py`](file:///c:/Users/kiran/Desktop/sentinel-rag/tests/test_telemetry_and_e2e.py).

---

### Module: `app/api/v1/search.py`
- **File Path:** [`app/api/v1/search.py`](file:///c:/Users/kiran/Desktop/sentinel-rag/app/api/v1/search.py)
- **Purpose & Responsibility:** Standalone hybrid search endpoint (`/v1/search/hybrid`) returning ranked document chunks without calling an LLM.
- **Validated By:** [`tests/test_hybrid_search.py`](file:///c:/Users/kiran/Desktop/sentinel-rag/tests/test_hybrid_search.py).

---

### Module: `app/api/v1/documents.py`
- **File Path:** [`app/api/v1/documents.py`](file:///c:/Users/kiran/Desktop/sentinel-rag/app/api/v1/documents.py)
- **Purpose & Responsibility:** Batch ingestion API (`/v1/documents/ingest`) persisting document chunks, metadata, and 1536-dimensional embeddings into PostgreSQL.
- **Validated By:** [`tests/test_document_ingestion.py`](file:///c:/Users/kiran/Desktop/sentinel-rag/tests/test_document_ingestion.py).

---

### Module: `app/api/v1/cache.py`
- **File Path:** [`app/api/v1/cache.py`](file:///c:/Users/kiran/Desktop/sentinel-rag/app/api/v1/cache.py)
- **Purpose & Responsibility:** Administrative cache inspection and invalidation endpoints (`/v1/cache/evaluate`, `/v1/cache/invalidate`).
- **Validated By:** [`tests/test_semantic_cache.py`](file:///c:/Users/kiran/Desktop/sentinel-rag/tests/test_semantic_cache.py).

---

## 5. Operations, Infrastructure & Dashboard Tier

### Module: `dashboard/app.py`
- **File Path:** [`dashboard/app.py`](file:///c:/Users/kiran/Desktop/sentinel-rag/dashboard/app.py)
- **Purpose & Responsibility:** Interactive Streamlit operational console displaying real-time latency waterfalls, cache hit ratios, cumulative dollars saved, and system health status.
- **Key Features:** Live dependency ping indicators, latency distribution histograms, and cost-savings counters.

---

### Module: `nginx/conf.d/sentinel.conf`
- **File Path:** [`nginx/conf.d/sentinel.conf`](file:///c:/Users/kiran/Desktop/sentinel-rag/nginx/conf.d/sentinel.conf)
- **Purpose & Responsibility:** Nginx virtual host configuration configuring upstream proxy pass to `sentinel-gateway:8000`, rate-limiting zones, buffer sizes (128k), and timeout parameters.

---

### Module: `docker-compose.prod.yml`
- **File Path:** [`docker-compose.prod.yml`](file:///c:/Users/kiran/Desktop/sentinel-rag/docker-compose.prod.yml)
- **Purpose & Responsibility:** Production multi-container orchestration defining `sentinel-db` (Postgres 16 + pgvector), `sentinel-redis` (Redis 7.2), `sentinel-gateway` (FastAPI app with unprivileged non-root user), `sentinel-dashboard` (Streamlit), and `sentinel-nginx`.


---

## Section 2 â€” End-to-End Request Trace

This section provides an exhaustive, low-level technical walkthrough of a client invoking `POST /v1/chat/completions` (mounted internally as `/api/v1/chat/completions`). It traces the operational path through operating system kernel primitives, reverse proxies, ASGI event loops, distributed state stores, vector indices, and upstream AI models.

### Numbered Request Trace (24 Architectural Steps)

1. **Client TLS Handshake & TCP Socket Establishment**
   - **File / Component:** External Client -> Edge Network -> Linux Kernel TCP Stack (`epoll`).
   - **Action:** The client establishes a TCP 3-way handshake over port 443 with the host server. The Linux kernel accepts the connection and queues it in the TCP socket backlog. TLS 1.3 cryptographic negotiation occurs using ECDHE key exchange and AES-256-GCM cipher suites configured in [nginx/conf.d/sentinel.conf](file:///c:/Users/kiran/Desktop/sentinel-rag/nginx/conf.d/sentinel.conf).

2. **Nginx Reverse Proxy Ingress & Edge Security Validation**
   - **File / Component:** [nginx/conf.d/sentinel.conf](file:///c:/Users/kiran/Desktop/sentinel-rag/nginx/conf.d/sentinel.conf) (`server block :443`).
   - **Action:** Nginx extracts HTTP headers, validates the `Host` header against `${DOMAIN_NAME}`, sets security headers (`HSTS`, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`), and generates or forwards `X-Request-Id`. Client IP is preserved via `X-Real-IP` and `X-Forwarded-For`.

3. **HTTP Streaming & Proxy Buffering Disablement**
   - **File / Component:** [nginx/conf.d/sentinel.conf](file:///c:/Users/kiran/Desktop/sentinel-rag/nginx/conf.d/sentinel.conf) (`location /`).
   - **Action:** Nginx identifies the request path and applies `proxy_buffering off` and `proxy_cache off`. This is mandatory for Server-Sent Events (SSE) and generative LLM streaming tokens to ensure packets stream directly to the client without buffering in reverse proxy memory buffers.

4. **Nginx Upstream Socket Forwarding via Docker Bridge Network**
   - **File / Component:** [nginx/conf.d/sentinel.conf](file:///c:/Users/kiran/Desktop/sentinel-rag/nginx/conf.d/sentinel.conf) (`upstream sentinel_backend`).
   - **Action:** Nginx proxies the raw HTTP/1.1 request across the Docker bridge network `sentinel_prod_network` to `api:8000` via persistent keepalive HTTP sockets (`keepalive 64`).

5. **Uvicorn ASGI Event Loop Ingress via `uvloop`**
   - **File / Component:** [Dockerfile](file:///c:/Users/kiran/Desktop/sentinel-rag/Dockerfile) (`CMD uvicorn ... --loop uvloop`).
   - **Action:** In the Python container, Uvicorn's `uvloop` (C-based `libuv` event loop) triggers an epoll notification on the listening socket, parses the ASGI `http` connection scope, and packages the request into the ASGI standard format.

6. **Request Context & Correlation ID Middleware Interception**
   - **File / Component:** [app/main.py](file:///c:/Users/kiran/Desktop/sentinel-rag/app/main.py) (`request_context_middleware`).
   - **Action:** Middleware intercepts the incoming ASGI request. It extracts `X-Request-ID` or generates `uuid.uuid4().hex[:12]`, binds `request_id`, `client_ip`, and `path` to the async-safe `structlog.contextvars` context storage, and records high-precision start timestamp `time.perf_counter()`.

7. **FastAPI Route Resolution & Controller Routing**
   - **File / Component:** [app/main.py](file:///c:/Users/kiran/Desktop/sentinel-rag/app/main.py) (`app.include_router(chat.router, prefix="/api/v1")`), [app/api/v1/chat.py](file:///c:/Users/kiran/Desktop/sentinel-rag/app/api/v1/chat.py) (`create_chat_completion`).
   - **Action:** FastAPI matches the path `/api/v1/chat/completions` with HTTP verb `POST` to the route handler `create_chat_completion`.

8. **Pydantic Model Deserialization & Payload Validation**
   - **File / Component:** [app/schemas/chat.py](file:///c:/Users/kiran/Desktop/sentinel-rag/app/schemas/chat.py) (`ChatCompletionRequest`, `ChatMessage`).
   - **Action:** Pydantic v2 parses JSON payload into `ChatCompletionRequest`. It strictly validates `messages` list (must not be empty, valid roles `user`, `assistant`, `system`), sets defaults (`model="groq/llama-3.3-70b-versatile"`, `use_cache=True`, `use_retrieval=True`), and validates optional `query_vector` dimension (1536 float elements).

9. **FastAPI Dependency Injection Resolution**
   - **File / Component:** [app/api/v1/chat.py](file:///c:/Users/kiran/Desktop/sentinel-rag/app/api/v1/chat.py), [app/core/redis.py](file:///c:/Users/kiran/Desktop/sentinel-rag/app/core/redis.py), [app/db/session.py](file:///c:/Users/kiran/Desktop/sentinel-rag/app/db/session.py), [app/services/router.py](file:///c:/Users/kiran/Desktop/sentinel-rag/app/services/router.py).
   - **Action:** FastAPI resolves required dependencies: `Redis` client from `get_redis_client`, asynchronous SQLAlchemy session `AsyncSession` from `get_async_session`, and `ResilientLLMRouter` from `get_llm_router`.

10. **Distributed Token-Bucket Rate Limiter Evaluation**
    - **File / Component:** [app/core/rate_limiter.py](file:///c:/Users/kiran/Desktop/sentinel-rag/app/core/rate_limiter.py) (`TokenBucketLimiter.check`), Redis 7.2.
    - **Action:** Rate limiter identifies client via `X-API-Key` or IP address. It executes an atomic Redis pipeline executing Lua script / commands: calculating token replenishment based on elapsed time, checking available capacity. If exhausted, it raises `HTTPException(429, detail="Rate limit quota exceeded")` with `Retry-After` headers.

11. **Query Text Extraction & Hash Key Derivation**
    - **File / Component:** [app/api/v1/chat.py](file:///c:/Users/kiran/Desktop/sentinel-rag/app/api/v1/chat.py), [app/services/cache.py](file:///c:/Users/kiran/Desktop/sentinel-rag/app/services/cache.py) (`RedisSemanticCache.hash_query`).
    - **Action:** Extracts user prompt from the last message in `messages`. Computes SHA-256 hash string of lowercased, stripped query text to create the exact-match cache key: `cache:{tenant_id}:{hash_key}`.

12. **Redis Exact & Semantic Cache Evaluation**
    - **File / Component:** [app/services/cache.py](file:///c:/Users/kiran/Desktop/sentinel-rag/app/services/cache.py) (`RedisSemanticCache.get_or_lookup`), Redis 7.2.
    - **Action:** If `use_cache=True`, system first issues Redis `GET cache:{tenant_id}:{hash_key}`. If absent and `query_vector` is provided, it scans tenant semantic keys, decodes embeddings, and computes cosine distance `1.0 - (u . v) / (||u|| * ||v||)`.
    - *Branch:* If distance $\le 0.08$ (threshold), route proceeds immediately to **Path (a) [Cache HIT]**. If distance $> 0.08$ or key missing, route proceeds to Step 13.

13. **Cache Miss Telemetry & Metric Increment**
    - **File / Component:** [app/core/telemetry.py](file:///c:/Users/kiran/Desktop/sentinel-rag/app/core/telemetry.py) (`CACHE_OPERATIONS_TOTAL.labels(result="miss", tenant=tenant_id).inc()`).
    - **Action:** Prometheus counter increments `result="miss"` for tenant. Execution proceeds to retrieval and model generation.

14. **Hybrid Retrieval Repository Execution**
    - **File / Component:** [app/api/v1/chat.py](file:///c:/Users/kiran/Desktop/sentinel-rag/app/api/v1/chat.py), [app/repositories/search.py](file:///c:/Users/kiran/Desktop/sentinel-rag/app/repositories/search.py) (`SearchRepository.search_hybrid_candidates`).
    - **Action:** If `use_retrieval=True`, gateway invokes hybrid search concurrently or in a unified transaction using PostgreSQL 16.

15. **pgvector HNSW Dense Vector Search**
    - **File / Component:** [app/repositories/search.py](file:///c:/Users/kiran/Desktop/sentinel-rag/app/repositories/search.py) (`search_dense_candidates`), PostgreSQL 16 (`pgvector`).
    - **Action:** Executes SQL: `SELECT id, document_name, chunk_text, embedding <=> :query_vector AS distance FROM document_chunks WHERE tenant_id = :tenant ORDER BY embedding <=> :query_vector LIMIT :limit`. Uses the HNSW vector index (`m=16, ef_construction=64`) to return the top dense semantic matches with cosine distance.

16. **PostgreSQL Sparse Lexical Full-Text Search (FTS)**
    - **File / Component:** [app/repositories/search.py](file:///c:/Users/kiran/Desktop/sentinel-rag/app/repositories/search.py) (`search_sparse_candidates`), PostgreSQL 16.
    - **Action:** Executes SQL: `SELECT id, document_name, chunk_text, ts_rank_cd(tsv_chunk_text, websearch_to_tsquery('english', :query)) AS rank FROM document_chunks WHERE tenant_id = :tenant AND tsv_chunk_text @@ websearch_to_tsquery('english', :query) ORDER BY rank DESC LIMIT :limit`. Employs GIN index for lexical matching.

17. **Reciprocal Rank Fusion (RRF) Ranking Algorithm**
    - **File / Component:** [app/services/rrf.py](file:///c:/Users/kiran/Desktop/sentinel-rag/app/services/rrf.py) (`reciprocal_rank_fusion`).
    - **Action:** Merges dense candidates $D$ and sparse candidates $S$ using the formula $RRF(d) = \sum_{m \in \{D, S\}} \frac{1}{k + rank_m(d)}$ with smoothing constant $k=60$. Sorts results in descending order and extracts the top `top_k_chunks`.

18. **Augmented Prompt Assembly & System Context Injection**
    - **File / Component:** [app/api/v1/chat.py](file:///c:/Users/kiran/Desktop/sentinel-rag/app/api/v1/chat.py).
    - **Action:** Builds context prompt by formatting retrieved chunks: `[Source: {doc_name}]\n{chunk_text}`. Injects this context into system instructions: `"Use the following retrieved context documents to answer the user query accurately:\n\n{context}\n\n"`.

19. **Resilient LLM Router Dispatch & Circuit Breaker Check**
    - **File / Component:** [app/services/router.py](file:///c:/Users/kiran/Desktop/sentinel-rag/app/services/router.py) (`ResilientLLMRouter.route_completion`), [app/core/circuit_breaker.py](file:///c:/Users/kiran/Desktop/sentinel-rag/app/core/circuit_breaker.py).
    - **Action:** Router evaluates `circuit_breaker.allow_request()`. If `CLOSED` or `HALF_OPEN`, it targets primary provider (`groq/llama-3.3-70b-versatile`). If `OPEN`, it bypasses primary immediately and triggers fallback (`openai/gpt-4o-mini`).

20. **Upstream LLM Execution with Tenacity Exponential Backoff**
    - **File / Component:** [app/services/resilience.py](file:///c:/Users/kiran/Desktop/sentinel-rag/app/services/resilience.py) (`execute_llm_with_retry`), LiteLLM, Upstream Model Provider.
    - **Action:** Calls `litellm.acompletion(model=model, messages=messages, temperature=0.7)`. If network timeout, rate limit (429), or 503 occurs, Tenacity retries up to 3 times with exponential backoff (`min=1.0s, max=4.0s`).
    - *Branch A:* Primary succeeds -> **Path (b) [Cache MISS + Primary LLM Success]**.
    - *Branch B:* Primary fails 3 times -> Circuit breaker records failures, transitions to `OPEN`, router catches `CircuitBreakerOpenError` and executes fallback -> **Path (c) [Cache MISS + Circuit Breaker OPEN + Fallback LLM]**.

21. **Token Analytics & Cost Computation**
    - **File / Component:** [app/services/token_analytics.py](file:///c:/Users/kiran/Desktop/sentinel-rag/app/services/token_analytics.py) (`TokenAnalyticsService.calculate_cost`), `tiktoken`.
    - **Action:** Computes prompt tokens, completion tokens, and dollar cost using pricing tiers (e.g. Groq $0.00000059/tok prompt, OpenAI $0.00000015/tok prompt).

22. **Asynchronous Cache Write-Through**
    - **File / Component:** [app/services/cache.py](file:///c:/Users/kiran/Desktop/sentinel-rag/app/services/cache.py) (`RedisSemanticCache.set`), Redis 7.2.
    - **Action:** Writes query text, embedding, response content, model, and metadata into Redis with TTL (default 86,400s / 24 hours).

23. **Prometheus Telemetry Recording**
    - **File / Component:** [app/core/telemetry.py](file:///c:/Users/kiran/Desktop/sentinel-rag/app/core/telemetry.py).
    - **Action:** Updates `TOKENS_CONSUMED_TOTAL.labels(tenant=tenant, model=model).inc(total_tokens)` and records total request duration in `HTTP_REQUEST_DURATION_SECONDS.labels(endpoint="/api/v1/chat/completions", status=200).observe(duration)`.

24. **Structured JSON Response Serialization & Client Transmission**
    - **File / Component:** [app/schemas/chat.py](file:///c:/Users/kiran/Desktop/sentinel-rag/app/schemas/chat.py) (`ChatCompletionResponse`), Nginx, Client.
    - **Action:** Returns OpenAI-compatible JSON schema containing generated text, usage stats, and `telemetry` metadata (latency, provider, cache hit status, cost). Nginx flushes HTTP response headers and body to client.

---

### Three Operational Execution Paths

```
                             [ Client Request ]
                                     â”‚
                             [ Nginx Ingress ]
                                     â”‚
                           [ Rate Limiter Check ]
                                     â”‚
                        [ Semantic Cache Evaluation ]
                                   /   \
                     HIT (<=0.08) /     \ MISS (>0.08)
                                 /       \
               â”Œâ”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”˜         â””â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”
               â–¼                                                    â–¼
    Path (a): CACHE HIT                                 [ Hybrid Search (HNSW + FTS) ]
    â€¢ Latency: < 20ms                                               â”‚
    â€¢ No DB / No LLM call                                    [ RRF Fusion (k=60) ]
    â€¢ Increments Dollars Saved                                      â”‚
    â€¢ Returns cached response                                [ Prompt Assembly ]
                                                                    â”‚
                                                       [ Circuit Breaker Check ]
                                                              /          \
                                                     CLOSED  /            \ OPEN
                                                            /              \
                                                           â–¼                â–¼
                                            Path (b): PRIMARY SUCCESS    Path (c): FALLBACK SUCCESS
                                            â€¢ Primary: Groq Llama 3.3    â€¢ Primary Skipped / Failed
                                            â€¢ Tenacity Retries pass      â€¢ Secondary: OpenAI GPT-4o-mini
                                            â€¢ Writes response to Redis   â€¢ Telemetry: fallback=True
```

#### (a) Path: Cache HIT
1. **Trigger:** User sends a query identical or semantically equivalent ($\text{cosine distance} \le 0.08$) to an earlier query cached within the last 24 hours.
2. **Components Involved:**
   - [app/main.py](file:///c:/Users/kiran/Desktop/sentinel-rag/app/main.py) (Middleware)
   - [app/api/v1/chat.py](file:///c:/Users/kiran/Desktop/sentinel-rag/app/api/v1/chat.py) (`create_chat_completion`)
   - [app/services/cache.py](file:///c:/Users/kiran/Desktop/sentinel-rag/app/services/cache.py) (`RedisSemanticCache.get_or_lookup`)
   - Redis 7.2 Docker container (`cache:6379`)
   - [app/core/telemetry.py](file:///c:/Users/kiran/Desktop/sentinel-rag/app/core/telemetry.py) (`CACHE_OPERATIONS_TOTAL`, `COST_SAVED_USD_TOTAL`)
3. **Execution Mechanics:**
   - Rate limiter passes client.
   - Exact hash lookup `cache:{tenant}:{hash}` returns cached JSON object in $< 1.5\text{ms}$.
   - System bypasses PostgreSQL completely (zero database I/O).
   - System bypasses LiteLLM and upstream APIs (zero external API egress, zero inference latency).
   - Calculates financial dollars saved based on prompt/completion tokens of cached generation.
   - Increments `COST_SAVED_USD_TOTAL` and `CACHE_OPERATIONS_TOTAL(result="hit")`.
   - Returns OpenAI-compatible payload in **$0.8\text{ms} - 15.0\text{ms}$** with telemetry header:
     `{"cache_hit": true, "provider": "semantic_cache", "dollars_saved": 0.00045, "latency_ms": 1.2}`.

#### (b) Path: Cache MISS + Primary LLM Success
1. **Trigger:** Query is novel or exceeds distance threshold ($> 0.08$), and primary model provider (Groq) is healthy.
2. **Components Involved:**
   - [app/services/cache.py](file:///c:/Users/kiran/Desktop/sentinel-rag/app/services/cache.py) (Returns `status="CACHE_MISS"`)
   - [app/repositories/search.py](file:///c:/Users/kiran/Desktop/sentinel-rag/app/repositories/search.py) (`SearchRepository`)
   - PostgreSQL 16 + pgvector container (`db:5432`)
   - [app/services/rrf.py](file:///c:/Users/kiran/Desktop/sentinel-rag/app/services/rrf.py) (`reciprocal_rank_fusion`)
   - [app/services/router.py](file:///c:/Users/kiran/Desktop/sentinel-rag/app/services/router.py) (`ResilientLLMRouter`)
   - [app/core/circuit_breaker.py](file:///c:/Users/kiran/Desktop/sentinel-rag/app/core/circuit_breaker.py) (State `CLOSED`)
   - [app/services/resilience.py](file:///c:/Users/kiran/Desktop/sentinel-rag/app/services/resilience.py) (`execute_llm_with_retry`)
   - LiteLLM Client -> Upstream Groq Cloud API (`llama-3.3-70b-versatile`)
   - [app/services/token_analytics.py](file:///c:/Users/kiran/Desktop/sentinel-rag/app/services/token_analytics.py)
   - Redis 7.2 container (Cache Write-Through)
3. **Execution Mechanics:**
   - Redis cache returns miss in $1.2\text{ms}$.
   - Hybrid search executes HNSW vector scan ($8\text{ms}$) and GIN text search ($4\text{ms}$).
   - RRF combines rankings in $< 0.5\text{ms}$, selecting top 3 chunks.
   - Context is formatted and passed to `ResilientLLMRouter`.
   - Circuit breaker checks primary failure count ($0 < 3$), allows request.
   - LiteLLM calls Groq API; Groq processes 70B parameter inference and returns HTTP 200 in $180\text{ms} - 450\text{ms}$.
   - Token analytics calculates actual token cost ($0.000035).
   - Generated answer and embedding are written to Redis with 24h TTL.
   - Telemetry increments Prometheus metrics.
   - Returns 200 OK with `telemetry.cache_hit=False`, `telemetry.provider="groq"`, `telemetry.fallback_triggered=False`.

#### (c) Path: Cache MISS + Circuit Breaker OPEN + Fallback LLM
1. **Trigger:** Cache misses, and primary provider (Groq) experiences an outage (503 Service Unavailable, connection resets, or consecutive timeouts).
2. **Components Involved:**
   - [app/services/cache.py](file:///c:/Users/kiran/Desktop/sentinel-rag/app/services/cache.py) (Returns `status="CACHE_MISS"`)
   - PostgreSQL 16 & [app/services/rrf.py](file:///c:/Users/kiran/Desktop/sentinel-rag/app/services/rrf.py) (Retrieval completes successfully)
   - [app/services/router.py](file:///c:/Users/kiran/Desktop/sentinel-rag/app/services/router.py) (`ResilientLLMRouter`)
   - [app/core/circuit_breaker.py](file:///c:/Users/kiran/Desktop/sentinel-rag/app/core/circuit_breaker.py) (Transitions `CLOSED` -> `OPEN`)
   - [app/services/resilience.py](file:///c:/Users/kiran/Desktop/sentinel-rag/app/services/resilience.py) (Tenacity retries exhausted)
   - LiteLLM Fallback Client -> OpenAI API (`gpt-4o-mini`)
3. **Execution Mechanics:**
   - Hybrid retrieval succeeds in $12\text{ms}$.
   - Router attempts primary invocation:
     - **Attempt 1:** Groq returns HTTP 503 Service Unavailable. Tenacity catches exception, sleeps $1.0\text{s}$.
     - **Attempt 2:** Groq returns HTTP 503. Tenacity sleeps $2.0\text{s}$.
     - **Attempt 3:** Groq returns HTTP 503. Tenacity retries exhausted, re-raises `ServiceUnavailableError`.
   - Circuit breaker records failure ($3 \ge \text{failure\_threshold}=3$), transitions state to `OPEN`, sets recovery deadline: $t_{\text{now}} + 60\text{s}$.
   - `ResilientLLMRouter` catches primary failure, logs structured warning: `"Primary provider failed. Triggering fallback"`.
   - Router switches target provider to secondary fallback: `openai/gpt-4o-mini`.
   - Fallback call succeeds with OpenAI API returning HTTP 200 in $220\text{ms}$.
   - Token analytics calculates OpenAI token pricing.
   - Successful fallback response is stored in Redis cache.
   - Response returned to client with HTTP 200 OK:
     `{"telemetry": {"fallback_triggered": true, "provider": "openai", "model": "openai/gpt-4o-mini", "circuit_breaker_state": "OPEN"}}`.
   - Subsequent incoming requests for the next 60 seconds bypass Groq entirely without waiting for timeouts, executing directly on OpenAI until recovery window tests `HALF_OPEN`.

---

