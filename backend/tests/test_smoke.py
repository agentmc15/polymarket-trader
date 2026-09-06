"""Smoke tests: the app boots, routes respond, and the backtest engine runs.

These are deliberately shallow — one assertion per surface — to prove the
test infrastructure itself (SQLite engine, ASGI client, aware-datetime
fixtures) actually works end to end. Deeper strategy/engine correctness
tests belong to later tasks.
"""
from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncEngine

from app.models import Base
from app.services.backtesting import (
    BacktestConfig,
    BacktestResult,
    Backtester,
    InMemoryDataReplayer,
    create_sample_snapshots,
)
from app.strategies import get_strategy
from app.utils.time import utcnow


@pytest.mark.asyncio
async def test_health(client: AsyncClient) -> None:
    """`GET /health` reports healthy without touching a real database."""
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


@pytest.mark.asyncio
async def test_list_backtest_strategies(client: AsyncClient) -> None:
    """`GET /api/v1/backtests/strategies` exposes at least 8 strategies.

    T10 deleted `cross_platform_arbitrage` (its "sell on the higher
    venue" logic assumed a naked short neither venue supports) and did
    not replace it; the registry drops from 9 to 8 until T18 adds
    `cross_venue_arbitrage` as its real, `Intent(kind="cross_venue")`
    replacement.
    """
    response = await client.get("/api/v1/backtests/strategies")
    assert response.status_code == 200
    data = response.json()
    assert len(data["strategies"]) >= 8


@pytest.mark.asyncio
async def test_create_all_tables_on_sqlite(test_engine: AsyncEngine) -> None:
    """`Base.metadata.create_all` succeeds for every mapped table on SQLite.

    This is the JSON-portability check: `JSONDict`/`JSONList` columns (which
    render as `JSONB` on Postgres) must fall back to plain SQLite `JSON`
    without raising, and every `ForeignKey` target must resolve. The
    `test_engine` fixture already ran `create_all` during setup; this
    confirms every table SQLAlchemy knows about actually landed.
    """
    expected_tables = set(Base.metadata.tables.keys())
    assert expected_tables, "Base.metadata has no tables registered"

    async with test_engine.connect() as conn:
        actual_tables = await conn.run_sync(
            lambda sync_conn: set(inspect(sync_conn).get_table_names())
        )

    assert expected_tables.issubset(actual_tables)


@pytest.mark.asyncio
async def test_bad_foreign_key_insert_raises(test_session) -> None:
    """SQLite must enforce declared `ForeignKey`s once `PRAGMA foreign_keys=ON`.

    Without the pragma (wired in `conftest.test_engine`'s `connect` event
    listener), SQLite silently accepts a row referencing a non-existent
    parent — the `ForeignKey("markets.id")` on `MarketPrice.market_id`
    would be purely decorative in every test. This proves the pragma is
    actually in effect.
    """
    from sqlalchemy.exc import IntegrityError

    from app.models import MarketPrice

    bad_price = MarketPrice(
        market_id=999_999,  # no such market exists
        timestamp=utcnow(),
        outcome="YES",
        open=0.5,
        high=0.5,
        low=0.5,
        close=0.5,
    )
    test_session.add(bad_price)

    with pytest.raises(IntegrityError):
        await test_session.flush()


@pytest.mark.asyncio
async def test_backtester_runs_favorite_compounder_with_aware_datetimes() -> None:
    """The engine must run end-to-end on strictly aware-UTC datetimes.

    `create_sample_snapshots` and `BacktestConfig` are given only aware UTC
    datetimes here (via `app.utils.time.utcnow()`). PLAN.md §3 flags
    `engine.py`'s internal `datetime.utcnow()` (naive) as a latent
    naive/aware mixing bug. Under a plain `pytest -q` run this test passes
    cleanly (no naive/aware value is ever compared against another in the
    path exercised here, since no `progress_callback` is supplied). It is
    NOT marked `xfail`: it does not fail under the acceptance-criterion-1
    invocation, so per the brief's own conditional ("if it fails for THAT
    reason, mark it xfail") no xfail applies. See NOTES.md / the T02 report
    for the separate, narrower defect this surfaces under
    `-W error::DeprecationWarning` (T02 acceptance criterion 2).
    """
    start = utcnow()
    end = start + timedelta(hours=6)

    snapshots = create_sample_snapshots(
        market_id="m1",
        start_date=start,
        end_date=end,
        interval_minutes=15,
    )
    assert snapshots, "sample snapshot generation produced no data"

    config = BacktestConfig(start_date=start, end_date=end, initial_capital=10_000.0)
    strategy = get_strategy("favorite_compounder")
    backtester = Backtester(config, strategy)
    replayer = InMemoryDataReplayer(snapshots)

    result = await backtester.run(replayer)

    assert isinstance(result, BacktestResult)
    assert result.snapshots_processed == len(snapshots)
