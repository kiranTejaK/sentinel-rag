import time
from typing import AsyncGenerator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from app.core.config import settings
from app.core.logging import logger


class Base(DeclarativeBase):
    """SQLAlchemy 2.0 Base Class."""
    pass


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
        self.engine = create_async_engine(
            settings.DATABASE_URL,
            echo=False,
            pool_size=settings.DB_POOL_SIZE,
            max_overflow=settings.DB_MAX_OVERFLOW,
            pool_timeout=settings.DB_TIMEOUT_SECONDS,
            pool_pre_ping=True,  # Test connection liveness before checking out
        )
        self.session_factory = async_sessionmaker(
            bind=self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )

    async def close(self) -> None:
        """Gracefully closes all pool connections."""
        if self.engine:
            logger.info("closing_database_pool")
            await self.engine.dispose()
            self.engine = None
            self.session_factory = None

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
