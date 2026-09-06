"""`LiveShapedAdapter` — an adapter whose ACKS AND FILLS look like a real venue's.

WHY THIS EXISTS, AND WHY `PaperVenueAdapter` CANNOT REPLACE IT. Every
router test in this kit runs the shared `OrderRouter` over a
`PaperVenueAdapter`, which stamps `metadata["client_order_id"]` on every
simulated fill it produces (`app/venues/paper.py::_apply`). The real
adapters do not, and cannot:

- `app/venues/polymarket/adapter.py::_parse_fill` emits
  `metadata={"market": ..., "asset_id": ...}` and puts the VENUE's
  `taker_order_id` in `Fill.order_id`.
- `app/venues/kalshi/adapter.py::_parse_fill` emits
  `metadata={"market_id": ..., "outcome": ..., "fee_source": ...}` and
  puts the VENUE's `order_id` in `Fill.order_id`.

Neither venue's fills endpoint echoes a client order id at all. So a
router that matched fills to orders by the client key alone was green
across 552 paper tests while being structurally unable to book a single
live fill — no `Trade`, no `Position`, a zero fee, and the whole capital
reservation handed back as though nothing had been spent, on a FULLY
FILLED real order. A paper-only suite cannot see that; this adapter is
what makes it visible.

GUARDRAILS.md §1.1/§1.4: this places NOTHING. It has no HTTP client, no
credentials, and no code path that reaches a venue — `place_order` mints
a deterministic in-memory acknowledgement and `get_fills` replays the
fills implied by it. It is a SHAPE, not a connection. Market data is
delegated to the `FixtureAdapter` it wraps, exactly as `PaperVenueAdapter`
delegates to one.
"""
from datetime import datetime, timedelta
from itertools import count
from typing import Any

from app.utils.time import utcnow
from app.venues.base import FeeModel
from app.venues.types import (
    Balance,
    Fill,
    OrderAck,
    OrderBook,
    OrderRequest,
    Position,
    VenueId,
    VenueMarket,
)
from tests.venues.fixture_adapter import FixtureAdapter

#: `Fill.metadata` keys each venue's real fill parser emits. Reproduced
#: verbatim so this adapter is wrong in exactly the ways the real ones
#: are: neither contains a client order id.
LIVE_FILL_METADATA_KEYS: dict[VenueId, tuple[str, ...]] = {
    "polymarket": ("market", "asset_id"),
    "kalshi": ("market_id", "outcome", "fee_source"),
}


class LiveShapedAdapter:
    """A venue adapter that identifies its fills the way a real venue does.

    Attributes:
        venue: The venue this adapter answers for.
        place_order_calls: How many placements it has been asked for.
    """

    def __init__(
        self,
        inner: FixtureAdapter,
        *,
        fill_ratio: float = 1.0,
        fee_per_contract: float = 0.01,
        available: float = 1_000.0,
    ) -> None:
        """Wrap a market-data fixture with venue-shaped order handling.

        Args:
            inner: The `FixtureAdapter` supplying markets and books.
            fill_ratio: Fraction of each order that fills immediately, in
                `[0.0, 1.0]`. `1.0` fills in full.
            fee_per_contract: USD fee charged per filled contract. Stated
                as a flat per-contract number so a test can compute the
                expected fee by hand in one multiplication.
            available: USD reported by `get_balance()`.
        """
        self.venue: VenueId = inner.venue
        self.place_order_calls = 0
        self._inner = inner
        self._fill_ratio = fill_ratio
        self._fee_per_contract = fee_per_contract
        self._available = available
        self._ids = count(1)
        self._fills: list[Fill] = []

    # -- Market data (delegated) -----------------------------------------

    async def list_markets(self, *args: Any, **kwargs: Any) -> list[VenueMarket]:
        """Delegate to the wrapped fixture adapter."""
        return await self._inner.list_markets(*args, **kwargs)

    async def get_market(self, market_id: str) -> VenueMarket:
        """Delegate to the wrapped fixture adapter."""
        return await self._inner.get_market(market_id)

    async def get_book(self, market_id: str, outcome: str) -> OrderBook:
        """Delegate to the wrapped fixture adapter."""
        return await self._inner.get_book(market_id, outcome)

    def fee_model(self) -> FeeModel:
        """Delegate to the wrapped fixture adapter."""
        return self._inner.fee_model()

    async def get_balance(self) -> Balance:
        """Report a fixed balance; capital lives in the ledger, not here."""
        return Balance(venue=self.venue, available=self._available, locked=0.0)

    async def get_positions(self) -> list[Position]:
        """Report no venue positions; the router's rows are the subject."""
        return []

    # -- Order handling (venue-SHAPED) -----------------------------------

    async def place_order(self, order: OrderRequest) -> OrderAck:
        """Acknowledge an order with a VENUE order id, and record its fills.

        The acknowledgement carries a venue-native `order_id` that has
        nothing to do with `order.client_order_id` — which is the whole
        point. Every fill recorded here is stamped with THAT id and with
        the venue's own metadata keys, so nothing in the fill tape names
        the client key.

        Args:
            order: The normalized order.

        Returns:
            OrderAck: `"filled"` or `"partially_filled"`, per
                `fill_ratio`.
        """
        self.place_order_calls += 1
        now = utcnow()
        venue_order_id = f"{self.venue}-venue-order-{next(self._ids)}"
        filled = order.size * self._fill_ratio
        remaining = order.size - filled
        if filled > 0.0:
            self._fills.append(
                Fill(
                    venue=self.venue,
                    # THE VENUE's id, not the client's. Both real
                    # adapters do exactly this.
                    order_id=venue_order_id,
                    price=order.price,
                    size=filled,
                    fee=filled * self._fee_per_contract,
                    ts=now,
                    liquidity="taker",
                    metadata=self._metadata(order),
                )
            )
        return OrderAck(
            venue=self.venue,
            order_id=venue_order_id,
            client_order_id=order.client_order_id,
            status="filled" if remaining <= 1e-9 else "partially_filled",
            filled_size=filled,
            remaining_size=max(0.0, remaining),
            avg_fill_price=order.price if filled > 0.0 else None,
            ts=now,
        )

    async def cancel_order(self, order_id: str) -> None:  # noqa: ARG002
        """Accept a cancel; nothing rests in this adapter."""
        return None

    async def get_open_orders(self) -> list[OrderAck]:
        """Report no resting orders."""
        return []

    async def get_fills(self, since: datetime) -> list[Fill]:
        """Replay the fills this adapter has produced at/after `since`.

        Args:
            since: Aware UTC lower bound.

        Returns:
            list[Fill]: Matching fills, oldest first. None of them
                carries a `client_order_id` — see this module's
                docstring.
        """
        return [fill for fill in self._fills if fill.ts >= since - timedelta(seconds=1)]

    def _metadata(self, order: OrderRequest) -> dict[str, Any]:
        """Return the venue's own fill metadata keys, and only those."""
        if self.venue == "kalshi":
            return {
                "market_id": order.market_id,
                "outcome": order.outcome,
                "fee_source": "venue",
            }
        return {"market": order.market_id, "asset_id": f"{order.market_id}-asset"}
