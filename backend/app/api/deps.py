"""API dependencies.

`get_router()` is the ONE place `OrderRouter` (`app.execution.router`,
PLAN.md D4) is constructed for the running application: an app-scoped
singleton, built once from `app.venues.registry.get_adapter()` in
whatever `Settings.trading_mode` is currently active, and reused across
every request. A fresh `OrderRouter` per request would not be UNSAFE
(each call still goes through the same database and the same paper
adapters, which are themselves process-wide singletons — see
`app.venues.paper.make_paper_adapter`), but it would rebuild the
`CapitalLedger` from scratch on every call, silently forgetting every
reservation the process had already made.

Tests never exercise this function's real body: `tests/api/
test_trading.py` replaces it wholesale via
`app.dependency_overrides[get_router]` with a router built over
`tests.venues.fixture_adapter.FixtureAdapter` and the test's own
in-memory session factory (GUARDRAILS.md §1.1/§1.4 — no network, no live
mode, no real adapter). `app.bots.base.BaseBot.execute_signal` calls
this same function directly (not through FastAPI's `Depends`) so a bot
and the API route it might otherwise duplicate always route through the
identical singleton.
"""
import asyncio
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session_factory, get_async_session
from app.execution.ledger import CapitalLedger
from app.execution.router import OrderRouter
from app.venues.base import MarketDataAdapter, VenueAdapter
from app.venues.registry import get_adapter, get_read_adapter
from app.venues.types import VenueId

# Type alias for database session dependency
AsyncSessionDep = Annotated[AsyncSession, Depends(get_async_session)]

#: Every venue `get_router()` wires an adapter for. Mirrors `VenueId` by
#: hand (same not-imported-as-a-loop rationale as
#: `app.config._PAPER_LEDGER_VENUES`) — a third venue needs an entry here
#: too.
_ROUTED_VENUES: tuple[VenueId, ...] = ("polymarket", "kalshi")

#: App-scoped `OrderRouter` singleton. `None` until the first
#: `get_router()` call builds it.
_router: OrderRouter | None = None

#: Guards `_router`'s construction so two requests racing on the very
#: first call cannot each build (and briefly seed) their own
#: `CapitalLedger` — only one may ever become the process-wide router.
_router_lock = asyncio.Lock()


async def _build_router() -> OrderRouter:
    """Construct the ONE `OrderRouter` this process routes every order through.

    Adapters come from `app.venues.registry.get_adapter`, in whatever
    mode `Settings.trading_mode` is currently set to: `"live"`
    constructs REAL venue adapters, whose constructors run
    `assert_live_allowed()` (PLAN.md D13) — an unarmed or kill-switched
    process cannot even build one; `"paper"` (the default, and the only
    mode any test process may run in — GUARDRAILS.md §1.2) constructs
    the in-memory `PaperVenueAdapter` singletons.

    The `CapitalLedger` is seeded to match: from each adapter's OWN
    reported balance in live mode (`CapitalLedger.from_balances`), since
    a live venue account is the only authority on what is actually free
    there; from `Settings.paper_starting_balances` in paper mode
    (`CapitalLedger.paper`), since there is no venue account to read at
    all.

    Returns:
        OrderRouter: Wired to the registry's adapters, a freshly seeded
            `CapitalLedger`, and the real `app.database` session factory.
    """
    adapters: dict[VenueId, VenueAdapter] = {
        venue: get_adapter(venue, settings.trading_mode) for venue in _ROUTED_VENUES
    }
    if settings.trading_mode == "live":
        balances = [await adapters[venue].get_balance() for venue in _ROUTED_VENUES]
        ledger = CapitalLedger.from_balances(balances)
    else:
        ledger = CapitalLedger.paper(settings)
    return OrderRouter(adapters, ledger, async_session_factory, fences=settings)


async def get_router() -> OrderRouter:
    """Return the app-scoped `OrderRouter` singleton, building it on first use.

    FastAPI dependency for every route in `app/api/routes/trading.py`
    that places or cancels an order. See this module's docstring for why
    tests never let this function's real body run.

    Returns:
        OrderRouter: The process-wide router.
    """
    global _router
    if _router is None:
        async with _router_lock:
            if _router is None:
                _router = await _build_router()
    return _router


# Type alias for the `OrderRouter` dependency.
RouterDep = Annotated[OrderRouter, Depends(get_router)]


async def get_market_data_adapters() -> dict[VenueId, MarketDataAdapter]:
    """Return one READ-ONLY adapter per venue, keyed by venue.

    Used by `app/api/routes/links.py` (T17), which reads market lists and
    market metadata to propose and to review cross-venue event links —
    and places nothing, ever.

    It routes through `app.venues.registry.get_read_adapter`, NOT
    `get_adapter`, and that distinction is the point. In `"live"` mode
    `get_adapter` constructs an adapter that CAN place orders, whose
    constructor runs `app.execution.fences.assert_live_allowed()`;
    `get_read_adapter` returns `PolymarketAdapter`/`KalshiAdapter`, which
    have no `place_order` at all (GUARDRAILS.md §1.1 keeps placement in
    the two `live.py` modules). The matcher therefore never evaluates a
    placement fence, and an engaged kill switch cannot stop a review —
    the same inversion `get_read_adapter`'s own docstring describes for
    reconciliation.

    A fresh adapter per call (not an app-scoped singleton like
    `get_router`) because these hold no state worth preserving: no
    ledger, no reservations, just HTTP configuration. In `"paper"` mode
    the registry hands back the process-wide `PaperVenueAdapter`
    singleton regardless.

    Returns:
        dict[VenueId, MarketDataAdapter]: One read adapter per routed
            venue.

    Raises:
        TypeError: If a registered adapter does not satisfy
            `MarketDataAdapter` — a wiring bug, caught here rather than
            as an `AttributeError` deep inside the matcher.
    """
    adapters: dict[VenueId, MarketDataAdapter] = {}
    for venue in _ROUTED_VENUES:
        adapter = get_read_adapter(venue, settings.trading_mode)
        if not isinstance(adapter, MarketDataAdapter):
            raise TypeError(
                f"read adapter for venue={venue!r} does not satisfy "
                f"MarketDataAdapter: {type(adapter).__name__}"
            )
        adapters[venue] = adapter
    return adapters


# Type alias for the read-only, per-venue market-data adapter mapping.
MarketDataAdaptersDep = Annotated[
    dict[VenueId, MarketDataAdapter], Depends(get_market_data_adapters)
]
