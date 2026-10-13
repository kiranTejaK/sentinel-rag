# DAY 1 - ASGI Foundation

> **Goal:** By end of Day 1 you have a runnable FastAPI server that:
> - Reads and validates all configuration from environment variables
> - Connects to PostgreSQL and Redis using async connection pools
> - Emits structured JSON logs
> - Answers GET /healthz with real connectivity checks
> - Has a correlation-ID middleware that tags every log line with a request ID
>
> No AI. No search. No caching yet. Just foundations.

---

## Concept 1 - Why async-first?

### The problem with synchronous code

In a standard Python web server each request is handled by one thread. When that thread
needs to wait for PostgreSQL to respond, it sits there blocked -- doing nothing, just waiting.
While it waits, it cannot serve any other request.

To handle many concurrent requests you need many threads. Threads are expensive: ~8 MB of
stack memory each. At 1,000 concurrent users you need 1,000 threads = 8 GB of RAM just for
thread stacks.

### The async solution

Async Python uses a single thread called the **event loop**. When async code needs to wait
for I/O it says "I am waiting -- give CPU to another task." The event loop switches immediately.

```
Event Loop (single thread):
  Task A: [process] [YIELD: waiting for DB]  ...returns later... [continue] [done]
  Task B:                   [process] [YIELD: waiting for Redis] ...returns... [done]
  Task C:                                  [process] [YIELD: waiting for LLM] ...
```

One thread can handle thousands of concurrent I/O-bound tasks.

**The golden rule:** Any function that does I/O must be defined with `async def` and called
with `await`. If you forget `await`, Python creates the coroutine object but never runs it.
This is one of the most common async bugs and produces no error -- the operation silently
never happens.

---

## Concept 2 - What ASGI is

**ASGI** = Asynchronous Server Gateway Interface. It is a standard contract between:
- **Uvicorn** -- receives raw TCP connections, speaks HTTP
- **FastAPI** -- receives structured request objects, runs your route handlers

When you run `uvicorn app.main:app`, Uvicorn hands each request to FastAPI via the ASGI
contract, which is a callable: `async def app(scope, receive, send)`. You never write this
directly, but understanding it explains why middleware "wraps" your app: each middleware is
an ASGI callable that calls the next one in the chain before doing its own work.

---

## Concept 3 - Pydantic BaseSettings

Your project never calls `os.environ.get("DATABASE_URL")` anywhere except inside one class.

### Without BaseSettings (fragile)

```python
import os

db_url = os.environ.get("DATABASE_URL")  # None if missing -- no error, silently broken
pool_size = int(os.environ.get("DB_POOL_SIZE", "20"))  # manual type conversion, no validation
```

### With BaseSettings (robust)

```python
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    DATABASE_URL: str           # missing = ValidationError at import time, not request time
    DB_POOL_SIZE: int = 20      # "20" string from env auto-converted to Python int
    ENVIRONMENT: Literal["development", "production"] = "development"  # enum validation
```

Pydantic validates everything when `Settings()` is created -- at import time. Your app fails
immediately on startup with a clear message if anything is wrong.

---

## Concept 4 - The lifespan pattern

**The problem:** You need to open a connection pool at startup and close it at shutdown.

You cannot put async code at module level:

```python
# WRONG
engine = await create_async_engine(...)  # SyntaxError: cannot await at module level
```

You cannot use `@app.on_event("startup")` -- deprecated in modern FastAPI.

**The solution:** `@asynccontextmanager` + `lifespan`:

```python
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # === STARTUP (before yield) ===
    db_manager.initialize()
    await db_manager.ping()  # verify connectivity before serving requests
    yield
    # === SHUTDOWN (after yield) ===
    await db_manager.close()

app = FastAPI(lifespan=lifespan)
```

The `yield` keyword is the key. Everything before yield runs at startup. Everything after
yield runs at shutdown. This is a Python context manager -- the same mechanism as
`with open(file) as f:` -- applied to the entire application lifecycle.

---

## Concept 5 - Structured logging with structlog

Standard Python logging produces:
```
2026-10-13 08:30:00 INFO  Connected to database
```

structlog produces:
```json
{"event": "db_connected", "latency_ms": 12.4, "request_id": "req_abc123", "timestamp": "..."}
```

Why? In production you ship logs to Datadog, CloudWatch, or ELK. These tools need
machine-readable JSON to build dashboards and alerts. You cannot alert on "all requests
from tenant acme that exceeded 500ms" if your logs are unstructured text.

structlog's key feature: **context binding via contextvars**.

```python
# In middleware, once per request:
structlog.contextvars.bind_contextvars(request_id=req_id, tenant_id=tid)

# Then in ANY function called during this request -- even nested functions:
logger.info("cache_miss")
# Output: {"event": "cache_miss", "request_id": "req_abc", "tenant_id": "acme", ...}
```

The values propagate automatically via Python's `contextvars` module -- per-async-task
storage (think async-safe thread-local storage).

---

## File 1 - app/core/config.py

```python
# app/core/config.py
# Single source of truth for all application settings.
# Every other module imports `settings` from here.

from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """SentinelRack Core Application Settings.

    Pydantic v2 BaseSettings automatically:
    1. Reads matching environment variables
    2. Falls back to .env file if not in the environment
    3. Validates types and constraints, raising ValidationError at startup if wrong
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,   # DATABASE_URL != database_url
        extra="ignore",        # do not error on unknown env vars
    )

    # Server and Environment
    ENVIRONMENT: Literal["development", "staging", "production", "test"] = Field(
        default="development"
    )
    DEBUG: bool = Field(default=False)
    HOST: str = Field(default="0.0.0.0")
    PORT: int = Field(default=8000)
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO"
    )
    APP_SECRET: str = Field(
        default="dev-insecure-secret-key-change-in-production-32bytes",
        min_length=16,
    )

    # Database
    # IMPORTANT: must be postgresql+asyncpg:// -- the +asyncpg tells SQLAlchemy
    # to use the async driver. Without it SQLAlchemy uses the sync driver and
    # blocks the event loop on every query.
    DATABASE_URL: str = Field(
        default="postgresql+asyncpg://sentinel:sentinel_secret@localhost:5432/sentinel_db"
    )
    DB_POOL_SIZE: int = Field(default=20, ge=5, le=100)
    DB_MAX_OVERFLOW: int = Field(default=10, ge=0, le=50)
    DB_TIMEOUT_SECONDS: int = Field(default=30, ge=1, le=120)

    # Redis
    REDIS_URL: str = Field(default="redis://localhost:6379/0")
    REDIS_POOL_SIZE: int = Field(default=50, ge=10, le=200)
    REDIS_TIMEOUT_MS: int = Field(default=500, ge=50, le=5000)

    # Semantic Cache
    # IMPORTANT: This is a cosine DISTANCE threshold, not similarity.
    # distance 0.08 = similarity 0.92  (distance = 1 - similarity)
    SEMANTIC_CACHE_THRESHOLD: float = Field(default=0.08, ge=0.01, le=0.50)
    SEMANTIC_CACHE_TTL_SECONDS: int = Field(default=86400)  # 24 hours
    EMBEDDING_MODEL: str = Field(default="text-embedding-3-small")
    EMBEDDING_DIMENSION: int = Field(default=1536, ge=128, le=4096)

    # Rate Limiting
    RATE_LIMIT_DEFAULT_CAPACITY: int = Field(default=100, ge=1)
    RATE_LIMIT_DEFAULT_REFILL_RATE_PER_SEC: int = Field(default=10, ge=1)

    # LLM Router
    DEFAULT_PRIMARY_MODEL: str = Field(default="groq/llama-3.3-70b-versatile")
    DEFAULT_FALLBACK_MODEL_1: str = Field(default="openai/gpt-4o-mini")
    DEFAULT_FALLBACK_MODEL_2: str = Field(default="mistral/mistral-small-latest")

    # Provider API Keys (optional -- app checks for None before calling each provider)
    OPENAI_API_KEY: str | None = Field(default=None)
    ANTHROPIC_API_KEY: str | None = Field(default=None)
    GROQ_API_KEY: str | None = Field(default=None)
    MISTRAL_API_KEY: str | None = Field(default=None)

    # Circuit Breaker
    CIRCUIT_BREAKER_FAILURE_THRESHOLD: int = Field(default=5, ge=1, le=10)
    CIRCUIT_BREAKER_RECOVERY_TIME_SECONDS: int = Field(default=60, ge=5, le=300)
    CIRCUIT_BREAKER_TIMEOUT_SECONDS: float = Field(default=4.0, ge=0.5, le=60.0)

    # Field validators run AFTER type coercion.
    # @classmethod is required by Pydantic v2 for field_validator.
    @field_validator("DATABASE_URL")
    @classmethod
    def validate_asyncpg_protocol(cls, v: str) -> str:
        if not v.startswith("postgresql+asyncpg://"):
            raise ValueError("DATABASE_URL must start with 'postgresql+asyncpg://'")
        return v

    @field_validator("REDIS_URL")
    @classmethod
    def validate_redis_protocol(cls, v: str) -> str:
        if not (v.startswith("redis://") or v.startswith("rediss://")):
            raise ValueError("REDIS_URL must begin with 'redis://' or 'rediss://'")
        return v


# Module-level singleton. All validation runs immediately at import time.
settings = Settings()
```

**What to notice:**
- `Literal["development", "staging", "production", "test"]` -- Pydantic rejects any other string
- `ge=5, le=100` on DB_POOL_SIZE -- "greater than or equal to 5, less than or equal to 100"
- `str | None = Field(default=None)` on API keys -- they are optional at config time; the
  code checks for None before making calls to that provider
- The `settings = Settings()` at the bottom runs all validation at import time

---

## File 2 - app/core/logging.py

```python
# app/core/logging.py
# Configure structlog for structured JSON logging.

import logging
import sys

import structlog

from app.core.config import settings


def setup_logging() -> None:
    """Configure the structlog pipeline. Called once in lifespan startup."""

    # Configure standard library logging -- structlog wraps it, so both need setup
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
        stream=sys.stdout,
        format="%(message)s",  # structlog handles formatting
    )

    structlog.configure(
        processors=[
            # Pull in anything bound via structlog.contextvars.bind_contextvars()
            # This is how request_id appears in every log line for a given request.
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,            # adds "level": "info"
            structlog.processors.TimeStamper(fmt="iso"), # adds "timestamp": "..."
            structlog.processors.StackInfoRenderer(),   # formats stack traces inline
            structlog.dev.set_exc_info,                 # attaches exception info
            structlog.processors.JSONRenderer(),        # final: produces JSON string
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


# Module-level logger. Import and use anywhere:
# from app.core.logging import logger
# logger.info("event_name", key1=val1, key2=val2)
logger = structlog.get_logger("sentinel")
```

---

## File 3 - app/core/database.py

```python
# app/core/database.py
# Manage a SQLAlchemy async connection pool to PostgreSQL.

import time

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine,
)

from app.core.config import settings
from app.core.logging import logger


class DatabaseManager:
    """Owns the async database engine and session factory.

    Lifecycle:
    1. db_manager.initialize()  -- called in lifespan startup (synchronous)
    2. await db_manager.ping()  -- verify connectivity before serving requests
    3. await db_manager.close() -- called in lifespan shutdown
    """

    def __init__(self) -> None:
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None

    def initialize(self) -> None:
        """Create the async engine and session factory.

        NOTE: create_async_engine() is SYNCHRONOUS. It creates a Python object;
        it does not open any network connections yet. Connections open lazily
        on first use, or eagerly when ping() is called.
        """
        self._engine = create_async_engine(
            settings.DATABASE_URL,
            pool_size=settings.DB_POOL_SIZE,       # steady-state connections
            max_overflow=settings.DB_MAX_OVERFLOW,  # burst (pool_size + overflow = max total)
            pool_recycle=1800,    # recycle connections every 30 min (avoids stale connections)
            pool_timeout=settings.DB_TIMEOUT_SECONDS,  # wait this long for a free connection
            echo=settings.DEBUG,  # True = log every SQL statement (debug only)
        )
        self._session_factory = async_sessionmaker(
            bind=self._engine,
            expire_on_commit=False,  # ORM objects remain accessible after commit without reload
            class_=AsyncSession,
        )
        logger.info("database_engine_initialized", pool_size=settings.DB_POOL_SIZE)

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        if self._session_factory is None:
            raise RuntimeError("DatabaseManager not initialized. Call initialize() first.")
        return self._session_factory

    async def ping(self) -> tuple[bool, float]:
        """Verify database connectivity. Returns (healthy, latency_ms)."""
        if self._engine is None:
            return False, 0.0
        start = time.perf_counter()
        try:
            async with self._engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            latency_ms = round((time.perf_counter() - start) * 1000.0, 2)
            return True, latency_ms
        except Exception as exc:
            latency_ms = round((time.perf_counter() - start) * 1000.0, 2)
            logger.error("database_ping_failed", error=str(exc))
            return False, latency_ms

    async def close(self) -> None:
        """Dispose the connection pool, closing all connections gracefully."""
        if self._engine:
            await self._engine.dispose()
            logger.info("database_engine_disposed")


# Singleton created at import time. Initialized in lifespan startup.
db_manager = DatabaseManager()
```

**What to notice:**
- `create_async_engine()` has no `await` because it only creates a Python object
- `expire_on_commit=False` prevents the "DetachedInstanceError" bug (explained in Q&A)
- `pool_recycle=1800` prevents stale connection errors from idle connections being dropped

---

## File 4 - app/db/session.py

```python
# app/db/session.py
# FastAPI dependency that provides a database session to route handlers.

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import db_manager


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield an AsyncSession for a single request.

    How this is used in a route handler:

        from fastapi import Depends
        from sqlalchemy.ext.asyncio import AsyncSession

        @router.post("/endpoint")
        async def my_handler(
            session: AsyncSession = Depends(get_async_session)
        ):
            # session is ready to use here
            result = await session.execute(select(MyModel))

    FastAPI:
    1. Calls get_async_session() as a generator
    2. Gets the yielded session
    3. Passes it as the `session` argument to my_handler
    4. After my_handler finishes (or raises), resumes the generator past yield

    The session lifecycle:
    - yield session          -- route handler uses the session
    - await session.commit() -- on success: persist all changes to the DB
    - await session.rollback() -- on exception: undo any partial changes
    - await session.close()  -- always: return the connection to the pool
    """
    async with db_manager.session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
```

---

## File 5 - app/core/redis.py

```python
# app/core/redis.py
# Manage an aioredis connection pool.

import time

import redis.asyncio as aioredis

from app.core.config import settings
from app.core.logging import logger


class RedisManager:
    """Owns the async Redis connection pool.

    Unlike the database manager, the Redis client is used directly --
    no session abstraction. Redis commands are stateless.
    """

    def __init__(self) -> None:
        self._pool: aioredis.ConnectionPool | None = None
        self._client: aioredis.Redis | None = None

    def initialize(self) -> None:
        """Create the Redis connection pool. Synchronous; connections are lazy."""
        self._pool = aioredis.ConnectionPool.from_url(
            settings.REDIS_URL,
            max_connections=settings.REDIS_POOL_SIZE,
            # decode_responses=True: Redis returns Python str, not bytes.
            # This means binary data (embedding vectors) must be base64-encoded
            # before storing. See Day 4 for where this matters.
            decode_responses=True,
            socket_timeout=settings.REDIS_TIMEOUT_MS / 1000,
            socket_connect_timeout=settings.REDIS_TIMEOUT_MS / 1000,
        )
        self._client = aioredis.Redis(connection_pool=self._pool)
        logger.info("redis_pool_initialized", pool_size=settings.REDIS_POOL_SIZE)

    @property
    def client(self) -> aioredis.Redis | None:
        return self._client

    async def ping(self) -> tuple[bool, float]:
        """Verify Redis connectivity. Returns (healthy, latency_ms)."""
        if self._client is None:
            return False, 0.0
        start = time.perf_counter()
        try:
            await self._client.ping()
            latency_ms = round((time.perf_counter() - start) * 1000.0, 2)
            return True, latency_ms
        except Exception as exc:
            latency_ms = round((time.perf_counter() - start) * 1000.0, 2)
            logger.error("redis_ping_failed", error=str(exc))
            return False, latency_ms

    async def close(self) -> None:
        if self._client:
            await self._client.close()
        if self._pool:
            await self._pool.disconnect()
        logger.info("redis_pool_closed")


redis_manager = RedisManager()


async def get_redis_client() -> aioredis.Redis:
    """FastAPI dependency: inject a Redis client into route handlers."""
    if redis_manager.client is None:
        raise RuntimeError("RedisManager not initialized.")
    return redis_manager.client
```

---

## File 6 - app/core/exceptions.py

```python
# app/core/exceptions.py
# RFC 7807 Problem Details error format.
# RFC 7807 is an HTTP standard ensuring every API error has a consistent structure.

from fastapi import Request
from fastapi.responses import JSONResponse


class ProblemDetailException(Exception):
    def __init__(
        self,
        status_code: int,
        error_type: str,   # URL identifying the error type
        title: str,        # Short human-readable summary
        detail: str,       # Full explanation
        instance: str = "/",
    ) -> None:
        self.status_code = status_code
        self.error_type = error_type
        self.title = title
        self.detail = detail
        self.instance = instance
        super().__init__(detail)


async def problem_detail_exception_handler(
    request: Request,
    exc: ProblemDetailException,
) -> JSONResponse:
    """Registered in main.py. When any route raises ProblemDetailException,
    FastAPI calls this function instead of its default error handling."""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "type": exc.error_type,
            "title": exc.title,
            "status": exc.status_code,
            "detail": exc.detail,
            "instance": exc.instance,
        },
        headers={"Content-Type": "application/problem+json"},
    )
```

---

## File 7 - app/main.py

```python
# app/main.py
# Application factory and entry point.

import time
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1 import api_v1_router
from app.core.config import settings
from app.core.database import db_manager
from app.core.exceptions import ProblemDetailException, problem_detail_exception_handler
from app.core.logging import logger, setup_logging
from app.core.rate_limiter import TokenBucketRateLimitMiddleware
from app.core.redis import redis_manager
from app.core.telemetry import HTTP_REQUEST_DURATION_SECONDS, metrics_response


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup and shutdown lifecycle.

    Everything BEFORE yield runs at startup.
    yield -- application runs.
    Everything AFTER yield runs at shutdown.
    """
    setup_logging()
    logger.info("starting_sentinel_gateway", environment=settings.ENVIRONMENT, port=settings.PORT)

    # Initialize connection pools (synchronous -- just creates Python objects)
    db_manager.initialize()
    redis_manager.initialize()

    # Pre-flight connectivity verification.
    # If PostgreSQL is misconfigured, the app fails HERE with a clear error,
    # not silently on the first real user request.
    db_healthy, db_lat = await db_manager.ping()
    redis_healthy, redis_lat = await redis_manager.ping()
    logger.info(
        "subsystems_preflight_check",
        database_ready=db_healthy, database_latency_ms=db_lat,
        redis_ready=redis_healthy, redis_latency_ms=redis_lat,
    )

    yield  # APPLICATION RUNS HERE

    logger.info("shutting_down_sentinel_gateway")
    await redis_manager.close()
    await db_manager.close()
    logger.info("sentinel_gateway_shutdown_complete")


app = FastAPI(
    title="SentinelRAG AI Middleware Gateway",
    version="1.0.0",
    # Disable Swagger UI in production (do not expose API docs publicly)
    docs_url="/docs" if settings.ENVIRONMENT != "production" else None,
    redoc_url="/redoc" if settings.ENVIRONMENT != "production" else None,
    lifespan=lifespan,
)

# Middleware: In Starlette, middleware added with add_middleware wraps from the outside.
# The LAST one added runs FIRST on incoming requests.
# Effective request order: CORSMiddleware -> TokenBucket -> correlation_id_middleware

app.add_middleware(
    TokenBucketRateLimitMiddleware,
    # lambda: lazy getter -- redis_manager.client is None at startup, evaluated at request time
    redis_getter=lambda: redis_manager.client,
    capacity=settings.RATE_LIMIT_DEFAULT_CAPACITY,
    refill_rate_per_sec=settings.RATE_LIMIT_DEFAULT_REFILL_RATE_PER_SEC,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=[
        "X-Sentinel-Cache", "X-Sentinel-Latency-Ms", "X-Sentinel-Provider",
        "X-Request-Id", "X-RateLimit-Limit", "X-RateLimit-Remaining", "Retry-After",
    ],
)

app.add_exception_handler(ProblemDetailException, problem_detail_exception_handler)
app.include_router(api_v1_router)


@app.middleware("http")
async def correlation_id_and_timing_middleware(request: Request, call_next: Any) -> Response:
    """Inject request correlation ID and measure end-to-end latency.

    CORRELATION: Each request gets a unique X-Request-Id. If the client sends one,
    reuse it (enables distributed tracing across microservices).
    The ID is bound to structlog contextvars so it appears in EVERY log line
    during this request, including logs inside deeply-nested service functions.

    TIMING: time.perf_counter() is monotonic (never goes backward, unaffected by
    NTP clock adjustments). Never use time.time() for duration measurement.
    """
    request_id = request.headers.get("X-Request-Id") or f"req_{uuid.uuid4().hex[:16]}"
    request.state.request_id = request_id

    # CRITICAL: clear previous request's context.
    # In async code, tasks can reuse the same OS thread. Without clearing,
    # the previous request's request_id would leak into this request's logs.
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        request_id=request_id,
        route=request.url.path,
        method=request.method,
    )

    start_time = time.perf_counter()

    try:
        response = await call_next(request)
    except Exception:
        duration_sec = time.perf_counter() - start_time
        HTTP_REQUEST_DURATION_SECONDS.labels(
            endpoint=request.url.path, status="500"
        ).observe(duration_sec)
        raise

    duration_sec = time.perf_counter() - start_time
    duration_ms = round(duration_sec * 1000.0, 2)

    response.headers["X-Request-Id"] = request_id
    response.headers["X-Sentinel-Latency-Ms"] = str(duration_ms)
    HTTP_REQUEST_DURATION_SECONDS.labels(
        endpoint=request.url.path, status=str(response.status_code)
    ).observe(duration_sec)

    return response


@app.get("/metrics", tags=["Diagnostics"])
async def get_metrics() -> Response:
    """Prometheus scrape endpoint.

    Prometheus is a pull-based metrics system: it calls this endpoint on a schedule
    (typically every 15 seconds) and stores data in its time-series database.
    Returns text/plain Prometheus exposition format -- not JSON.
    """
    return metrics_response()


@app.get("/healthz", tags=["Diagnostics"])
async def health_check() -> JSONResponse:
    """Liveness and readiness probe.

    Kubernetes uses this to decide:
    - Liveness: is the container alive? If not, restart it.
    - Readiness: should this instance receive traffic?

    HTTP 200 = all dependencies reachable.
    HTTP 503 = at least one dependency is down (Service Unavailable, not Internal Error).
    503 is correct here: the problem is with an upstream service, not our code.
    """
    db_ok, db_latency = await db_manager.ping()
    redis_ok, redis_latency = await redis_manager.ping()

    is_healthy = db_ok and redis_ok
    status_code = status.HTTP_200_OK if is_healthy else status.HTTP_503_SERVICE_UNAVAILABLE

    return JSONResponse(
        status_code=status_code,
        content={
            "status": "healthy" if is_healthy else "degraded",
            "timestamp": time.time(),
            "environment": settings.ENVIRONMENT,
            "dependencies": {
                "postgresql": {"status": "up" if db_ok else "down", "latency_ms": db_latency},
                "redis": {"status": "up" if redis_ok else "down", "latency_ms": redis_latency},
            },
        },
    )


@app.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    return {"service": "SentinelRAG Gateway", "version": "1.0.0", "docs": "/docs"}
```

---

## Day 1 - Knowledge Check

Answer in plain English before reading the answers.

**Q1.** Why does the lifespan function use `yield` instead of `return`?

**Q2.** What happens if you write `DATABASE_URL: str | None = Field(default=None)`?
What breaks, and when?

**Q3.** `db_manager.initialize()` has no `await`. `await db_manager.ping()` does.
Explain why one needs await and the other does not.

**Q4.** Why does the correlation middleware call `clear_contextvars()` before
`bind_contextvars()`? What breaks if you omit the clear?

**Q5.** The healthz endpoint returns HTTP 503 when a dependency is down, not HTTP 500.
Why is 503 the correct status code?

**Q6.** `decode_responses=True` is set on the Redis pool. A later module stores embedding
vectors in Redis. Why does this force binary data to be base64-encoded first?

**Q7.** `time.perf_counter()` is used instead of `time.time()` for latency measurement.
What is the difference and why does it matter?

**Q8.** `expire_on_commit=False` is set on the session factory. What would happen if you
omitted it and tried to access an ORM object's attributes after a commit?

---

### Answers

**A1.** `yield` turns the function into a generator/context manager. Code before yield runs
at startup; code after yield runs at shutdown. `return` would run all code at startup with
nothing for shutdown. The `@asynccontextmanager` decorator makes this work as a context manager
that FastAPI calls when the app starts and stops.

**A2.** Pydantic accepts `None` as valid. SQLAlchemy receives `None` as the connection URL
and raises a confusing `AttributeError: 'NoneType' has no attribute 'split'` deep inside
engine creation -- not at startup but at the moment a connection is first attempted. The mistake
is invisible until the first database query, which could be any time.

**A3.** `initialize()` just creates Python objects in memory -- `create_async_engine()` builds
a pool manager object with no network I/O. `ping()` sends `SELECT 1` over TCP to PostgreSQL,
which requires waiting for a network round-trip. Network I/O must be awaited so the event loop
can handle other tasks while waiting for the response.

**A4.** In async Python, tasks can share the same OS thread. If task A binds
`request_id=abc` to contextvars and then yields (e.g., waiting for Redis), task B might start
running on the same thread. Without clearing, task B inherits task A's `request_id=abc`. Every
log line from task B would be attributed to task A's request -- a debugging nightmare in
production where you would trace a request that never existed.

**A5.** HTTP 503 Service Unavailable means "I am working correctly, but a service I depend on
is unavailable." HTTP 500 Internal Server Error means "I have a bug." When PostgreSQL is down,
the app itself is healthy -- it is an upstream dependency failing. Using 500 would route alerts
to "application bugs" instead of "infrastructure issues" -- wrong on-call team gets paged.

**A6.** `decode_responses=True` tells aioredis to always decode bytes to str. If you store
raw bytes (struct.pack output of floats) in Redis, aioredis would try to decode them as UTF-8,
which fails because IEEE 754 binary data contains arbitrary byte values, most of which are not
valid UTF-8 sequences. Base64 encodes arbitrary bytes as ASCII characters (A-Z, a-z, 0-9, +, /),
which are always valid UTF-8.

**A7.** `time.time()` is wall-clock time. It can jump backward or forward if NTP adjusts the
system clock. `time.perf_counter()` is a monotonic hardware counter -- it always increases,
never jumps. For duration measurement you compute end - start. If `time.time()` jumps between
readings, you get a negative duration or an impossibly large one. perf_counter never has this
problem.

**A8.** SQLAlchemy with `expire_on_commit=True` (the default) marks all ORM objects as
"expired" after commit. Accessing any attribute on an expired object triggers a lazy SQL query
to reload it from the DB. In async context, lazy loading requires an active session. If you try
to access an attribute after the session is closed (e.g., after returning from the route
handler), SQLAlchemy raises `MissingGreenlet` or `DetachedInstanceError`. Setting
`expire_on_commit=False` keeps the in-memory values intact after commit.

---

## Day 1 - What the Real Commit Would Have Contained

- `app/core/config.py`: Pydantic BaseSettings with all 20+ settings, type validation,
  field validators for DATABASE_URL and REDIS_URL protocol enforcement
- `app/core/logging.py`: structlog JSON pipeline, setup_logging() factory function
- `app/core/database.py`: DatabaseManager with initialize/ping/close lifecycle
- `app/core/redis.py`: RedisManager with initialize/ping/close, get_redis_client() dependency
- `app/db/session.py`: get_async_session() generator dependency with commit/rollback/close
- `app/core/exceptions.py`: ProblemDetailException + RFC 7807 handler
- `app/main.py`: lifespan context manager, middleware stack, /healthz returning structured JSON,
  /metrics stub, correlation middleware

---

*End of Day 1. Study the Q&A. When ready, reply "continue" for Day 2.*
