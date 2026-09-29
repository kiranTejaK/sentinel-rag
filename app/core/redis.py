import time

import redis.asyncio as aioredis

from app.core.config import settings
from app.core.logging import logger


class RedisManager:
    """Manages Async Redis client pool and lifecycle."""

    def __init__(self) -> None:
        self.client: aioredis.Redis | None = None
        self.pool: aioredis.ConnectionPool | None = None

    def initialize(self) -> None:
        """Initializes Redis async connection pool."""
        logger.info(
            "initializing_redis_pool",
            url=settings.REDIS_URL,
            max_connections=settings.REDIS_POOL_SIZE,
        )
        self.pool = aioredis.ConnectionPool.from_url(
            settings.REDIS_URL,
            max_connections=settings.REDIS_POOL_SIZE,
            decode_responses=True,
            socket_timeout=settings.REDIS_TIMEOUT_MS / 1000.0,
            socket_connect_timeout=settings.REDIS_TIMEOUT_MS / 1000.0,
        )
        self.client = aioredis.Redis(connection_pool=self.pool)

    async def close(self) -> None:
        """Gracefully tears down Redis connection pool."""
        if self.client:
            logger.info("closing_redis_pool")
            await self.client.aclose()
            self.client = None
        if self.pool:
            await self.pool.disconnect()
            self.pool = None

    async def ping(self) -> tuple[bool, float]:
        """Executes a PING command and returns status and round-trip latency in ms."""
        if not self.client:
            return False, 0.0
        start = time.perf_counter()
        try:
            res = await self.client.ping()
            latency_ms = (time.perf_counter() - start) * 1000.0
            return bool(res), round(latency_ms, 2)
        except Exception as exc:
            logger.error("redis_ping_failed", error=str(exc))
            return False, 0.0


redis_manager = RedisManager()


async def get_redis_client() -> aioredis.Redis:
    """FastAPI Dependency for accessing Redis."""
    if not redis_manager.client:
        raise RuntimeError("Redis client is not initialized.")
    return redis_manager.client
