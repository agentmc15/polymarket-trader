"""Pytest configuration and fixtures.

`TRADING_MODE=paper` is set via `os.environ.setdefault` at *import time*
(before `app.main` / `app.config` are imported below), not inside a fixture.
`app.config.get_settings` is `@lru_cache`d, and the module-level
`settings = get_settings()` in `app/config.py` runs the moment `app.config`
is first imported — which happens as a side effect of `from app.main import
app` below, long before any fixture body would run. Setting the env var
here guarantees paper mode is locked in for the whole test process before
any `Settings` instance (and therefore any live-trading adapter) can be
constructed. `os.environ.setdefault` (not `monkeypatch`) is used because
`monkeypatch` is fixture-scoped and torn down between tests; this needs to
hold for the entire session and never overwrite an already-`paper` value.
"""
import os

# BEFORE any `app.*` import: `Settings` reads the repo-root `.env`, so a
# developer's real credentials would otherwise load into every test
# process -- making tests depend on the machine they run on, and putting
# live keys one careless print away from a log (GUARDRAILS.md §1.3).
# Empty string means "no env file at all"; see `app.config._ENV_FILES`.
os.environ.setdefault("POLYMARKET_TRADER_ENV_FILE", "")

import os

os.environ.setdefault("TRADING_MODE", "paper")

from collections.abc import AsyncGenerator  # noqa: E402
from typing import Any  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import event  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.database import get_async_session  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base  # noqa: E402


@pytest_asyncio.fixture
async def test_engine() -> AsyncGenerator[AsyncEngine, None]:
    """Create a fresh in-memory SQLite engine for one test function.

    `StaticPool` + `check_same_thread=False` pins the engine to a single
    physical connection for its whole lifetime, so the `:memory:` database
    (otherwise private to whichever connection created it) stays visible
    across the several connections SQLAlchemy's async engine opens during a
    test. `Base.metadata.create_all` runs fresh per test function so no
    state leaks between tests.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_connection: Any, connection_record: Any) -> None:
        """SQLite ignores FK constraints unless enabled per connection.

        Without this, an insert with a bad `market_id` foreign key silently
        succeeds on SQLite even though the column declares `ForeignKey(...)`
        — the constraint is decorative until this pragma is issued on every
        new DBAPI connection.
        """
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    await engine.dispose()


@pytest_asyncio.fixture
async def test_session(test_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """Create a test database session bound to `test_engine`."""
    session_factory = async_sessionmaker(
        test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture
async def client(test_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """Create a test HTTP client wired to the in-memory test session."""

    async def override_get_session() -> AsyncGenerator[AsyncSession, None]:
        yield test_session

    app.dependency_overrides[get_async_session] = override_get_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
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
