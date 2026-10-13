# SentinelRAG Project Mastery & Interview Battle-Station 🛡️⚡

Welcome to your central study hub for **SentinelRAG (Enterprise AI Gateway & Security Middleware)**.

This repository documentation is split into two sequential phases:
1. **Phase 1: Project Implementation Mastery** — Learn exactly how every component is built, how data flows through the system, and how the code executes from first principles.
2. **Phase 2: Technical Interview Preparation** — Master system design defenses, trade-offs, whiteboard scripts, and 56 staff/principal-grade technical interview questions with complete answers.

---

## 🗺️ The Two-Phase Study Pipeline

```
┌────────────────────────────────────────────────────────────────────────────────┐
│                     PHASE 1: PROJECT IMPLEMENTATION MASTERY                    │
│                     (Understand the entire project first)                      │
└──────────────────────────────────────┬─────────────────────────────────────────┘
                                       │
  ├── 01-project-overview.md           │ The Big Picture: elevator pitches, problem space, SLAs
  ├── 02-architecture-and-contracts.md │ Topology, component contracts, diagrams & exceptions
  ├── 03-core-concepts.md              │ 10-point first-principles deep dive into all 5 engines
  └── 04-codebase-and-trace.md         │ Codebase walkthrough + 24-step socket-to-LLM trace
                                       │
                                       ▼
┌────────────────────────────────────────────────────────────────────────────────┐
│                    PHASE 2: TECHNICAL INTERVIEW PREPARATION                    │
│                    (Prepare to explain, defend, and pass)                      │
└──────────────────────────────────────┬─────────────────────────────────────────┘
                                       │
  ├── 05-tradeoffs-and-system-design.md│ 5 defensible trade-offs + 3 STAR system design scenarios
  ├── 06-security-and-resilience.md    │ Threat models, failure cascades, testing & local debug drills
  ├── 07-technology-qa-bank.md         │ 56 staff/principal Q&As across 14 technologies (with full answers)
  └── 08-whiteboard-and-revision.md    │ 5-minute whiteboard script, code tracing drills & scorecard
```

---

## 📂 Directory Structure

### `phase-1-implementation/` (Read First)
- [01-project-overview.md](phase-1-implementation/01-project-overview.md): Problem space, 30s & 2m elevator pitches, 3 levels of explanation, feature matrix, and 7-day build order.
- [02-architecture-and-contracts.md](phase-1-implementation/02-architecture-and-contracts.md): Visual topology, component boundaries, inputs/outputs, module interaction matrix, and exception hierarchy.
- [03-core-concepts.md](phase-1-implementation/03-core-concepts.md): 10-point analysis of Token Bucket Lua, IEEE 754 float packing, pgvector HNSW, RRF ($k=60$), and Circuit Breaker state machines.
- [04-codebase-and-trace.md](phase-1-implementation/04-codebase-and-trace.md): Systematic file-by-file codebase guide + the 24-step end-to-end request trace + 3 execution paths.

### `phase-2-interview-prep/` (Read Second)
- [05-tradeoffs-and-system-design.md](phase-2-interview-prep/05-tradeoffs-and-system-design.md): 5 core architectural decisions with defensible interview answers, plus 3 STAR-format system design scenarios.
- [06-security-and-resilience.md](phase-2-interview-prep/06-security-and-resilience.md): Threat modeling, attack vectors, edge hardening, test suite execution, and local failure reproduction drills.
- [07-technology-qa-bank.md](phase-2-interview-prep/07-technology-qa-bank.md): 56 technical questions and exhaustive production-grade answers across 14 technologies (FastAPI, asyncpg, aioredis, pgvector, LiteLLM, Tenacity, structlog, Prometheus, tiktoken, Docker, Nginx, GitHub Actions, pytest, Pydantic v2).
- [08-whiteboard-and-revision.md](phase-2-interview-prep/08-whiteboard-and-revision.md): 5-minute whiteboard architecture drill, live code tracing drills, code modification drills, and readiness scorecard.

### `archive/` (Historical Reference Only)
- `archive/daily_logs/`: The 8 original chronological dev logs (Day 0 through Day 7).
- `archive/curriculum/`: The progressive curriculum drafts (PART 1 and PART 2 Days 1–7).
- `archive/supplementary.md`: The complete 7,750-line unabridged reference document.
