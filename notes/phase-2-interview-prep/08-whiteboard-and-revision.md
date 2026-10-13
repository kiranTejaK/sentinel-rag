# 13. Code Walkthrough Practice & Whiteboard Simulations

This document contains hands-on technical walkthrough drills designed to simulate live coding rounds, system architecture whiteboarding, and code-reading interviews.

---

## Exercise 1: Whiteboard Architecture in 5 Minutes

### The Task
Draw and explain the complete SentinelRAG gateway architecture on a whiteboard or virtual canvas to an interviewer without referencing documentation.

### Relevant Files to Internalize
- [`docs/sentinel-rack-mastery/diagrams/system-architecture.md`](file:///c:/Users/kiran/Desktop/sentinel-rag/docs/sentinel-rack-mastery/diagrams/system-architecture.md)
- [`app/main.py`](file:///c:/Users/kiran/Desktop/sentinel-rag/app/main.py)
- [`app/api/v1/chat.py`](file:///c:/Users/kiran/Desktop/sentinel-rag/app/api/v1/chat.py)

### Expected Verbal Structure
1. **Edge Ingress:** Nginx reverse proxy terminating TLS, buffering slow connections (10MB body max), and enforcing coarse IP rate limiting (30r/s).
2. **Middleware Pipeline:** ASGI Uvicorn workers running FastAPI; timing & correlation middleware binding `X-Request-Id` to `structlog contextvars`; distributed token-bucket rate limiter executing atomic Redis Lua scripts.
3. **Stage 1 (Cache Fast-Path):** Redis semantic cache evaluating exact SHA-256 hash first, then unpacking IEEE 754 binary vectors for cosine similarity $\ge 0.92$. (Sub-20ms return on hit).
4. **Stage 2 (Single-Store Retrieval):** PostgreSQL 16 `document_chunks` table executing concurrent `pgvector` HNSW dense search and `tsvector` BM25 sparse search merged via RRF ($k=60$).
5. **Stage 3 (Resilient LLM Mesh):** LiteLLM router protected by a Michael Nygard Circuit Breaker. Primary Groq (Llama-3.3-70B); 5 consecutive errors trip circuit to OPEN, fast-failing in 0ms to OpenAI (GPT-4o-mini).
6. **Telemetry:** Asynchronous background cache write and Prometheus histogram observation.

### Self-Evaluation Checklist
- [ ] Did I mention why Nginx sits in front of Uvicorn (Slowloris, TLS)?
- [ ] Did I explain why Redis Lua is used instead of Python locks?
- [ ] Did I clearly separate exact SHA-256 match from vector cosine similarity?
- [ ] Did I explain why we use PostgreSQL for both dense and sparse retrieval instead of two databases?
- [ ] Did I explain the 3 states of the Circuit Breaker and the 0ms fast-fail guarantee?

---

## Exercise 2: Tracing the Code of `TokenBucketLimiter.consume()`

### The Task
Open [`app/core/rate_limiter.py`](file:///c:/Users/kiran/Desktop/sentinel-rag/app/core/rate_limiter.py#L17-L70). Walk through lines 17â€“70 statement by statement. Explain what every variable represents in the Redis Lua script.

### Key Questions to Answer Aloud
1. What does `redis.call('TIME')` return, and why do we combine `time_arr[1]` and `time_arr[2]`?
2. What happens if `current_data[1]` is `nil`?
3. How is `delta` calculated, and what does `math.max(0, now - last_updated)` protect against?
4. How is `retry_after` computed when tokens are insufficient?
5. Why is `EXPIRE` set to `math.ceil((capacity / refill_rate) * 2)`?

### Reference Answer & Mechanics
1. `TIME` returns a two-element array: `[seconds, microseconds]`. We compute `now = seconds + (microseconds / 1,000,000)` to get sub-microsecond server time without NTP client drift.
2. If `current_data[1]` is `nil`, this is a fresh bucket for a new tenant. We initialize `tokens = capacity` and `last_updated = now`.
3. `delta` is the elapsed seconds since the last request. `math.max(0, ...)` ensures that if Redis server time ever steps backwards slightly due to an NTP sync event, tokens are never decremented by negative time.
4. When `tokens < requested`, `needed = requested - tokens`. Dividing `needed / refill_rate` and taking `math.ceil()` yields the exact integer seconds until sufficient tokens will be replenished.
5. Inactive buckets auto-expire after twice their total refill window to prevent abandoned tenant keys from leaking Redis memory forever.

---

## Exercise 3: Predicting Failure Behavior under Upstream Provider Outage

### The Task
Explain what happens in [`app/services/router.py`](file:///c:/Users/kiran/Desktop/sentinel-rag/app/services/router.py) and [`app/core/circuit_breaker.py`](file:///c:/Users/kiran/Desktop/sentinel-rag/app/core/circuit_breaker.py) when Groq's API begins timing out at 15 seconds per call under 50 requests/sec.

### Walkthrough & Expected Reasoning
1. **Initial 5 Requests:**
   - Arrive while circuit is `CLOSED`.
   - Each request attempts to call Groq via `execute_with_resilience`.
   - Tenacity retries up to 3 times with exponential backoff.
   - Each attempt fails after timeout.
   - The router catches the error, calls `circuit_breaker.record_failure()`, and invokes OpenAI fallback.
2. **On the 5th Consecutive Failure:**
   - In `record_failure()`, `self._consecutive_failures >= 5` is reached.
   - State flips: `CLOSED` $\to$ `OPEN`.
   - `_last_failure_time` is set to `time.time()`.
3. **Requests 6 to 3,000 (Next 60 seconds):**
   - Each incoming request calls `can_execute()`.
   - `can_execute()` sees state is `OPEN` and `elapsed < 60.0`.
   - Returns `False`.
   - The router immediately calls `self._execute_fallback(...)` without touching Groq.
   - **Result:** Latency for Groq is **$0\text{ ms}$**. Gateway workers remain completely unblocked!
4. **At Second 61:**
   - Next request calls `can_execute()`.
   - `elapsed >= 60.0`. State transitions to `HALF_OPEN`.
   - Canary request is sent to Groq.
   - If successful $\to$ resets to `CLOSED`. If failed $\to$ resets to `OPEN` for another 60s.

---

## Exercise 4: Code Modification Simulation â€” Altering RRF $k$ Parameter

### The Task
Suppose an interviewer asks: *"What would happen to retrieval ranking if we changed the RRF constant $k$ in `app/services/rrf.py` from $60$ to $1$?"*

### Expected Reasoning & Answer
- In the formula $\text{Score} = 1 / (k + \text{rank})$:
  - If $k=1$:
    - Rank 1 receives $1 / (1 + 1) = 0.500$.
    - Rank 2 receives $1 / (1 + 2) = 0.333$.
    - Rank 3 receives $1 / (1 + 3) = 0.250$.
    The gap between Rank 1 and Rank 2 is huge ($0.167$). A document that ranked #1 in Dense search would easily beat a document that ranked #2 in Dense AND #2 in Sparse ($0.333 + 0.333 = 0.666$ vs $0.500$). Top rank completely dominates.
  - If $k=60$:
    - Rank 1 receives $1 / (60 + 1) = 0.01639$.
    - Rank 2 receives $1 / (60 + 2) = 0.01613$.
    The difference between Rank 1 and Rank 2 in a single list is only $0.00026$.
    Therefore, any document appearing in the top 5 of *both* lists ($0.016 + 0.016 = 0.032$) overwhelmingly beats a document appearing at #1 in only one list ($0.016$).
- **Conclusion:** Lowering $k$ to $1$ destroys the fusion effect and turns RRF into a "winner-take-all" mechanism dominated by whichever engine ranked a document first. $k=60$ acts as a dampener that rewards consensus across multiple retrieval engines.


---

# 14. Hands-On Mastery Exercises

These practical exercises are designed to test your deep understanding through hands-on terminal, code inspection, and debugging tasks.

---

## Exercise 1: Live Redis Lua Script Inspection & Execution

- **Learning Objective:** Verify the atomic execution and exact output format of `TOKEN_BUCKET_LUA_SCRIPT` directly against a live Redis instance.
- **Prerequisites:** Running Redis container (`docker compose up -d redis` or local Redis on port 6379).
- **Execution Steps:**
  1. Open Redis CLI:
     ```powershell
     docker compose exec redis redis-cli
     ```
  2. Execute the exact Lua script embedded in [`app/core/rate_limiter.py`](file:///c:/Users/kiran/Desktop/sentinel-rag/app/core/rate_limiter.py):
     ```redis
     EVAL "local key = KEYS[1]; local capacity = tonumber(ARGV[1]); local refill_rate = tonumber(ARGV[2]); local requested = tonumber(ARGV[3]); local time_arr = redis.call('TIME'); local now = tonumber(time_arr[1]) + (tonumber(time_arr[2]) / 1000000); local current_data = redis.call('HMGET', key, 'tokens', 'last_updated'); local tokens = tonumber(current_data[1]); local last_updated = tonumber(current_data[2]); if tokens == nil then tokens = capacity; last_updated = now else local delta = math.max(0, now - last_updated); tokens = math.min(capacity, tokens + (delta * refill_rate)); last_updated = now end; local allowed = 0; local retry_after = 0; if tokens >= requested then tokens = tokens - requested; allowed = 1 else allowed = 0; retry_after = math.ceil((requested - tokens) / refill_rate) end; redis.call('HSET', key, 'tokens', tokens, 'last_updated', last_updated); return {allowed, math.floor(tokens), retry_after, capacity}" 1 ratelimit:tenant:manual_test 10 1 1
     ```
  3. Inspect the returned 4-element array: `[1, 9, 0, 10]`.
  4. Inspect the created Redis hash:
     ```redis
     HGETALL ratelimit:tenant:manual_test
     TTL ratelimit:tenant:manual_test
     ```
- **Validation:** Confirm that `tokens` is set to 9, `last_updated` contains the Redis Unix timestamp, and the key has a positive TTL.

---

## Exercise 2: Tracing IEEE 754 Binary Packing in Python REPL

- **Learning Objective:** Prove that binary float packing reduces memory footprint by ~66% compared to JSON string serialization without precision loss.
- **Prerequisites:** Python 3.12 with `uv`.
- **Execution Steps:**
  1. Launch Python REPL:
     ```powershell
     uv run python
     ```
  2. Run the serialization benchmark:
     ```python
     import json, struct, base64, random

     # Generate 1536-dimensional float vector
     vector = [random.uniform(-1.0, 1.0) for _ in range(1536)]

     # Method A: JSON serialization
     json_str = json.dumps(vector)
     json_bytes = len(json_str.encode('utf-8'))

     # Method B: IEEE 754 Binary Pack (SentinelRAG method)
     packed_bytes = struct.pack(f"{len(vector)}f", *vector)
     b64_str = base64.b64encode(packed_bytes).decode('ascii')
     b64_bytes = len(b64_str.encode('utf-8'))

     # Verify lossless roundtrip
     unpacked = list(struct.unpack(f"{len(vector)}f", base64.b64decode(b64_str.encode('ascii'))))
     max_diff = max(abs(a - b) for a, b in zip(vector, unpacked))

     print(f"JSON Payload Size:   {json_bytes} bytes (~{round(json_bytes/1024, 1)} KB)")
     print(f"IEEE 754 Packed Size: {b64_bytes} bytes (~{round(b64_bytes/1024, 1)} KB)")
     print(f"Storage Savings:      {round((1 - b64_bytes/json_bytes) * 100, 1)}%")
     print(f"Max Float Roundtrip Diff: {max_diff:.8e}")
     ```
- **Validation:** Confirm that packed size is ~8,192 bytes vs JSON's ~28,000 bytes, and the roundtrip diff is $< 10^{-7}$ (single-precision epsilon).

---

## Exercise 3: Simulating Circuit Breaker Failover via Integration Tests

- **Learning Objective:** Observe the real-time state transition from `CLOSED` to `OPEN` and fast-failover behavior under mock provider failure.
- **Prerequisites:** Pytest installed.
- **Execution Steps:**
  1. Run the dedicated circuit breaker integration test with verbose logging:
     ```powershell
     uv run pytest tests/test_circuit_breaker.py -v -s
     ```
  2. Inspect the test `test_circuit_breaker_trips_to_open_after_threshold_failures`:
     - Consecutive calls 1 to 4 fail. State remains `CLOSED`.
     - Call 5 fails. State transitions to `OPEN`.
     - Immediate subsequent call raises `CircuitBreakerOpenException` without executing the protected coroutine.
- **Validation:** All 6 tests in `test_circuit_breaker.py` pass.

---

## Exercise 4: Validating Single-Store Hybrid Search via SQL

- **Learning Objective:** Verify the execution plan of dense HNSW and sparse full-text queries inside PostgreSQL 16.
- **Prerequisites:** Running PostgreSQL container with pgvector.
- **Execution Steps:**
  1. Open psql CLI:
     ```powershell
     docker compose exec postgres psql -U sentinel -d sentinel_rag
     ```
  2. Verify installed extensions and indexes:
     ```sql
     \dx vector
     \d document_chunks
     ```
  3. Verify the generated column and indices:
     ```sql
     SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'document_chunks';
     ```
- **Validation:** Confirm the presence of `ix_document_chunks_embedding_hnsw` using `hnsw (embedding vector_cosine_ops)` and `ix_document_chunks_search_vector` using `gin (search_vector)`.


---

# 15. Final Revision & Project Mastery Checklist

Use this checklist and self-assessment rubric to audit your readiness to explain, debug, and defend SentinelRAG in senior, staff, and principal engineering interviews.

---

## 1. Mastery Self-Rating Rubric

| Rating Score | Mastery Level Definition |
| :---: | :--- |
| **0** | **Unfamiliar:** Cannot explain the concept or mechanism. |
| **1** | **Surface Recognition:** Recognizes technology buzzwords (e.g. "Redis Lua", "HNSW") but cannot explain how they work internally. |
| **2** | **Conceptual Understanding:** Can explain the theoretical concept, but cannot connect it to actual files or lines of code in SentinelRAG. |
| **3** | **Codebase Grounded:** Can explain the concept, cite the exact source files, trace execution paths, and describe inputs and outputs. |
| **4** | **Staff / Production Mastery:** Can trace the code, justify architectural trade-offs, explain failure modes, discuss Linux/OS-level mechanics, and defend design decisions against interviewer pushback. |

**Goal:** Achieve a rating of **3 or 4** across all 10 mastery domains before interviewing.

---

## 2. Mastery Audit Checklist

### Domain 1: Project Overview & Architecture
- [ ] Can deliver the 30-second elevator pitch clearly without stuttering. *(Target: 4)*
- [ ] Can deliver the 2-minute technical pitch covering latency, cost, and resilience. *(Target: 4)*
- [ ] Can draw the complete high-level system architecture from memory (Nginx $\to$ ASGI $\to$ RateLimiter $\to$ Cache $\to$ Postgres Hybrid $\to$ CircuitBreaker $\to$ LLM). *(Target: 4)*
- [ ] Can explain why Nginx sits in front of Uvicorn (Slowloris buffering, SSL termination, request body limits). *(Target: 3)*

### Domain 2: Distributed Rate Limiting
- [ ] Can explain the Token Bucket algorithm and its advantages over Fixed Window counters. *(Target: 4)*
- [ ] Can explain why atomic Redis Lua is required instead of Redis `MULTI/EXEC` or Python locks. *(Target: 4)*
- [ ] Can explain why Redis server time (`redis.call('TIME')`) is queried rather than Python's `time.time()` (NTP clock drift immunity). *(Target: 4)*
- [ ] Can trace lines in [`app/core/rate_limiter.py`](file:///c:/Users/kiran/Desktop/sentinel-rag/app/core/rate_limiter.py) where tokens are replenished and `Retry-After` is calculated. *(Target: 3)*

### Domain 3: Semantic Caching & Vector Math
- [ ] Can explain why natural language queries defeat standard key-value caches. *(Target: 3)*
- [ ] Can explain the exact two-tier lookup: SHA-256 exact match ($O(1)$ fast-path) followed by cosine similarity scan ($\ge 0.92$). *(Target: 4)*
- [ ] Can explain how IEEE 754 binary packing via Python's `struct` module slashes vector memory footprint by ~66% compared to JSON string serialization. *(Target: 4)*
- [ ] Can explain how multi-tenant isolation is enforced via Redis key prefixes (`cache:{tenant_id}:{hash}`). *(Target: 3)*

### Domain 4: Single-Store Hybrid Retrieval (PostgreSQL 16)
- [ ] Can justify using a single database (PostgreSQL 16) over a dedicated vector DB (Pinecone) + Elasticsearch. *(Target: 4)*
- [ ] Can explain the mathematical difference between dense cosine distance (`<=>`) and sparse lexical scoring (`ts_rank_cd`). *(Target: 4)*
- [ ] Can explain the internal structure of `pgvector`'s HNSW index (`m=16`, `ef_construction=64`). *(Target: 3)*
- [ ] Can explain why `search_vector` is configured as a `STORED` generated column with a GIN index. *(Target: 3)*

### Domain 5: Reciprocal Rank Fusion (RRF)
- [ ] Can explain why raw vector distance and BM25 scores cannot be combined linearly without calibration. *(Target: 4)*
- [ ] Can write down the RRF formula: $\text{Score}(d) = \sum 1 / (k + \text{rank})$. *(Target: 4)*
- [ ] Can explain why $k=60$ is the empirical industry standard and what happens if $k=1$. *(Target: 4)*
- [ ] Can trace the implementation in [`app/services/rrf.py`](file:///c:/Users/kiran/Desktop/sentinel-rag/app/services/rrf.py). *(Target: 3)*

### Domain 6: Resilience & Circuit Breaker Pattern
- [ ] Can explain the three states: `CLOSED`, `OPEN`, `HALF_OPEN`. *(Target: 4)*
- [ ] Can explain the "Fail-Fast" principle and why catching exceptions without a circuit breaker causes thread pool exhaustion. *(Target: 4)*
- [ ] Can explain the role of Tenacity retry with exponential backoff and full jitter. *(Target: 4)*
- [ ] Can explain why `asyncio.Lock()` is used to synchronize state transitions in [`app/core/circuit_breaker.py`](file:///c:/Users/kiran/Desktop/sentinel-rag/app/core/circuit_breaker.py). *(Target: 3)*

### Domain 7: Concurrency & ASGI Internals
- [ ] Can explain how Python's `asyncio` event loop interleaves I/O tasks on a single thread. *(Target: 4)*
- [ ] Can explain why `threading.local` leaks data across async coroutines and how `contextvars` solves this. *(Target: 4)*
- [ ] Can explain what happens if a blocking synchronous function is called inside an async FastAPI route. *(Target: 4)*
- [ ] Can trace how `X-Request-Id` is propagated into structlog JSON logs via `app/main.py`. *(Target: 3)*

### Domain 8: Container & Linux Security
- [ ] Can explain why running containers as root (`UID 0`) is dangerous (container escape to host root). *(Target: 4)*
- [ ] Can cite the lines in `Dockerfile` where unprivileged `appuser` (UID 10001) is created and activated. *(Target: 4)*
- [ ] Can explain the difference between Docker `HEALTHCHECK` and application `/healthz` probes. *(Target: 3)*
- [ ] Can explain how Linux cgroups enforce CPU CFS quotas and memory limits. *(Target: 3)*

### Domain 9: Testing & Validation
- [ ] Can run the test suite via `uv run pytest -v` and verify that all 44 test cases pass. *(Target: 4)*
- [ ] Can describe what each of the 12 test files validates. *(Target: 3)*
- [ ] Can reproduce an HTTP 429 response using curl. *(Target: 4)*
- [ ] Can verify sub-20ms response time on a semantic cache hit using curl headers. *(Target: 4)*

### Domain 10: Operational Telemetry & Cost ROI
- [ ] Can list the Prometheus metrics exposed at `/metrics` (`HTTP_REQUEST_DURATION_SECONDS`, `CACHE_OPERATIONS_TOTAL`, `TOKENS_CONSUMED_TOTAL`, `COST_SAVED_USD_TOTAL`). *(Target: 3)*
- [ ] Can explain how tiktoken token counting computes financial cost using model pricing tables. *(Target: 3)*
- [ ] Can launch and navigate the Streamlit monitoring console (`dashboard/app.py`). *(Target: 3)*

---

## 3. Final Pre-Interview Readiness Scorecard

Sum your ratings across the 10 domains (Maximum Score: 40):
- **35 â€“ 40:** **Staff / Principal Engineer Ready.** You can deeply explain, demonstrate, debug, and defend SentinelRAG in any top-tier technical round.
- **28 â€“ 34:** **Senior Engineer Ready.** Solid grasp of implementation; review the OS, Linux, and concurrency internals in [`12-interview-answer-guide.md`](file:///c:/Users/kiran/Desktop/sentinel-rag/docs/sentinel-rack-mastery/12-interview-answer-guide.md).
- **Below 28:** Review [`03-core-concepts.md`](file:///c:/Users/kiran/Desktop/sentinel-rag/docs/sentinel-rack-mastery/03-core-concepts.md) and [`04-codebase-file-by-file.md`](file:///c:/Users/kiran/Desktop/sentinel-rag/docs/sentinel-rack-mastery/04-codebase-file-by-file.md) and run the practical exercises in [`14-hands-on-mastery-exercises.md`](file:///c:/Users/kiran/Desktop/sentinel-rag/docs/sentinel-rack-mastery/14-hands-on-mastery-exercises.md).

