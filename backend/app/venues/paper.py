"""The paper-mode venue adapter (PLAN.md D4, T14).

WHY THIS EXISTS AT ALL. PLAN.md D4: "paper and live share one execution
path; only the last hop differs". `OrderRouter` is the single object that
turns an `Intent` into venue orders, and it must not know which mode it
is in — idempotency, partial fills, leg-failure policy, the capital
ledger and reconciliation all have to be exercised in paper exactly as
they would be live, or paper proves nothing about the code that will
eventually move real money. `PaperVenueAdapter` is that last hop: it
implements the FULL `VenueAdapter` Protocol by delegating every
market-data read to a real read adapter (`PolymarketAdapter`,
`KalshiAdapter`, or a fixture stand-in) and answering `place_order` from
`app.execution.fill_engine.SimulatedFillEngine` — the same engine the
backtester uses (PLAN.md D5).

GUARDRAILS.md §1.1: nothing here places a real order. `place_order`
below never contacts a venue; it reads that venue's CURRENT book through
the inner adapter and simulates against it.

Registry: `("polymarket", "paper")` and `("kalshi", "paper")` were left
unregistered by T11/T12 on purpose — the read adapters have no
order-placement methods (by design, §1.1) and so cannot satisfy the
Protocol alone. `make_paper_adapter()` at the bottom of this module is
what populates those two entries, and `app/venues/registry.py` imports it
lazily for the import-cycle reason documented there.

TICK / MIN_SIZE PARITY (Phase 1 review finding 7)
-------------------------------------------------
The backtester calls `SimulatedFillEngine.fill()` with NO `market=`,
deliberately: replayed history is off-grid and the resulting fills are
LABELED `tick_validated=False`. This adapter always HAS a real
`VenueMarket` (from `inner.get_market()`), so it always passes `market=`
and the engine therefore enforces `tick_size` and `min_size`. That is
the venue-realistic behaviour and it is the right call here — paper must
not book a fill a venue would have rejected. The consequence is an
asymmetry with the backtest path, which this module narrows but cannot
close on its own:

- NARROWED: `OrderRouter` snaps every limit price to the market tick, in
  the CONSERVATIVE direction, before an `OrderRequest` is ever built,
  and drops a leg below `min_size` before placing it — so a strategy's
  off-grid limit is not a hard rejection here.
- STILL OPEN: the backtester can book off-tick and sub-`min_size` fills
  that this adapter would decline. That residue is why T22 must report
  `tick_unvalidated_fills` beside every sweep row.

WHAT PAPER MODE CANNOT MODEL, AND WILL FLATTER YOU ON
-----------------------------------------------------
A simulated fill does not remove liquidity from the venue's real book,
because no trade actually happened there. Two consecutive paper orders
can therefore each consume the SAME resting $500 at the touch, and a
resting order re-checked by `poll()` can fill against depth an earlier
paper fill "already took". There is no market-impact model here and
inventing one would be a guess. The consequence is that paper fill rates
are an UPPER BOUND on live fill rates, not an estimate of them — the
sizing question ("at what capital does this edge die?") belongs to T22's
capital sweep against recorded depth, not to a paper run.

WHAT THIS ADAPTER DOES *NOT* DO: it never mutates the `CapitalLedger`.
The ledger it holds is read-only here, used to answer `get_balance()` so
paper's balance reads the way live's does. Reserving, settling and
crediting are `OrderRouter`'s job and only its job — two writers would
double-count every fill. This mirrors the fill engine, which also
declines to model capital ("the `CapitalLedger` in T14 owns those").

Units (GUARDRAILS.md §4): prices are probabilities in `[0.0, 1.0]`,
sizes are contracts (each pays $1.00 at resolution), fees and cash are
USD.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime
from itertools import count
from typing import Literal

from app.execution.fill_engine import SimulatedFillEngine
from app.execution.ledger import CapitalLedger
from app.utils.time import ensure_aware, utcnow
from app.venues.base import BaseAdapter, FeeModel, MarketDataAdapter
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

logger = logging.getLogger(__name__)

#: `Fill.metadata` key carrying the originating `client_order_id`.
#: `Fill` has no such field, and `Fill.order_id` is the VENUE's order id
#: — so without this, reconciliation could not tie a fill back to the
#: local `Order` row that caused it while that row is still `PENDING`
#: (a `PENDING` row has no `order_id` yet, by design: it is written
#: BEFORE the venue call, for crash-safety).
CLIENT_ORDER_ID_KEY = "client_order_id"

#: `Fill.metadata` key marking a fill as produced by this simulator
#: rather than by a venue. Persisted rows are already separated by their
#: `mode` column (PLAN.md D4), but a `Fill` that has escaped a row is
#: still self-identifying.
SIMULATED_KEY = "simulated"

#: The `OrderAck.status` values this adapter produces. Spelled as a
#: `Literal` (rather than left as `str`) so a typo is a type error here
#: instead of a `ValueError` out of `OrderAck.__post_init__` three frames
#: away. `"open"`/`"filled"`/`"partially_filled"`/`"rejected"` are the
#: four the brief names; `"cancelled"` is the fifth, reachable only
#: through an explicit cancellation.
PaperOrderStatus = Literal[
    "open", "filled", "partially_filled", "cancelled", "rejected"
]

#: `FillReason` values that are STRUCTURAL — the ORDER is wrong, not the
#: market. The fill engine splits its six reasons this way precisely so a
#: caller can tell "the book was thin this second" from "retrying this
#: forever is a bug", and this adapter turns that split into the
#: difference between an order that RESTS (retryable) and one that is
#: rejected outright. Resting a structurally-doomed order is how a
#: `post_only` order becomes an infinite retry loop.
_STRUCTURAL_REASONS = frozenset(
    {"crossed_book", "post_only_taker_engine", "zero_size_order"}
)

#: Absolute tolerance (contracts) for "is this order complete?" and "do
#: we hold enough to sell?". Deliberately the same magnitude
#: `OrderBook.walk()` and `SimulatedFillEngine` use, so this adapter can
#: never call an order complete that the engine still reports a residual
#: for, or vice versa.
_SIZE_EPSILON = 1e-9


@dataclass
class _PaperOrder:
    """One simulated order's mutable state.

    Attributes:
        order_id: Simulated venue-side id, minted by this adapter.
        request: The `OrderRequest` as submitted.
        filled_size: Contracts filled so far, across every re-check.
        remaining_size: Contracts still resting; `0.0` once terminal.
        notional: Running `price * size` total over this order's fills,
            so `avg_fill_price` never has to re-walk the fill list.
        fee: Running total fee, USD.
        status: Current `PaperOrderStatus`.
        created_at: When the order was accepted.
        fills: Every `Fill` this order has produced.
    """

    order_id: str
    request: OrderRequest
    filled_size: float
    remaining_size: float
    notional: float
    fee: float
    status: PaperOrderStatus
    created_at: datetime
    fills: list[Fill] = field(default_factory=list)

    @property
    def avg_fill_price(self) -> float | None:
        """Return the size-weighted average fill price, or `None`.

        Returns:
            float | None: `notional / filled_size`, clamped into
                `[0.0, 1.0]` against 1-ulp overshoot; `None` if nothing
                has filled (which is what `OrderAck` requires).
        """
        if self.filled_size <= 0.0:
            return None
        return min(1.0, max(0.0, self.notional / self.filled_size))

    def ack(self, ts: datetime) -> OrderAck:
        """Build the `OrderAck` describing this order's CURRENT state.

        Args:
            ts: Aware UTC timestamp to stamp on the acknowledgement.

        Returns:
            OrderAck: The acknowledgement.
        """
        return OrderAck(
            venue=self.request.venue,
            order_id=self.order_id,
            client_order_id=self.request.client_order_id,
            status=self.status,
            filled_size=self.filled_size,
            remaining_size=self.remaining_size,
            avg_fill_price=self.avg_fill_price,
            ts=ts,
        )


@dataclass
class _PaperPosition:
    """One simulated (market, outcome) holding.

    Attributes:
        market_id: Venue-native market identifier.
        outcome: Canonical outcome name (already normalized upstream by
            `app.strategies.base.normalize_outcome`).
        size: Contracts held, `>= 0` — neither venue supports naked
            shorts (PLAN.md §3), so this never goes negative.
        avg_price: Size-weighted average entry price.
    """

    market_id: str
    outcome: str
    size: float
    avg_price: float


class PaperVenueAdapter(BaseAdapter):
    """A full `VenueAdapter` that simulates the write path (PLAN.md D4).

    Reads (`list_markets`/`get_market`/`get_book`) are PROXIED to a real
    read adapter, so paper trades against the same book a live order
    would have hit. Writes are simulated against that book by
    `SimulatedFillEngine`.

    Attributes:
        venue: Taken from `inner.venue`. A paper adapter is always a
            paper adapter for ONE venue, never a merged view of two —
            capital and positions are per venue (GUARDRAILS.md §1.6).
    """

    def __init__(
        self,
        inner: MarketDataAdapter,
        fill_engine: SimulatedFillEngine | None = None,
        ledger: CapitalLedger | None = None,
    ) -> None:
        """Wrap a read adapter with a simulated write path.

        Args:
            inner: The read adapter to proxy market data to. Its
                order-placement methods are never called — the read
                adapters do not have any (GUARDRAILS.md §1.1).
            fill_engine: The shared simulator (PLAN.md D5). `None`
                builds the default engine for this venue: the inner
                adapter's own `FeeModel`, and a fee-schedule resolver
                reading each market's OWN `FeeSchedule` from this
                adapter's cache (never a default rate — see
                `_fee_schedule`).
            ledger: The `CapitalLedger` whose per-venue balances
                `get_balance()` reports. `None` builds a fresh
                `CapitalLedger.paper()`. READ ONLY here; see the module
                docstring for why this adapter never mutates it.
        """
        self.venue: VenueId = inner.venue
        self._inner = inner
        self._ledger = ledger if ledger is not None else CapitalLedger.paper()
        self._orders: dict[str, _PaperOrder] = {}
        self._by_client_id: dict[str, str] = {}
        self._open: dict[str, _PaperOrder] = {}
        self._fills: list[Fill] = []
        self._positions: dict[tuple[str, str], _PaperPosition] = {}
        self._markets: dict[str, VenueMarket] = {}
        self._reasons: dict[str, str | None] = {}
        self._ids = count(1)
        self._engine = (
            fill_engine
            if fill_engine is not None
            else SimulatedFillEngine(
                fee_models={self.venue: inner.fee_model()},
                schedules=self._fee_schedule,
            )
        )

    # -- Market data (proxied to the real read adapter) ------------------

    async def list_markets(
        self,
        status: MarketStatus | None = None,
        updated_since: datetime | None = None,
    ) -> list[VenueMarket]:
        """Proxy to the inner read adapter, caching what comes back.

        Args:
            status: Only return markets in this status, if given.
            updated_since: Only return markets updated at/after this
                aware UTC timestamp, if given.

        Returns:
            list[VenueMarket]: Whatever the inner adapter returns.
        """
        markets = await self._inner.list_markets(status, updated_since)
        for market in markets:
            self._markets[market.market_id] = market
        return markets

    async def get_market(self, market_id: str) -> VenueMarket:
        """Proxy to the inner read adapter, refreshing the cache.

        The cache is what makes `SimulatedFillEngine`'s SYNCHRONOUS
        `schedules(venue, market_id)` resolver possible: the engine
        cannot await, so the market — and therefore its `FeeSchedule` —
        must already be in hand when `fill()` is called. It is refreshed
        on every call, so a market that closes, or whose fee waiver
        expires, is picked up on the next order rather than being served
        stale forever.

        Args:
            market_id: Venue-native market identifier.

        Returns:
            VenueMarket: The normalized market.
        """
        market = await self._inner.get_market(market_id)
        self._markets[market_id] = market
        return market

    async def get_book(self, market_id: str, outcome: str) -> OrderBook:
        """Proxy to the inner read adapter.

        Args:
            market_id: Venue-native market identifier.
            outcome: Outcome name, e.g. `"YES"`/`"NO"`.

        Returns:
            OrderBook: The current normalized book.
        """
        return await self._inner.get_book(market_id, outcome)

    def fee_model(self) -> FeeModel:
        """Return the inner adapter's `FeeModel` — the real venue's fees.

        Returns:
            FeeModel: `inner.fee_model()`. Paper pays the same modelled
                fees live would (GUARDRAILS.md §1.5); a paper mode with
                free trades would validate nothing.
        """
        return self._inner.fee_model()

    def cached_market(self, market_id: str) -> VenueMarket | None:
        """Return a market previously fetched through this adapter.

        Args:
            market_id: Venue-native market identifier.

        Returns:
            VenueMarket | None: The cached market, or `None` if this
                adapter has not fetched it yet.
        """
        return self._markets.get(market_id)

    # -- Account state (answered from memory) ---------------------------

    async def get_balance(self) -> Balance:
        """Return THIS venue's balance from the `CapitalLedger`.

        Returns:
            Balance: `available`/`locked` for this venue only. Unlike
                Kalshi's real payload (whose `locked` is always `0.0`),
                this `locked` is meaningful — it is the router's own
                accounting of capital committed to outstanding
                reservations, which is the only place that commitment is
                visible at all.
        """
        return self._ledger.balance(self.venue)

    async def get_positions(self) -> list[Position]:
        """Return every non-empty simulated holding on this venue.

        Returns:
            list[Position]: Current positions. A fully-sold position is
                dropped rather than reported at size zero, matching what
                a venue's portfolio endpoint returns.
        """
        return [
            Position(
                venue=self.venue,
                market_id=pos.market_id,
                outcome=pos.outcome,
                size=pos.size,
                avg_price=pos.avg_price,
            )
            for pos in self._positions.values()
            if pos.size > _SIZE_EPSILON
        ]

    async def get_open_orders(self) -> list[OrderAck]:
        """Return an ack for every order still resting on this venue.

        Returns:
            list[OrderAck]: One per resting order. Only a `"GTC"` order
                can rest — `"IOC"`/`"FOK"` residuals are killed at
                placement time, as both real venues do.
        """
        now = utcnow()
        return [order.ack(now) for order in self._open.values()]

    async def get_fills(self, since: datetime) -> list[Fill]:
        """Return simulated fills at or after `since`.

        Args:
            since: Aware UTC lower bound (inclusive).

        Returns:
            list[Fill]: Matching fills, in the order they occurred. Each
                carries `metadata[CLIENT_ORDER_ID_KEY]`, so a caller can
                tie it back to a local order row that has no venue
                `order_id` yet.

        Raises:
            ValueError: If `since` is naive.
            TypeError: If `since` is not a `datetime`.
        """
        ensure_aware(since)
        return [fill for fill in self._fills if fill.ts >= since]

    def fill_reason(self, client_order_id: str) -> str | None:
        """Return WHY a submitted order did not fill, if it did not.

        The `FillReason` vocabulary is the fill engine's, and its
        structural-vs-retryable split is what a caller needs in order to
        decide whether a retry is sane. `OrderAck` has nowhere to carry
        it, so it is offered here as an OPTIONAL enrichment:
        `OrderRouter` reaches for it with `getattr` and works without
        it, which keeps the paper and live paths identical where it
        matters.

        Args:
            client_order_id: The order's idempotency key.

        Returns:
            str | None: A `FillReason` (or a paper-specific rejection
                reason), or `None` if the order filled or is unknown.
        """
        return self._reasons.get(client_order_id)

    # -- The simulated write path ---------------------------------------

    async def place_order(self, order: OrderRequest) -> OrderAck:
        """Simulate `order` against this venue's current book.

        IDEMPOTENCY IS REAL HERE, NOT FAKED. A repeat of a
        `client_order_id` this adapter has already accepted returns the
        ORIGINAL order's acknowledgement and simulates nothing — exactly
        what a venue does with a duplicate idempotency key, and exactly
        the behaviour `OrderRouter`'s retry path depends on. It is not a
        frozen copy of the first ack: the live order state is
        re-serialized, so an order that has since filled reports filled.

        Args:
            order: The normalized order to simulate.

        Returns:
            OrderAck: `"filled"`, `"partially_filled"`, `"open"` (a
                `"GTC"` residual now resting) or `"rejected"`.

        Raises:
            ValueError: If `order.venue` is not this adapter's venue.
        """
        now = utcnow()
        if order.venue != self.venue:
            raise ValueError(
                f"order for venue {order.venue!r} submitted to the "
                f"{self.venue!r} paper adapter; capital and positions are per "
                "venue (GUARDRAILS.md §1.6)"
            )
        existing_id = self._by_client_id.get(order.client_order_id)
        if existing_id is not None:
            existing = self._orders[existing_id]
            logger.info(
                "order",
                extra={
                    "event": "duplicate_client_order_id",
                    "venue": self.venue,
                    "client_order_id": order.client_order_id,
                    "order_id": existing.order_id,
                    "status": existing.status,
                    "filled_size": existing.filled_size,
                    "simulated": True,
                },
            )
            return existing.ack(now)

        state = _PaperOrder(
            order_id=f"paper-{self.venue}-{next(self._ids)}",
            request=order,
            filled_size=0.0,
            remaining_size=order.size,
            notional=0.0,
            fee=0.0,
            status="open",
            created_at=now,
        )
        self._orders[state.order_id] = state
        self._by_client_id[order.client_order_id] = state.order_id

        if order.side == "SELL" and order.size > self._held(
            order.market_id, order.outcome
        ) + _SIZE_EPSILON:
            # Neither venue supports naked shorts (PLAN.md §3), so a SELL
            # larger than the holding is REJECTED rather than quietly
            # opening a negative position. An unwind that tries to sell
            # more than it actually got filled on lands here.
            return self._reject(state, "insufficient_position", now)

        try:
            market = await self.get_market(order.market_id)
        except Exception:
            logger.exception(
                "order",
                extra={
                    "event": "market_unavailable",
                    "venue": self.venue,
                    "market_id": order.market_id,
                    "client_order_id": order.client_order_id,
                },
            )
            return self._reject(state, "market_unavailable", now)

        book = await self.get_book(order.market_id, order.outcome)
        try:
            result = self._engine.fill(order, book, now, market=market)
        except ValueError as exc:
            # The engine raises for an off-tick limit price once a
            # `VenueMarket` is supplied. A real venue rejects that order
            # too, so a rejection — rather than an exception escaping
            # `place_order` — is the venue-faithful answer.
            # `OrderRouter` snaps limits to the tick before reaching
            # here, so this is a backstop for any other caller.
            logger.warning(
                "order",
                extra={
                    "event": "rejected_by_engine",
                    "venue": self.venue,
                    "market_id": order.market_id,
                    "client_order_id": order.client_order_id,
                    "reason": str(exc),
                },
            )
            return self._reject(state, "engine_rejected", now)

        self._apply(state, result.fills)
        self._reasons[order.client_order_id] = result.reason
        rests = (
            order.tif == "GTC"
            and result.remaining_size > _SIZE_EPSILON
            and result.reason not in _STRUCTURAL_REASONS
        )
        if rests:
            state.remaining_size = result.remaining_size
            state.status = "partially_filled" if state.filled_size > 0.0 else "open"
            self._open[state.order_id] = state
        else:
            state.remaining_size = 0.0
            if result.status == "filled":
                state.status = "filled"
            elif state.filled_size > 0.0:
                state.status = "partially_filled"
            else:
                state.status = "rejected"
        self._log(state, event="placed", reason=result.reason)
        return state.ack(now)

    async def cancel_order(self, order_id: str) -> None:
        """Cancel a resting simulated order.

        Args:
            order_id: The id carried on the order's `OrderAck`.

        Raises:
            KeyError: If no order with that id is resting. A venue also
                refuses to cancel an order it has already filled or
                cancelled, and silently succeeding here would let a
                caller believe it had removed exposure it still holds.
        """
        state = self._open.pop(order_id, None)
        if state is None:
            raise KeyError(f"no resting paper order {order_id!r} to cancel")
        state.remaining_size = 0.0
        state.status = "partially_filled" if state.filled_size > 0.0 else "cancelled"
        self._log(state, event="cancelled", reason=None)

    async def poll(self) -> list[OrderAck]:
        """Re-check every resting order against the current book.

        A real venue fills a resting order when someone else's
        aggression reaches it. This simulator has no counterparty model,
        so the honest approximation is to re-walk the book whenever it is
        asked: an order that becomes fillable because the book MOVED
        fills here, which is a genuine market event and not invented
        liquidity. An order that is still uncrossed simply stays resting.

        Returns:
            list[OrderAck]: An ack for every order whose state changed.
        """
        now = utcnow()
        changed: list[OrderAck] = []
        for order_id in list(self._open):
            state = self._open[order_id]
            market = self._markets.get(state.request.market_id)
            if market is None:
                market = await self.get_market(state.request.market_id)
            book = await self.get_book(state.request.market_id, state.request.outcome)
            residual = _resize(state.request, state.remaining_size)
            try:
                result = self._engine.fill(residual, book, now, market=market)
            except ValueError:
                continue
            if result.filled_size <= 0.0:
                continue
            self._apply(state, result.fills)
            state.remaining_size = result.remaining_size
            if state.remaining_size <= _SIZE_EPSILON:
                state.remaining_size = 0.0
                state.status = "filled"
                del self._open[order_id]
            else:
                state.status = "partially_filled"
            self._log(state, event="polled", reason=result.reason)
            changed.append(state.ack(now))
        return changed

    # -- Internals ------------------------------------------------------

    def _fee_schedule(self, venue: VenueId, market_id: str) -> FeeSchedule:
        """Return a market's OWN `FeeSchedule` for the fill engine.

        Raising when the market has not been cached is the point: the
        alternative — falling back to a default rate — is exactly how a
        market whose fee was never actually read ends up filling for
        free (GUARDRAILS.md §1.5, and the fill engine's own zero-rate
        provenance guard).

        Args:
            venue: The venue the engine is resolving for; must be this
                adapter's venue.
            market_id: Venue-native market identifier.

        Returns:
            FeeSchedule: The market's schedule, with its real `source`
                (`"clob_market"`, `"category_table"`, `"fee_waiver"`,
                `"settings"`), so a zero rate stays traceable to
                whatever declared it.

        Raises:
            ValueError: If `venue` is not this adapter's venue, or the
                market has not been fetched through this adapter.
        """
        if venue != self.venue:
            raise ValueError(
                f"fee schedule requested for venue {venue!r} from the "
                f"{self.venue!r} paper adapter"
            )
        market = self._markets.get(market_id)
        if market is None:
            raise ValueError(
                f"no cached VenueMarket for {market_id!r}; refusing to simulate "
                "a fill against an unknown fee schedule"
            )
        return market.fee

    def _held(self, market_id: str, outcome: str) -> float:
        """Return contracts currently held in one (market, outcome)."""
        pos = self._positions.get((market_id, outcome))
        return pos.size if pos is not None else 0.0

    def _reject(self, state: _PaperOrder, reason: str, now: datetime) -> OrderAck:
        """Mark an order rejected, log it, and return its ack."""
        state.status = "rejected"
        state.remaining_size = 0.0
        self._reasons[state.request.client_order_id] = reason
        self._log(state, event="rejected", reason=reason)
        return state.ack(now)

    def _apply(self, state: _PaperOrder, fills: tuple[Fill, ...]) -> None:
        """Record `fills` against an order, the fill tape, and positions.

        Args:
            state: The order the fills belong to.
            fills: Fills produced by the engine, best price first.
        """
        for raw in fills:
            metadata = dict(raw.metadata)
            metadata[CLIENT_ORDER_ID_KEY] = state.request.client_order_id
            metadata[SIMULATED_KEY] = True
            # `Fill.order_id` is the VENUE order id everywhere else in
            # the system, so the simulated venue's own id belongs there;
            # the client's key travels in metadata above. The engine
            # mirrors `client_order_id` into `order_id` (it has no venue
            # id to use), and leaving that in place would make paper the
            # only mode where the two fields mean the same thing.
            fill = Fill(
                venue=raw.venue,
                order_id=state.order_id,
                price=raw.price,
                size=raw.size,
                fee=raw.fee,
                ts=raw.ts,
                liquidity=raw.liquidity,
                metadata=metadata,
            )
            state.fills.append(fill)
            self._fills.append(fill)
            state.filled_size += fill.size
            state.notional += fill.price * fill.size
            state.fee += fill.fee
            self._book_position(state.request, fill)

    def _book_position(self, order: OrderRequest, fill: Fill) -> None:
        """Move one fill into the in-memory position map.

        A BUY raises the holding and re-weights the average entry price;
        a SELL lowers it and leaves the average alone — the cost basis of
        what REMAINS is not changed by selling part of it. Realized P&L
        is deliberately not tracked here: it belongs on the persisted
        `Position` row, which is `OrderRouter`'s business.
        """
        key = (order.market_id, order.outcome)
        pos = self._positions.get(key)
        if pos is None:
            pos = _PaperPosition(
                market_id=order.market_id,
                outcome=order.outcome,
                size=0.0,
                avg_price=0.0,
            )
            self._positions[key] = pos
        if order.side == "BUY":
            total = pos.size + fill.size
            if total > 0.0:
                pos.avg_price = (
                    pos.avg_price * pos.size + fill.price * fill.size
                ) / total
            pos.size = total
        else:
            pos.size = max(0.0, pos.size - fill.size)
            if pos.size <= _SIZE_EPSILON:
                pos.size = 0.0
                pos.avg_price = 0.0

    def _log(self, state: _PaperOrder, *, event: str, reason: str | None) -> None:
        """Emit one structured order line. Never carries a secret."""
        logger.info(
            "order",
            extra={
                "event": event,
                "venue": self.venue,
                "market_id": state.request.market_id,
                "outcome": state.request.outcome,
                "side": state.request.side,
                "tif": state.request.tif,
                "order_id": state.order_id,
                "client_order_id": state.request.client_order_id,
                "status": state.status,
                "price": state.request.price,
                "size": state.request.size,
                "filled_size": state.filled_size,
                "remaining_size": state.remaining_size,
                "fee": state.fee,
                "reason": reason,
                "simulated": True,
            },
        )


def _resize(order: OrderRequest, size: float) -> OrderRequest:
    """Return a copy of `order` sized to its unfilled residual.

    Used when re-checking a resting order: the engine simulates ONE
    request against ONE book, so the residual is expressed as a smaller
    request rather than by teaching the engine about resting state, which
    it explicitly does not model.

    Args:
        order: The original request.
        size: Contracts still outstanding.

    Returns:
        OrderRequest: The same order, sized to the residual.
    """
    return OrderRequest(
        venue=order.venue,
        market_id=order.market_id,
        outcome=order.outcome,
        side=order.side,
        price=order.price,
        size=size,
        tif=order.tif,
        client_order_id=order.client_order_id,
        post_only=order.post_only,
    )


#: Process-wide paper adapters, keyed by venue. See
#: `make_paper_adapter()` for why paper (unlike live) is cached.
_PAPER_ADAPTERS: dict[VenueId, PaperVenueAdapter] = {}


def make_paper_adapter(
    venue: VenueId,
    inner: MarketDataAdapter | None = None,
    ledger: CapitalLedger | None = None,
) -> PaperVenueAdapter:
    """Build — or return the cached — paper adapter for one venue.

    THIS RETURNS A PROCESS-WIDE SINGLETON when called with no arguments,
    a deliberate departure from `app/venues/registry.py`'s "never hands
    back a cached singleton" rule. That rule exists so a LIVE adapter's
    fence check (`assert_live_allowed`) runs at every construction;
    nothing here can place a real order, so it does not apply. What DOES
    apply is that a paper adapter's positions, resting orders and fill
    tape live only in memory: a fresh instance per `get_adapter()` call
    would forget every position between the order that opened it and the
    reconciliation pass meant to check it, so reconciliation would report
    every order as lost.

    Args:
        venue: `"polymarket"` or `"kalshi"`.
        inner: Read adapter to wrap. Defaults to the venue's real read
            adapter.
        ledger: Ledger to report balances from. Defaults to a fresh
            `CapitalLedger.paper()`.

    Returns:
        PaperVenueAdapter: The paper adapter for `venue`. Passing either
            `inner` or `ledger` bypasses the cache entirely and returns a
            fresh, unshared adapter — which is what tests want.
    """
    explicit = inner is not None or ledger is not None
    if not explicit and venue in _PAPER_ADAPTERS:
        return _PAPER_ADAPTERS[venue]
    adapter = PaperVenueAdapter(
        inner if inner is not None else _read_adapter(venue), None, ledger
    )
    if not explicit:
        _PAPER_ADAPTERS[venue] = adapter
    return adapter


def reset_paper_adapters() -> None:
    """Drop every cached paper adapter, discarding its simulated state.

    For an operator restarting a paper session, and for any test that
    touches `get_adapter(venue, "paper")` — a cached adapter carrying the
    previous test's positions is exactly the cross-test leak the cache
    would otherwise cause.
    """
    _PAPER_ADAPTERS.clear()


def _read_adapter(venue: VenueId) -> MarketDataAdapter:
    """Construct the real READ adapter for `venue`.

    Imported inside the function for the same import-cycle reason
    `app/venues/registry.py::_register` documents.

    Args:
        venue: `"polymarket"` or `"kalshi"`.

    Returns:
        MarketDataAdapter: A read-only adapter (no order placement).

    Raises:
        KeyError: If `venue` is not one of the two supported venues.
    """
    if venue == "polymarket":
        from app.venues.polymarket.adapter import PolymarketAdapter

        # `PolymarketAdapter.__init__` takes only `transport`, while
        # `KalshiAdapter.__init__` takes `(transport, settings_obj)` — an
        # asymmetry the Phase 1 audit flagged. Handled by constructing
        # each explicitly rather than through one shared factory
        # signature that would have to lie about one of them.
        return PolymarketAdapter()
    if venue == "kalshi":
        from app.venues.kalshi.adapter import KalshiAdapter

        return KalshiAdapter()
    raise KeyError(f"no read adapter for venue {venue!r}")
