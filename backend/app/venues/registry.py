"""Venue adapter registry.

`get_adapter(venue, mode)` is the only way strategy/execution code obtains
a `VenueAdapter` instance (PLAN.md D3: "the seam is the answer, not a pile
of adapters"). Values are zero-argument factories, not already-constructed
adapters, so `get_adapter` never hands back a cached singleton and never
constructs a live adapter until it is actually asked for one — a live
adapter's confirmation-gate check (`app.execution.fences.
assert_live_allowed`, T11) runs at CONSTRUCTION time, inside the factory
call, not at registration time.

T11 registers Polymarket's `"live"` mode: `"live"` -> `PolymarketLiveAdapter`,
which refuses to construct unless the live-trading fence
(`app.execution.fences.assert_live_allowed`) is satisfied. T12 registers
Kalshi's `"live"` mode the same way. A read adapter (`PolymarketAdapter`,
`KalshiAdapter`) cannot be registered for either mode on its own: it has
no order-placement methods — by design, GUARDRAILS.md §1.1 keeps those in
`live.py` only — so it does not structurally satisfy the full
`VenueAdapter` Protocol and cannot be the value type this dict declares.
(It does satisfy `app.venues.base.MarketDataAdapter`, the read subset.)

T14 registers both `"paper"` entries. PLAN.md D4: paper mode is answered
by `app.venues.paper.PaperVenueAdapter`, which WRAPS a read adapter for
market-data reads and answers the write half itself from
`SimulatedFillEngine` — that composition is what satisfies the Protocol.
The paper factories are the ONE exception to the "never a cached
singleton" rule above, and `_register()` explains why. (The T11 brief
text says `mode="read"`, which is not one of `TradingMode`'s two values;
this is the resolution — see NOTES.md.)

REGISTRATION IS LAZY ON PURPOSE (`_register()` below, called from
`get_adapter`, not at module import time): `app.venues.polymarket.live`
imports `app.execution.fences`, and importing ANY name from the
`app.execution` PACKAGE runs `app/execution/__init__.py`, which imports
`app.execution.fill_engine`, which imports `app.strategies.base` for
`MarketSnapshot`. `app.strategies.base` itself imports `app.venues.types`
— which, because `app.venues.types` is a sibling of this module inside
the same `app.venues` package, first runs `app/venues/__init__.py`, which
imports THIS module. Importing the concrete adapters at `app.venues.
registry` module scope would therefore close a real import cycle back
onto `app.strategies.base` while it is still mid-initialization
(`ImportError: cannot import name 'MarketSnapshot' from partially
initialized module`). Deferring the adapter imports to first-call time —
well after normal app startup has finished importing everything — avoids
the cycle without changing what any caller sees.
"""
from collections.abc import Callable
from functools import partial
from typing import Literal

from app.venues.base import ReconcileAdapter, VenueAdapter
from app.venues.types import VenueId

#: `"paper"` (default, GUARDRAILS.md §1.2) or `"live"`.
TradingMode = Literal["paper", "live"]

#: `(venue, mode) -> zero-arg factory returning a fresh VenueAdapter`.
#: Populated lazily by `_register()` (see module docstring) on the first
#: `get_adapter()` call: T11 (Polymarket `"live"`), T12 (Kalshi `"live"`),
#: T14 (`PaperVenueAdapter`, both venues' `"paper"`).
_ADAPTERS: dict[tuple[VenueId, TradingMode], Callable[[], VenueAdapter]] = {}

#: Set `True` once `_register()` has populated `_ADAPTERS`, so repeated
#: `get_adapter()` calls don't re-import/re-populate every time.
_registered = False


def _register() -> None:
    """Populate `_ADAPTERS`, importing concrete adapters lazily.

    See the module docstring for why this is deferred to first-call time
    rather than done at module import time.
    """
    global _registered
    if _registered:
        return
    from app.venues.kalshi.live import KalshiLiveAdapter
    from app.venues.paper import make_paper_adapter
    from app.venues.polymarket.live import PolymarketLiveAdapter

    _ADAPTERS[("polymarket", "live")] = PolymarketLiveAdapter
    _ADAPTERS[("kalshi", "live")] = KalshiLiveAdapter
    # T14: `PaperVenueAdapter` wraps the venue's READ adapter and answers
    # the write half from `SimulatedFillEngine`, which is what finally
    # lets these two entries satisfy the `VenueAdapter` Protocol. Unlike
    # the live factories above, `make_paper_adapter` returns a
    # process-wide singleton per venue — a paper adapter's positions and
    # resting orders live only in memory, so a fresh instance per call
    # would forget every position between the order that opened it and
    # the reconciliation pass meant to check it. See that function's
    # docstring; the "never a cached singleton" rule in this module's
    # header is about LIVE adapters, whose fence check must re-run at
    # every construction.
    _ADAPTERS[("polymarket", "paper")] = partial(make_paper_adapter, "polymarket")
    _ADAPTERS[("kalshi", "paper")] = partial(make_paper_adapter, "kalshi")
    _registered = True


def get_adapter(venue: VenueId, mode: TradingMode) -> VenueAdapter:
    """Look up and construct the registered adapter for `(venue, mode)`.

    Args:
        venue: `"polymarket"` or `"kalshi"`.
        mode: `"paper"` or `"live"`.

    Returns:
        VenueAdapter: A freshly constructed adapter instance.

    Raises:
        KeyError: If no adapter is registered for `(venue, mode)`.
    """
    _register()
    try:
        factory = _ADAPTERS[(venue, mode)]
    except KeyError:
        raise KeyError(
            f"no adapter registered for venue={venue!r} mode={mode!r}"
        ) from None
    return factory()


def get_read_adapter(venue: VenueId, mode: TradingMode) -> ReconcileAdapter:
    """Return an adapter that can READ a venue's orders and fills, nothing more.

    This is what `app.tasks.execution` reconciles with, and it exists to
    separate two concerns that `get_adapter` had welded together. In
    `"live"` mode `get_adapter` constructs a LIVE adapter, whose
    constructor runs `app.execution.fences.assert_live_allowed()` — a
    fence about ORDER PLACEMENT. Reconciliation places nothing; it only
    looks. Making the read-only pass depend on the placement fence
    inverted the kill switch: engaging it stopped reconciliation (the
    pass an operator most wants during a halt) while cached routers went
    on placing orders. Here, `"live"` returns the venue's READ adapter —
    `PolymarketAdapter`/`KalshiAdapter`, which have no `place_order` at
    all (GUARDRAILS.md §1.1 keeps placement in the two `live.py`
    modules) — so no placement fence is involved and an engaged kill
    switch cannot stop a read.

    `"paper"` returns the SAME process-wide `PaperVenueAdapter` singleton
    `get_adapter` returns, because in paper mode that object IS the
    venue: its in-memory orders and fills are the only record there is to
    reconcile against.

    Args:
        venue: `"polymarket"` or `"kalshi"`.
        mode: `"paper"` or `"live"`.

    Returns:
        ReconcileAdapter: An adapter exposing `get_open_orders()`/
            `get_fills()`.

    Raises:
        KeyError: If `venue`/`mode` is not known.
    """
    if mode == "paper":
        from app.venues.paper import make_paper_adapter

        return make_paper_adapter(venue)
    if mode != "live":
        raise KeyError(f"unknown trading mode {mode!r}")
    if venue == "polymarket":
        from app.venues.polymarket.adapter import PolymarketAdapter

        return PolymarketAdapter()
    if venue == "kalshi":
        from app.venues.kalshi.adapter import KalshiAdapter

        return KalshiAdapter()
    raise KeyError(f"no read adapter for venue={venue!r}")
