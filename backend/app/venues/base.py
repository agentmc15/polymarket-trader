"""The `VenueAdapter` seam (PLAN.md D3).

Strategies and the execution router talk only to `VenueAdapter`, never to a
venue's HTTP client directly. Concrete adapters — `app/venues/polymarket/`
(T11) and `app/venues/kalshi/` (T12) — implement this Protocol;
`app/venues/registry.py::get_adapter()` is how callers obtain one. Every
I/O method is `async`: concrete adapters use `httpx.AsyncClient` with an
injectable `transport` (GUARDRAILS.md §4), and the synchronous
`py_clob_client` is wrapped in `asyncio.to_thread`, never called directly
on the event loop.

GUARDRAILS.md §1.1: the only modules allowed to contain order-placement
calls are `app/venues/polymarket/live.py` and `app/venues/kalshi/live.py`
(`tests/test_fences.py` enforces this by AST walk, T13). Nothing in this
file places an order.
"""
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from app.venues.types import (
    Balance,
    FeeSchedule,
    Fill,
    MarketStatus,
    OrderAck,
    OrderBook,
    OrderRequest,
    Position,
    VenueId,
    VenueMarket,
)


class VenueError(Exception):
    """Base class for all venue-adapter errors."""


class VenuePayloadError(VenueError):
    """A venue response could not be parsed into a normalized type.

    Attributes:
        raw: The raw, unparsed payload that failed to parse (dict/str/
            bytes), kept for logging/debugging. Venue market/book payloads
            never carry a secret, but callers should still log
            deliberately (GUARDRAILS.md §1.3).
    """

    def __init__(self, message: str, raw: object) -> None:
        """Store the parse-failure message and the offending raw payload."""
        super().__init__(message)
        self.raw = raw


class VenueAuthError(VenueError):
    """The venue rejected credentials or a signed request."""


class VenueRateLimited(VenueError):
    """The venue responded with a rate limit (e.g. HTTP 429).

    Attributes:
        retry_after_s: Seconds to wait before retrying, if the venue
            supplied one (e.g. a `Retry-After` header); `None` if it did
            not, in which case the caller applies its own backoff.
    """

    def __init__(self, message: str, retry_after_s: float | None = None) -> None:
        """Store the rate-limit message and an optional retry-after hint."""
        super().__init__(message)
        self.retry_after_s = retry_after_s


class FeeModel(ABC):
    """Computes the dollar fee for one fill under a venue's fee formula.

    NOTE ON PLACEMENT: this ABC is defined here (T04), not in
    `app/venues/fees.py` (T05), only because `VenueAdapter.fee_model()`
    below needs a concrete return type before T05 exists. T05 ("Fee models
    and cost settings") must `from app.venues.base import FeeModel` and
    subclass it as `PolymarketFeeModel`/`KalshiFeeModel` — it must not
    redefine a second, incompatible `FeeModel`.
    """

    @abstractmethod
    def fee(
        self,
        price: float,
        size_contracts: float,
        liquidity: Literal["maker", "taker"],
        schedule: FeeSchedule,
    ) -> float:
        """Return the dollar fee for a single fill.

        Args:
            price: Fill price, a probability in [0.0, 1.0].
            size_contracts: Fill size in contracts (1 contract pays $1.00
                at resolution).
            liquidity: `"maker"` or `"taker"` — the side of the fill.
            schedule: The market's `FeeSchedule` (rate + source).

        Returns:
            float: Fee in USD, always >= 0.
        """
        raise NotImplementedError


@runtime_checkable
class MarketDataAdapter(Protocol):
    """The READ-ONLY subset of `VenueAdapter` (T14).

    `PolymarketAdapter` and `KalshiAdapter` satisfy this but NOT the full
    `VenueAdapter` Protocol, and that is by design: order placement and
    cancellation exist only in the two `live.py` modules (GUARDRAILS.md
    §1.1), so a read adapter structurally cannot be one. Before this
    Protocol existed there was no type that said "market data, nothing
    more", and anything holding a read adapter had to annotate it as the
    full `VenueAdapter` and be wrong.

    `app.venues.paper.PaperVenueAdapter` is the concrete consumer: it
    wraps one of these for market data and answers the write half of
    `VenueAdapter` itself, from `SimulatedFillEngine`. Anything that only
    reads books and market metadata (a scanner, a matcher, a scorer)
    should ask for this rather than for a full `VenueAdapter`, so it
    cannot accidentally acquire the ability to place an order.
    """

    venue: VenueId

    async def list_markets(
        self,
        status: MarketStatus | None = None,
        updated_since: datetime | None = None,
    ) -> list[VenueMarket]:
        """List markets on this venue, optionally filtered.

        Args:
            status: Only return markets in this status, if given.
            updated_since: Only return markets updated at/after this
                aware UTC timestamp, if given.

        Returns:
            list[VenueMarket]: Matching markets.
        """
        ...

    async def get_market(self, market_id: str) -> VenueMarket:
        """Fetch one market by its venue-native identifier.

        Args:
            market_id: Venue-native market identifier.

        Returns:
            VenueMarket: The normalized market.
        """
        ...

    async def get_book(self, market_id: str, outcome: str) -> OrderBook:
        """Fetch the current order book for one (market, outcome).

        Args:
            market_id: Venue-native market identifier.
            outcome: Outcome name, e.g. `"YES"`/`"NO"`.

        Returns:
            OrderBook: The normalized book snapshot.
        """
        ...

    def fee_model(self) -> FeeModel:
        """Return this venue's `FeeModel`.

        Returns:
            FeeModel: The fee model to use for fills on this venue.
        """
        ...


@runtime_checkable
class ReconcileAdapter(Protocol):
    """The READ-ONLY subset `app.execution.reconcile` needs (T14 remediation).

    Reconciliation compares locally-persisted orders against the venue's
    own record of them. That is a pure READ — `get_open_orders()` and
    `get_fills()` and nothing else — yet before this Protocol existed the
    pass had to be handed a full `VenueAdapter`, which in live mode meant
    constructing an adapter that CAN place orders, behind the live-trading
    fence. The consequence was backwards: an engaged kill switch made the
    live adapter unconstructible and therefore STOPPED reconciliation,
    the read-only pass an operator most wants during a halt, while
    already-constructed routers went on placing (see
    `app/execution/fences.py`'s docstring).

    Asking for this Protocol instead is the separation: the read adapters
    (`PolymarketAdapter`/`KalshiAdapter`) satisfy it and structurally
    cannot place an order (GUARDRAILS.md §1.1 keeps placement in the two
    `live.py` modules), so the reconciliation path never evaluates a
    placement fence at all. `PaperVenueAdapter` satisfies it too, which is
    what keeps paper-mode reconciliation unchanged.
    """

    venue: VenueId

    async def get_open_orders(self) -> list[OrderAck]:
        """List orders currently resting on the venue.

        Returns:
            list[OrderAck]: One acknowledgement per open order.
        """
        ...

    async def get_fills(self, since: datetime) -> list[Fill]:
        """List fills executed at/after `since`.

        Args:
            since: Aware UTC lower bound.

        Returns:
            list[Fill]: Fills, oldest first.
        """
        ...


@runtime_checkable
class VenueAdapter(Protocol):
    """Normalized read/write interface to one venue, in one trading mode.

    Prices are probabilities in [0.0, 1.0] and sizes are contracts on
    every venue; a venue's native price/size encoding (e.g. Kalshi's
    fixed-point dollar strings) is converted at the concrete adapter
    boundary and nowhere else.
    """

    venue: VenueId

    async def list_markets(
        self,
        status: MarketStatus | None = None,
        updated_since: datetime | None = None,
    ) -> list[VenueMarket]:
        """List markets on this venue, optionally filtered.

        Args:
            status: Only return markets in this status, if given.
            updated_since: Only return markets updated at/after this
                aware UTC timestamp, if given.

        Returns:
            list[VenueMarket]: Matching markets.
        """
        ...

    async def get_market(self, market_id: str) -> VenueMarket:
        """Fetch one market by its venue-native identifier.

        Args:
            market_id: Venue-native market identifier.

        Returns:
            VenueMarket: The normalized market.
        """
        ...

    async def get_book(self, market_id: str, outcome: str) -> OrderBook:
        """Fetch the current order book for one (market, outcome).

        Args:
            market_id: Venue-native market identifier.
            outcome: Outcome name, e.g. `"YES"`/`"NO"`.

        Returns:
            OrderBook: The normalized book snapshot.
        """
        ...

    async def get_balance(self) -> Balance:
        """Fetch this venue's account balance.

        Returns:
            Balance: Available and locked USD.
        """
        ...

    async def get_positions(self) -> list[Position]:
        """Fetch all open positions on this venue.

        Returns:
            list[Position]: Currently held positions.
        """
        ...

    async def get_open_orders(self) -> list[OrderAck]:
        """Fetch all currently open (resting) orders on this venue.

        Returns:
            list[OrderAck]: Acknowledgements for each open order.
        """
        ...

    async def get_fills(self, since: datetime) -> list[Fill]:
        """Fetch fills at/after a given time.

        Args:
            since: Aware UTC timestamp; only fills at or after this time
                are returned.

        Returns:
            list[Fill]: Matching fills.
        """
        ...

    async def place_order(self, order: OrderRequest) -> OrderAck:
        """Place an order on this venue.

        Args:
            order: The normalized order to place.

        Returns:
            OrderAck: The venue's acknowledgement.
        """
        ...

    async def cancel_order(self, order_id: str) -> None:
        """Cancel a resting order.

        Args:
            order_id: Venue-native order identifier to cancel.
        """
        ...

    def fee_model(self) -> FeeModel:
        """Return this venue's `FeeModel`.

        Returns:
            FeeModel: The fee model to use for fills on this venue.
        """
        ...

    def stream_books(
        self, subscriptions: list[tuple[str, str]]
    ) -> AsyncIterator[OrderBook]:
        """Stream order-book updates for the given (market_id, outcome) pairs.

        Not implemented in this kit — PLAN.md §2: "No Kalshi WebSocket in
        this kit. REST polling only." Declared without `async def` (like
        `typeshed`'s `__aiter__`) because calling something that returns
        an async iterator is itself a synchronous call; only iterating the
        result is asynchronous.

        Args:
            subscriptions: `(market_id, outcome)` pairs to stream.

        Returns:
            AsyncIterator[OrderBook]: A stream of book updates.
        """
        ...


class BaseAdapter:
    """Mixin providing the shared, deliberately-unimplemented `stream_books`.

    Concrete adapters (T11 Polymarket, T12 Kalshi) mix this in so neither
    repeats the stub. See `VenueAdapter.stream_books` above for why this is
    a plain (non-`async`) method.
    """

    venue: VenueId

    def stream_books(
        self, subscriptions: list[tuple[str, str]]
    ) -> AsyncIterator[OrderBook]:
        """Raise immediately — REST polling only in this kit.

        Args:
            subscriptions: `(market_id, outcome)` pairs to stream.

        Raises:
            NotImplementedError: Always.
        """
        raise NotImplementedError(
            "stream_books is not implemented in this kit (REST polling "
            "only, see PLAN.md §2)"
        )
