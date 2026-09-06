"""Async SQLAlchemy database configuration."""
import asyncio
from collections.abc import AsyncGenerator, Coroutine
from contextlib import asynccontextmanager
from typing import Any, TypeVar

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings

# Create async engine
engine = create_async_engine(
    settings.async_database_url,
    echo=settings.debug,
    pool_size=settings.database_pool_size,
    max_overflow=settings.database_max_overflow,
    pool_pre_ping=True,
)

# Create async session factory
async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


_T = TypeVar("_T")


def run_async_task(coro: Coroutine[Any, Any, _T]) -> _T:
    """Run `coro` in a fresh event loop, then dispose the engine pool.

    EVERY Celery entry point must use this instead of a bare
    `asyncio.run(...)`. `engine` above is a module-level singleton, and
    its pool holds asyncpg connections bound to whatever loop first
    opened them. `asyncio.run` closes its loop on the way out, so those
    pooled connections survive into the next task invocation attached to
    a loop that no longer exists.

    In a worker process that means the FIRST tick of a beat succeeds and
    every tick after it raises `RuntimeError: Event loop is closed` --
    observed directly against a live Postgres: tick 1 OK, ticks 2 and 3
    dead. It is not a venue problem and no amount of retrying helps,
    because the pool never heals.

    No test could catch this. `conftest.py` binds one SQLite
    `StaticPool` connection inside a single event loop, so the second
    loop that breaks production never exists in the suite.

    Disposing in a `finally` returns the process to a clean slate: the
    next call builds a new pool against its own loop. The cost is one
    connection handshake per beat tick, which is negligible next to a
    pass that fetches hundreds of books.

    Args:
        coro: The coroutine to run to completion.

    Returns:
        The coroutine's result.
    """

    async def _run() -> _T:
        try:
            return await coro
        finally:
            await engine.dispose()

    return asyncio.run(_run())


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    """Dependency for getting async database sessions.

    Yields:
        AsyncSession: Database session for request.
    """
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


@asynccontextmanager
async def get_session_context() -> AsyncGenerator[AsyncSession, None]:
    """Context manager for getting async database sessions outside of requests.

    Yields:
        AsyncSession: Database session.
    """
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db() -> None:
    """Initialize database connection.

    Call this on application startup to verify database connectivity.
    """
    async with engine.begin() as conn:
        # Just verify we can connect
        await conn.execute(text("SELECT 1"))


async def close_db() -> None:
    """Close database connections.

    Call this on application shutdown.
    """
    await engine.dispose()
