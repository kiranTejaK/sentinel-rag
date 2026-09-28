# Day 0: SentinelRAG Infrastructure Architecture, PRD & Roadmap

**Date:** September 27, 2026  
**Document Version:** 1.0.0  
**Author:** Principal Systems & AI Infrastructure Architect  
**Project:** SentinelRAG (Enterprise AI Gateway & Hybrid Retrieval Engine)  
**Status:** Approved for Implementation

---

## 1. Executive Summary & Problem Statement

Production Generative AI deployments in startups and growth-stage enterprises encounter severe systemic vulnerabilities when client applications interface directly with LLM providers (e.g., OpenAI, Anthropic, Google) or unoptimized vector databases:

1. **Unpredictable & Runaway Token Economics:**
   - Without an intelligent semantic caching layer, 30–50% of production queries are near-duplicate or identical (e.g., FAQ inquiries, catalog lookups, standard customer support queries). Calling frontier LLMs repeatedly for semantic duplicates yields massive operational expense, linear cost scaling, and unnecessary API spend.
2. **P95 Latency Spikes Exceeding 1500ms–3000ms:**
   - LLM generation time imposes severe latency constraints on interactive web applications. Even lightweight queries incur token generation penalties. A cold roundtrip to an LLM provider p95 is ~1800ms.
3. **Lexical Retrieval Failure in Pure Vector Databases:**
   - Dense vector embeddings fail catastrophically on exact lexical identifiers, SKUs, part numbers, UUIDs, code tokens, and acronyms (e.g., querying `SKU-894-AB` often retrieves general product manuals instead of the exact component). A pure vector store lack of keyword inverted indices creates critical recall voids.
4. **Single-Provider Fragility & Unmitigated Outages:**
   - Direct integration couples application availability to a single vendor's API uptime. When a provider suffers rate limiting (HTTP 429), regional outages (HTTP 503), or p99 tail latency degredations (>10s), user-facing applications fail with zero fallback or circuit breaking.
5. **Security Gaps & Prompt Injection Vulnerabilities:**
   - Without gateway-level sanitization, malicious user inputs reach downstream agents and models unfiltered, risking jailbreaks, system prompt exfiltration, and downstream data poisoning.
6. **Noisy Neighbor & Resource Starvation:**
   - Multi-tenant systems without tenant-scoped atomic rate limiting experience service degradation when a single client exhausts the global LLM quota.

**SentinelRAG** resolves these enterprise pain points through a centralized, high-throughput AI Middleware Gateway sitting between consumer clients and upstream LLM providers / data stores.

---

## 2. Product Requirements Document (PRD)

### 2.1 Project Objectives & Core Value Propositions
- **Semantic Caching Layer:** Intercept queries and return cached generations with sub-25ms latency when semantic similarity is within cosine distance $\le 0.08$.
- **Unified Hybrid Retrieval:** Single-database hybrid search combining dense vector embeddings (pgvector HNSW) and sparse lexical search (`tsvector` BM25) fused via Reciprocal Rank Fusion (RRF).
- **Resilient Model Routing:** Dynamic multi-provider failover (OpenAI $\to$ Anthropic $\to$ Groq/Local) with state-machine circuit breakers.
- **Tenant-Aware Guardrails:** Atomic token-bucket rate limiting and sanitization executed at the gateway ingress.

### 2.2 Strict In-Scope Features

| Component | Technical Implementation | Specification Details |
| :--- | :--- | :--- |
| **Ingress API** | FastAPI (Async ASGI, Python 3.11+) | Strict Pydantic v2 validation, RFC 7807 Error handling, OpenAPI 3.1 schemas. |
| **Rate Limiter** | Redis 7 + Atomic Lua Scripting | Distributed Token-Bucket per `tenant_id` and API key; instantaneous rate checking without race conditions. |
| **Prompt Sanitization** | Ingress Regex + Heuristic Filter | Injection defense, system directive striping, token length truncation. |
| **Semantic Cache** | Redis 7 Vector Similarity (HNSW / Flat) | Cosine metric; distance threshold $\le 0.08$ (similarity $\ge 0.92$); cache hit returns prior response without LLM invocation. |
| **Hybrid Retrieval** | PostgreSQL 16 + `pgvector` + `tsvector` | Single-store storage: dense cosine HNSW index + GIN inverted index for lexical tokens; zero multi-database sprawl. |
| **Re-Ranking** | Reciprocal Rank Fusion (RRF) | Pure mathematical rank-fusion ($k=60$) combining top-$N$ vector and top-$N$ lexical results inside PostgreSQL / Python service. |
| **LLM Gateway** | LiteLLM + Tenacity Circuit Breaker | Provider agnostic interface, exponential backoff retries, sliding-window circuit breaker state machine (`CLOSED`, `OPEN`, `HALF-OPEN`). |
| **Observability** | `structlog` + Prometheus + OpenTelemetry | Zero raw prints; structured JSON logs with correlation IDs (`request_id`, `tenant_id`); Prometheus latency waterfalls and cache hit counters. |
| **Demo Dashboard** | Streamlit | Real-time telemetry dashboard showcasing cache hits, latency waterfall comparisons, cost savings ticker, and live query interface. |
| **DevOps & Infra** | Docker Compose + GitHub Actions | Multi-stage Dockerfiles, healthcheck-gated compose topology, CI linting/testing, automated deployment to AWS EC2/VPS. |

### 2.3 Strict Out-of-Scope Boundaries (DO NOT BUILD)
To maintain elite production engineering velocity and prevent architectural bloat:
1. **NO Multi-Agent Swarms:** No LangGraph, CrewAI, AutoGen, or autonomous multi-agent loops. SentinelRAG is a deterministic, high-throughput gateway, not an agent playground.
2. **NO Heavy Orchestration Frameworks:** Zero dependency on LangChain or LlamaIndex. All retrieval, prompt assembly, and client logic are written in lean, idiomatic native Python.
3. **NO Multi-Database Sprawl:** No Pinecone, Weaviate, Milvus, Qdrant, Elasticsearch, or Mongo. PostgreSQL 16 alone handles relational data, vectors, and lexical search.
4. **NO Custom Scrapers or OCR Pipelines:** Ingestion expects clean JSON/text payloads. No unstructured PDF visual OCR or distributed web crawling engines.
5. **NO Custom Full-Stack React/Vue Frontend:** Frontend is strictly limited to an operational Streamlit dashboard designed for demonstration, benchmarking, and internal metrics.

### 2.4 Service Level Objectives (SLOs) & Key Performance Indicators (KPIs)

| Metric | Target SLO | Measurement Method |
| :--- | :--- | :--- |
| **Semantic Cache Hit Latency (p95)** | **$< 25\text{ ms}$** | Gateway ingress to egress response time on cosine distance $\le 0.08$. |
| **Cold LLM Generation Latency (p95)** | **$< 1800\text{ ms}$** | Full pipeline execution (Sanitization $\to$ Hybrid Retrieval $\to$ LiteLLM generation). |
| **Token Cost Reduction** | **$40\% - 70\%$** | Calculated against baseline raw token spend on recurring/clustered workloads. |
| **Hybrid Search Execution** | **$< 120\text{ ms}$** | PostgreSQL HNSW + GIN execution across 500,000 document chunks. |
| **Gateway Availability** | **$99.95\%$** | Zero application-level downtime during primary upstream provider outages via circuit breaking. |
| **Rate Limit Overhead** | **$< 2\text{ ms}$** | Overhead added by Redis Lua token-bucket check per request. |

---

## 3. Technical Architecture & Component Design

### 3.1 End-to-End System Flow Diagram

```
+---------------------------------------------------------------------------------------------------+
|                                      CLIENT APPLICATION                                           |
+---------------------------------------------------------------------------------------------------+
                                                  │
                                                  │ POST /v1/chat/completions (Bearer API Key, JSON)
                                                  ▼
+---------------------------------------------------------------------------------------------------+
|                                  SENTINELRAG GATEWAY (FastAPI)                                    |
|                                                                                                   |
|  [ Step 1: Ingress Authentication & Request ID Injection (structlog correlation) ]               |
|                                                  │                                                |
|                                                  ▼                                                |
|  [ Step 2: Distributed Token Bucket Rate Limiter (Redis Lua Script) ]                             |
|          │ Rate Limit Exceeded (HTTP 429 Too Many Requests + Retry-After)                         |
|          ▼                                                                                        |
|  [ Step 3: Prompt Sanitizer & Guardrails (Regex heuristics, injection detection) ]                 |
|                                                  │                                                |
|                                                  ▼                                                |
|  [ Step 4: Query Embedding Generation (text-embedding-3-small: 1536d) ]                           |
|                                                  │                                                |
+──────────────────────────────────────────────────┼────────────────────────────────────────────────+
                                                   │
                         Query Vector + Tenant ID  │
                                                   ▼
                +──────────────────────────────────────────────────────+
                |         REDIS 7 SEMANTIC CACHE (Vector HNSW)         |
                +──────────────────────────────────────────────────────+
                                                   │
                          Vector Cosine Distance $\le 0.08$?
                                   ┌───────────────┴──────────────┐
                                   │                              │
                          YES (Cache Hit)                 NO (Cache Miss)
                                   │                              │
                                   │                              ▼
                                   │            +───────────────────────────────────+
                                   │            |    POSTGRESQL 16 (HYBRID SEARCH)  |
                                   │            |                                   |
                                   │            |  Query A: Dense HNSW (pgvector)   |
                                   │            |  Query B: Sparse BM25 (tsvector)  |
                                   │            |  Fusion: Reciprocal Rank (RRF)    |
                                   │            +───────────────────────────────────+
                                   │                              │
                                   │                              │ Top-K Augmented Context
                                   │                              ▼
                                   │            +───────────────────────────────────+
                                   │            |   LITELLM ROUTER & RESILIENCE     |
                                   │            |                                   |
                                   │            |  Primary: OpenAI gpt-4o-mini      |
                                   │            |  Circuit Breaker: Sliding Window  |
                                   │            |  Fallback: Anthropic Claude-3.5   |
                                   │            |  Fallback: Groq Llama-3.3-70b     |
                                   │            +───────────────────────────────────+
                                   │                              │
                                   │                              │ LLM Response
                                   │                              ▼
                                   │            +───────────────────────────────────+
                                   │            |   ASYNC WRITE TO REDIS CACHE      |
                                   │            |   (Vector + Text + TTL: 86400s)   |
                                   │            +───────────────────────────────────+
                                   │                              │
                                   └──────────────┬───────────────┘
                                                  │
                                                  ▼
+---------------------------------------------------------------------------------------------------+
|                        STEP 5: TELEMETRY, AUDIT LOGGING & EGRESS                                  |
|   - structlog JSON emission (latency_ms, cache_hit, tokens_used, cost_usd)                        |
|   - Prometheus metric updates (histogram_observe, counter_inc)                                    |
|   - Write asynchronous audit log to PostgreSQL audit_logs table                                   |
|   - Return RFC-compliant JSON response with custom diagnostic headers:                            |
|       * X-Sentinel-Cache: HIT | MISS                                                              |
|       * X-Sentinel-Latency-Ms: 18.42                                                              |
|       * X-Sentinel-Provider: openai | anthropic | cache                                           |
+---------------------------------------------------------------------------------------------------+
                                                  │
                                                  ▼
                                          CLIENT RESPONSE
```

---

### 3.2 Database Schema Definition (PostgreSQL 16 + pgvector)

All relational structures, document chunks, vector embeddings, and audit trails reside in PostgreSQL 16.

```sql
-- Enable necessary extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "vector";

-- 1. Tenants Table (Multi-tenancy isolation)
CREATE TABLE IF NOT EXISTS tenants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    api_key_hash VARCHAR(64) NOT NULL UNIQUE,
    rate_limit_rpm INT NOT NULL DEFAULT 600,
    rate_limit_tpm INT NOT NULL DEFAULT 100000,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_tenants_api_key ON tenants(api_key_hash);

-- 2. Documents Master Table
CREATE TABLE IF NOT EXISTS documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    title VARCHAR(512) NOT NULL,
    source_uri TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_documents_tenant ON documents(tenant_id);
CREATE INDEX idx_documents_metadata ON documents USING GIN(metadata);

-- 3. Document Chunks (Hybrid Dense Vector + Sparse Full-Text)
CREATE TABLE IF NOT EXISTS document_chunks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    chunk_index INT NOT NULL,
    content TEXT NOT NULL,
    token_count INT NOT NULL,
    -- 1536-dimensional dense embedding (e.g. OpenAI text-embedding-3-small)
    embedding vector(1536) NOT NULL,
    -- Generated TSVector column for lexical search
    tsv_content tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indexing Strategy:
-- A. Multi-tenant partitioning / filtering index
CREATE INDEX idx_chunks_tenant_doc ON document_chunks(tenant_id, document_id);

-- B. HNSW Index for Cosine Distance Vector Search
-- m=16, ef_construction=64 provides optimal trade-off between recall and indexing speed
CREATE INDEX idx_chunks_embedding_hnsw 
ON document_chunks 
USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 64);

-- C. GIN Inverted Index for BM25 Lexical Keyword Search
CREATE INDEX idx_chunks_tsv ON document_chunks USING GIN(tsv_content);

-- 4. Audit & Telemetry Log Table
CREATE TABLE IF NOT EXISTS audit_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    request_id VARCHAR(64) NOT NULL,
    prompt_raw TEXT,
    prompt_sanitized TEXT,
    cache_hit BOOLEAN NOT NULL DEFAULT FALSE,
    cache_distance FLOAT,
    provider_used VARCHAR(64) NOT NULL,
    model_name VARCHAR(64) NOT NULL,
    prompt_tokens INT NOT NULL DEFAULT 0,
    completion_tokens INT NOT NULL DEFAULT 0,
    total_tokens INT NOT NULL DEFAULT 0,
    cost_usd NUMERIC(10, 6) NOT NULL DEFAULT 0.000000,
    latency_ms NUMERIC(10, 2) NOT NULL,
    status_code INT NOT NULL DEFAULT 200,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_audit_logs_tenant_created ON audit_logs(tenant_id, created_at DESC);
CREATE INDEX idx_audit_logs_request_id ON audit_logs(request_id);
```

---

### 3.3 Reciprocal Rank Fusion (RRF) Hybrid Search Query

Rather than trusting raw scalar scores between cosine distance (bounded $[0, 2]$) and BM25 `ts_rank` (unbounded $[0, \infty)$), SentinelRAG executes rank fusion using the RRF algorithm with standard hyperparameter $k = 60$:

$$\text{RRF\_Score}(d) = \frac{1}{60 + \text{rank}_{\text{vector}}(d)} + \frac{1}{60 + \text{rank}_{\text{bm25}}(d)}$$

```sql
WITH vector_search AS (
    SELECT 
        id, 
        ROW_NUMBER() OVER (ORDER BY embedding <=> :query_embedding) AS rank_vec
    FROM document_chunks
    WHERE tenant_id = :tenant_id
    ORDER BY embedding <=> :query_embedding
    LIMIT :limit_candidates
),
lexical_search AS (
    SELECT 
        id, 
        ROW_NUMBER() OVER (ORDER BY ts_rank_cd(tsv_content, plainto_tsquery('english', :query_text)) DESC) AS rank_lex
    FROM document_chunks
    WHERE tenant_id = :tenant_id
      AND tsv_content @@ plainto_tsquery('english', :query_text)
    ORDER BY rank_lex
    LIMIT :limit_candidates
)
SELECT 
    c.id,
    c.content,
    c.document_id,
    c.metadata,
    COALESCE(1.0 / (60 + v.rank_vec), 0.0) + 
    COALESCE(1.0 / (60 + l.rank_lex), 0.0) AS rrf_score,
    v.rank_vec,
    l.rank_lex
FROM document_chunks c
LEFT JOIN vector_search v ON c.id = v.id
LEFT JOIN lexical_search l ON c.id = l.id
WHERE v.id IS NOT NULL OR l.id IS NOT NULL
ORDER BY rrf_score DESC
LIMIT :top_k;
```

---

### 3.4 Redis Semantic Cache Design & Key Topology

#### Redis Key Schema
- **Cache Index Name:** `idx:semantic_cache`
- **Cache Item Key Format:** `cache:{tenant_id}:{md5(normalized_query)}`
- **Hash Fields:**
  - `query_text`: String (normalized, lowercase stripped query)
  - `query_vector`: Binary Float32 array (1536 dimensions)
  - `response_content`: Serialized JSON payload containing generated text, model, token counts
  - `created_at`: Epoch timestamp
  - `ttl`: Integer (default 86400 seconds = 24h)

#### Cosine Distance Thresholding Strategy
1. Client query is embedded into vector $\vec{q} \in \mathbb{R}^{1536}$.
2. Redis performs K-Nearest Neighbor search ($K=1$) using Cosine distance:
   $$D_{\cos}(\vec{u}, \vec{v}) = 1 - \frac{\vec{u} \cdot \vec{v}}{\|\vec{u}\|_2 \|\vec{v}\|_2}$$
3. **Threshold Boundary:**
   - **$D_{\cos} \le 0.08$ (Similarity $\ge 0.92$):** Classified as **CACHE HIT**. Query has identical semantic intent. Return stored response immediately ($\text{p95} < 20\text{ms}$).
   - **$D_{\cos} > 0.08$:** Classified as **CACHE MISS**. Dispatch to hybrid retrieval and upstream LLM router.

---

### 3.5 Distributed Token-Bucket Rate Limiter (Redis Lua)

Atomic Lua script executed on Redis 7 ensures tenant isolation with zero race conditions:

```lua
-- KEYS[1]: Rate limit key, e.g. "rate_limit:{tenant_id}:tokens"
-- ARGV[1]: Max bucket capacity (burst limit)
-- ARGV[2]: Refill rate per millisecond
-- ARGV[3]: Requested tokens
-- ARGV[4]: Current timestamp (milliseconds)

local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local requested = tonumber(ARGV[3])
local now = tonumber(ARGV[4])

local data = redis.call("HMGET", key, "tokens", "last_updated")
local tokens = tonumber(data[1])
local last_updated = tonumber(data[2])

if tokens == nil then
    tokens = capacity
    last_updated = now
else
    local elapsed = now - last_updated
    if elapsed > 0 then
        tokens = math.min(capacity, tokens + (elapsed * refill_rate))
        last_updated = now
    end
end

if tokens >= requested then
    tokens = tokens - requested
    redis.call("HMSET", key, "tokens", tokens, "last_updated", last_updated)
    redis.call("EXPIRE", key, 3600)
    return {1, math.floor(tokens)} -- Allowed, remaining tokens
else
    local wait_ms = math.ceil((requested - tokens) / refill_rate)
    return {0, wait_ms} -- Rejected, retry-after in ms
end
```

---

## 4. Architectural Defense: Why an AI Gateway Pattern Outperforms Direct Client Invocations

Enterprise systems engineering mandates that client applications should never directly query raw LLM APIs or bare vector databases. The AI Gateway middleware pattern offers definitive systemic superiority across four core dimensions:

```
+───────────────────────────────────+───────────────────────────────────+
|      DIRECT CLIENT INVOCATION     |      SENTINELRAG GATEWAY PATTERN  |
+───────────────────────────────────+───────────────────────────────────+
| ❌ Zero centralized caching        | ✅ Sub-25ms semantic cache hits   |
| ❌ API keys exposed to clients     | ✅ Secrets isolated in secure VPC |
| ❌ Single provider outage halts app| ✅ Transparent multi-LLM failover |
| ❌ Fragmented, un-audited logging  | ✅ Unified structlog & telemetry  |
| ❌ Uncontrolled, runaway billing   | ✅ Strict tenant token quotas     |
| ❌ No lexical search (poor recall) | ✅ RRF fused hybrid retrieval     |
+───────────────────────────────────+───────────────────────────────────+
```

### 1. Decoupling Client Release Cycles from AI Vendor Churn
Upstream AI providers deprecate models, alter tokenizer behaviors, and adjust API schemas frequently. In direct-invocation architectures, every client app (web, iOS, Android, internal microservices) must be refactored, re-tested, and redeployed. SentinelRAG standardizes all internal communication on an enterprise-controlled schema, insulating client systems from upstream volatility.

### 2. Elimination of Vector-Only Lexical Blindspots
A pure vector store retrieval paradigm fails on domain-specific identifiers (e.g., invoice IDs, user handles, medical record codes, exact error codes). By placing hybrid retrieval behind a unified gateway interface, SentinelRAG transparently combines dense vector search with sparse full-text search, returning superior context without forcing clients to understand search mechanics.

### 3. Financial Firewall & Predictable Unit Economics
Direct client-to-LLM connections expose companies to denial-of-wallet attacks: a runaway script or malicious user can burn through tens of thousands of dollars in token billing overnight. SentinelRAG enforces hard token quotas, burst caps, and short-circuits up to 70% of requests through its semantic cache.

---

## 5. System Design Interview Q&A (STAR Format)

**Question:**  
*"How would you design a multi-tenant AI gateway to optimize cost and latency while preventing noisy-neighbor issues?"*

### Situation
A high-growth enterprise platform with 500+ B2B customers was experiencing severe reliability and cost degradation:
- Monthly LLM API expenditures were scaling linearly with user growth, exceeding \$180,000/month.
- Peak-hour p95 latency reached 2400ms due to LLM queueing.
- Several high-volume tenants repeatedly triggered global API rate limits, starving smaller tenants (the "noisy neighbor" defect).

### Task
Architect and deploy a centralized, production-grade AI Middleware Gateway that:
1. Slashes token expenditure by at least 40%.
2. Delivers sub-30ms responses on repetitive queries.
3. Enforces strict tenant isolation with zero noisy-neighbor interference.
4. Maintains 99.95% service availability with seamless failover across LLM providers.

### Action
I designed and spearheaded the deployment of **SentinelRAG**, utilizing an asynchronous pipeline in Python/FastAPI, Redis 7, and PostgreSQL 16:
- **Tenant-Isolated Atomic Rate Limiting:** Implemented a distributed Token-Bucket algorithm inside Redis using atomic Lua scripts. Each tenant was provisioned an isolated bucket with configurable sustained rates and burst tolerances, evaluated in $< 2\text{ms}$ at the network edge.
- **Hierarchical Semantic Caching:** Integrated Redis vector similarity search using Cosine distance on 1536-dimensional query embeddings. Queries with distance $\le 0.08$ were resolved directly from RAM in 18ms, bypassing downstream retrieval and model inference.
- **Circuit-Breaker Multi-Provider Fallback:** Built an asynchronous model router via LiteLLM integrated with a sliding-window circuit breaker. If the primary provider (OpenAI) threw $5\text{xx}$ errors or timed out (>3000ms) on 3 consecutive calls, the circuit tripped to `OPEN`, immediately routing requests to Anthropic Claude and Groq Llama-3 without client failure.
- **Unified Hybrid Storage:** Consolidated disparate relational and vector databases into PostgreSQL 16 with `pgvector` (HNSW) and `tsvector` (BM25), eliminating multi-database network hops and achieving sub-100ms retrieval fused via Reciprocal Rank Fusion.

### Result
- **Cost Reduction:** Achieved a **54.2% reduction in monthly token billing** within the first 30 days due to a 48% semantic cache hit rate.
- **Latency Acceleration:** Reduced overall p95 latency from **2400ms down to 210ms** across all traffic, with cache-hit queries completing in **16.8ms**.
- **Noisy-Neighbor Elimination:** Complete eradication of cross-tenant resource starvation; tenant spikes were throttled deterministically with RFC 7807 compliant HTTP 429 payloads.
- **Uptime Improvement:** Maintained **100% gateway uptime** during a major 42-minute OpenAI API outage by autonomously diverting 100% of live traffic to Claude-3.5-Sonnet.

---

## 6. 8-Day Systems Engineering & Beyond-Project Technical Interview Curriculum

To prepare engineering leaders for rigorous Staff/Principal-level backend & infrastructure interviews that test deep systems knowledge beyond standard application code, the curriculum covers multi-topic deep dives for each milestone:

| Day | Systems Architecture & Project Focus | Beyond-Project Interview Deep-Dive Domains | Core Interview Topics & Conceptual Challenges |
| :---: | :--- | :--- | :--- |
| **Day 1** | **Async Engine, uvloop & Docker Orchestration** | **Python Internals, Concurrency & Linux Hardening** | • `uvloop` vs standard `asyncio` event loop architecture<br>• The GIL, cooperative multitasking & thread-pool offloading<br>• Docker multi-stage builds, Linux namespaces/cgroups & non-root UID security |
| **Day 2** | **Redis Semantic Cache & Lua Rate Limiting** | **Distributed Caching, State & Race Condition Prevention** | • Redis single-threaded reactor model & memory allocation (Jemalloc)<br>• Atomic Lua scripting vs optimistic transactions (`WATCH`/`MULTI`)<br>• Cache stampedes, probabilistic early expiration & sliding-window math |
| **Day 3** | **FastAPI Ingress & Prompt Sanitization** | **Networking, ASGI Protocols & API Contract Standards** | • ASGI specification internals vs WSGI synchronous execution<br>• TCP backpressure, socket buffer saturation & keep-alive reuse<br>• RFC 7807 problem details & enterprise security attack surfaces |
| **Day 4** | **Hybrid Retrieval (HNSW + BM25) & Fusion** | **Database Engines, Vector Math & Indexing Trade-offs** | • PostgreSQL MVCC, write-ahead logs (WAL) & connection pool sizing<br>• `asyncpg` binary wire protocol vs text protocols<br>• Vector indexing mathematics: HNSW graph search vs IVFFlat Voronoi cells |
| **Day 5** | **Resilient LLM Router & Circuit Breakers** | **Distributed Reliability, Cascading Failures & Resilience** | • Sliding-window Circuit Breaker finite state machines (`CLOSED`/`OPEN`/`HALF-OPEN`)<br>• Mitigating cascading failures via bulkhead isolation & load shedding<br>• Exponential backoff with decorrelated jitter and timeout budget propagation |
| **Day 6** | **Telemetry, Structured Logging & Metrics** | **Observability Systems, High-Cardinality & Monitoring** | • Structured JSON logging vs text parsing overhead in high-throughput systems<br>• Prometheus metrics: Histograms vs Summaries & quantile calculation math<br>• OpenTelemetry context propagation across asynchronous boundaries |
| **Day 7** | **Interactive Gateway Dashboard & UI** | **Streaming Protocols, Sockets & Latency Profiling** | • Server-Sent Events (SSE) vs WebSockets vs HTTP Chunked Transfer Encoding<br>• Profiling p95/p99 waterfall latency distributions across distributed microservices<br>• Token accounting, cost tracking & rate-quota synchronization |
| **Day 8** | **Container Compose, CI/CD & Cloud Orchestration** | **Infrastructure as Code, Zero-Downtime & Chaos Engineering** | • Health check probes (`liveness` vs `readiness`) & graceful termination (`SIGTERM`)<br>• Reproducible builds with cryptographic lockfiles & CI security scanning<br>• Chaos engineering: Simulating network partitions, DB failovers & partial outages |


---

## 7. Production Roadmap & Milestone Milestones

```
+───────────────────────────────────────────────────────────────────────────+
|                            8-DAY EXECUTION TIMELINE                       |
+───────────────────────────────────────────────────────────────────────────+
  DAY 0: Architecture Blueprint, PRD, Agent Directives, Core Documentation
  DAY 1: PostgreSQL 16 Schema, pgvector HNSW Setup, Asyncpg Connection Pool
  DAY 2: Redis 7 Semantic Cache Engine & Lua Token-Bucket Rate Limiter
  DAY 3: FastAPI Ingress, Pydantic v2 Contracts, Prompt Sanitization Pipeline
  DAY 4: PostgreSQL Hybrid Retrieval (HNSW + BM25) & Reciprocal Rank Fusion
  DAY 5: LiteLLM Router, Tenacity Retries & Sliding-Window Circuit Breaker
  DAY 6: structlog JSON Logging, Prometheus Metrics & Telemetry Exporter
  DAY 7: Streamlit Interactive Dashboard (Waterfall Latency & Cost Ticker)
  DAY 8: Multi-container Docker Compose, GitHub Actions CI/CD, AWS Deployment
+───────────────────────────────────────────────────────────────────────────+
```
