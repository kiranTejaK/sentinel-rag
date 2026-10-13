# DAY 2 - Database Layer

> **Goal:** By end of Day 2 you can ingest batches of document chunks (text + 1536-dim
> embedding vector + metadata) into PostgreSQL, query them back, and understand how the ORM
> model maps to actual database columns including the automatically-maintained full-text
> search vector.

---

## Concept 1 - What an ORM is (and what it is not)

**ORM** = Object-Relational Mapper. It is a library that translates between two worlds:

| Python World | SQL World |
|---|---|
| A class | A table |
| An instance of that class | A row in the table |
| An attribute on the instance | A column value |
| `session.add(obj)` | `INSERT INTO ...` |
| `session.execute(select(Model))` | `SELECT * FROM ...` |

**What an ORM is NOT:** It is not magic. Every ORM call eventually produces a SQL statement.
When you are debugging a performance problem, the first thing you do is look at the SQL being
generated (set `echo=True` on the engine during development).

**Why use SQLAlchemy's async ORM?** Because your application is async. If you used a
synchronous ORM (like Django ORM) inside an async function, the ORM would block the event
loop while waiting for PostgreSQL -- destroying the entire benefit of going async.

---

## Concept 2 - Mixins: sharing code across ORM models

Python allows a class to inherit from multiple parent classes. In SQLAlchemy we use this
to share common columns across models:

```python
class UUIDPrimaryKeyMixin:
    id: Mapped[uuid.UUID] = mapped_column(UUID(...), primary_key=True, default=uuid.uuid4)

class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

class DocumentChunk(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    # Inherits id, created_at, updated_at automatically
    chunk_text: Mapped[str] = ...
```

This is called the **mixin pattern**. You write `id`, `created_at`, `updated_at` once and
every model in your application gets them automatically.

---

## Concept 3 - pgvector and the Vector column type

PostgreSQL is a relational database -- it stores rows of typed columns. By default, it does not
know what a "vector" is or how to compute similarity between two of them.

**pgvector** is a PostgreSQL extension that adds:
1. A new column type: `vector(N)` -- stores N floating-point numbers
2. New operators: `<=>` (cosine distance), `<->` (L2 distance), `<#>` (inner product)
3. New indexes: HNSW (fast approximate nearest-neighbour search)

Without pgvector, the only way to find the most similar document would be to load ALL
embeddings into Python and compute similarity in Python. At 1 million documents with
1,536-dimensional embeddings, that is 6 GB of RAM and 10+ seconds per query.

With pgvector's HNSW index, the same query takes 2-5ms because the index does approximate
graph traversal to find near-neighbours without scanning every row.

### What HNSW means

HNSW = Hierarchical Navigable Small World. It is a graph-based index where:
- Each vector is a node
- Each node is connected to its M nearest neighbours (M=16 in our config)
- Queries traverse this graph, following the edges toward the closest node
- ef_construction=64 controls the quality/speed tradeoff during index build time

You do not need to understand HNSW deeply for interviews. Know that it is an approximate
search (might miss the absolute best match 1% of the time) that is orders of magnitude
faster than exact search.

---

## Concept 4 - The computed column for full-text search

This is one of the most powerful features of PostgreSQL that is almost never mentioned in
tutorials.

A **generated column** (PostgreSQL 12+) is a column whose value is automatically computed
by PostgreSQL from other columns in the same row. You never write to it manually.

```sql
search_vector TSVECTOR
    GENERATED ALWAYS AS (to_tsvector('english', chunk_text)) STORED
```

What this means:
- When you INSERT a row with `chunk_text = "The quick brown fox"`, PostgreSQL automatically
  computes `to_tsvector('english', 'The quick brown fox')` and stores the result in
  `search_vector`
- When you UPDATE `chunk_text`, PostgreSQL automatically updates `search_vector`
- You NEVER touch `search_vector` in application code

`to_tsvector('english', ...)` does several things:
- Tokenises the text into words
- Removes stop words ("the", "a", "is") using the English language configuration
- Applies stemming ("foxes" -> "fox", "running" -> "run")
- Returns a sorted, deduplicated list of (lexeme, positions) pairs stored compactly

The GIN index on `search_vector` makes text searches like
`search_vector @@ websearch_to_tsquery('english', 'quick fox')` take milliseconds.

---

## Concept 5 - Why UUID primary keys (not integer auto-increment)

Integer auto-increment PKs like `id SERIAL` are simple and common. But they have a problem:
they are globally sequential and thus predictable. In a multi-tenant system, a user can
enumerate other tenants' records by guessing `id=1, 2, 3, ...`.

UUIDv4 keys are randomly generated 128-bit numbers. The probability of two collisions is
astronomically low (1 in 2^61 for 10^18 records). They are safe to expose in URLs
(`/documents/550e8400-e29b-41d4-a716-446655440000`) because they cannot be guessed.

The tradeoff: UUIDs are 16 bytes vs 8 bytes for bigint, and they are less cache-friendly
for B-tree index traversal because they are random (not sequential). For this application,
the security benefit outweighs the performance cost.

---

## Concept 6 - Database migrations with Alembic

**The problem:** You define an ORM model in Python, but that only creates a Python object.
The actual table in PostgreSQL does not know about it. You need to run `CREATE TABLE` SQL.

You could run `Base.metadata.create_all(engine)` -- but this only creates tables that do not
exist yet. It will not alter an existing table if you add a new column. You need a migration
tool.

**Alembic** is the standard migration tool for SQLAlchemy. It:
1. Inspects your current ORM models
2. Inspects the current database schema
3. Generates the SQL `ALTER TABLE` / `CREATE INDEX` / `ADD COLUMN` commands to bring the DB
   in line with your models
4. Stores a version history so you can roll back

```bash
# Generate a migration based on changes to your models:
alembic revision --autogenerate -m "add document_chunks table"

# Apply the migration to the database:
alembic upgrade head
```

---

## File 1 - app/db/base.py

```python
# app/db/base.py
# SQLAlchemy 2.0 Declarative Base and reusable model mixins.

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base class for all ORM models.

    DeclarativeBase is the SQLAlchemy 2.0 way to define the ORM base.
    All model classes inherit from Base (and optionally from mixins).
    """
    pass


class UUIDPrimaryKeyMixin:
    """Mixin: injects an automated UUIDv4 primary key into any model.

    Usage: class MyModel(Base, UUIDPrimaryKeyMixin): ...
    The `id` column appears automatically in every inheriting model.
    """

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),     # store as native PostgreSQL UUID type
        primary_key=True,
        default=uuid.uuid4,     # Python generates the UUID on INSERT (before the SQL runs)
        nullable=False,
    )


def utc_now() -> datetime:
    """Returns current timezone-aware UTC datetime.

    Using datetime.now(UTC) instead of datetime.utcnow() -- the latter
    is deprecated in Python 3.12 because it returns a naive datetime
    (no timezone info) which is ambiguous and error-prone.
    """
    return datetime.now(UTC)


class TimestampMixin:
    """Mixin: injects created_at and updated_at UTC timestamps.

    `server_default=func.now()` means PostgreSQL also sets the value if
    a row is inserted WITHOUT going through SQLAlchemy (e.g., raw SQL scripts).
    `onupdate=func.now()` means PostgreSQL updates the column on every UPDATE.
    The `default=utc_now` handles it when inserting through SQLAlchemy.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
```

---

## File 2 - app/models/document.py

```python
# app/models/document.py
# DocumentChunk ORM model -- the core data entity of SentinelRack.

from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import Computed, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class DocumentChunk(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A chunk of text from a document, stored with its vector embedding.

    This is the central table. Everything in SentinelRack either reads or
    writes to document_chunks.

    Columns inherited from mixins:
    - id: UUID primary key (from UUIDPrimaryKeyMixin)
    - created_at, updated_at: UTC timestamps (from TimestampMixin)

    Columns defined here:
    - tenant_id: partitions data per customer -- all queries filter by this
    - document_name: source document identifier
    - chunk_text: the actual text content
    - metadata_json: arbitrary key-value data (page number, author, etc.)
    - embedding: the 1536-float vector from the embedding model
    - search_vector: automatically-computed tsvector for full-text search
    """

    __tablename__ = "document_chunks"

    tenant_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,   # B-tree index on tenant_id -- every query filters by this
    )
    document_name: Mapped[str] = mapped_column(String(255), nullable=False)
    chunk_text: Mapped[str] = mapped_column(Text, nullable=False)

    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        # server_default: if inserting raw SQL without SQLAlchemy, use empty JSON object
        server_default=text("'{}'::jsonb"),
    )

    embedding: Mapped[list[float]] = mapped_column(
        Vector(1536),   # pgvector column type: stores 1536 float32 values
        nullable=False,
    )

    # COMPUTED COLUMN -- PostgreSQL maintains this automatically.
    # `Computed(...)` tells SQLAlchemy: this column is controlled by PostgreSQL,
    # not by Python. SQLAlchemy will never try to INSERT or UPDATE this column.
    # persisted=True means the value is stored on disk (STORED), not recomputed
    # on every read (VIRTUAL). STORED is faster to read; uses more disk space.
    #
    # to_tsvector('english', chunk_text):
    # - tokenises the text
    # - removes English stop words ("the", "a", "is")
    # - stems words ("running" -> "run", "foxes" -> "fox")
    # - produces a sorted, compressed lexeme list
    search_vector: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', chunk_text)", persisted=True),
        nullable=True,
    )

    __table_args__ = (
        # GIN index on metadata_json: enables fast queries like
        # WHERE metadata_json @> '{"author": "Smith"}'
        Index("ix_document_chunks_metadata_json", "metadata_json", postgresql_using="gin"),

        # GIN index on search_vector: enables fast full-text queries like
        # WHERE search_vector @@ websearch_to_tsquery('english', 'quick fox')
        Index("ix_document_chunks_search_vector", "search_vector", postgresql_using="gin"),

        # HNSW index on embedding: enables fast approximate nearest-neighbour search.
        # vector_cosine_ops: use cosine distance as the similarity metric
        # m=16: each node connects to 16 neighbours (higher = more accurate, more memory)
        # ef_construction=64: explore 64 candidates while building the index
        #   (higher = more accurate index, slower to build)
        Index(
            "ix_document_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<DocumentChunk(id={self.id}, tenant='{self.tenant_id}', "
            f"doc='{self.document_name}', len={len(self.chunk_text)})>"
        )
```

**What to notice:**
- Three kinds of indexes are used for three different query types: B-tree (equality),
  GIN (full-text and JSONB containment), HNSW (vector similarity)
- `search_vector` uses `Computed(...)` which tells SQLAlchemy "do not include this in INSERT
  or UPDATE statements -- PostgreSQL owns this column"
- The HNSW index specifies `vector_cosine_ops` -- this MUST match the operator used in queries
  (`<=>` is cosine distance). Using a different operator at query time would not use the index

---

## File 3 - app/schemas/document.py

```python
# app/schemas/document.py
# Pydantic schemas for document ingestion API request and response.
# These are DIFFERENT from ORM models: schemas validate API input/output.
# ORM models represent database tables.

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class DocumentChunkInput(BaseModel):
    """Input schema for a single document chunk in a batch ingest request."""

    document_name: str = Field(..., min_length=1, max_length=255)
    chunk_text: str = Field(..., min_length=1, max_length=50000)
    embedding: list[float] = Field(
        ...,
        # The embedding must be exactly 1536 floats -- min_length and max_length
        # on a list check the list's length, not string length
        min_length=1536,
        max_length=1536,
        description="1536-dimensional dense vector from the embedding model",
    )
    metadata_json: dict[str, Any] = Field(default_factory=dict)


class BatchIngestRequest(BaseModel):
    """Request body for POST /v1/documents/batch."""

    tenant_id: str = Field(..., min_length=1, max_length=64)
    chunks: list[DocumentChunkInput] = Field(..., min_length=1, max_length=1000)


class BatchIngestResponse(BaseModel):
    """Response body for a successful batch ingest."""

    status: str
    tenant_id: str
    inserted_count: int
    chunk_ids: list[UUID]
    duration_ms: float
```

**Schema vs ORM model -- what is the difference?**

| Schema (Pydantic) | ORM Model (SQLAlchemy) |
|---|---|
| Validates API input/output | Represents a database table |
| Lives at API boundary | Used in database queries |
| No database connection needed | Requires session to query/insert |
| Can have fields that don't exist in DB | Must match actual DB schema |
| Used for request parsing and serialization | Used for row insertion and retrieval |

For example: `BatchIngestRequest` is what the client sends. `DocumentChunk` is what gets
saved to the database. The route handler translates between them.

---

## File 4 - app/api/v1/documents.py

```python
# app/api/v1/documents.py
# Document ingestion API routes.

import time
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import logger
from app.db.session import get_async_session
from app.models.document import DocumentChunk
from app.schemas.document import BatchIngestRequest, BatchIngestResponse

router = APIRouter(prefix="/documents", tags=["Document Ingestion"])


@router.post(
    "/batch",
    response_model=BatchIngestResponse,
    status_code=status.HTTP_201_CREATED,  # 201 Created (not 200 OK) for resource creation
)
async def batch_ingest_documents(
    payload: BatchIngestRequest,
    # Depends(get_async_session) is FastAPI dependency injection.
    # FastAPI calls get_async_session(), gets the yielded AsyncSession,
    # and passes it here as `session`.
    # Annotated[AsyncSession, Depends(...)] is the modern syntax for this.
    session: Annotated[AsyncSession, Depends(get_async_session)],
) -> BatchIngestResponse:
    """Batch insert document chunks into PostgreSQL.

    Uses PostgreSQL's INSERT ... RETURNING to get all inserted UUIDs in one round-trip.
    The `search_vector` computed column is populated automatically by PostgreSQL.
    The HNSW and GIN indexes are updated automatically by PostgreSQL on insert.
    """
    start_time = time.perf_counter()

    # Build a list of dicts -- one per chunk. This is what SQLAlchemy's
    # bulk insert expects: a list of column-name -> value mappings.
    chunk_records = [
        {
            "tenant_id": payload.tenant_id,
            "document_name": chunk.document_name,
            "chunk_text": chunk.chunk_text,
            "metadata_json": chunk.metadata_json,
            "embedding": chunk.embedding,
            # search_vector is NOT included -- it is computed by PostgreSQL
        }
        for chunk in payload.chunks
    ]

    # sqlalchemy.dialects.postgresql.insert is the PostgreSQL-specific INSERT.
    # It is different from the generic sqlalchemy.insert because it supports
    # PostgreSQL extensions like ON CONFLICT DO UPDATE (upsert) and RETURNING.
    stmt = insert(DocumentChunk).returning(DocumentChunk.id)

    # session.scalars() executes the statement and returns the scalar result column.
    # Since we are RETURNING only DocumentChunk.id (a UUID), each result is a UUID.
    result = await session.scalars(stmt, chunk_records)
    chunk_ids = list(result.all())

    # Explicit commit: the session is in autocommit=False mode (the default).
    # Without this commit, the INSERT would be rolled back when the session closes.
    await session.commit()

    duration_ms = round((time.perf_counter() - start_time) * 1000.0, 2)

    logger.info(
        "document_chunks_batch_ingested",
        tenant_id=payload.tenant_id,
        count=len(chunk_ids),
        duration_ms=duration_ms,
    )

    return BatchIngestResponse(
        status="success",
        tenant_id=payload.tenant_id,
        inserted_count=len(chunk_ids),
        chunk_ids=chunk_ids,
        duration_ms=duration_ms,
    )
```

**What to notice:**
- `Annotated[AsyncSession, Depends(get_async_session)]`: the modern FastAPI way to inject
  dependencies. The type hint `AsyncSession` is for IDE type checking; the `Depends(...)` is
  for FastAPI runtime injection.
- `sqlalchemy.dialects.postgresql.insert` (not `sqlalchemy.insert`): we use the
  PostgreSQL-specific version to access the `RETURNING` clause
- `session.scalars(stmt, chunk_records)`: the `chunk_records` list is the bulk insert data;
  SQLAlchemy generates a single multi-row INSERT statement

---

## How Alembic migrations connect to this

After defining the ORM model, you need to create the table in PostgreSQL. Alembic does this:

```
alembic/
    env.py            -- migration environment (connects to DB, imports your Base)
    versions/
        001_initial.py -- the generated migration script
```

A generated migration for DocumentChunk looks approximately like:

```python
def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")  # enable pgvector

    op.create_table(
        'document_chunks',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('tenant_id', sa.String(length=64), nullable=False),
        sa.Column('chunk_text', sa.Text(), nullable=False),
        sa.Column('embedding', vector.VECTOR(dim=1536), nullable=False),
        sa.Column('search_vector', postgresql.TSVECTOR(),
                  sa.Computed("to_tsvector('english', chunk_text)", persisted=True),
                  nullable=True),
        # ... other columns
    )

    # Create HNSW index
    op.create_index(
        'ix_document_chunks_embedding_hnsw', 'document_chunks', ['embedding'],
        postgresql_using='hnsw',
        postgresql_with={'m': 16, 'ef_construction': 64},
        postgresql_ops={'embedding': 'vector_cosine_ops'},
    )
```

You run `alembic upgrade head` and PostgreSQL creates the table, computed column, and all
three indexes. From that point on, inserting a row with `chunk_text` automatically populates
`search_vector`. No application code handles that.

---

## Day 2 - Knowledge Check

**Q1.** Why does the DocumentChunk model NOT include `search_vector` in the list of columns
being inserted (`chunk_records`)? Would the INSERT fail if you tried to include it?

**Q2.** The HNSW index specifies `postgresql_ops={"embedding": "vector_cosine_ops"}`.
What does this mean and what would happen if you queried with `<->` (L2 distance) instead
of `<=>` (cosine distance) after building the index with `vector_cosine_ops`?

**Q3.** Why is `Annotated[AsyncSession, Depends(get_async_session)]` used instead of the
older `session: AsyncSession = Depends(get_async_session)` syntax?

**Q4.** The batch ingest route calls `await session.commit()` explicitly. But `get_async_session`
also calls `await session.commit()` in its `try` block. Is this a double commit?

**Q5.** What is the difference between `sqlalchemy.insert` and
`sqlalchemy.dialects.postgresql.insert`? When does the distinction matter?

**Q6.** `UUIDPrimaryKeyMixin` sets `default=uuid.uuid4`. This means the UUID is generated
in Python before the SQL runs. What is the alternative (letting PostgreSQL generate the UUID),
and what is the practical advantage of Python-side generation?

**Q7.** `server_default=func.now()` is set on `created_at` in addition to `default=utc_now`.
Why are both needed? When would one fire and the other not?

**Q8.** Why are three different index types used (B-tree, GIN, HNSW) instead of three B-tree
indexes?

---

### Answers

**A1.** `search_vector` is a GENERATED ALWAYS column, which PostgreSQL controls entirely.
If you try to INSERT a value for it, PostgreSQL raises an error:
`ERROR: column "search_vector" can only be updated to DEFAULT`. SQLAlchemy knows about the
`Computed(...)` definition and automatically excludes that column from INSERT statements.

**A2.** `vector_cosine_ops` means the HNSW index is built using cosine distance metric.
If you query using `<->` (L2/Euclidean distance), the query planner cannot use the HNSW index
(different metric means different graph topology) and falls back to a sequential scan. The
query still returns correct results but scans every row -- extremely slow at scale.

**A3.** `Annotated[T, Depends(...)]` separates type information from runtime injection,
which makes the code more readable in IDEs (the type hint `AsyncSession` is immediately
visible without needing to follow the `=` default). It is also cleaner in Python's type
system because `session: AsyncSession = Depends(...)` gives the variable a non-AsyncSession
default value, which confuses type checkers. `Annotated` is the recommended pattern since
FastAPI 0.95+.

**A4.** No. The explicit `await session.commit()` inside the route handler commits the
transaction. When control returns to `get_async_session()`, the `try` block executes
`await session.commit()` again -- but committing an already-committed transaction in
SQLAlchemy is a no-op (it starts a new empty transaction and commits it, which does nothing).
The `finally: await session.close()` still runs and returns the connection to the pool.

**A5.** `sqlalchemy.insert` is the generic, database-agnostic INSERT builder. It does not
support PostgreSQL-specific clauses like `RETURNING` or `ON CONFLICT DO UPDATE`.
`sqlalchemy.dialects.postgresql.insert` is the PostgreSQL-specific version that supports these.
The distinction matters when you need to retrieve generated values (like UUIDs or auto-increment
IDs) from an INSERT in a single round-trip.

**A6.** Python-side UUID generation means you know the ID before the INSERT executes. This
is useful for: (a) building the response object before waiting for the database confirmation,
(b) using the ID in related inserts without an extra SELECT, (c) logging the ID immediately.
Server-side generation requires a `RETURNING id` clause and an extra round-trip to get the ID.

**A7.** `default=utc_now` fires when SQLAlchemy creates an ORM object in Python and inserts
it via the ORM (`session.add(obj)`). `server_default=func.now()` fires when someone inserts
directly via raw SQL (e.g., a migration script, a database admin, a data load tool). Both
are needed to handle all insertion paths.

**A8.** B-tree indexes support equality (`=`), range (`<`, `>`), and sort operations.
They are useless for full-text search (need inverted index) and for vector similarity
(need graph-based ANN). GIN (Generalized Inverted Index) is optimised for tsvector and JSONB
containment queries -- it maintains a posting list per lexeme. HNSW is a graph-based index
for approximate nearest-neighbour search in high-dimensional spaces. Using B-tree for any
of these would result in sequential scans (no index used), making queries thousands of times
slower.

---

## Day 2 - What the Real Commit Would Have Contained

- `app/db/base.py`: DeclarativeBase, UUIDPrimaryKeyMixin, TimestampMixin with UTC timestamps
- `app/models/document.py`: DocumentChunk model with Vector(1536), Computed TSVECTOR,
  HNSW index, two GIN indexes, B-tree tenant_id index
- `app/schemas/document.py`: DocumentChunkInput, BatchIngestRequest, BatchIngestResponse Pydantic
  schemas with length constraints
- `app/api/v1/documents.py`: batch_ingest_documents route with PostgreSQL RETURNING clause
- `alembic/versions/001_initial_schema.py`: migration creating document_chunks table,
  enabling pgvector extension, creating all indexes

---

*End of Day 2. Study the Q&A. When ready, reply "continue" for Day 3.*
