"""`FixtureAdapter` — a deterministic, in-memory stand-in for a read adapter.

GUARDRAILS.md §1.4 forbids any network to a venue from a test, and
§1.1 forbids anything outside the two `live.py` modules from placing an
order. This adapter satisfies both by construction: it holds
already-normalized `VenueMarket`/`OrderBook` values in a dict and has no
`place_order`/`cancel_order` AT ALL — exactly like the real
`PolymarketAdapter`/`KalshiAdapter`, which is the point. It satisfies
`app.venues.base.MarketDataAdapter`, not the full `VenueAdapter`.

It is therefore the INNER adapter a `PaperVenueAdapter` wraps in tests:
paper supplies the write half from `SimulatedFillEngine`, this supplies
the market data. Because the books are exact, hand-written values rather
than replayed payloads, every fee and every fill in a router test can be
computed by hand in the test body (GUARDRAILS.md §5) instead of being
whatever a recorded fixture happened to contain.

The books are STATIC: `get_book` returns the same snapshot however many
times it is called, and a simulated fill does not deplete it. That is
deliberate — a test that wants a book to move calls `set_book()` between
steps, so the movement is visible in the test rather than being an
emergent side effect.
"""
from datetime import datetime, timedelta
from typing import Any

from app.utils.time import utcnow
from app.venues.base import BaseAdapter, FeeModel
from app.venues.fees import KalshiFeeModel, PolymarketFeeModel
from app.venues.types import (
    Balance,
    FeeSchedule,
    Fill,
    MarketStatus,
    OrderAck,
    OrderBook,
    Position,
    VenueId,
    VenueMarket,
)

#: Default fee schedules, one per venue, matching the rates PLAN.md §3
#: pins: Polymarket Politics 0.04 from the published category table,
#: Kalshi 0.07 from `Settings`. `source` is behavioural, not decoration —
#: `SimulatedFillEngine` treats a zero taker rate from an UNDECLARED
#: source as suspicious — so these use the same source strings the real
#: adapters emit.
DEFAULT_SCHEDULES: dict[VenueId, FeeSchedule] = {
    "polymarket": FeeSchedule(
        taker_rate=0.04, maker_rate=0.0, source="category_table"
    ),
    "kalshi": FeeSchedule(taker_rate=0.07, maker_rate=0.0, source="settings"),
}


def make_venue_market(
    venue: VenueId = "polymarket",
    market_id: str = "PM-1",
    *,
    outcomes: tuple[str, ...] = ("YES", "NO"),
    outcome_ids: dict[str, str] | None = None,
    tick_size: float = 0.01,
    min_size: float = 1.0,
    fee: FeeSchedule | None = None,
    status: MarketStatus = "open",
    close_time: datetime | None = None,
    question: str = "Will the fixture resolve YES?",
    **kwargs: Any,
) -> VenueMarket:
    """Build a normalized `VenueMarket` fixture.

    `outcome_ids` defaults to each venue's real convention: Polymarket
    mints a distinct CLOB token id per outcome, while Kalshi has no
    per-outcome token concept and its adapter maps BOTH outcomes to the
    market ticker. That asymmetry is exactly what
    `app.execution.router.token_id_for` has to cope with, so the fixture
    reproduces it rather than smoothing it over.

    Args:
        venue: `"polymarket"` or `"kalshi"`.
        market_id: Venue-native market identifier.
        outcomes: Outcome names.
        outcome_ids: Outcome -> venue-native id. Defaults per venue.
        tick_size: Minimum price increment.
        min_size: Minimum order size in contracts.
        fee: Fee schedule. Defaults to the venue's entry in
            `DEFAULT_SCHEDULES`.
        status: `"open"`, `"closed"` or `"resolved"`.
        close_time: Aware UTC close time. Defaults to 30 days out, well
            outside `near_resolution_hours`.
        question: Market question text.
        **kwargs: Any other `VenueMarket` field.

    Returns:
        VenueMarket: The fixture market.
    """
    if outcome_ids is None:
        outcome_ids = (
            {name: f"{market_id}-{name.lower()}" for name in outcomes}
            if venue == "polymarket"
            else dict.fromkeys(outcomes, market_id)
        )
    fields: dict[str, Any] = {
        "venue": venue,
        "market_id": market_id,
        "event_id": None,
        "question": question,
        "outcomes": outcomes,
        "outcome_ids": outcome_ids,
        "rules_text": "Resolves YES if the fixture says so.",
        "resolution_source": None,
        "close_time": close_time if close_time is not None else utcnow() + timedelta(days=30),
        "expected_settle_time": None,
        "status": status,
        "result": None,
        "tick_size": tick_size,
        "min_size": min_size,
        "fee": fee if fee is not None else DEFAULT_SCHEDULES[venue],
        "raw": {},
    }
    fields.update(kwargs)
    return VenueMarket(**fields)


class FixtureAdapter(BaseAdapter):
    """In-memory read adapter over hand-written markets and books.

    Attributes:
        venue: The venue this adapter answers for.
        get_book_calls: How many times `get_book` was asked, so a test
            can prove the paper adapter re-read the book (rather than
            reusing a stale one) before an unwind.
    """

    def __init__(
        self,
        venue: VenueId = "polymarket",
        *,
        markets: dict[str, VenueMarket] | None = None,
        books: dict[tuple[str, str], OrderBook] | None = None,
        balance: Balance | None = None,
        fee_model: FeeModel | None = None,
    ) -> None:
        """Build the adapter over the supplied fixtures.

        Args:
            venue: `"polymarket"` or `"kalshi"`.
            markets: Market id -> `VenueMarket`.
            books: `(market_id, outcome)` -> `OrderBook`.
            balance: Venue-reported balance. Note the router uses the
                `CapitalLedger`, not this, for sizing; it exists so the
                read surface is complete.
            fee_model: Override the venue's real `FeeModel`. Defaults to
                the genuine `PolymarketFeeModel`/`KalshiFeeModel`, so a
                test's hand-computed fee is the production formula.
        """
        self.venue: VenueId = venue
        self._markets: dict[str, VenueMarket] = dict(markets or {})
        self._books: dict[tuple[str, str], OrderBook] = dict(books or {})
        self._balance = (
            balance if balance is not None else Balance(venue=venue, available=0.0, locked=0.0)
        )
        self._fee_model = fee_model or (
            PolymarketFeeModel() if venue == "polymarket" else KalshiFeeModel()
        )
        self.get_book_calls = 0

    # -- Fixture construction -------------------------------------------

    def add_market(self, market: VenueMarket) -> "FixtureAdapter":
        """Register a market; returns `self` so calls can be chained."""
        self._markets[market.market_id] = market
        return self

    def set_book(self, book: OrderBook) -> "FixtureAdapter":
        """Register (or replace) one book; returns `self`."""
        self._books[(book.market_id, book.outcome)] = book
        return self

    # -- The read surface ------------------------------------------------

    async def list_markets(
        self,
        status: MarketStatus | None = None,
        updated_since: datetime | None = None,  # noqa: ARG002 - part of the Protocol
    ) -> list[VenueMarket]:
        """Return the registered markets, optionally filtered by status."""
        markets = list(self._markets.values())
        if status is not None:
            markets = [market for market in markets if market.status == status]
        return markets

    async def get_market(self, market_id: str) -> VenueMarket:
        """Return one registered market.

        Raises:
            KeyError: If the market was never registered — the same
                shape of failure a venue's 404 produces, so a test can
                exercise the router's `market_unavailable` rejection.
        """
        return self._markets[market_id]

    async def get_book(self, market_id: str, outcome: str) -> OrderBook:
        """Return the registered book for one (market, outcome).

        Raises:
            KeyError: If no book was registered for that pair.
        """
        self.get_book_calls += 1
        return self._books[(market_id, outcome)]

    async def get_balance(self) -> Balance:
        """Return the configured venue balance."""
        return self._balance

    async def get_positions(self) -> list[Position]:
        """Return no positions — a read adapter never opened one."""
        return []

    async def get_open_orders(self) -> list[OrderAck]:
        """Return no open orders — a read adapter never placed one."""
        return []

    async def get_fills(self, since: datetime) -> list[Fill]:  # noqa: ARG002
        """Return no fills — a read adapter never placed an order."""
        return []

    def fee_model(self) -> FeeModel:
        """Return the venue's real `FeeModel`."""
        return self._fee_model
