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
from app.core.exceptions import (
    ProblemDetailException,
    problem_detail_exception_handler,
)
from app.core.logging import logger, setup_logging
from app.core.rate_limiter import TokenBucketRateLimitMiddleware
from app.core.redis import redis_manager
from app.core.telemetry import HTTP_REQUEST_DURATION_SECONDS, metrics_response


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manages application startup and graceful shutdown hooks."""
    setup_logging()
    logger.info(
        "starting_sentinel_gateway",
        environment=settings.ENVIRONMENT,
        port=settings.PORT,
        embedding_model=settings.EMBEDDING_MODEL,
    )

    # Initialize connection pools
    db_manager.initialize()
    redis_manager.initialize()

    # Pre-warm connection pools and verify initial connectivity
    db_healthy, db_lat = await db_manager.ping()
    redis_healthy, redis_lat = await redis_manager.ping()

    logger.info(
        "subsystems_preflight_check",
        database_ready=db_healthy,
        database_latency_ms=db_lat,
        redis_ready=redis_healthy,
        redis_latency_ms=redis_lat,
    )

    yield

    # Teardown connection pools cleanly
    logger.info("shutting_down_sentinel_gateway")
    await redis_manager.close()
    await db_manager.close()
    logger.info("sentinel_gateway_shutdown_complete")


app = FastAPI(
    title="SentinelRAG AI Middleware Gateway",
    description="Enterprise-Grade AI Gateway: Semantic Cache + Hybrid Retrieval + Token Optimizer",
    version="1.0.0",
    docs_url="/docs" if settings.ENVIRONMENT != "production" else None,
    redoc_url="/redoc" if settings.ENVIRONMENT != "production" else None,
    lifespan=lifespan,
)

# Distributed Token-Bucket Rate Limiting
app.add_middleware(
    TokenBucketRateLimitMiddleware,
    redis_getter=lambda: redis_manager.client,
    capacity=settings.RATE_LIMIT_DEFAULT_CAPACITY,
    refill_rate_per_sec=settings.RATE_LIMIT_DEFAULT_REFILL_RATE_PER_SEC,
)

# CORS Configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=[
        "X-Sentinel-Cache",
        "X-Sentinel-Latency-Ms",
        "X-Sentinel-Provider",
        "X-Request-Id",
        "X-RateLimit-Limit",
        "X-RateLimit-Remaining",
        "Retry-After",
    ],
)


# Exception handlers
app.add_exception_handler(ProblemDetailException, problem_detail_exception_handler)  # type: ignore[arg-type]

# API Routers
app.include_router(api_v1_router)


# Prometheus Telemetry & Correlation Middleware
@app.middleware("http")
async def correlation_id_and_timing_middleware(request: Request, call_next: Any) -> Response:
    """Injects unique request correlation ID, binds structlog context, and measures request lifecycle latency."""
    request_id = request.headers.get("X-Request-Id") or f"req_{uuid.uuid4().hex[:16]}"
    request.state.request_id = request_id

    # Bind request-scoped context to structlog contextvars
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
        # Measure duration on unhandled exceptions
        duration_sec = time.perf_counter() - start_time
        endpoint = request.url.path
        HTTP_REQUEST_DURATION_SECONDS.labels(endpoint=endpoint, status="500").observe(duration_sec)
        raise

    duration_sec = time.perf_counter() - start_time
    duration_ms = round(duration_sec * 1000.0, 2)

    response.headers["X-Request-Id"] = request_id
    response.headers["X-Sentinel-Latency-Ms"] = str(duration_ms)

    # Observe Prometheus histogram duration partitioned by endpoint and status
    endpoint = request.url.path
    status_str = str(response.status_code)
    HTTP_REQUEST_DURATION_SECONDS.labels(endpoint=endpoint, status=status_str).observe(duration_sec)

    return response


@app.get(
    "/metrics",
    tags=["Diagnostics"],
    summary="Prometheus Telemetry Metrics Endpoint",
    response_description="Exposes Prometheus operational metrics for scraping",
)
async def get_metrics() -> Response:
    """Prometheus metrics endpoint returning scrape telemetry in standard text exposition format."""
    return metrics_response()


@app.get(
    "/healthz",
    tags=["Diagnostics"],
    summary="Liveness and Readiness Healthcheck",
    response_description="Returns the operational status and latency pings of upstream dependencies",
)
async def health_check() -> JSONResponse:
    """Performs real-time async health checks against PostgreSQL 16 and Redis 7.
    Returns HTTP 200 if all services are operational, or HTTP 503 if any subsystem is degraded.
    """
    db_ok, db_latency = await db_manager.ping()
    redis_ok, redis_latency = await redis_manager.ping()

    is_healthy = db_ok and redis_ok
    status_code = status.HTTP_200_OK if is_healthy else status.HTTP_503_SERVICE_UNAVAILABLE

    payload = {
        "status": "healthy" if is_healthy else "degraded",
        "timestamp": time.time(),
        "environment": settings.ENVIRONMENT,
        "dependencies": {
            "postgresql": {
                "status": "up" if db_ok else "down",
                "latency_ms": db_latency,
                "engine": "PostgreSQL 16 + pgvector",
            },
            "redis": {
                "status": "up" if redis_ok else "down",
                "latency_ms": redis_latency,
                "engine": "Redis 7.2 Alpine",
            },
        },
    }

    return JSONResponse(status_code=status_code, content=payload)


@app.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    return {
        "service": "SentinelRAG Gateway",
        "version": "1.0.0",
        "docs": "/docs",
        "health": "/healthz",
    }
