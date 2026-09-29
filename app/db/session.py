"""Asynchronous Database Engine and Session Management for SentinelRAG.
Configured with asyncpg, SQLAlchemy 2.0, and production-grade connection pooling.
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings
from app.core.logging import logger

# Async SQLAlchemy 2.0 Engine with asyncpg driver
# pool_size=20: Baseline warm connections maintained in the pool
# max_overflow=10: Additional burst connections allocated under peak load
# pool_recycle=300: Recycles connections older than 5 minutes to prevent stale connections/memory leaks
# pool_pre_ping=True: Executes a lightweight 'SELECT 1' test ping prior to checkout to detect dropped/broken TCP sockets
engine: AsyncEngine = create_async_engine(
    url=settings.DATABASE_URL,
    echo=settings.DEBUG,
    pool_size=settings.DB_POOL_SIZE,  # 20
    max_overflow=settings.DB_MAX_OVERFLOW,  # 10
    pool_recycle=300,  # 5 minutes
    pool_pre_ping=True,  # Test connection liveness before checkout
    pool_timeout=settings.DB_TIMEOUT_SECONDS,  # 30 seconds
)

# Async session factory producing AsyncSession instances
async_session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,  # Prevents lazy-loading attributes from expiring post-commit in async context
    autoflush=False,  # Manual flush control for deterministic query execution
)


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding an asynchronous database session.

    Guarantees rollback on unhandled exceptions and ensures clean closure
    to prevent connection leakages back into the asyncpg pool.
    """
    async with async_session_factory() as session:
        try:
            yield session
        except Exception as exc:
            logger.error("database_session_transaction_failed", error=str(exc))
            await session.rollback()
            raise
        finally:
            await session.close()
