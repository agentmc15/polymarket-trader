"""Pytest configuration and fixtures."""
import asyncio
from collections.abc import AsyncGenerator, Generator
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import get_async_session
from app.main import app
from app.models import Base

# Test database URL (use SQLite for tests)
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture(scope="session")
def event_loop() -> Generator[asyncio.AbstractEventLoop, None, None]:
    """Create event loop for async tests."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="function")
async def test_engine():
    """Create test database engine."""
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

    await engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def test_session(test_engine) -> AsyncGenerator[AsyncSession, None]:
    """Create test database session."""
    session_factory = async_sessionmaker(
        test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture(scope="function")
async def client(test_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """Create test HTTP client."""

    async def override_get_session() -> AsyncGenerator[AsyncSession, None]:
        yield test_session

    app.dependency_overrides[get_async_session] = override_get_session

    async with AsyncClient(app=app, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


@pytest.fixture
def sample_market_data() -> dict[str, Any]:
    """Sample market data for testing."""
    return {
        "condition_id": "0x1234567890abcdef",
        "question": "Will event X happen?",
        "outcomes": ["Yes", "No"],
        "token_ids": {
            "Yes": "0xtoken1",
            "No": "0xtoken2",
        },
        "outcome_prices": {
            "Yes": 0.65,
            "No": 0.35,
        },
        "volume_24h": 10000.0,
        "liquidity": 50000.0,
    }


@pytest.fixture
def sample_order_data() -> dict[str, Any]:
    """Sample order data for testing."""
    return {
        "condition_id": "0x1234567890abcdef",
        "token_id": "0xtoken1",
        "side": "BUY",
        "price": 0.60,
        "size": 100.0,
        "order_type": "GTC",
    }
