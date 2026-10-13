"""Distributed Token-Bucket Rate Limiter using Atomic Redis Lua Scripts.

Guarantees thread and cluster safety across stateless ASGI worker replicas
without race conditions or database bottlenecks.
"""

from dataclasses import dataclass
from typing import Any

import redis.asyncio as aioredis
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.core.logging import logger

TOKEN_BUCKET_LUA_SCRIPT = """
-- Distributed Token Bucket Algorithm (Atomic Redis Lua Script)
-- KEYS[1]: Redis rate limit hash key (e.g., ratelimit:apikey:xxx)
-- ARGV[1]: Bucket capacity (maximum tokens allowed)
-- ARGV[2]: Refill rate per second (tokens added per second)
-- ARGV[3]: Requested tokens to consume (typically 1)

local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local requested = tonumber(ARGV[3])

-- Query high-precision Redis server time to prevent clock skew across replicas
local time_arr = redis.call('TIME')
local now = tonumber(time_arr[1]) + (tonumber(time_arr[2]) / 1000000)

local current_data = redis.call('HMGET', key, 'tokens', 'last_updated')
local tokens = tonumber(current_data[1])
local last_updated = tonumber(current_data[2])

if tokens == nil or last_updated == nil then
    -- Fresh bucket: initialize with max capacity
    tokens = capacity
    last_updated = now
else
    -- Compute token replenishment since last access
    local delta = math.max(0, now - last_updated)
    tokens = math.min(capacity, tokens + (delta * refill_rate))
    last_updated = now
end

local allowed = 0
local retry_after = 0

if tokens >= requested then
    tokens = tokens - requested
    allowed = 1
    retry_after = 0
else
    allowed = 0
    local needed = requested - tokens
    retry_after = math.ceil(needed / refill_rate)
end

-- Persist current bucket state
redis.call('HSET', key, 'tokens', tokens, 'last_updated', last_updated)

-- Auto-expire inactive buckets (2x refill window to prevent memory leak)
local ttl = math.ceil((capacity / refill_rate) * 2)
redis.call('EXPIRE', key, math.max(60, ttl))

return {allowed, math.floor(tokens), retry_after, capacity}
"""


@dataclass(slots=True)
class RateLimitResult:
    """Outcome of token bucket evaluation."""

    allowed: bool
    remaining: int
    retry_after: int
    limit: int


class TokenBucketLimiter:
    """Evaluates token bucket consumption via atomic Redis Lua script."""

    def __init__(
        self,
        redis_client: aioredis.Redis | None = None,
        redis_getter: Any | None = None,
    ) -> None:
        self._client = redis_client
        self._getter = redis_getter
        self._script_sha: str | None = None

    @property
    def redis(self) -> aioredis.Redis | None:
        if self._client is not None:
            return self._client
        if self._getter is not None:
            return self._getter()
        return None

    async def _get_script_sha(self, client: aioredis.Redis) -> str:
        if self._script_sha is None:
            self._script_sha = await client.script_load(TOKEN_BUCKET_LUA_SCRIPT)
        return self._script_sha

    async def check(
        self,
        identifier: str,
        capacity: int = 60,
        refill_rate_per_sec: float = 1.0,
        requested: int = 1,
    ) -> RateLimitResult:
        """Atomically checks and consumes tokens for a given identifier."""
        key = f"ratelimit:{identifier}"
        client = self.redis
        if client is None:
            # Redis not yet available or configured; fail-open safely
            return RateLimitResult(
                allowed=True,
                remaining=capacity,
                retry_after=0,
                limit=capacity,
            )

        try:
            sha = await self._get_script_sha(client)
            try:
                raw = await client.evalsha(
                    sha,
                    1,
                    key,
                    str(capacity),
                    str(refill_rate_per_sec),
                    str(requested),
                )
            except aioredis.exceptions.NoScriptError:
                # Script flush occurred; reload and evaluate directly
                self._script_sha = None
                raw = await client.eval(
                    TOKEN_BUCKET_LUA_SCRIPT,
                    1,
                    key,
                    str(capacity),
                    str(refill_rate_per_sec),
                    str(requested),
                )

            allowed = bool(raw[0])
            remaining = int(raw[1])
            retry_after = int(raw[2])
            limit = int(raw[3])

            return RateLimitResult(
                allowed=allowed,
                remaining=remaining,
                retry_after=retry_after,
                limit=limit,
            )
        except Exception as exc:
            logger.error("rate_limit_redis_evaluation_error", error=str(exc), key=key)
            # Fail-open policy to maintain availability during temporary Redis network hiccups
            return RateLimitResult(
                allowed=True,
                remaining=capacity,
                retry_after=0,
                limit=capacity,
            )


class TokenBucketRateLimitMiddleware(BaseHTTPMiddleware):
    """Custom FastAPI/Starlette middleware enforcing distributed Token-Bucket rate limiting.

    - Identifies caller via `X-API-Key` (or client IP fallback).
    - Injects `X-RateLimit-Limit`, `X-RateLimit-Remaining`, and `Retry-After`.
    - Returns RFC 7807 429 Problem Details when capacity is exhausted.
    """

    EXEMPT_PATHS: set[str] = {
        "/healthz",
        "/",
        "/docs",
        "/redoc",
        "/openapi.json",
    }

    def __init__(
        self,
        app: Any,
        redis_client: aioredis.Redis | None = None,
        redis_getter: Any | None = None,
        capacity: int = 60,
        refill_rate_per_sec: float = 1.0,
    ) -> None:
        super().__init__(app)
        self.limiter = TokenBucketLimiter(redis_client=redis_client, redis_getter=redis_getter)
        self.capacity = capacity
        self.refill_rate_per_sec = refill_rate_per_sec

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path

        # Bypass rate limiting for operational and documentation endpoints
        if path in self.EXEMPT_PATHS or path.startswith(("/docs", "/redoc")):
            return await call_next(request)

        # Extract identifier: prioritize X-API-Key, fallback to client IP
        api_key = request.headers.get("X-API-Key")
        if api_key:
            identifier = f"key:{api_key.strip()}"
        else:
            client_ip = request.client.host if request.client else "anonymous"
            identifier = f"ip:{client_ip}"

        res = await self.limiter.check(
            identifier=identifier,
            capacity=self.capacity,
            refill_rate_per_sec=self.refill_rate_per_sec,
            requested=1,
        )

        request_id = getattr(request.state, "request_id", None) or request.headers.get(
            "X-Request-Id", "unknown"
        )

        if not res.allowed:
            logger.warning(
                "rate_limit_exceeded",
                identifier=identifier,
                limit=res.limit,
                remaining=res.remaining,
                retry_after=res.retry_after,
                path=path,
                request_id=request_id,
            )

            headers = {
                "Content-Type": "application/problem+json",
                "X-RateLimit-Limit": str(res.limit),
                "X-RateLimit-Remaining": "0",
                "Retry-After": str(max(1, res.retry_after)),
                "X-Request-Id": str(request_id),
            }

            problem_body = {
                "type": "https://sentinelrag.io/errors/rate-limit-exceeded",
                "title": "Too Many Requests",
                "status": 429,
                "detail": f"Rate limit quota exceeded ({res.limit} req/min). Retry after {max(1, res.retry_after)}s.",
                "instance": path,
                "request_id": str(request_id),
                "invalid_params": [],
            }

            return JSONResponse(
                status_code=429,
                content=problem_body,
                headers=headers,
            )

        response = await call_next(request)

        # Inject standard rate limit headers on successful request
        response.headers["X-RateLimit-Limit"] = str(res.limit)
        response.headers["X-RateLimit-Remaining"] = str(res.remaining)

        return response
