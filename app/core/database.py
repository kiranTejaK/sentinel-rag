import time
from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from app.core.config import settings
from app.core.logging import logger
from app.db.session import async_session_factory as db_session_factory
from app.db.session import engine as db_engine


class DatabaseManager:
    """Manages Asyncpg database connections and connection lifecycle."""

    def __init__(self) -> None:
        self.engine: AsyncEngine | None = None
        self.session_factory: async_sessionmaker[AsyncSession] | None = None

    def initialize(self) -> None:
        """Initializes the async database engine and pool."""
        logger.info(
            "initializing_database_pool",
            url=settings.DATABASE_URL.split("@")[-1],  # mask password
            pool_size=settings.DB_POOL_SIZE,
            max_overflow=settings.DB_MAX_OVERFLOW,
        )
        self.engine = db_engine
        self.session_factory = db_session_factory

    async def close(self) -> None:
        """Gracefully closes all pool connections."""
        if self.engine:
            logger.info("closing_database_pool")
            await self.engine.dispose()

    async def ping(self) -> tuple[bool, float]:
        """Executes a lightweight query to test connectivity and measure latency."""
        if not self.engine:
            return False, 0.0
        start = time.perf_counter()
        try:
            async with self.engine.connect() as conn:
                await conn.execute(text("SELECT 1;"))
            latency_ms = (time.perf_counter() - start) * 1000.0
            return True, round(latency_ms, 2)
        except Exception as exc:
            logger.error("database_ping_failed", error=str(exc))
            return False, 0.0


db_manager = DatabaseManager()


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI Dependency for scoped async database sessions."""
    if not db_manager.session_factory:
        raise RuntimeError("Database session factory is not initialized.")
    async with db_manager.session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
