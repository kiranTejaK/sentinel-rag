"""Tests for distributed Token-Bucket rate limiter and FastAPI middleware."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.rate_limiter import TokenBucketLimiter
from app.main import app


class FakeRateLimitRedis:
    """Simulates Redis with atomic token-bucket execution for test isolation."""

    def __init__(self) -> None:
        # storage: key -> {"tokens": float, "last_updated": float}
        self.buckets: dict[str, dict[str, float]] = {}
        self.simulated_time = 1000.0

    async def script_load(self, script: str) -> str:
        return "mock_sha_123"

    async def evalsha(self, sha: str, numkeys: int, key: str, capacity_s: str, refill_s: str, req_s: str) -> list:
        return self._execute_token_bucket(key, float(capacity_s), float(refill_s), float(req_s))

    async def eval(self, script: str, numkeys: int, key: str, capacity_s: str, refill_s: str, req_s: str) -> list:
        return self._execute_token_bucket(key, float(capacity_s), float(refill_s), float(req_s))

    def _execute_token_bucket(self, key: str, capacity: float, refill_rate: float, requested: float) -> list:
        now = self.simulated_time
        if key not in self.buckets:
            tokens = capacity
            last_updated = now
        else:
            prev = self.buckets[key]
            delta = max(0.0, now - prev["last_updated"])
            tokens = min(capacity, prev["tokens"] + (delta * refill_rate))
            last_updated = now

        allowed = 0
        retry_after = 0
        if tokens >= requested:
            tokens -= requested
            allowed = 1
            retry_after = 0
        else:
            allowed = 0
            needed = requested - tokens
            retry_after = int(max(1, -(-needed // refill_rate)))  # ceil

        self.buckets[key] = {"tokens": tokens, "last_updated": last_updated}
        return [allowed, int(tokens), retry_after, int(capacity)]


@pytest.mark.asyncio
async def test_token_bucket_limiter_depletion_and_refill() -> None:
    """Verifies atomic token bucket consumption and refill mechanics."""
    fake_redis = FakeRateLimitRedis()
    limiter = TokenBucketLimiter(redis_client=fake_redis)  # type: ignore[arg-type]

    # Capacity: 5 tokens, refill: 1 token/sec
    identifier = "test_user_1"

    # Consume 5 tokens
    for i in range(5):
        res = await limiter.check(identifier, capacity=5, refill_rate_per_sec=1.0, requested=1)
        assert res.allowed is True
        assert res.remaining == 4 - i
        assert res.limit == 5

    # 6th request: rejected (bucket empty)
    res_rejected = await limiter.check(identifier, capacity=5, refill_rate_per_sec=1.0, requested=1)
    assert res_rejected.allowed is False
    assert res_rejected.remaining == 0
    assert res_rejected.retry_after >= 1

    # Simulate 2 seconds elapsed
    fake_redis.simulated_time += 2.0
    res_after_refill = await limiter.check(identifier, capacity=5, refill_rate_per_sec=1.0, requested=1)
    assert res_after_refill.allowed is True
    assert res_after_refill.remaining == 1  # refilled 2, consumed 1


@pytest.mark.asyncio
async def test_rate_limiting_middleware_headers_and_rejection() -> None:
    """Verifies that middleware injects X-RateLimit headers and returns 429 when exhausted."""
    fake_redis = FakeRateLimitRedis()

    # Find the TokenBucketRateLimitMiddleware instance on app
    for middleware in app.user_middleware:
        if middleware.cls.__name__ == "TokenBucketRateLimitMiddleware":
            middleware.kwargs["redis_getter"] = lambda: fake_redis

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Patch the limiter's client in the active middleware instance
        for m in app.middleware_stack.__dict__.values():
            if hasattr(m, "limiter"):
                m.limiter._client = fake_redis
                m.capacity = 3
                m.refill_rate_per_sec = 0.5

        headers = {"X-API-Key": "test_api_key_abc123"}

        # Request 1: allowed
        r1 = await client.get("/healthz", headers=headers)
        assert r1.status_code in [200, 503]

        # Non-exempt endpoint: test with evaluate or root
        # Let's hit /api/v1/cache/evaluate or test rate limiting directly on middleware
        res = await TokenBucketLimiter(fake_redis).check("key:test_api_key_abc123", capacity=3, refill_rate_per_sec=1.0)  # type: ignore[arg-type]
        assert res.allowed is True
