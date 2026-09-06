"""`app/api/routes/trading.py` wired to the real `OrderRouter` (T16).

Every test here runs the SAME `OrderRouter`/`PaperVenueAdapter` the live
path will use (PLAN.md D4) behind the real FastAPI app — only the
`get_router` dependency is replaced, via `app.dependency_overrides`, with
a router built over `tests.venues.fixture_adapter.FixtureAdapter` (no
network, GUARDRAILS.md §1.4) and this test's own in-memory session
factory, exactly as the brief specifies. `get_async_session` is likewise
overridden so the API's own reads (`GET /orders`, `GET /positions`) see
what the router just wrote — both share the SAME `async_sessionmaker`
over the SAME in-memory SQLite engine, each dependency call opening and
closing its own short-lived session (matching `app.database.
get_async_session`'s real shape) rather than one long-lived session held
open across requests, which would fight the router's own internal
sessions for the single `StaticPool` connection.

This intentionally does NOT reuse `tests/conftest.py`'s `client` fixture
(which pins one ambient session with no `get_router` override) — the
`client` fixture defined below shadows it for this module only, a
standard pytest fixture-resolution rule.

No money-math is re-derived here (that is `tests/execution/test_router.
py`'s job in full, with every fee hand-computed per GUARDRAILS.md §5);
these tests exercise the WIRING — request -> `Intent` -> `OrderRouter` ->
persisted rows -> response — with a locked 0.50/0.50 book so a 10-contract
BUY fills completely and unambiguously.
"""
from collections.abc import AsyncGenerator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.api.deps import get_router
from app.config import Settings
from app.database import get_async_session
from app.execution.ledger import CapitalLedger
from app.execution.router import OrderRouter
from app.main import app
from app.venues.paper import make_paper_adapter
from tests.helpers import make_book
from tests.venues.fixture_adapter import FixtureAdapter, make_venue_market

PM_MARKET = "PM-1"


def build_settings(**overrides: object) -> Settings:
    """Build an explicit paper-mode `Settings` (GUARDRAILS.md §1.2)."""
    fields: dict[str, object] = {
        "trading_mode": "paper",
        "paper_starting_balances": {"polymarket": 1000.0, "kalshi": 1000.0},
    }
    fields.update(overrides)
    return Settings(**fields)  # type: ignore[arg-type]


@pytest_asyncio.fixture
async def session_factory(
    test_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """An `async_sessionmaker` over the shared in-memory `test_engine`.

    Both the fixture `OrderRouter` and the API's own overridden
    `get_async_session` read/write through THIS factory, so a row the
    router persists is immediately visible to a `GET` that follows it.
    """
    return async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def fixture_router(
    session_factory: async_sessionmaker[AsyncSession],
) -> OrderRouter:
    """An `OrderRouter` over a `PaperVenueAdapter`/`FixtureAdapter` pair.

    The YES book is LOCKED at 0.50/0.50 (legal — only a crossed book is
    refused) with 500 contracts of depth, so a 10-contract BUY at 0.50
    fills completely and deterministically.
    """
    settings_obj = build_settings()
    inner = FixtureAdapter("polymarket")
    inner.add_market(make_venue_market("polymarket", PM_MARKET))
    inner.set_book(
        make_book(
            bids=[(0.50, 500.0)],
            asks=[(0.50, 500.0)],
            venue="polymarket",
            market_id=PM_MARKET,
            outcome="YES",
        )
    )
    ledger = CapitalLedger.paper(settings_obj)
    adapter = make_paper_adapter("polymarket", inner=inner, ledger=ledger)
    return OrderRouter(
        {"polymarket": adapter}, ledger, session_factory, fences=settings_obj
    )


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
    fixture_router: OrderRouter,
) -> AsyncGenerator[AsyncClient, None]:
    """An HTTP client wired to the fixture router and a fresh-per-call session.

    Shadows `tests/conftest.py`'s `client` fixture for this module only.
    """

    async def override_get_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

    app.dependency_overrides[get_async_session] = override_get_session
    app.dependency_overrides[get_router] = lambda: fixture_router

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


def _order_payload(**overrides: object) -> dict[str, object]:
    """A valid `POST /orders` body: BUY 10 YES @ 0.50 on the fixture market."""
    fields: dict[str, object] = {
        "market_id": PM_MARKET,
        "outcome": "YES",
        "side": "BUY",
        "size": 10.0,
        "price": 0.50,
        "venue": "polymarket",
    }
    fields.update(overrides)
    return fields


async def test_place_order_returns_200_with_status_and_client_order_id(
    client: AsyncClient,
) -> None:
    """Brief acceptance: place -> 200 with `status` and `client_order_id`."""
    response = await client.post("/api/v1/trading/orders", json=_order_payload())

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "executed"
    assert body["client_order_id"]
    assert body["id"] is not None
    assert body["filled_size"] == 10.0
    assert body["avg_price"] == 0.50


async def test_list_orders_shows_the_placed_order(client: AsyncClient) -> None:
    """Brief acceptance: list orders shows it."""
    placed = await client.post("/api/v1/trading/orders", json=_order_payload())
    client_order_id = placed.json()["client_order_id"]

    response = await client.get("/api/v1/trading/orders")

    assert response.status_code == 200
    orders = response.json()["orders"]
    assert len(orders) == 1
    order = orders[0]
    assert order["client_order_id"] == client_order_id
    assert order["venue"] == "polymarket"
    assert order["market_id"] == PM_MARKET
    assert order["outcome"] == "YES"
    assert order["side"] == "BUY"
    assert order["status"] == "FILLED"
    assert order["mode"] == "paper"
    assert order["filled_size"] == 10.0


async def test_positions_reflect_the_fill(client: AsyncClient) -> None:
    """Brief acceptance: positions reflect the fill."""
    await client.post("/api/v1/trading/orders", json=_order_payload())

    list_response = await client.get("/api/v1/trading/positions")
    scoped_response = await client.get(f"/api/v1/trading/positions/polymarket/{PM_MARKET}")

    for response in (list_response, scoped_response):
        assert response.status_code == 200
        positions = response.json()["positions"]
        assert len(positions) == 1
        position = positions[0]
        assert position["venue"] == "polymarket"
        assert position["market_id"] == PM_MARKET
        assert position["outcome"] == "YES"
        assert position["size"] == 10.0
        assert position["avg_entry_price"] == 0.50


async def test_positions_scoped_to_a_different_market_are_empty(
    client: AsyncClient,
) -> None:
    """The venue/market_id path filters, rather than returning everything."""
    await client.post("/api/v1/trading/orders", json=_order_payload())

    response = await client.get("/api/v1/trading/positions/polymarket/OTHER-MARKET")

    assert response.status_code == 200
    assert response.json()["positions"] == []


async def test_trading_mode_says_paper(client: AsyncClient) -> None:
    """Brief acceptance: `GET /trading/mode` says `paper`."""
    response = await client.get("/api/v1/trading/mode")

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "paper"
    assert body["kill_switch"] is False


async def test_cancel_an_already_filled_order_is_a_reported_no_op(
    client: AsyncClient,
) -> None:
    """`DELETE /orders/{id}` on a FILLED order does not lie about cancelling it.

    See `OrderRouter.cancel()`'s docstring: a terminal order is a no-op,
    not an error, and not a false "cancelled" either.
    """
    placed = await client.post("/api/v1/trading/orders", json=_order_payload())
    order_id = placed.json()["id"]

    response = await client.delete(f"/api/v1/trading/orders/{order_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == order_id
    assert body["cancelled"] is False
    assert body["reason"] == "already_terminal"
    assert body["status"] == "FILLED"


async def test_cancel_an_unknown_order_returns_404(client: AsyncClient) -> None:
    """A nonexistent `orders.id` is reported as not found, not a crash."""
    response = await client.delete("/api/v1/trading/orders/999999")

    assert response.status_code == 404
