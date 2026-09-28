# Daily Documentation & Technical Interview Questionnaire Directives

## 1. ABSOLUTE PROHIBITION ON DSA / LEETCODE
- **STRICTLY FORBIDDEN:** Do NOT include any Data Structures & Algorithms (DSA) problems, LeetCode challenges, toy coding puzzles, or algorithmic competitive programming exercises (e.g., Two Pointers, Sliding Window toy problems, Linked List reversals) in any `daily_logs` or documentation.
- Daily logs are strictly for **Production Systems Engineering, Architecture, Deep Implementation Analysis, and Exhaustive Beyond-Project Technical Interview Questionnaires**.

## 2. MANDATORY TOPIC-WISE INTERVIEW QUESTIONNAIRE STANDARDS
Instead of superficial 2-3 questions or DSA, every daily log MUST feature a **Thorough, Multi-Topic Interview Questionnaire** containing in-depth, rigorous technical questions and authoritative, production-grade answers.

### Core Structure & Topic Coverage:
Interviewers at top tech companies evaluate candidates far beyond just the project's superficial surface code. Questions must probe underlying fundamentals, systems design, protocol internals, operating system mechanics, and failure modes.

Every daily log must organize questions topic-wise, covering (where applicable to the milestone):
1. **Python Internals & Asynchronous Concurrency**
   - ASGI specification, `uvloop` vs `asyncio`, event loop lifecycle, cooperative multitasking.
   - Event loop starvation, synchronous blocking vs non-blocking I/O, GIL mechanics, thread pool (`anyio.to_thread`) vs process pool offloading, coroutine memory overhead.
2. **Containerization, OS & Linux Security**
   - Multi-stage Docker builds, BuildKit cache mechanics, content-addressable storage.
   - Principle of least privilege, non-root users (UID/GID), Linux namespaces and cgroups, attack surface reduction, CVE elimination.
   - Packaging toolchains: Rust-based PubGrub resolution (`uv`) vs legacy single-threaded resolvers (`pip`, `poetry`).
3. **Networking, HTTP & API Gateway Protocols**
   - HTTP/1.1 vs HTTP/2 multiplexing, TCP socket backpressure, `epoll`/`kqueue` file descriptor polling.
   - RFC 7807 (Problem Details for HTTP APIs), connection pooling, keep-alives, reverse proxy buffering.
4. **Database Engines, Vector Storage & Low-Level Persistence**
   - Connection pool sizing formulas (Little's Law, CPU cores $\times 2$), `asyncpg` binary wire protocol vs text protocols.
   - PostgreSQL MVCC, write-ahead logging (WAL), pgvector indexing (HNSW graph exploration vs IVFFlat inverted files), cosine distance math.
5. **Distributed Caching, State & Redis Concurrency**
   - Redis single-threaded event loop, RESP protocol, atomic Lua scripting vs optimistic locking (`WATCH`/`MULTI`/`EXEC`).
   - Token bucket vs leaky bucket vs sliding-window rate limiters, semantic caching cosine distance thresholds, cache stampede mitigation (probabilistic early expiration / single-flight locking).
6. **System Reliability, Resilience & Failure Modes**
   - Sliding-window Circuit Breakers (`CLOSED`, `OPEN`, `HALF-OPEN`), cascading failure prevention.
   - Retry strategies with exponential backoff and decorrelated jitter, bulkhead pattern, timeout budgets across upstream hops.

### Question Depth & Answer Quality:
- **Quantity:** Comprehensive questionnaire (minimum 8-12+ deep questions organized across clear topic categories), never just 2-3 questions.
- **Answer Format:** Use STAR format (Situation, Task, Action, Result) for behavioral/architectural questions, and deep technical root-cause/architectural breakdowns for systems and theory questions.
- **Beyond-Project Scope:** Questions must explicitly explore real-world production edge cases, scaling limits, low-level OS/networking behaviors, and architectural trade-offs that extend beyond SentinelRAG's codebase.
