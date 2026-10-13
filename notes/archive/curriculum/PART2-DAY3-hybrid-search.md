# DAY 3 - Single-Store Hybrid Search (HNSW Dense + FTS Sparse + RRF)

> **Goal:** By the end of Day 3, you will understand how SentinelRack combines high-dimensional semantic vector search (pgvector HNSW) and lexical keyword search (PostgreSQL Full-Text Search) into a single-store retrieval engine, merged and re-ranked using Reciprocal Rank Fusion (RRF, $k=60$). You will be able to write and explain the repository, ranking service, Pydantic schemas, and FastAPI route from scratch.

---

## 1. The Core Engineering Problem

Why can't a production enterprise RAG system rely solely on dense vector search or solely on keyword search?

### Failure Mode 1: Pure Dense Vector Retrieval (Embedding Only)
Dense embeddings (e.g., OpenAI `text-embedding-3-small`, 1536 dimensions) compress semantic meaning into continuous geometric space. They are exceptional at conceptual synonyms:
- Query: *"automobile repair manual"* -> Matches: *"car maintenance guide"*

However, vector embeddings suffer from severe blind spots:
1. **Exact Identifier & SKU Failures:** If a user searches for an exact serial number, error code, CVE identifier, or UUID (e.g., `CVE-2024-38077`, `0x80070005`, `ERR_CONN_RESET`), dense embeddings fail. The embedding model treats unfamiliar hexadecimal tokens as rare subwords or out-of-vocabulary artifacts, matching random semantically "similar" technical text instead of the exact document containing that specific error code.
2. **Specialized Industry Acronyms & Part Numbers:** Part numbers like `C1005X7R1H104K` or contract clauses like `Section 14.3(b)` get blurred into general semantic regions.
3. **Short Keyword Queries:** Queries with 1 or 2 words often lack sufficient semantic context for embedding models to distinguish user intent.

### Failure Mode 2: Pure Sparse Lexical Search (BM25 / Full-Text Search)
Lexical search algorithms index inverted lists of exact words (lexemes). They match exact strings and count term frequencies:
- Query: *"CVE-2024-38077"* -> Finds exact matches in under 2ms.

However, lexical search fails on:
1. **The Vocabulary Mismatch Problem:** If a user asks *"How do I restart the background worker?"*, but the engineering documentation says *"Procedure to recycle the Celery daemon"*, lexical search returns **0 results**. There is zero string overlap.
2. **Conceptual and Paraphrased Queries:** Complex natural language questions where the words differ from the stored documentation.

---

## 2. Why Single-Store PostgreSQL vs. Two-Store Architecture?

Many tutorial architectures recommend running two separate search systems:
- **Vector Store:** Pinecone, Weaviate, Milvus, or Qdrant
- **Keyword Store:** Elasticsearch or OpenSearch

```
Dual-Store Architecture (Operational Nightmare):
[Client Ingest] ──────┬──────> [Elasticsearch / OpenSearch] (Keyword Index)
                      │
                      └──────> [Pinecone / Milvus]          (Vector Index)
                      
* Failure Modes:
  - Dual-write inconsistency (write succeeds to Pinecone, crashes before ES)
  - Split-brain transaction state (no atomic rollback across two external networks)
  - Network hop multiplication (2 external API calls per query)
  - Ballooning operational costs (managing 2 distributed search clusters + DB)
```

### The SentinelRack Single-Store Architecture
PostgreSQL 16 handles **both modalities in the same database engine**:
- **Dense Vector Search:** Powered by `pgvector` extension using HNSW (Hierarchical Navigable Small World) index over 1536-dimensional vectors.
- **Sparse Lexical Search:** Powered by PostgreSQL native Full-Text Search (FTS) using `tsvector`, `websearch_to_tsquery`, and GIN (Generalized Inverted Index).

```
SentinelRack Single-Store Architecture:
[Client Ingest] ─────────────> [PostgreSQL 16] (Single ACID Transaction)
                                ├── document_chunks table
                                ├── HNSW index on embedding column
                                └── GIN index on search_vector column
```

**Architectural Advantages:**
1. **ACID Transactional Consistency:** A chunk insert is atomic. If the transaction rolls back, neither the vector nor the text index is left orphaned.
2. **Zero Synchronization Pipelines:** No Kafka connectors, no Debezium CDC pipelines, and no synchronization lag between vector store and keyword index.
3. **Multi-Tenant Row-Level Filtering:** Both searches filter on `WHERE tenant_id = :tenant_id` at the index level, preventing data leakage across organizational boundaries.
4. **Massive Cost Reduction:** Zero licensing fees for third-party vector databases.

---

## 3. Mathematical Foundations: Distance Metrics & Score Incompatibility

### Dense Similarity: Cosine Distance in pgvector
In high-dimensional space, the angle between two embedding vectors reflects semantic similarity.

The cosine similarity between vectors $\mathbf{u}$ and $\mathbf{v}$ is:
$$\text{Cosine Similarity}(\mathbf{u}, \mathbf{v}) = \frac{\mathbf{u} \cdot \mathbf{v}}{\|\mathbf{u}\| \|\mathbf{v}\|}$$

In `pgvector`, the `<=>` operator computes **Cosine Distance**:
$$d = 1 - \text{Cosine Similarity}(\mathbf{u}, \mathbf{v})$$
Because cosine similarity ranges from $[-1.0, 1.0]$, cosine distance $d$ ranges from $[0.0, 2.0]$:
- $d = 0.0$: Identical direction (angle $= 0^\circ$, similarity $= 1.0$)
- $d = 1.0$: Orthogonal vectors (angle $= 90^\circ$, similarity $= 0.0$)
- $d = 2.0$: Directly opposite vectors (angle $= 180^\circ$, similarity $= -1.0$)

In SentinelRack, cosine distance is translated into normalized similarity score bounded in $[0.0, 1.0]$:
$$\text{similarity\_score} = \max(0.0, 1.0 - d)$$

### Sparse Lexical Ranking: `ts_rank_cd` (Cover Density)
PostgreSQL provides two full-text ranking functions:
- `ts_rank`: Based strictly on frequency of matching lexemes.
- `ts_rank_cd`: Based on **Cover Density**. It calculates how close together the matching query words appear in the document chunk. Matching words grouped closely together in the same sentence receive significantly higher scores than words scattered across different paragraphs.

### The Score Incompatibility Trap
Why can't we simply combine them with a weighted sum?
$$\text{Score} \neq \alpha \cdot \text{DenseScore} + (1 - \alpha) \cdot \text{SparseScore}$$

**Why this fails catastrophically:**
1. **Unmatched Ranges:** `DenseScore` is strictly bounded $[0.0, 1.0]$. `ts_rank_cd` is unbounded $[0.0, \infty)$ and depends entirely on chunk length and keyword density. A short document with repetitive keywords can produce a `ts_rank_cd` of $0.85$, while a long document with exact matches might produce $0.04$.
2. **Distribution Variance:** Vector similarity distributions are heavily clustered (e.g., most candidate scores fall tightly between $0.72$ and $0.88$). FTS scores are sparse with long tails.
3. **Score Normalization Fragility:** Attempting Min-Max normalization ($\frac{s - \min}{\max - \min}$) over a single query's candidate pool makes results hypersensitive to outliers. A single outlier candidate crushes the normalized scores of all other candidates.

---

## 4. Reciprocal Rank Fusion (RRF): The Solution

To merge results from disparate search engines without relying on incompatible raw scores, SentinelRack implements **Reciprocal Rank Fusion (RRF)**, formulated by Cormack, Clarke, and Büttcher (2009).

### The Mathematical Formula
Instead of looking at raw scores, RRF evaluates **ordinal ranks**:

$$\text{RRF\_Score}(d) = \sum_{m \in M} \frac{1}{k + r_m(d)}$$

Where:
- $M$: The set of retrieval modalities ($M = \{\text{dense}, \text{sparse}\}$).
- $r_m(d)$: The **1-based rank** of document chunk $d$ in retrieval modality $m$ (1st place $= 1$, 2nd place $= 2$, etc.). If document $d$ did not appear in the top candidate pool for modality $m$, its score component for that modality is 0.
- $k$: The **smoothing constant** (default $k = 60$).

### Why $k = 60$?
1. **Dampening Rank Dominance:** Without $k$ (i.e., $\frac{1}{r}$):
   - Rank 1 gets $\frac{1}{1} = 1.000$
   - Rank 2 gets $\frac{1}{2} = 0.500$
   - The drop from rank 1 to rank 2 is $50\%$. A document ranked 1st in only one list would overwhelmingly defeat a document ranked 2nd in **both** lists ($1.0 > 0.5 + 0.5$).
2. **With $k = 60$:**
   - Rank 1 gets $\frac{1}{61} \approx 0.016393$
   - Rank 2 gets $\frac{1}{62} \approx 0.016129$
   - The drop from rank 1 to rank 2 is subtle ($\approx 1.6\%$).
   - A document ranked 2nd in both dense and sparse retrieval gets:
     $$\frac{1}{62} + \frac{1}{62} = \frac{2}{62} \approx 0.032258$$
   - A document ranked 1st in dense retrieval but missing entirely from sparse retrieval gets:
     $$\frac{1}{61} + 0 = 0.016393$$
   - **Result:** The consensus document that performed strongly across *both* semantic and lexical retrieval easily wins over a document that only performed well in one system.

---

## 5. Concurrency Architecture: The AsyncSession Socket Trap

When fetching dense and sparse candidates, an advanced beginner might be tempted to run them concurrently using `asyncio.gather`:

```python
# DANGEROUS BUG - DO NOT DO THIS WITH A SINGLE AsyncSession
dense_task = repo.search_dense(tenant_id, query_vector)
sparse_task = repo.search_sparse(tenant_id, query_text)
dense_candidates, sparse_candidates = await asyncio.gather(dense_task, sparse_task)
```

### Why this crashes in production
A SQLAlchemy `AsyncSession` wraps a single underlying `asyncpg` connection (a single TCP socket connected to PostgreSQL).
- TCP sockets are streaming protocols. An active SQL query writes to the socket buffer and awaits the server response stream.
- If two coroutines attempt to call `session.execute()` concurrently on the same session, both coroutines write SQL frames onto the same TCP socket simultaneously.
- `asyncpg` detects this concurrent socket usage and raises an immediate, unrecoverable exception:
  ```
  asyncpg.exceptions._base.InterfaceError: cannot perform operation: another operation is in progress
  ```

### The Solution: Sequential Execution on Single Session
Because PostgreSQL evaluates both index scans in under 5ms, SentinelRack executes them sequentially on the checked-out session:

```python
async def search_hybrid_candidates(
    self,
    tenant_id: str,
    query_text: str,
    query_vector: list[float],
    dense_limit: int = 50,
    sparse_limit: int = 50,
) -> tuple[list[DenseCandidate], list[SparseCandidate]]:
    # Execute sequentially on the single TCP connection
    dense = await self.search_dense(tenant_id, query_vector, dense_limit)
    sparse = await self.search_sparse(tenant_id, query_text, sparse_limit)
    return dense, sparse
```

*(Note: If you wanted true parallel execution at the database level, you would need to checkout two independent `AsyncSession` connections from the pool. For 5ms queries, the connection checkout overhead and pool contention outweigh any parallel execution benefit).*

---

## 6. Code Walkthrough - File by File

Let's implement and explain every component required for Day 3.

### File 1: Data Structures - `DenseCandidate`, `SparseCandidate`, `FusedSearchResult`

Notice the use of `@dataclass(slots=True)`.

```python
# app/repositories/search.py (Candidate definitions)

from dataclasses import dataclass
from app.models.document import DocumentChunk

@dataclass(slots=True)
class DenseCandidate:
    """Represents a candidate retrieved via dense vector similarity."""
    chunk: DocumentChunk
    distance: float
    similarity_score: float
    rank: int


@dataclass(slots=True)
class SparseCandidate:
    """Represents a candidate retrieved via sparse full-text search."""
    chunk: DocumentChunk
    lexical_score: float
    rank: int
```

#### Why `slots=True`?
In standard Python classes, instance attributes are stored in a dictionary called `__dict__`. While flexible, `__dict__` consumes significant heap memory per object and introduces pointer indirection.
- When `slots=True` is enabled (Python 3.10+ dataclasses), Python creates a fixed-size C array of attribute pointers instead of a heap-allocated dictionary.
- Memory usage is reduced by **~60%** per candidate object.
- Attribute access (`candidate.rank`, `candidate.distance`) is measurably faster.
- For search workloads instantiating hundreds of candidate objects per request under high concurrency, `slots=True` prevents unnecessary GC pressure.

---

### File 2: Repository Layer - `app/repositories/search.py`

```python
# app/repositories/search.py

from dataclasses import dataclass
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import DocumentChunk


@dataclass(slots=True)
class DenseCandidate:
    chunk: DocumentChunk
    distance: float
    similarity_score: float
    rank: int


@dataclass(slots=True)
class SparseCandidate:
    chunk: DocumentChunk
    lexical_score: float
    rank: int


class SearchRepository:
    """Repository handling dense semantic and sparse lexical vector retrieval."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def search_dense(
        self,
        tenant_id: str,
        query_vector: list[float],
        limit: int = 50,
    ) -> list[DenseCandidate]:
        """Retrieves top-N dense vector candidates using pgvector cosine distance (<=>)."""
        # DocumentChunk.embedding.cosine_distance generates `embedding <=> :query_vector`
        distance_expr = DocumentChunk.embedding.cosine_distance(query_vector).label("distance")

        stmt = (
            select(DocumentChunk, distance_expr)
            .where(DocumentChunk.tenant_id == tenant_id)
            .order_by(distance_expr.asc())
            .limit(limit)
        )

        result = await self._session.execute(stmt)
        # rows is a list of tuples: (DocumentChunk, float)
        rows: list[tuple[DocumentChunk, float]] = result.all()

        candidates: list[DenseCandidate] = []
        for rank_idx, (chunk, dist) in enumerate(rows, start=1):
            # Cosine distance d in [0, 2]; similarity = max(0.0, 1.0 - d)
            similarity = max(0.0, 1.0 - float(dist))
            candidates.append(
                DenseCandidate(
                    chunk=chunk,
                    distance=float(dist),
                    similarity_score=similarity,
                    rank=rank_idx,
                )
            )

        return candidates

    async def search_sparse(
        self,
        tenant_id: str,
        query_text: str,
        limit: int = 50,
    ) -> list[SparseCandidate]:
        """Retrieves top-N sparse candidates using websearch_to_tsquery and ts_rank_cd."""
        sanitized_query = query_text.strip()
        if not sanitized_query:
            return []

        # websearch_to_tsquery parses Google-style search syntax:
        # e.g., "error 404" OR "not found" -timeout
        ts_query = func.websearch_to_tsquery("english", sanitized_query)
        rank_expr = func.ts_rank_cd(DocumentChunk.search_vector, ts_query).label("rank_score")

        stmt = (
            select(DocumentChunk, rank_expr)
            .where(
                DocumentChunk.tenant_id == tenant_id,
                # The @@ operator tests if tsvector matches tsquery
                DocumentChunk.search_vector.op("@@")(ts_query),
            )
            .order_by(rank_expr.desc())
            .limit(limit)
        )

        result = await self._session.execute(stmt)
        rows: list[tuple[DocumentChunk, float]] = result.all()

        candidates: list[SparseCandidate] = []
        for rank_idx, (chunk, rank_score) in enumerate(rows, start=1):
            candidates.append(
                SparseCandidate(
                    chunk=chunk,
                    lexical_score=float(rank_score),
                    rank=rank_idx,
                )
            )

        return candidates

    async def search_hybrid_candidates(
        self,
        tenant_id: str,
        query_text: str,
        query_vector: list[float],
        dense_limit: int = 50,
        sparse_limit: int = 50,
    ) -> tuple[list[DenseCandidate], list[SparseCandidate]]:
        """Retrieves candidate pools sequentially on the session to avoid socket collision."""
        dense_candidates = await self.search_dense(
            tenant_id=tenant_id,
            query_vector=query_vector,
            limit=dense_limit,
        )
        sparse_candidates = await self.search_sparse(
            tenant_id=tenant_id,
            query_text=query_text,
            limit=sparse_limit,
        )
        return dense_candidates, sparse_candidates
```

#### Line-by-Line Technical Analysis:
1. `DocumentChunk.embedding.cosine_distance(query_vector)`:
   - This method is provided by `pgvector.sqlalchemy`. It compiles to the PostgreSQL SQL syntax:
     ```sql
     document_chunks.embedding <=> ARRAY[0.01, 0.02, ...]::vector
     ```
   - Because our index on `embedding` in Day 2 was defined with `postgresql_ops={"embedding": "vector_cosine_ops"}`, the PostgreSQL query planner chooses an **Index Scan using ix_document_chunks_embedding_hnsw**.
2. `func.websearch_to_tsquery("english", sanitized_query)`:
   - Unlike `to_tsquery`, which crashes if the user inputs special characters like `&`, `|`, or unclosed brackets, `websearch_to_tsquery` safely parses human-typed input, converting spaces to `&` (AND), quotes to phrase searches, and words preceded by `-` to `!` (NOT).
3. `DocumentChunk.search_vector.op("@@")(ts_query)`:
   - `op("@@")` instructs SQLAlchemy to render the native PostgreSQL full-text search match operator:
     ```sql
     document_chunks.search_vector @@ websearch_to_tsquery('english', '...')
     ```
   - This condition utilizes the GIN index on `search_vector`.
4. `func.ts_rank_cd(...)`:
   - Computes Cover Density ranking, scoring clusters of matching words higher than isolated occurrences.

---

### File 3: Fusion Service - `app/services/rrf.py`

```python
# app/services/rrf.py

from collections.abc import Hashable, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

from app.models.document import DocumentChunk
from app.repositories.search import DenseCandidate, SparseCandidate

# Generic type variable bound to Hashable types (like UUID, str, or int)
T = TypeVar("T", bound=Hashable)

DEFAULT_RRF_K: int = 60


@dataclass(slots=True)
class FusedSearchResult:
    """Represents a fused, deduplicated search candidate with individual component scores."""
    chunk: DocumentChunk
    fused_score: float
    dense_score: float | None = None
    dense_rank: int | None = None
    sparse_score: float | None = None
    sparse_rank: int | None = None


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[T]],
    k: int = DEFAULT_RRF_K,
) -> list[tuple[T, float]]:
    """Generic Reciprocal Rank Fusion across arbitrary ranked lists of identifiers."""
    if k < 1:
        raise ValueError(f"RRF smoothing constant k must be >= 1, got {k}")

    rrf_scores: dict[T, float] = {}

    for ranking in rankings:
        for rank_idx, item in enumerate(ranking, start=1):
            rrf_scores[item] = rrf_scores.get(item, 0.0) + (1.0 / (k + rank_idx))

    # Sort descending by fused score
    sorted_items = sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)
    return sorted_items


def fuse_hybrid_candidates(
    dense_candidates: list[DenseCandidate],
    sparse_candidates: list[SparseCandidate],
    k: int = DEFAULT_RRF_K,
    top_k: int | None = None,
) -> list[FusedSearchResult]:
    """Merges and deduplicates dense vector and sparse lexical candidates using RRF."""
    if k < 1:
        raise ValueError(f"RRF smoothing constant k must be >= 1, got {k}")

    chunk_map: dict[Any, DocumentChunk] = {}
    dense_info: dict[Any, tuple[int, float]] = {}    # chunk_id -> (rank, similarity_score)
    sparse_info: dict[Any, tuple[int, float]] = {}   # chunk_id -> (rank, lexical_score)
    fused_scores: dict[Any, float] = {}

    # Accumulate dense candidates
    for cand in dense_candidates:
        c_id = cand.chunk.id
        chunk_map[c_id] = cand.chunk
        dense_info[c_id] = (cand.rank, cand.similarity_score)
        fused_scores[c_id] = fused_scores.get(c_id, 0.0) + (1.0 / (k + cand.rank))

    # Accumulate sparse candidates
    for cand in sparse_candidates:
        c_id = cand.chunk.id
        chunk_map[c_id] = cand.chunk
        sparse_info[c_id] = (cand.rank, cand.lexical_score)
        fused_scores[c_id] = fused_scores.get(c_id, 0.0) + (1.0 / (k + cand.rank))

    # Construct fused candidate objects
    fused_results: list[FusedSearchResult] = []
    for c_id, score in fused_scores.items():
        d_rank, d_score = dense_info.get(c_id, (None, None))
        s_rank, s_score = sparse_info.get(c_id, (None, None))

        fused_results.append(
            FusedSearchResult(
                chunk=chunk_map[c_id],
                fused_score=score,
                dense_score=d_score,
                dense_rank=d_rank,
                sparse_score=s_score,
                sparse_rank=s_rank,
            )
        )

    # Sort primarily by fused_score descending, secondarily by dense similarity
    fused_results.sort(
        key=lambda r: (
            r.fused_score,
            r.dense_score if r.dense_score is not None else -1.0,
            r.sparse_score if r.sparse_score is not None else -1.0,
        ),
        reverse=True,
    )

    if top_k is not None:
        fused_results = fused_results[:top_k]

    return fused_results
```

#### Line-by-Line Technical Analysis:
1. `TypeVar("T", bound=Hashable)`:
   - TypeVar allows `reciprocal_rank_fusion` to work with any hashable item identifier (e.g., `UUID`, `str`, `int`). This makes the pure algorithm easily testable in isolation without depending on heavy database ORM objects.
2. `fused_scores[c_id] = fused_scores.get(c_id, 0.0) + (1.0 / (k + cand.rank))`:
   - If a chunk ID appears in both lists, its reciprocal rank terms add together.
   - If it appears in only one list, it gets a single term.
3. Multi-Key Sorting Tie-Breaker:
   ```python
   key=lambda r: (
       r.fused_score,
       r.dense_score if r.dense_score is not None else -1.0,
       r.sparse_score if r.sparse_score is not None else -1.0,
   )
   ```
   - Python sorts tuples element-by-element. If two documents have identical RRF scores (e.g., Doc A is #1 dense / #2 sparse, and Doc B is #2 dense / #1 sparse), Python breaks the tie using `dense_score`. If those are also equal, it uses `sparse_score`.

---

### File 4: Validation Schemas - `app/schemas/search.py`

```python
# app/schemas/search.py

import uuid
from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class HybridSearchRequest(BaseModel):
    """Request payload for hybrid search."""

    query: str = Field(
        ...,
        min_length=1,
        description="Raw search query text for sparse lexical search",
        examples=["enterprise cloud security protocols"],
    )
    query_vector: list[float] = Field(
        ...,
        min_length=1536,
        max_length=1536,
        description="1536-dimensional query embedding vector for dense semantic search",
    )
    tenant_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Tenant identifier for multi-tenant data isolation",
        examples=["tenant_acme_corp"],
    )
    top_k: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Final number of re-ranked documents to return",
    )
    dense_limit: int = Field(
        default=50,
        ge=1,
        le=200,
        description="Maximum candidates retrieved from dense vector search",
    )
    sparse_limit: int = Field(
        default=50,
        ge=1,
        le=200,
        description="Maximum candidates retrieved from sparse full-text search",
    )
    rrf_k: int = Field(
        default=60,
        ge=1,
        le=1000,
        description="Smoothing constant k for Reciprocal Rank Fusion",
    )


class HybridSearchResultItem(BaseModel):
    """Schema for an individual document chunk returned by hybrid search."""

    id: uuid.UUID = Field(..., description="Chunk unique UUID identifier")
    tenant_id: str = Field(..., description="Tenant identifier")
    document_name: str = Field(..., description="Source document name")
    chunk_text: str = Field(..., description="Text content of the retrieved chunk")
    metadata_json: dict[str, Any] = Field(..., description="Arbitrary JSONB metadata")
    fused_score: float = Field(..., description="Calculated Reciprocal Rank Fusion score")
    dense_score: float | None = Field(
        default=None,
        description="Cosine similarity score (1 - distance) from dense retrieval",
    )
    dense_rank: int | None = Field(
        default=None,
        description="1-based rank in dense retrieval candidate pool",
    )
    sparse_score: float | None = Field(
        default=None,
        description="PostgreSQL ts_rank_cd score from sparse retrieval",
    )
    sparse_rank: int | None = Field(
        default=None,
        description="1-based rank in sparse retrieval candidate pool",
    )

    model_config = ConfigDict(from_attributes=True)


class HybridSearchResponse(BaseModel):
    """Response payload containing fused search results and performance metadata."""

    status: str = Field(default="success")
    query: str
    tenant_id: str
    total_results: int
    results: list[HybridSearchResultItem]
    duration_ms: float
```

#### Why `min_length=1536` and `max_length=1536`?
Vector similarity queries in PostgreSQL will immediately raise a database exception (`ERROR: different vector dimensions 1535 and 1536`) if the client sends an incorrect dimension array. By enforcing length bounds directly in Pydantic:
- Validation occurs at the ASGI boundary before touching the database or acquiring a connection.
- A malformed request immediately returns `422 Unprocessable Entity` with a clear JSON error description, preserving database pool resources.

---

### File 5: API Route - `app/api/v1/search.py`

```python
# app/api/v1/search.py

import time
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import logger
from app.db.session import get_async_session
from app.repositories.search import SearchRepository
from app.schemas.search import (
    HybridSearchRequest,
    HybridSearchResponse,
    HybridSearchResultItem,
)
from app.services.rrf import fuse_hybrid_candidates

router = APIRouter(prefix="/search", tags=["Hybrid Search"])


@router.post(
    "/hybrid",
    response_model=HybridSearchResponse,
    status_code=status.HTTP_200_OK,
    summary="Hybrid Vector & Full-Text Search with RRF Fusion",
)
async def hybrid_search(
    payload: HybridSearchRequest,
    session: Annotated[AsyncSession, Depends(get_async_session)],
) -> HybridSearchResponse:
    """Executes hybrid retrieval combining dense vector similarity and sparse lexical search."""
    start_time = time.perf_counter()

    repo = SearchRepository(session)
    dense_candidates, sparse_candidates = await repo.search_hybrid_candidates(
        tenant_id=payload.tenant_id,
        query_text=payload.query,
        query_vector=payload.query_vector,
        dense_limit=payload.dense_limit,
        sparse_limit=payload.sparse_limit,
    )

    fused_results = fuse_hybrid_candidates(
        dense_candidates=dense_candidates,
        sparse_candidates=sparse_candidates,
        k=payload.rrf_k,
        top_k=payload.top_k,
    )

    duration_ms = round((time.perf_counter() - start_time) * 1000.0, 2)

    logger.info(
        "hybrid_search_executed",
        tenant_id=payload.tenant_id,
        dense_candidates_count=len(dense_candidates),
        sparse_candidates_count=len(sparse_candidates),
        returned_count=len(fused_results),
        duration_ms=duration_ms,
    )

    items = [
        HybridSearchResultItem(
            id=res.chunk.id,
            tenant_id=res.chunk.tenant_id,
            document_name=res.chunk.document_name,
            chunk_text=res.chunk.chunk_text,
            metadata_json=res.chunk.metadata_json,
            fused_score=round(res.fused_score, 6),
            dense_score=round(res.dense_score, 6) if res.dense_score is not None else None,
            dense_rank=res.dense_rank,
            sparse_score=round(res.sparse_score, 6) if res.sparse_score is not None else None,
            sparse_rank=res.sparse_rank,
        )
        for res in fused_results
    ]

    return HybridSearchResponse(
        status="success",
        query=payload.query,
        tenant_id=payload.tenant_id,
        total_results=len(items),
        results=items,
        duration_ms=duration_ms,
    )
```

---

## 7. Testing Strategy for Hybrid Retrieval

Testing hybrid search involves two primary suites:
1. **Algorithmic Unit Tests (`tests/test_rrf.py`):** Verify pure mathematical invariants of the fusion algorithm without spinning up database dependencies.
2. **API Mock Integration Tests (`tests/test_hybrid_search.py`):** Mock database query execution using `AsyncMock`, validating payload deserialization, candidate mapping, status codes, and response structure.

### Verifying Mathematical Correctness:
```python
# From tests/test_rrf.py
def test_reciprocal_rank_fusion_generic_formula() -> None:
    list1 = ["doc_a", "doc_b"]
    list2 = ["doc_b", "doc_c"]

    # k = 60
    # doc_a: 1 / (60 + 1) = 1/61 ~= 0.016393
    # doc_b: 1 / (60 + 2) + 1 / (60 + 1) = 1/62 + 1/61 ~= 0.032522
    # doc_c: 1 / (60 + 2) = 1/62 ~= 0.016129
    fused = reciprocal_rank_fusion([list1, list2], k=60)

    assert fused[0][0] == "doc_b"
    assert pytest.approx(fused[0][1], rel=1e-4) == (1 / 61 + 1 / 62)
```

---

## 8. Comprehensive Topic-Wise Interview Questionnaire

*(Mandatory Directive: Zero DSA/LeetCode puzzles. Exhaustive, production-grade technical answers covering high-concurrency systems, storage engines, and system architecture).*

### Topic 1: Python & ASGI Concurrency Internals

#### Q1.1: Why does calling `asyncio.gather(search_dense(), search_sparse())` on the same SQLAlchemy `AsyncSession` raise an `InterfaceError`? Detail the operating-system-level and network-socket mechanics.
**Answer:**
A SQLAlchemy `AsyncSession` is bound to a single `asyncpg.Connection`, which in turn manages a single streaming TCP socket connected to PostgreSQL. The PostgreSQL frontend/backend protocol is a message-oriented, request-response protocol: when a client initiates a query (`Query` or `Bind`/`Execute` message), the server streams back a sequence of response messages (`RowDescription`, `DataRow`, `CommandComplete`, `ReadyForQuery`). 

If two coroutines attempt to call `session.execute()` concurrently via `asyncio.gather()`, both coroutines attempt to write framing bytes to the underlying TCP buffer simultaneously and attempt to read incoming wire frames concurrently. The incoming byte stream cannot be demultiplexed without an application-level transaction identifier multiplexer (which PostgreSQL does not support on a single connection). To prevent state corruption and interleaving byte frames, `asyncpg` explicitly enforces a non-reentrant state machine lock: if any operation is initiated while another operation has not received `ReadyForQuery`, it immediately raises `asyncpg.exceptions.InterfaceError: cannot perform operation: another operation is in progress`.

#### Q1.2: What is the performance and memory implication of `@dataclass(slots=True)` on search candidate objects under high throughput?
**Answer:**
By default, Python class instances maintain a dynamic dictionary (`__dict__`) to store attribute references. Each `__dict__` is a dynamically resizable hash table that introduces memory overhead (typically 150 to 300 bytes per instance) and requires hash lookups on attribute access. 

When `slots=True` is declared, the Python interpreter eliminates `__dict__` and `__weakref__` attributes, replacing them with a fixed-size C struct array of pointers corresponding strictly to the declared dataclass fields. Under high throughput (e.g., retrieving 100 dense candidates and 100 sparse candidates per query across 500 concurrent worker tasks), `slots=True` reduces total candidate memory allocation by over 60%, drastically diminishes memory fragmentation, improves CPU L1/L2 cache locality, and minimizes garbage collection pause times by reducing the total number of objects tracked by the Python cyclic GC.

---

### Topic 2: Database & pgvector Internals

#### Q2.1: Compare HNSW (Hierarchical Navigable Small World) with IVFFlat indexing in pgvector. Why does SentinelRack mandate HNSW for production search?
**Answer:**
- **IVFFlat (Inverted File Flat):** Partitions vector space into Voronoi cells using k-means clustering. At query time, only vectors belonging to the closest centroids are scanned. 
  - *Weaknesses:* Requires an extensive offline training phase on a representative dataset before the index can be built. As new documents are ingested, centroids become stale, degrading recall unless the index is fully rebuilt. Furthermore, achieving high recall requires searching many lists (`probes`), which causes query latency to scale linearly with data size.
- **HNSW:** Constructs a multi-layer graph where lower layers contain all vectors and higher layers contain long-range "skip-list" connections.
  - *Advantages:* Incremental indexing without an upfront training step. Adding new document chunks updates the graph immediately with zero downtime. 
  - Query complexity is logarithmic $O(\log N)$, providing consistent sub-10ms latency even with millions of vectors. 
  - Recall is typically $\ge 98\%$, significantly outperforming IVFFlat under high concurrency.
- *Trade-off:* HNSW consumes substantially more RAM (for graph pointer storage) and has higher build time per inserted vector compared to IVFFlat, but for an enterprise AI gateway, predictable latency and high recall are non-negotiable.

#### Q2.2: Explain the internal mechanics of PostgreSQL `tsvector`, `tsquery`, and GIN indexes. How does `ts_rank_cd` calculate Cover Density?
**Answer:**
- **`tsvector`:** A sorted list of distinct normalized lexemes (words reduced to root forms via stemming, with stop words removed) paired with integer arrays representing their 1-based positions within the original document.
- **`tsquery`:** A boolean expression tree of search lexemes combined via logical operators (`&`, `|`, `!`, `<->`).
- **GIN (Generalized Inverted Index):** A B-tree of unique lexemes where each leaf node contains a posting list (or posting tree) of physical heap pointers (TIDs: Tuple IDs) pointing to the database rows containing that lexeme.
- **`ts_rank_cd` (Cover Density):** While standard `ts_rank` simply sums term frequency weights, `ts_rank_cd` evaluates the shortest text window (the "cover") that contains all query terms. The score is inversely proportional to the distance between matching terms: if query words *"zero"*, *"trust"*, and *"architecture"* appear adjacent within 4 words, the cover density score is dramatically higher than if they appear 80 words apart across different paragraphs.

#### Q2.3: In pgvector, what happens if an HNSW index is built with `vector_cosine_ops`, but a query executes using `<->` (L2 distance) instead of `<=>` (Cosine distance)?
**Answer:**
The PostgreSQL query planner evaluates available index operator families. The HNSW index built with `vector_cosine_ops` only registers support for the `<=>` operator. 

If the application queries using `<->` (L2 Euclidean distance), the query planner detects an operator mismatch: it cannot traverse the cosine-based HNSW graph because distance calculations and pruning heuristics differ fundamentally. As a consequence, the query planner **aborts index scan selection and falls back to a full sequential table scan (`Seq Scan`)**. Every single row in the `document_chunks` table is read from disk into shared buffers to compute Euclidean distance, increasing query latency from 3ms to hundreds or thousands of milliseconds as the table grows.

---

### Topic 3: Information Retrieval & Search Ranking Mechanics

#### Q3.1: Why is Reciprocal Rank Fusion (RRF) preferred over linear score combination ($\alpha \cdot \text{Dense} + (1 - \alpha) \cdot \text{Sparse}$) in hybrid systems?
**Answer:**
Linear score combination requires **score calibration and normalization**. However:
1. **Incompatible Probability Distributions:** Dense cosine similarity is bounded $[0, 1]$ and typically exhibits a Gaussian distribution centered around $0.75\text{--}0.85$. FTS scores (`ts_rank_cd`) are unbounded $[0, \infty)$, follow a power-law distribution, and depend directly on document length.
2. **Failure of Min-Max Normalization:** Attempting to normalize scores per query via $\frac{s - \min}{\max - \min}$ is fragile: if a single outlier document has an extremely high keyword density, it compresses the scores of all other legitimate candidates to near zero.
3. **Robustness of Ordinal Ranks:** RRF operates solely on positional rankings ($1\text{st}, 2\text{nd}, 3\text{rd}\dots$). Because ranks are uniform across all retrieval backends regardless of underlying scoring algorithms, RRF is invariant to score scaling, requires zero hyperparameter calibration across different datasets, and guarantees that items appearing in multiple candidate lists are boosted monotonically.

#### Q3.2: How does the choice of smoothing constant $k$ in RRF alter retrieval behavior? What occurs if $k \to 0$ versus $k \to \infty$?
**Answer:**
In $RRF(d) = \sum \frac{1}{k + r(d)}$:
- As $k \to 0$: The score approaches pure reciprocal rank $\frac{1}{r}$. Rank 1 receives $1.0$, while Rank 2 receives $0.5$. The disparity between top ranks is massive ($100\%$ higher for rank 1). An item that appears in 1st place in only one modality will overwhelm an item that appears in 2nd place across all modalities ($1.0 > 0.5 + 0.5$).
- As $k \to \infty$: The fraction $\frac{1}{k + r}$ approaches $\frac{1}{k}$, making the rank term $r(d)$ negligible. The algorithm degrades into a simple frequency counter (counting how many candidate lists an item appears in, with zero consideration for whether it was ranked 1st or 50th).
- At $k = 60$ (the empirically established standard): The curve balances discrimination and consensus. The drop between Rank 1 ($\frac{1}{61} \approx 0.01639$) and Rank 2 ($\frac{1}{62} \approx 0.01613$) is roughly $1.6\%$, allowing items that achieve consistent consensus across multiple retrieval engines to comfortably outrank items that spike in only one engine.

---

### Topic 4: Networking & Protocols

#### Q4.1: What is the network serialization overhead of sending a 1536-dimensional float vector in JSON over HTTP, and how does it affect gateway throughput?
**Answer:**
A 1536-dimensional vector represented as raw 32-bit IEEE 754 floats occupies $1536 \times 4 = 6,144\text{ bytes}$ (approx $6\text{ KB}$) in binary. 

However, when serialized as a JSON array of floating-point ASCII strings (e.g., `[0.0123456789012345, -0.0987654321098765, ...]`), each float requires approximately 18 to 22 ASCII characters plus commas and whitespace. This expands the payload size to **$30\text{ KB}$ to $35\text{ KB}$ per request**—a $5\times$ to $6\times$ serialization inflation.
- **CPU Overhead:** Parsing a $35\text{ KB}$ JSON array requires scanning strings, validating numbers, and allocating Python `float` objects on the heap inside the ASGI parser.
- **Network Overhead:** Increases bandwidth consumption and TCP packet fragmentation (a standard Ethernet MTU is 1500 bytes, requiring roughly 24 TCP segments per request).
- *Production Mitigation:* SentinelRack enforces payload size limits at Nginx and uses high-speed Pydantic v2 Rust core deserializers. For internal microservice communication, binary protocols like Protobuf or gRPC reduce payload size back to the native 6 KB binary boundary.

---

### Topic 5: Resilience, Reliability & Failure Modes

#### Q5.1: If the PostgreSQL database is under heavy write load and the HNSW vector search takes 250ms while lexical search takes 10ms, how can the gateway prevent request queue pileup?
**Answer:**
Under high write loads, HNSW index updates cause buffer pool contention and lock waiting. If vector searches slow to 250ms, ASGI worker threads will accumulate waiting sockets, eventually exhausting the database connection pool (`pool_timeout`).
To prevent catastrophic queue pileup:
1. **Query-Level Statement Timeouts:** Enforce strict statement timeouts on the connection before executing retrieval:
   ```sql
   SET LOCAL statement_timeout = '100ms';
   ```
2. **Circuit Breaking and Degradation:** If dense search repeatedly breaches latency thresholds or times out, the gateway can catch the timeout exception and gracefully degrade by returning **lexical-only search results** with a warning header (`X-Sentinel-Degraded: sparse-only`). Returning partial, high-precision lexical results in 15ms is vastly superior to timing out or crashing the entire gateway.
3. **Separate Read-Replica Routing:** Offload read-only hybrid search queries to dedicated read replicas equipped with high RAM allocation, isolating search workloads from primary ingestion write transactions.

---

## 9. Day 3 - What the Real Commit Would Have Contained

A Git commit for Day 3 implementation includes:
- `app/repositories/search.py`: `SearchRepository` with `search_dense`, `search_sparse`, and `search_hybrid_candidates` methods.
- `app/services/rrf.py`: Generic `reciprocal_rank_fusion` and domain-specific `fuse_hybrid_candidates` functions with tie-breaking logic.
- `app/schemas/search.py`: Pydantic v2 schemas (`HybridSearchRequest`, `HybridSearchResponse`, `HybridSearchResultItem`) with 1536-dim array validation.
- `app/api/v1/search.py`: `POST /v1/search/hybrid` FastAPI route with dependency-injected session and structured logging.
- `tests/test_rrf.py`: Comprehensive unit tests verifying RRF formula calculation and boundary conditions ($k < 1$).
- `tests/test_hybrid_search.py`: Endpoint test suite mocking repository methods and verifying schema validation errors (e.g., HTTP 422 on dimension mismatch).

---

*End of Day 3. Study the implementation and interview mechanics. When ready, reply "continue" for Day 4.*
