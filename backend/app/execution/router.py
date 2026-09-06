"""`OrderRouter` — the ONE path from an `Intent` to venue orders (PLAN.md D4).

Paper and live share this object. The registry hands it a
`PaperVenueAdapter` in paper mode and a live adapter in live mode; nothing
else about the flow below changes, which is the entire point of D4: the
risk fences, the idempotency key, the capital reservations, the
crash-safe `PENDING` row, the partial-fill handling and the unwind are
all exercised in paper exactly as they will be with real money, or paper
proves nothing.

GUARDRAILS.md §1.1: nothing in this module places a real order. It calls
`adapter.place_order()` on whatever adapter it was given; only
`app/venues/polymarket/live.py` and `app/venues/kalshi/live.py` may
contain a venue's actual order-placement call.

THE UNWIND, AND WHY IT IS THE MOST DANGEROUS CODE HERE
-------------------------------------------------------
Prediction-market venues offer NO CROSS-VENUE ATOMICITY. You cannot
place a Kalshi leg and a Polymarket leg as one transaction, and you
cannot place two legs on ONE venue as one transaction either. So an
`atomicity="all_or_none"` intent that fills one leg and misses the other
does not leave you flat — it leaves you holding a NAKED DIRECTIONAL
POSITION, which is economically the opposite of the arbitrage the
strategy asked for: the whole thesis was that the two legs cancel.

`_unwind()` sells that leg back at market, and **that sale is a real,
realized loss**. You crossed the spread on the way in and you cross it
again on the way out, you pay a taker fee both times, and you earned
nothing in between. Three requirements follow, and each is implemented
below rather than assumed:

1. The unwind is sized and fee-modelled through the SAME fill engine as
   the entry (`adapter.place_order` -> `SimulatedFillEngine` in paper,
   the venue itself live). It is not assumed to complete, and it is not
   assumed to complete at the price the leg was bought at — the limit is
   derived from the CURRENT book's bid, and the fills come back at the
   book's own level prices.
2. A FAILED unwind (no bid at all, or a partial fill that leaves a
   residue) leaves a LOUD, PERSISTED record: `logger.error`, a
   `naked_legs` entry on `RoutedIntent`, and
   `IntentRecord.extra_data["unwind"]["naked_legs"]`. An un-unwound
   naked leg is the single worst state this system can reach and it must
   never be silent.
3. The realized unwind cost is recorded
   (`RoutedIntent.unwind_cost_usd`, and on the intent row). It is the
   true cost of ATTEMPTING cross-venue arbitrage — the number T22's
   edge-decay report needs in order to say whether the edge survives the
   attempts that failed, not only the ones that worked.

MASS ASSIGNMENT (Phase 0 security audit; `app/models/intent.py` carries
the full writeup). Every `IntentRecord`/`Order`/`Trade`/`Position` below
is constructed FIELD BY FIELD from a validated value — a frozen
`VenueMarket`/`OrderAck`/`Fill`, a `Leg` that normalized its own
outcome, or this router's own configuration. There is no
`Model(**something.model_dump())` and no splat of caller-controlled data
anywhere in this file. `mode` in particular comes from
`Settings.trading_mode` and never from an argument: it is the single
column separating a SIMULATED row from a REAL-MONEY one in these shared
tables, and every read path must filter on it.

Units (GUARDRAILS.md §4): prices are probabilities in `[0.0, 1.0]`,
sizes are contracts (each pays $1.00 at resolution), notionals/fees/cash
are USD.
"""
import asyncio
import logging
import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any, Literal, cast

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.config import settings as _default_settings
from app.execution.fences import (
    KNOWN_BUCKETS,
    KillSwitchEngaged,
    RiskLimitExceeded,
    assert_placement_allowed,
    check_order_limits,
)
from app.execution.ledger import (
    CapitalLedger,
    InsufficientCapital,
    LedgerError,
    ReservationId,
    UnknownReservation,
)
from app.models.intent import IntentRecord
from app.models.market import Market as MarketRow
from app.models.position import Position as PositionRow
from app.models.trade import Order as OrderRow
from app.models.trade import OrderSide as OrderSideEnum
from app.models.trade import OrderStatus, OrderType
from app.models.trade import Trade as TradeRow
from app.strategies.base import DOWNSIZE_TO_CAPITAL_KEY, Intent, Leg
from app.utils.time import utcnow
from app.venues.base import FeeModel, VenueAdapter
from app.venues.types import (
    Fill,
    OrderAck,
    OrderRequest,
    OrderSide,
    TimeInForce,
    VenueId,
    VenueMarket,
)

logger = logging.getLogger(__name__)

#: Extra USD held back on every BUY reservation, on top of
#: `notional + worst-case fee`. Not a fee rate (GUARDRAILS.md §1.5 —
#: every RATE comes from a `FeeSchedule`); it is slack for the one thing
#: a single `FeeModel.fee()` call cannot bound: fees are charged PER FILL
#: and a multi-level walk therefore costs strictly more than one
#: aggregate call on the same total size, by up to a rounding step per
#: level on Kalshi. $0.10 covers ~10 levels of that. Overspending past
#: the reservation is still handled (`CapitalLedger.settle` draws the
#: excess from the SAME venue and logs it); this just keeps the ordinary
#: case off that path.
_RESERVATION_FEE_HEADROOM_USD = 0.10

#: Decimal places a price is de-noised to before being snapped to the
#: tick grid. Same value and same reason as
#: `app.execution.fill_engine._TICK_DENOISE_PLACES`: a limit that arrived
#: via arithmetic (`0.40 + 0.01 == 0.41000000000000003`) must snap as the
#: `0.41` the caller meant.
_PRICE_DENOISE_PLACES = 12

#: Absolute tolerance (contracts) for size comparisons, matching
#: `OrderBook.walk()` and the fill engine so this module can never
#: disagree with them about whether an order is complete.
_SIZE_EPSILON = 1e-9

#: `Position.extra_data` key holding `{bucket: usd_notional}` — the USD
#: entry basis of the contracts in this position that were bought under
#: each risk bucket (T21e). Maintained by `_upsert_position` and read by
#: `_bucket_open_notional`; see both for why exposure cannot be
#: attributed through `Position.intent_id`. Its ABSENCE from a row's
#: `extra_data` means "written before this key existed", which is a
#: different thing from an empty mapping ("nothing in this position was
#: bought under any bucket") and is handled differently.
_BUCKET_NOTIONAL_KEY = "bucket_notional"

#: `Order.order_type` for each normalized `TimeInForce`.
#:
#: LOSSY, ON PURPOSE, AND DOCUMENTED: the `ordertype` enum shipped in
#: migration `004` has exactly three members (`GTC`, `GTD`, `FOK`) and no
#: `IOC`. Widening it needs an `ALTER TYPE` in a new migration, which
#: this task must not add (`alembic heads` stays `004`). Of the three
#: available members, `FOK` is the honest stand-in for `IOC`: both are
#: NON-RESTING (the venue kills whatever did not fill immediately),
#: whereas `GTC` would mark the row as a resting order that
#: reconciliation and open-notional accounting would then expect to find
#: on the venue. The AUTHORITATIVE time-in-force is written verbatim to
#: `Order.extra_data["tif"]`; a follow-up migration adding `IOC` to the
#: enum should backfill from there.
_ORDER_TYPE_BY_TIF: dict[TimeInForce, OrderType] = {
    "GTC": OrderType.GTC,
    "FOK": OrderType.FOK,
    "IOC": OrderType.FOK,
}

#: `Order.side` for each normalized venue-level side.
_ORDER_SIDE: dict[OrderSide, OrderSideEnum] = {
    "BUY": OrderSideEnum.BUY,
    "SELL": OrderSideEnum.SELL,
}

#: Paper-adapter/venue rejection reasons that are STRUCTURAL — the order
#: itself was wrong, so the local row is `FAILED` rather than `EXPIRED`.
#: The distinction matters to reconciliation and to any retry logic: an
#: `EXPIRED` order can sensibly be re-submitted with a fresh attempt
#: number, a `FAILED` one cannot.
_STRUCTURAL_REASONS = frozenset(
    {
        "crossed_book",
        "post_only_taker_engine",
        "zero_size_order",
        "engine_rejected",
        "insufficient_position",
        "market_unavailable",
    }
)

#: `RoutedLeg.status` values. `"replayed"` is distinct from every venue
#: status: it means this exact `client_order_id` had already been routed,
#: so the venue returned the ORIGINAL acknowledgement and this submission
#: booked nothing new (see `submit`'s idempotency handling).
LegStatus = Literal[
    "filled",
    "partially_filled",
    "open",
    "cancelled",
    "rejected",
    "failed",
    "replayed",
    "not_placed",
]

#: `RoutedIntent.status`, mirroring `app.models.intent.IntentRecordStatus`.
RoutedStatus = Literal["pending", "executed", "rejected", "expired"]


@dataclass(frozen=True)
class RoutedLeg:
    """What one leg of an `Intent` actually did.

    Attributes:
        index: The leg's position in `Intent.legs`; part of its
            `client_order_id`.
        venue: `"polymarket"` or `"kalshi"`.
        market_id: Venue-native market identifier.
        outcome: Canonical outcome name.
        side: `"BUY"` or `"SELL"`.
        client_order_id: `f"{intent_id}:{index}:{attempt}"` for an entry
            leg, `f"{intent_id}:{index}:u{attempt}"` for an unwind.
        limit_price: The limit actually sent, AFTER tick snapping.
        requested_size: Contracts asked for.
        filled_size: Contracts obtained.
        avg_price: Size-weighted average fill price, or `None`.
        fee: Fees paid on this leg, USD, `>= 0`. A positive COST, never
            netted into `avg_price`.
        status: See `LegStatus`.
        unwind: `True` if this leg is an unwind SELL rather than an entry.
        reason: Venue/engine reason the leg did not (fully) fill, when
            the adapter exposes one; `None` otherwise.
        order_row_id: Primary key of the persisted `orders` row, or
            `None` if no row was written (a pre-flight rejection).
        venue_order_id: The venue's OWN order id from the ack, or `None`
            if the leg never got one. Carried because a live venue's
            fills identify their order by THIS id and not by the client
            key — see `_fills_for`.
    """

    index: int
    venue: VenueId
    market_id: str
    outcome: str
    side: OrderSide
    client_order_id: str
    limit_price: float
    requested_size: float
    filled_size: float
    avg_price: float | None
    fee: float
    status: LegStatus
    unwind: bool = False
    reason: str | None = None
    order_row_id: int | None = None
    venue_order_id: str | None = None

    @property
    def notional(self) -> float:
        """Return `filled_size * avg_price` in USD, excluding fees."""
        return self.filled_size * (self.avg_price or 0.0)


@dataclass(frozen=True)
class NakedLeg:
    """A filled leg that could NOT be unwound — the worst state we reach.

    Every field here exists to make the exposure actionable by a human
    at 3am: which venue, which market, which side of it, how much, and
    what it cost to get into.

    Attributes:
        index: The entry leg's index in `Intent.legs`.
        venue: Venue the exposure sits on.
        market_id: Venue-native market identifier.
        outcome: Outcome held.
        size: Contracts left naked, `> 0`.
        avg_price: Average price they were bought at.
        reason: Why the unwind could not complete (`"no_bid"`,
            `"partial_unwind"`, a venue rejection reason, ...).
    """

    index: int
    venue: VenueId
    market_id: str
    outcome: str
    size: float
    avg_price: float
    reason: str

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable form for `extra_data` persistence."""
        return {
            "leg_index": self.index,
            "venue": self.venue,
            "market_id": self.market_id,
            "outcome": self.outcome,
            "size": self.size,
            "avg_price": self.avg_price,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class RoutedIntent:
    """The outcome of one `OrderRouter.submit()` call.

    Attributes:
        intent_id: The id every `client_order_id` here is derived from,
            and the primary key of the persisted `intents` row.
        strategy: Strategy that produced the intent.
        mode: `"paper"` or `"live"` — which world every persisted row
            belongs to.
        status: See `RoutedStatus`.
        atomicity: The intent's `"all_or_none"`/`"best_effort"` policy.
        legs: One `RoutedLeg` per entry leg, in `Intent.legs` order.
        unwinds: One `RoutedLeg` per unwind SELL attempted.
        naked_legs: Exposure that could not be unwound. NON-EMPTY IS AN
            ALARM, not a status.
        unwind_cost_usd: Realized cost of unwinding, USD — entry cost
            of the unwound contracts minus what selling them actually
            returned, fees on both sides included. Positive is a loss.
        reason: Why the intent was rejected before placement, if it was.
    """

    intent_id: str
    strategy: str
    mode: str
    status: RoutedStatus
    atomicity: str
    legs: tuple[RoutedLeg, ...] = ()
    unwinds: tuple[RoutedLeg, ...] = ()
    naked_legs: tuple[NakedLeg, ...] = ()
    unwind_cost_usd: float = 0.0
    reason: str | None = None

    @property
    def filled_size(self) -> float:
        """Return total contracts filled across the entry legs."""
        return math.fsum(leg.filled_size for leg in self.legs)

    @property
    def has_naked_exposure(self) -> bool:
        """Return `True` if any filled leg could not be unwound."""
        return bool(self.naked_legs)


class UnknownOrder(Exception):
    """No persisted `orders` row with the given id exists in THIS router's `mode`.

    Raised rather than falling through to some other "not found" path
    when a row DOES exist but under the other mode (paper vs live): `mode`
    is the only column separating a simulated row from a real-money one
    in this shared table (GUARDRAILS.md §1.2/§4), and a cancel request
    must never be allowed to reach across that boundary, not even to
    report a not-found in a way that would confirm the id exists at all.
    """


#: `OrderStatus` values `OrderRouter.cancel()` treats as already decided.
#: Cancelling one of these is a no-op, not an error: the order already
#: reached an end state, a venue would either reject a cancel on it or
#: the "cancel" would be meaningless, and claiming success would be a
#: lie about what actually happened to the position (GUARDRAILS.md: never
#: silently report an action that did not occur).
_TERMINAL_ORDER_STATUSES = frozenset(
    {
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.EXPIRED,
        OrderStatus.FAILED,
    }
)


@dataclass(frozen=True)
class CancelResult:
    """Outcome of one `OrderRouter.cancel()` call.

    Attributes:
        order_id: The persisted `orders.id` primary key that was targeted.
        status: The order's `OrderStatus` AFTER this call — unchanged
            from before the call whenever `cancelled` is `False`.
        cancelled: `True` only if a cancel request actually reached the
            venue (or the paper simulator) and the row was moved to
            `CANCELLED` as a result.
        reason: Why nothing was cancelled, when `cancelled` is `False`:
            `"already_terminal"` (the order had already filled, expired,
            failed, or was already cancelled — see `_TERMINAL_ORDER_
            STATUSES`) or `"no_venue_ack"` (the row is still `PENDING`:
            no venue order id exists yet for anything to cancel — this
            is what `app.execution.reconcile.reconcile` resolves, not a
            client-initiated cancel). `None` when `cancelled` is `True`.
    """

    order_id: int
    status: OrderStatus
    cancelled: bool
    reason: str | None = None


@dataclass
class _LegPlan:
    """A leg after sizing, tick snapping and market resolution."""

    index: int
    leg: Leg
    market: VenueMarket
    price: float
    size: float
    client_order_id: str
    token_id: str
    market_row_id: int = 0
    reservation: ReservationId | None = None
    order_row_id: int | None = None
    replayed: bool = False

    @property
    def notional(self) -> float:
        """Return this leg's USD notional at the snapped limit price."""
        return self.size * self.price


class OrderRouter:
    """Turns an `Intent` into venue orders, persistently and safely.

    One instance owns one `CapitalLedger` and one adapter per venue.

    CONCURRENT `submit()` CALLS ARE SERIALIZED WHERE IT MATTERS (T21e).
    This used to say "not concurrency-safe", as though that were a
    property of the caller's discipline — but `app/api/deps.py` caches
    ONE router for the life of the process and FastAPI serves two
    overlapping `POST /trading/orders` requests concurrently, so nothing
    downstream of that cache had any way to take one submit at a time.
    Two overlapping submits demonstrably (a) both read the SAME
    pre-trade aggregates and both passed a cap their sum breached, and
    (b) both read-modify-wrote the same `Position` row, so one whole
    900-contract fill vanished from the ledger while the venue kept it.

    `_submit_gate()` is an `asyncio.Lock` held across the two
    read-then-write critical sections that produced those two failures:

      1. risk check -> capital reservation -> COMMITTED `PENDING` rows.
         By the time the gate is released, this intent's full notional
         is visible to the next submit's `_risk_context` /
         `_bucket_open_notional` as resting-order exposure
         (`remaining_size * price` on a `PENDING` row), so the next
         submit measures itself against it instead of against a stale
         snapshot.
      2. `_settle`'s single session/commit, which is where a `PENDING`
         order becomes a FILLED order plus a folded `Position`. Holding
         the gate across that ONE transaction is what makes the
         hand-off atomic — exposure is counted as an order right up to
         the instant it starts being counted as a position, never in
         neither place and never in both — and it is what makes
         `_upsert_position`'s select-then-write incapable of losing an
         update to a concurrent one.

    Venue calls are deliberately OUTSIDE the gate: placement
    (`_place`), book/market reads (`_plan`) and fill reads (`_settle`
    hoists every `get_fills` above the gate). A slow venue therefore
    delays its own intent, not every other intent in the process.

    WHAT THIS DOES NOT PROTECT AGAINST, stated plainly because a fence
    that is trusted beyond its reach is worse than no fence: an
    `asyncio.Lock` is scoped to ONE event loop in ONE process. A second
    API worker, a Celery worker, or any second process sharing the same
    database can still interleave with this one — and, at that point,
    both failure modes above return. Closing that gap needs something
    the database enforces, and the portable option (this repo targets
    SQLite in tests and Postgres in production, so `SELECT FOR UPDATE`
    and advisory locks are out) is optimistic concurrency: a version
    column on `positions`, `UPDATE ... WHERE version = :seen`, and a
    retry. That is a schema change, so it is deliberately not made
    here. Until it exists, run ONE order-routing process.
    """

    def __init__(
        self,
        adapters: dict[VenueId, VenueAdapter],
        ledger: CapitalLedger,
        session_factory: async_sessionmaker[AsyncSession],
        fences: Settings | None = None,
    ) -> None:
        """Wire the router to its adapters, capital and persistence.

        Args:
            adapters: Venue -> adapter, already in the right mode (the
                registry's `get_adapter(venue, settings.trading_mode)`).
                A venue absent from this mapping cannot be routed to;
                an intent naming it is rejected rather than silently
                dropped.
            ledger: Per-venue capital. Never summed across venues
                (GUARDRAILS.md §1.6).
            session_factory: Async session factory. `submit()` opens TWO
                sessions: one that COMMITS the `PENDING` rows before any
                venue call (crash-safety — a row with no ack is what
                reconciliation looks for), and one for the results.
            fences: `Settings` whose risk limits `check_order_limits()`
                is evaluated against, and whose `trading_mode` becomes
                the `mode` column on every row written. Defaults to the
                process-wide singleton; tests pass an explicit
                `Settings(...)` (GUARDRAILS.md §1.2).
        """
        self._adapters = dict(adapters)
        self._ledger = ledger
        self._sessions = session_factory
        self._fences = fences if fences is not None else _default_settings
        self._mode = self._fences.trading_mode
        self._gate: asyncio.Lock | None = None
        self._gate_loop: asyncio.AbstractEventLoop | None = None

    def _submit_gate(self) -> asyncio.Lock:
        """Return the lock serializing this router's critical sections.

        Created LAZILY, and rebound if the running loop has changed,
        because `asyncio.Lock` binds to the first loop that awaits it
        and raises for good on any other — and a router built at import
        time (`app/api/deps.py` caches one process-wide) can outlive the
        loop it was built under. Rebinding is honest rather than clever:
        two loops running at once are two threads, which an
        `asyncio.Lock` could not have serialized in any case (see the
        class docstring on what this gate does NOT cover).

        Returns:
            asyncio.Lock: The gate, bound to the running event loop.
        """
        loop = asyncio.get_running_loop()
        if self._gate is None or self._gate_loop is not loop:
            self._gate = asyncio.Lock()
            self._gate_loop = loop
        return self._gate

    @property
    def ledger(self) -> CapitalLedger:
        """Return the per-venue capital ledger this router spends from."""
        return self._ledger

    @property
    def mode(self) -> str:
        """Return `"paper"` or `"live"` — the `mode` stamped on every row."""
        return self._mode

    async def submit(self, intent: Intent, strategy: str) -> RoutedIntent:
        """Route one `Intent`: check, reserve, persist, place, settle.

        The order of the steps is load-bearing:

        0. **The kill switch** (`app.execution.fences.
           assert_placement_allowed`, PLAN.md D13), evaluated on EVERY
           submission, in paper and in live. Nothing is planned,
           reserved, persisted or placed while it is engaged. It is
           checked here — not only in a live adapter's constructor —
           because `app/api/deps.py` caches this router and its adapters
           for the life of the process, so a construction-time check
           runs once and can never halt anything afterwards.
        1. **Risk limits**, per leg AND for the intent's total notional
           (`app.execution.fences.check_order_limits`, called in paper
           too). A breach rejects the intent and persists that decision;
           nothing is placed.
        2. **Capital reservation**, per venue per leg, from the
           `CapitalLedger`. A shortfall on ONE venue never draws on the
           other venue's balance (GUARDRAILS.md §1.6). What it does
           instead depends on the intent: one carrying
           `metadata["downsize_to_capital"]` is first SCALED DOWN, every
           leg together, to what the poorest venue can fund
           (`_downsize_to_capital` — PLAN.md D8's
           `min(available / ask)` bound, applied here against the live
           ledger); any other intent is rejected outright. Anything
           already reserved is released.
        3. **`PENDING` order rows, COMMITTED BEFORE the venue is
           called.** A crash between the commit and the ack leaves a row
           with no `order_id`, which is precisely what
           `app.execution.reconcile` is built to find. Writing the row
           afterwards would instead lose the order entirely.
        4. **Placement.** `all_or_none` places with `tif="IOC"` so
           nothing rests half-done; `best_effort` uses `"GTC"`. If an
           `all_or_none` leg comes back short, the legs that DID fill are
           unwound — see this module's docstring for why that is a
           realized loss and not a free undo.
        5. **Results**: a `Trade` row per fill, an upserted `Position`
           row per (venue, market, outcome), reservations settled at what
           was actually spent and released where nothing was.

        Args:
            intent: The multi-leg execution unit. `intent.metadata` may
                carry an `"intent_id"`; otherwise a UUID4 is minted.
                `Intent` itself has no id field (PLAN.md D7 does not give
                it one), and `Order.client_order_id` is `String(64)`, so
                an id longer than ~36 characters is rejected rather than
                silently truncated into a collision on the venue's
                idempotency key.
            strategy: Strategy name recorded on the intent row.

        Returns:
            RoutedIntent: What every leg did, what any unwind cost, and
                any naked exposure left behind.

        Raises:
            ValueError: If `intent.metadata["intent_id"]` is empty or
                long enough to push a `client_order_id` past its
                `String(64)` column — see `_intent_id`. This is raised
                rather than rejected-and-persisted because it is a
                caller bug, not a market or capital condition, and
                truncating the venue's idempotency key would silently
                collide two distinct orders.
        """
        now = utcnow()
        intent_id = _intent_id(intent)
        tif: TimeInForce = "IOC" if intent.atomicity == "all_or_none" else "GTC"

        # Step 0 -- the kill switch, checked HERE and on EVERY submission,
        # in paper as well as live. `app/api/deps.py` caches one router
        # and its adapters process-wide, so the construction-time check
        # inside `assert_live_allowed()` runs once, at the first order,
        # and an operator throwing the switch afterwards changed nothing
        # at all. See `app/execution/fences.py`.
        try:
            assert_placement_allowed(self._fences)
        except KillSwitchEngaged as exc:
            return await self._reject(
                intent,
                intent_id,
                strategy,
                _Rejected("kill_switch", str(exc)),
            )

        try:
            plans = await self._plan(intent, intent_id)
        except _Rejected as rejection:
            return await self._reject(intent, intent_id, strategy, rejection)

        # Steps 1-3 are ONE critical section (T21e). Reading the
        # aggregates, spending the ledger and committing the rows that
        # make this intent's exposure visible to the NEXT submit have to
        # be indivisible, or two concurrent submits both measure
        # themselves against the same pre-trade snapshot and both pass a
        # cap their sum breaches. `_plan` (above) and `_place` (below)
        # stay outside it: those are the venue calls, and a slow venue
        # must not be able to stall every other intent in the process.
        async with self._submit_gate():
            try:
                await self._check_limits(intent, plans)
            except _Rejected as rejection:
                return await self._reject(intent, intent_id, strategy, rejection)

            try:
                self._downsize_to_capital(intent, plans)
                self._reserve(plans)
            except _Rejected as rejection:
                self._release_all(plans)
                return await self._reject(intent, intent_id, strategy, rejection)

            # Step 3 -- crash-safety. Its own session, committed before any
            # venue call.
            async with self._sessions() as session:
                await self._persist_pending(
                    session, intent, intent_id, strategy, plans, tif
                )
                await session.commit()

        placed = await self._place(plans, tif)
        unwinds, naked, unwind_cost = await self._maybe_unwind(intent, plans, placed)

        return await self._settle(
            intent=intent,
            intent_id=intent_id,
            strategy=strategy,
            plans=plans,
            acks=placed,
            unwinds=unwinds,
            naked=naked,
            unwind_cost=unwind_cost,
            tif=tif,
            now=now,
        )

    async def cancel(self, order_id: int) -> CancelResult:
        """Cancel one resting order by its persisted `orders.id`.

        This is the ONLY place a venue's cancellation is reached from a
        request that originates outside the execution layer:
        `app/api/routes/trading.py`'s `DELETE /orders/{id}` calls THIS
        method, never `adapter.cancel_order` directly (`tests/
        test_fences.py` enforces this structurally — RULE 2 there
        confines `place_order`/`cancel_order` to `app/execution/` and
        `app/venues/`, exactly as `submit()` is the only path to
        placement, PLAN.md D4). Named `cancel`, not `cancel_order`, so a
        legitimate caller of THIS method is never textually
        indistinguishable, to that AST walk, from one reaching an
        adapter directly — see `tests/test_fences.py`'s module
        docstring for the full reasoning.

        Two states are treated as a NO-OP rather than an error, and
        both are reported honestly rather than claimed as a cancellation
        that did not happen:

        1. The order already reached a terminal `OrderStatus` (`FILLED`/
           `CANCELLED`/`EXPIRED`/`FAILED`) — nothing to cancel; the venue
           would either reject the attempt or the attempt would be
           meaningless.
        2. The order is still `PENDING` with no venue `order_id` yet
           (T14 crash-safety: the local row is committed BEFORE the
           venue call). There is nothing at the venue to cancel yet;
           `app.execution.reconcile.reconcile` is what resolves this
           state, not a client-initiated cancel.

        Args:
            order_id: Primary key of the persisted `orders` row.

        Returns:
            CancelResult: What happened.

        Raises:
            UnknownOrder: If no row with `order_id` exists in THIS
                router's `mode` — including a row that exists under the
                OTHER mode, which must never be reachable from here.
        """
        async with self._sessions() as session:
            row = await session.get(OrderRow, order_id)
            if row is None or row.mode != self._mode:
                raise UnknownOrder(
                    f"no {self._mode!r}-mode order with id {order_id!r}"
                )
            if row.status in _TERMINAL_ORDER_STATUSES:
                return CancelResult(
                    order_id=order_id,
                    status=row.status,
                    cancelled=False,
                    reason="already_terminal",
                )
            if row.order_id is None:
                return CancelResult(
                    order_id=order_id,
                    status=row.status,
                    cancelled=False,
                    reason="no_venue_ack",
                )
            adapter = self._adapters.get(cast(VenueId, row.venue))
            if adapter is None:
                raise UnknownOrder(
                    f"order {order_id!r} is on venue {row.venue!r}, which this "
                    "router has no adapter for"
                )
            try:
                await adapter.cancel_order(row.order_id)
            except Exception:
                logger.exception(
                    "order",
                    extra={
                        "event": "cancel_failed",
                        "mode": self._mode,
                        "order_id": order_id,
                        "venue": row.venue,
                        "client_order_id": row.client_order_id,
                    },
                )
                raise
            row.status = OrderStatus.CANCELLED
            row.remaining_size = 0.0
            # The order is off the venue, so the capital it was holding
            # is genuinely free again — see `_resolve_reservation`, which
            # KEEPS a reservation for as long as the venue is working the
            # order rather than releasing it the moment placement returns.
            self._release_recorded_reservation(row)
            await session.commit()
            logger.info(
                "order",
                extra={
                    "event": "order_cancelled",
                    "mode": self._mode,
                    "order_id": order_id,
                    "venue": row.venue,
                    "client_order_id": row.client_order_id,
                },
            )
            return CancelResult(
                order_id=order_id, status=OrderStatus.CANCELLED, cancelled=True
            )

    # -- Step 1: planning ------------------------------------------------

    async def _plan(self, intent: Intent, intent_id: str) -> list[_LegPlan]:
        """Resolve each leg's market, size it, and snap its limit to tick.

        Args:
            intent: The intent being routed.
            intent_id: The minted/validated intent id.

        Returns:
            list[_LegPlan]: One plan per leg, in order.

        Raises:
            _Rejected: If a venue is not routable, a market cannot be
                read, a leg carries no usable size, or a leg is below the
                market's `min_size`.
        """
        plans: list[_LegPlan] = []
        for index, leg in enumerate(intent.legs):
            adapter = self._adapters.get(leg.venue)
            if adapter is None:
                raise _Rejected(
                    "venue_not_routable",
                    f"no adapter for venue {leg.venue!r} (have "
                    f"{sorted(self._adapters)})",
                )
            try:
                market = await adapter.get_market(leg.market_id)
            except Exception as exc:  # noqa: BLE001 - re-raised as a rejection
                raise _Rejected(
                    "market_unavailable",
                    f"could not read market {leg.market_id!r} on {leg.venue}: {exc}",
                ) from exc

            size = _leg_size(leg)
            if size is None or size <= 0.0:
                raise _Rejected(
                    "unsized_leg",
                    f"leg {index} carries neither size_contracts nor a usable "
                    "size_usd/limit_price",
                )
            if size < market.min_size - _SIZE_EPSILON:
                raise _Rejected(
                    "below_min_size",
                    f"leg {index} size {size} is below {leg.venue} min_size "
                    f"{market.min_size} for {leg.market_id!r}",
                )
            price = snap_to_tick(leg.limit_price, market.tick_size, leg.side)
            plans.append(
                _LegPlan(
                    index=index,
                    leg=leg,
                    market=market,
                    price=price,
                    size=size,
                    client_order_id=f"{intent_id}:{index}:0",
                    token_id=token_id_for(market, leg.outcome),
                )
            )
        return plans

    # -- Step 1b: risk fences --------------------------------------------

    async def _check_limits(self, intent: Intent, plans: Sequence[_LegPlan]) -> None:
        """Apply `check_order_limits` per leg and to the intent total.

        Both are needed: a single $200 leg passes a $250 per-order cap,
        and three of them still must not slip past it as a $600 intent.

        THE BUCKET CAP (T20, PLAN.md D10(d)) is applied ONLY to the
        intent-total call, not per leg — `max_near_resolution_notional_usd`
        bounds AGGREGATE exposure in the bucket, not any one leg. It
        supersedes an earlier (T14) version of this method that guessed
        at "near-resolution" from `hours_to_resolution <=
        near_resolution_hours` and compared only THIS intent's own total
        against the raw cap — that neither aggregated across multiple
        submissions nor distinguished a settlement-edge capital-lockup
        trade from any other strategy that happened to fire on a
        near-expiry market. Now that
        `app.strategies.settlement_edge`/`app.services.scanner
        .near_resolution_pass` (T20) tag exactly the intents this cap is
        FOR (`intent.metadata["bucket"] == "near_resolution"`), the
        router reads that tag directly instead of re-deriving a proxy
        for it, and `_bucket_open_notional` sums what is ALREADY
        committed to the bucket (resting orders + open positions whose
        owning intent carries the same tag) so a second near-resolution
        intent is rejected once the first has already filled the cap,
        not only when one single intent alone exceeds it.

        Args:
            intent: The intent (for its `metadata["bucket"]` tag).
            plans: The planned legs.

        Raises:
            _Rejected: If any limit would be breached. Nothing has been
                reserved or placed at this point.
        """
        open_notional, daily_pnl = await self._risk_context()
        total = math.fsum(plan.notional for plan in plans)
        bucket = _bucket_tag(intent)
        # An unrecognized tag has no cap to be measured against, so
        # aggregating it would be work done for a fence that will never
        # read the answer. `check_order_limits` below is what makes the
        # typo LOUD (`app.execution.fences.warn_unknown_bucket`); it is
        # deliberately not made fatal here.
        bucket_notional = (
            await self._bucket_open_notional(bucket)
            if bucket is not None and bucket in KNOWN_BUCKETS
            else 0.0
        )
        try:
            for plan in plans:
                check_order_limits(
                    plan.notional, open_notional, daily_pnl, self._fences
                )
            check_order_limits(
                total,
                open_notional,
                daily_pnl,
                self._fences,
                bucket=bucket,
                bucket_notional=bucket_notional,
            )
        except RiskLimitExceeded as exc:
            raise _Rejected("risk_limit", str(exc)) from exc

    async def _risk_context(self) -> tuple[float, float]:
        """Return `(open_notional, daily_pnl)` for THIS mode, from the DB.

        Both queries filter on `mode` explicitly. Paper and live rows
        share these tables by D4's design, and a read that forgets the
        filter would size a live order against simulated exposure (or
        halt live trading over a paper loss).

        TODAY'S P&L COMES FROM A PER-DAY LEDGER, NOT A LIFETIME COLUMN.
        `PositionRow.realized_pnl` is CUMULATIVE over the position's
        whole life (`app/models/position.py`), so summing it over rows
        with `updated_at >= day_start` — which is what this did — answered
        a different question entirely: "the lifetime P&L of every
        position that happened to be touched today". A position that
        realized +50 last month and −5 today contributed +45, and the
        daily-loss fence saw a PROFIT on a losing day. The source now is
        `Position.extra_data["realized_by_day"]`, which
        `_upsert_position` credits with the delta of each realization at
        the moment it happens, keyed by that realization's own UTC date
        (see `_book_realized`). Only today's key is summed, so a
        historical gain on a row touched today contributes nothing.

        Returns:
            tuple[float, float]: USD notional already open (resting
                orders plus open positions), and realized P&L booked
                since the start of the current UTC day (negative is a
                loss).
        """
        day_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        today = day_start.strftime(_DAY_KEY_FORMAT)
        async with self._sessions() as session:
            resting = await session.execute(
                select(
                    func.coalesce(
                        func.sum(OrderRow.remaining_size * OrderRow.price), 0.0
                    )
                ).where(
                    OrderRow.mode == self._mode,
                    OrderRow.status.in_(
                        (
                            OrderStatus.PENDING,
                            OrderStatus.OPEN,
                            OrderStatus.PARTIALLY_FILLED,
                        )
                    ),
                )
            )
            held = await session.execute(
                select(
                    func.coalesce(
                        func.sum(PositionRow.size * PositionRow.avg_entry_price), 0.0
                    )
                ).where(
                    PositionRow.mode == self._mode,
                    PositionRow.closed_at.is_(None),
                )
            )
            realized_rows = (
                (
                    await session.execute(
                        select(PositionRow.extra_data).where(
                            PositionRow.mode == self._mode,
                            PositionRow.updated_at >= day_start,
                        )
                    )
                )
                .scalars()
                .all()
            )
        open_notional = float(resting.scalar_one() or 0.0) + float(
            held.scalar_one() or 0.0
        )
        daily_pnl = math.fsum(
            _today_realized(extra, today) for extra in realized_rows
        )
        return open_notional, daily_pnl

    async def _bucket_open_notional(self, bucket: str) -> float:
        """Return USD notional already committed to `bucket`, for THIS mode.

        T20, PLAN.md D10(d). Mirrors `_risk_context`'s two-source shape
        (resting orders + open positions) — a near-resolution cap bounds
        AGGREGATE exposure across every settlement-edge intent, not the
        account's whole open notional.

        THE TWO SOURCES ARE ATTRIBUTED DIFFERENTLY, BECAUSE THEY ARE
        DIFFERENT KINDS OF ROW. An `Order` belongs to exactly ONE intent
        for its whole life, so its bucket is still read off the owning
        `IntentRecord.extra_data["bucket"]` (`_persist_pending` copies
        `intent.metadata["bucket"]` there at submission time). A
        `Position` does NOT: it is keyed by `(venue, market, outcome)`
        and MANY intents fold into it, while `Position.intent_id` names
        only the one that OPENED it. Attributing the whole position
        through that column let a single untagged $0.50 buy on the
        identity hide every later near-resolution buy from this
        aggregate, permanently and silently (T21e). Positions are
        therefore summed from their OWN per-bucket ledger,
        `Position.extra_data["bucket_notional"]`, which
        `_upsert_position` maintains contribution by contribution.

        A position row with NO such key predates it. It is not read as
        "no bucket exposure" — the fallback is exactly the old
        opener-based attribution, which is the best (and the more
        conservative) thing that can be said about a row whose per-buy
        tags were never recorded.

        Neither `Order` nor `Position` carries a `bucket` COLUMN (no
        migration for this — both already have a free-form `extra_data`
        JSON column, and this task's scope is the router/fences, not a
        new schema). The matching is done in Python, not SQL, because
        JSON-field filtering is not portably expressible across this
        repo's two target databases (SQLite for tests, Postgres in prod)
        with plain SQLAlchemy Core — acceptable here since the
        near-resolution bucket is a small subset of an account's rows,
        unlike `_risk_context`'s account-wide aggregate.

        Args:
            bucket: The bucket tag to sum, e.g. `"near_resolution"`.

        Returns:
            float: `sum(remaining_size * price)` over resting orders
                (`PENDING`/`OPEN`/`PARTIALLY_FILLED`) whose `intent_id`
                resolves to an `IntentRecord` tagged with this `bucket`,
                PLUS, over open positions (`closed_at IS NULL`), each
                row's own `extra_data["bucket_notional"][bucket]` — or,
                for a row written before that key existed, its whole
                `size * avg_entry_price` when its OPENING intent carried
                the tag. `0.0` if nothing is tagged with it yet.
        """
        async with self._sessions() as session:
            resting_rows = (
                await session.execute(
                    select(
                        OrderRow.remaining_size, OrderRow.price, OrderRow.intent_id
                    ).where(
                        OrderRow.mode == self._mode,
                        OrderRow.status.in_(
                            (
                                OrderStatus.PENDING,
                                OrderStatus.OPEN,
                                OrderStatus.PARTIALLY_FILLED,
                            )
                        ),
                    )
                )
            ).all()
            held_rows = list(
                (
                    await session.execute(
                        select(PositionRow).where(
                            PositionRow.mode == self._mode,
                            PositionRow.closed_at.is_(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
            # Legacy positions (no per-bucket ledger) are the only ones
            # still attributed through their opening intent, so they are
            # the only ones whose `intent_id` needs resolving here.
            legacy = [row for row in held_rows if _position_buckets(row) is None]
            intent_ids: set[str] = set()
            for order_row in resting_rows:
                if order_row.intent_id is not None:
                    intent_ids.add(order_row.intent_id)
            for legacy_row in legacy:
                if legacy_row.intent_id is not None:
                    intent_ids.add(legacy_row.intent_id)
            bucket_by_intent: dict[str, object] = {}
            if intent_ids:
                intent_rows = (
                    await session.execute(
                        select(IntentRecord.id, IntentRecord.extra_data).where(
                            IntentRecord.id.in_(intent_ids)
                        )
                    )
                ).all()
                bucket_by_intent = {
                    row.id: (row.extra_data or {}).get("bucket")
                    for row in intent_rows
                }

        total = 0.0
        for resting in resting_rows:
            if (
                resting.intent_id is not None
                and bucket_by_intent.get(resting.intent_id) == bucket
            ):
                total += resting.remaining_size * resting.price
        for held in held_rows:
            buckets = _position_buckets(held)
            if buckets is None:
                if (
                    held.intent_id is not None
                    and bucket_by_intent.get(held.intent_id) == bucket
                ):
                    total += held.size * held.avg_entry_price
                continue
            total += buckets.get(bucket, 0.0)
        return total

    # -- Step 2: capital --------------------------------------------------

    def _downsize_to_capital(
        self, intent: Intent, plans: Sequence[_LegPlan]
    ) -> None:
        """Scale every leg down to what the POOREST venue can fund.

        PLAN.md D8 says a cross-venue intent's size is *bounded by*
        `min(available_A / ask_A, available_B / ask_B, max_contracts)`.
        Without this step the router turned that bound into a refusal:
        `_reserve` aborted on the first venue that came up short, so an
        account holding $5,000 on Polymarket and $50 on Kalshi placed
        NOTHING rather than the ~100-contract pair the $50 could actually
        support. That is not conservatism, it is a bug — the bound was
        computed and then discarded.

        WHY HERE AND NOT ONLY IN THE STRATEGY'S SIZING HOOK. The strategy
        (`app.strategies.cross_venue_arbitrage.calculate_position_size`)
        applies the same `min` at signal time, from whatever ledger
        snapshot it was handed. But that snapshot is a COPY, taken before
        risk checks, possibly several ticks ago, and possibly never handed
        over at all; this router owns the ONE authoritative
        `CapitalLedger`, and it is the last place the size can still be
        changed before capital is committed. Both applications are the
        same formula; this one is the one that actually governs what is
        placed.

        The scale is applied to EVERY leg, to the SAME contract count,
        because these intents are hedges: `cross_venue` and `complement`
        legs must stay equal in contracts or the "arbitrage" acquires a
        naked directional residual on whichever leg was left larger. And
        the result is floored to a WHOLE contract — Kalshi trades whole
        contracts, and flooring is the only rounding that cannot round
        back up into a second shortfall.

        Capital is still never pooled: each leg is measured against its
        OWN venue's `available`, and where several BUY legs sit on one
        venue that venue's balance is split evenly between them. The only
        cross-venue operation is `min`.

        Opt-in via `metadata["downsize_to_capital"]`
        (`app.strategies.base.DOWNSIZE_TO_CAPITAL_KEY` — see there for why
        a directional single-leg intent must NOT get this treatment).

        Args:
            intent: The intent being routed; its `metadata` opts in.
            plans: The planned legs, mutated in place when a downsize
                applies.

        Raises:
            _Rejected: If the poorest venue cannot fund even one whole
                contract, or if the downsized size falls below a leg's
                market `min_size`. Nothing has been reserved yet.
        """
        if not intent.metadata.get(DOWNSIZE_TO_CAPITAL_KEY):
            return

        buys = [plan for plan in plans if plan.leg.side == "BUY"]
        if not buys:
            return

        # A venue's free balance is read per venue and split between that
        # venue's own BUY legs. Never summed with another venue's
        # (GUARDRAILS.md §1.6): `available_by_venue()` is a mapping and is
        # consumed as one.
        available = self._ledger.available_by_venue()
        legs_per_venue: dict[VenueId, int] = {}
        for plan in buys:
            legs_per_venue[plan.leg.venue] = legs_per_venue.get(plan.leg.venue, 0) + 1

        target = min(plan.size for plan in buys)
        for plan in buys:
            budget = float(available.get(plan.leg.venue, 0.0)) / legs_per_venue[
                plan.leg.venue
            ]
            target = min(target, self._max_fundable_size(plan, budget))

        if target >= min(plan.size for plan in buys) - _SIZE_EPSILON:
            return

        if target <= 0.0:
            shortest = min(
                buys,
                key=lambda plan: float(available.get(plan.leg.venue, 0.0)),
            )
            raise _Rejected(
                "insufficient_capital",
                f"no whole contract is fundable: {shortest.leg.venue} holds "
                f"{float(available.get(shortest.leg.venue, 0.0)):.4f} USD free; "
                "capital is per venue and is never drawn from the other "
                "venue's balance",
            )

        for plan in plans:
            if plan.size <= target:
                continue
            if target < plan.market.min_size - _SIZE_EPSILON:
                raise _Rejected(
                    "insufficient_capital",
                    f"capital on the poorer venue funds only {target} contracts, "
                    f"below {plan.leg.venue} min_size {plan.market.min_size} for "
                    f"{plan.leg.market_id!r}",
                )
            logger.info(
                "order",
                extra={
                    "event": "downsized_to_capital",
                    "leg": plan.index,
                    "venue": plan.leg.venue,
                    "market_id": plan.leg.market_id,
                    "requested_size": plan.size,
                    "funded_size": target,
                    "venue_available": float(available.get(plan.leg.venue, 0.0)),
                },
            )
            plan.size = target

    def _max_fundable_size(self, plan: _LegPlan, budget: float) -> float:
        """Return the largest whole-contract size `budget` covers for `plan`.

        `_reservation_amount` is monotone non-decreasing in size but is
        NOT affine on every venue — Kalshi's fee is ceilinged to six
        decimal places and then to whole cents per fill — so there is no
        closed form to invert. This bisects on whole contracts instead,
        which is exact for any monotone cost function and needs ~20
        evaluations of a pure arithmetic call.

        Args:
            plan: The BUY leg being sized.
            budget: USD this leg may draw from its OWN venue.

        Returns:
            float: `plan.size` unchanged when it already fits, else the
                largest whole number of contracts whose reservation
                (notional + worst-case fee + headroom) fits in `budget`.
                `0.0` when not even one contract fits.
        """
        if self._reservation_amount(plan, plan.size) <= budget + _SIZE_EPSILON:
            return plan.size
        low, high = 0, int(math.floor(plan.size))
        while low < high:
            mid = (low + high + 1) // 2
            if self._reservation_amount(plan, float(mid)) <= budget + _SIZE_EPSILON:
                low = mid
            else:
                high = mid - 1
        return float(low)

    def _reserve(self, plans: Sequence[_LegPlan]) -> None:
        """Reserve capital per venue for every BUY leg.

        A SELL leg needs no capital — it returns cash — so it gets no
        reservation. Reservations are made in leg order and the FIRST
        shortfall aborts: the caller releases whatever was already taken,
        and nothing is placed. The shortfall is never covered from the
        other venue (GUARDRAILS.md §1.6): Kalshi dollars sit in an FCM
        account that settles in days, so a "combined" balance is not
        capital that could reach this order.

        `_downsize_to_capital` has already run by this point, so an
        intent that opted into downsizing has been scaled to a size the
        poorest venue CAN fund and reaches here fitting; a shortfall seen
        here is therefore a genuine one (a leg that did not opt in, or a
        balance that moved underneath us).

        Args:
            plans: The planned legs; each BUY plan's `reservation` is
                filled in.

        Raises:
            _Rejected: On the first venue that cannot cover its leg, and
                on ANY other ledger failure. `InsufficientCapital` is not
                the only thing `reserve()` can raise: a venue absent from
                the ledger raises `UnknownVenue`, which
                `PAPER_STARTING_BALANCES={"polymarket": 500}` used to make
                reachable from a LEGAL config. Catching only the
                capital-shortfall case let that escape `submit()`
                entirely — past the reservations already taken, with
                nothing persisted. Every `LedgerError` is a reason not to
                place this intent, so every one of them is a rejection.
        """
        for plan in plans:
            if plan.leg.side != "BUY":
                continue
            amount = self._reservation_amount(plan)
            try:
                plan.reservation = self._ledger.reserve(plan.leg.venue, amount)
            except InsufficientCapital as exc:
                raise _Rejected(
                    "insufficient_capital",
                    f"leg {plan.index} needs {amount:.4f} USD on "
                    f"{plan.leg.venue} but only {exc.available:.4f} is free; "
                    "capital is per venue and is never drawn from the other "
                    "venue's balance",
                ) from exc
            except LedgerError as exc:
                raise _Rejected(
                    "capital_unavailable",
                    f"leg {plan.index} could not reserve {amount:.4f} USD on "
                    f"{plan.leg.venue}: {type(exc).__name__}: {exc}",
                ) from exc

    def _reservation_amount(self, plan: _LegPlan, size: float | None = None) -> float:
        """Return the USD to hold for one BUY leg.

        `notional` is an upper bound on the cash part (a BUY never fills
        above its limit). The fee part is bounded by evaluating the
        venue's own `FeeModel` at `p = 0.5`, where `p * (1 - p)` — the
        shape both venues' formulas share — is maximal, so a leg limited
        at 0.90 that fills at 0.55 is still covered.

        Args:
            plan: The BUY leg being reserved for.
            size: Contracts to cover. Defaults to the whole leg; a
                partially-filled RESTING order passes its remaining size
                so the capital still committed at the venue stays locked
                without over-holding for the part that already filled.

        Returns:
            float: `notional + worst-case fee + headroom`, in USD.
        """
        adapter = self._adapters[plan.leg.venue]
        fee_model: FeeModel = adapter.fee_model()
        contracts = plan.size if size is None else max(0.0, size)
        worst_case_fee = fee_model.fee(0.5, contracts, "taker", plan.market.fee)
        return contracts * plan.price + worst_case_fee + _RESERVATION_FEE_HEADROOM_USD

    def _release_all(self, plans: Sequence[_LegPlan]) -> None:
        """Release every reservation held by `plans`, unspent."""
        for plan in plans:
            if plan.reservation is not None:
                self._ledger.release(plan.reservation)
                plan.reservation = None

    # -- Step 3: crash-safe PENDING rows ---------------------------------

    async def _persist_pending(
        self,
        session: AsyncSession,
        intent: Intent,
        intent_id: str,
        strategy: str,
        plans: Sequence[_LegPlan],
        tif: TimeInForce,
    ) -> None:
        """Write the intent row and one `PENDING` order row per leg.

        Every field is set explicitly from a validated value; `mode`
        comes from `Settings.trading_mode` and never from an argument
        (see this module's docstring on mass assignment).

        A UNIQUE violation on `client_order_id` is a LEGITIMATE outcome,
        not a crash: it means this exact order was already submitted
        (`app/models/trade.py` pins that contract, and T15's
        `test_duplicate_client_order_id_raises_integrity_error` proves
        the constraint fires). The row is then resolved to the EXISTING
        order and the leg is marked `replayed`, which suppresses a second
        `Trade` and returns the freshly-reserved capital — the original
        submission already spent the real capital.

        Args:
            session: Session to write in. The caller commits.
            intent: The intent being routed.
            intent_id: Its id.
            strategy: Strategy name.
            plans: The planned legs; `market_row_id`/`order_row_id`/
                `replayed` are filled in.
            tif: The time-in-force every leg will be placed with.
        """
        existing_intent = await session.get(IntentRecord, intent_id)
        if existing_intent is None:
            record = IntentRecord(
                id=intent_id,
                kind=intent.kind,
                strategy=strategy,
                mode=self._mode,
                status="pending",
                legs=[_leg_json(plan) for plan in plans],
                score=dict(intent.metadata.get("score") or {}),
                extra_data={
                    "atomicity": intent.atomicity,
                    "hold_to_resolution": intent.hold_to_resolution,
                    "confidence": intent.confidence,
                    "tif": tif,
                    # T20, PLAN.md D10(d): `_bucket_open_notional` reads
                    # this back off the OWNING intent (neither `Order`
                    # nor `Position` carries its own bucket column) to
                    # aggregate exposure across every intent tagged with
                    # the same bucket, e.g. `"near_resolution"`.
                    "bucket": intent.metadata.get("bucket"),
                },
            )
            session.add(record)
            await session.flush()

        for plan in plans:
            plan.market_row_id = await self._market_row_id(session, plan.market)
            order = OrderRow(
                market_id=plan.market_row_id,
                venue=plan.leg.venue,
                client_order_id=plan.client_order_id,
                intent_id=intent_id,
                token_id=plan.token_id,
                outcome=plan.leg.outcome,
                side=_ORDER_SIDE[plan.leg.side],
                order_type=_ORDER_TYPE_BY_TIF[tif],
                status=OrderStatus.PENDING,
                mode=self._mode,
                price=plan.price,
                size=plan.size,
                filled_size=0.0,
                remaining_size=plan.size,
                extra_data={"tif": tif, "attempt": 0},
            )
            try:
                async with session.begin_nested():
                    session.add(order)
                    await session.flush()
            except IntegrityError:
                existing = await session.scalar(
                    select(OrderRow).where(
                        OrderRow.client_order_id == plan.client_order_id
                    )
                )
                plan.replayed = True
                plan.order_row_id = existing.id if existing is not None else None
                self._log(
                    "idempotent_replay",
                    intent_id=intent_id,
                    plan=plan,
                    status="replayed",
                )
                continue
            plan.order_row_id = order.id
            self._log(
                "order_pending", intent_id=intent_id, plan=plan, status="PENDING"
            )

    async def _market_row_id(self, session: AsyncSession, market: VenueMarket) -> int:
        """Return the `markets` row id for a venue market, inserting once.

        `Order`/`Trade`/`Position` all reference `markets.id`, so an
        order cannot be persisted until the venue's market exists
        locally. Identity is the `(venue, condition_id)` composite T15
        introduced — the same venue-native id can legitimately appear on
        two venues.

        Args:
            session: Session to read/write in.
            market: The normalized venue market.

        Returns:
            int: The `markets` row id.
        """
        row_id = await session.scalar(
            select(MarketRow.id).where(
                MarketRow.venue == market.venue,
                MarketRow.condition_id == market.market_id,
            )
        )
        if row_id is not None:
            return int(row_id)
        row = MarketRow(
            venue=market.venue,
            condition_id=market.market_id,
            question=market.question,
            outcomes=list(market.outcomes),
            token_ids=dict(market.outcome_ids),
            is_active=market.status == "open",
            is_resolved=market.status == "resolved",
            resolution_outcome=market.result,
            end_date=market.close_time,
        )
        session.add(row)
        await session.flush()
        return int(row.id)

    # -- Step 4: placement ------------------------------------------------

    async def _place(
        self, plans: Sequence[_LegPlan], tif: TimeInForce
    ) -> dict[int, OrderAck | Exception]:
        """Place every planned leg, returning each leg's ack or failure.

        A venue error on one leg does NOT abort the loop: the other legs
        may already be filled, and their fills still have to be booked
        and (for `all_or_none`) unwound. The failure is carried through
        as the leg's result instead.

        Args:
            plans: The planned legs.
            tif: Time-in-force for every leg.

        Returns:
            dict[int, OrderAck | Exception]: Keyed by leg index.
        """
        results: dict[int, OrderAck | Exception] = {}
        for plan in plans:
            adapter = self._adapters[plan.leg.venue]
            request = OrderRequest(
                venue=plan.leg.venue,
                market_id=plan.leg.market_id,
                outcome=plan.leg.outcome,
                side=plan.leg.side,
                price=plan.price,
                size=plan.size,
                tif=tif,
                client_order_id=plan.client_order_id,
            )
            try:
                results[plan.index] = await adapter.place_order(request)
            except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
                logger.exception(
                    "order",
                    extra={
                        "event": "place_failed",
                        "venue": plan.leg.venue,
                        "market_id": plan.leg.market_id,
                        "outcome": plan.leg.outcome,
                        "client_order_id": plan.client_order_id,
                        "mode": self._mode,
                    },
                )
                results[plan.index] = exc
        return results

    # -- Step 4b: the unwind ----------------------------------------------

    async def _maybe_unwind(
        self,
        intent: Intent,
        plans: Sequence[_LegPlan],
        acks: dict[int, OrderAck | Exception],
    ) -> tuple[list[RoutedLeg], list[NakedLeg], float]:
        """Unwind the legs that filled, if an `all_or_none` leg came short.

        See this module's docstring: there is no cross-venue (or even
        cross-order) atomicity on these venues, so a half-filled
        `all_or_none` intent is a naked directional bet, and selling out
        of it is a REALIZED loss — two spread crossings and two taker
        fees for nothing. It is attempted anyway because holding the
        naked leg is worse, but it is never assumed to succeed.

        Args:
            intent: The intent, for its `atomicity` policy.
            plans: The planned legs.
            acks: Placement results by leg index.

        Returns:
            tuple: `(unwind legs, naked legs, realized unwind cost USD)`.
        """
        if intent.atomicity != "all_or_none":
            return [], [], 0.0
        tolerance = self._fences.all_or_none_fill_tolerance
        fresh = [plan for plan in plans if not plan.replayed]
        if not fresh:
            # Nothing new was placed (every leg was an idempotent
            # replay), so there is nothing new to unwind — the original
            # submission already resolved this intent one way or another.
            return [], [], 0.0
        short = any(
            _filled(acks.get(plan.index)) < plan.size * tolerance - _SIZE_EPSILON
            for plan in fresh
        )
        if not short:
            return [], [], 0.0

        logger.warning(
            "order",
            extra={
                "event": "atomicity_breach",
                "mode": self._mode,
                "atomicity": intent.atomicity,
                "reason": "a leg filled below tolerance; unwinding the legs that "
                "did fill. Prediction-market venues offer no cross-venue "
                "atomicity, so the alternative is holding a naked directional "
                "position",
            },
        )

        unwinds: list[RoutedLeg] = []
        naked: list[NakedLeg] = []
        cost = 0.0
        for plan in fresh:
            ack = acks.get(plan.index)
            filled = _filled(ack)
            if filled <= _SIZE_EPSILON:
                continue
            if plan.leg.side != "BUY":
                # Only a long can be sold back; neither venue supports
                # naked shorts (PLAN.md §3), so a SELL leg has no unwind
                # — but it is NOT nothing. This leg reduced a position
                # the strategy wanted to keep, as one half of a trade
                # whose other half never happened, and the reversal
                # (buying it back) is exactly the naked-short this venue
                # does not offer. Silently skipping it left the single
                # worst state in the system unrecorded; it is recorded
                # here instead.
                naked.append(
                    await self._naked(
                        plan,
                        filled,
                        _avg_price(ack) or plan.price,
                        "sell_leg_not_unwindable",
                    )
                )
                continue
            entry_price = _avg_price(ack) or plan.price
            # The ENTRY fee is part of what the unwound contracts cost.
            # Leaving it out would understate the realized cost of the
            # reversal by exactly one taker fee, which is half the point
            # of measuring it.
            entry_fee = await self._fee_for(
                plan.leg.venue,
                plan.client_order_id,
                ack if isinstance(ack, OrderAck) else None,
            )
            leg_unwind, leg_naked, leg_cost = await self._unwind_leg(
                plan, filled, entry_price, entry_fee
            )
            if leg_unwind is not None:
                unwinds.append(leg_unwind)
            if leg_naked is not None:
                naked.append(leg_naked)
            cost += leg_cost
        return unwinds, naked, cost

    async def _unwind_leg(
        self,
        plan: _LegPlan,
        size: float,
        entry_price: float,
        entry_fee: float,
    ) -> tuple[RoutedLeg | None, NakedLeg | None, float]:
        """Sell one filled leg back at market, best effort.

        THE LIMIT IS NOT ZERO. A SELL limit of `0.0` accepts any price
        and would walk an arbitrarily deep book — in a simulation that
        fabricates an exit that never existed, and live it is an
        unbounded market order. Instead the limit is placed
        `unwind_slippage_ticks` BELOW the current best bid: real, but
        bounded. Fills still occur at the book's own level prices, so
        this bounds how far down the book the unwind is willing to
        reach, not what it pays.

        NO BID AT ALL means there is nothing to sell into. That is not
        an error to swallow: it is the naked-leg state, and it is logged
        at ERROR and returned as a `NakedLeg`.

        Args:
            plan: The entry leg being reversed.
            size: Contracts to sell back.
            entry_price: Average price they were bought at.
            entry_fee: Fee paid entering, USD (pro-rated by the caller).

        Returns:
            tuple: `(the unwind leg or None, naked exposure or None,
                realized unwind cost USD)`.
        """
        adapter = self._adapters[plan.leg.venue]
        venue = plan.leg.venue
        try:
            book = await adapter.get_book(plan.leg.market_id, plan.leg.outcome)
        except Exception as exc:  # noqa: BLE001 - becomes a naked leg
            unreadable = await self._naked(
                plan, size, entry_price, f"book_error:{exc}"
            )
            return None, unreadable, 0.0

        best_bid = book.best_bid()
        if best_bid is None:
            return None, await self._naked(plan, size, entry_price, "no_bid"), 0.0

        tick = plan.market.tick_size
        floor_price = max(
            0.0, best_bid.price - self._fences.unwind_slippage_ticks * tick
        )
        limit = snap_to_tick(floor_price, tick, "SELL")
        request = OrderRequest(
            venue=venue,
            market_id=plan.leg.market_id,
            outcome=plan.leg.outcome,
            side="SELL",
            price=limit,
            size=size,
            tif="IOC",
            # `f"{intent_id}:{index}:u{attempt}"` -- the `u` keeps an
            # unwind's idempotency key distinct from the entry leg's
            # (`f"{intent_id}:{index}:{attempt}"`) while staying inside
            # `client_order_id`'s String(64) budget.
            client_order_id=f"{_intent_of(plan.client_order_id)}:{plan.index}:u0",
        )
        ack: OrderAck | None
        reason: str | None
        try:
            ack = await adapter.place_order(request)
        except Exception as exc:  # noqa: BLE001 - becomes a naked leg
            ack = None
            reason = f"place_failed:{exc}"
        else:
            reason = _reason_for(adapter, request.client_order_id)

        sold = _filled(ack)
        sold_price = _avg_price(ack)
        proceeds = sold * (sold_price or 0.0)
        fee = await self._fee_for(venue, request.client_order_id, ack)
        if sold > 0.0:
            self._credit(venue, max(0.0, proceeds - fee))

        # Realized cost of the reversal: what the unwound contracts cost
        # to acquire, minus what selling them actually returned. Positive
        # is a loss, and it is the honest price of attempting an
        # arbitrage that only half-executed.
        entry_cost = sold * entry_price + entry_fee * (
            sold / size if size > 0.0 else 0.0
        )
        realized = entry_cost - (proceeds - fee)

        residual = size - sold
        naked: NakedLeg | None = None
        if residual > _SIZE_EPSILON:
            naked = await self._naked(
                plan,
                residual,
                entry_price,
                "partial_unwind" if sold > 0.0 else (reason or "unfilled"),
            )

        unwind_leg = RoutedLeg(
            index=plan.index,
            venue=venue,
            market_id=plan.leg.market_id,
            outcome=plan.leg.outcome,
            side="SELL",
            client_order_id=request.client_order_id,
            limit_price=limit,
            requested_size=size,
            filled_size=sold,
            avg_price=sold_price,
            fee=fee,
            status=_leg_status(ack, reason),
            unwind=True,
            reason=reason,
            venue_order_id=_venue_order_id(ack),
        )
        self._log(
            "unwound",
            intent_id=_intent_of(request.client_order_id),
            plan=plan,
            status=unwind_leg.status,
            extra={
                "unwind": True,
                "sold": sold,
                "residual": residual,
                "realized_unwind_cost_usd": realized,
            },
        )
        return unwind_leg, naked, realized

    async def _naked(
        self, plan: _LegPlan, size: float, avg_price: float, reason: str
    ) -> NakedLeg:
        """Build, LOUDLY log, and IMMEDIATELY PERSIST a naked-exposure record.

        This is the worst state the system reaches: a directional
        position the strategy never wanted, left open because the exit
        did not complete. It is logged at ERROR (not WARNING) and
        persisted on the intent row, because an un-unwound naked leg
        that is only visible in a log line nobody reads is functionally
        invisible.

        PERSISTED HERE, NOT ONLY IN `_settle`. The record used to be
        written exclusively by `_finalize_intent`, inside `_settle`'s
        single commit — after placement and after the unwind. Anything
        raising in between (a ledger error, a database error, a venue
        adapter throwing on the way back) lost the record permanently
        while the fills stayed real at the venue. Writing it in its OWN
        transaction, at the moment the exposure is created, makes it
        durable independently of everything that happens afterwards;
        `_finalize_intent` still rewrites the complete list when it is
        reached, so nothing is duplicated.
        """
        logger.error(
            "order",
            extra={
                "event": "unwind_failed",
                "mode": self._mode,
                "venue": plan.leg.venue,
                "market_id": plan.leg.market_id,
                "outcome": plan.leg.outcome,
                "client_order_id": plan.client_order_id,
                "size": size,
                "price": avg_price,
                "reason": reason,
                "naked_exposure": True,
            },
        )
        naked = NakedLeg(
            index=plan.index,
            venue=plan.leg.venue,
            market_id=plan.leg.market_id,
            outcome=plan.leg.outcome,
            size=size,
            avg_price=avg_price,
            reason=reason,
        )
        await self._persist_naked(plan, naked)
        return naked

    async def _persist_naked(self, plan: _LegPlan, naked: NakedLeg) -> None:
        """Commit one naked-leg record to the intent row, on its own.

        Its own session and its own commit, so this survives a failure
        anywhere in the rest of `submit()`. A failure to persist is
        logged and swallowed rather than raised: the caller is already
        mid-unwind on real fills, and turning a bookkeeping failure into
        an exception there would abandon the rest of the intent
        un-booked — strictly worse than an alarm that reached the log but
        not the database.

        Args:
            plan: The entry leg the exposure came from.
            naked: The exposure to record.
        """
        intent_id = _intent_of(plan.client_order_id)
        try:
            async with self._sessions() as session:
                record = await session.get(IntentRecord, intent_id)
                if record is None:
                    return
                extra = dict(record.extra_data or {})
                raw_unwind = extra.get("unwind")
                unwind = dict(raw_unwind) if isinstance(raw_unwind, dict) else {}
                legs = list(unwind.get("naked_legs") or [])
                legs.append(naked.as_dict())
                unwind["naked_legs"] = legs
                extra["unwind"] = unwind
                extra["naked_exposure"] = True
                record.extra_data = extra
                await session.commit()
        except Exception:  # noqa: BLE001 - never abandon a live unwind for this
            logger.exception(
                "order",
                extra={
                    "event": "naked_leg_persist_failed",
                    "mode": self._mode,
                    "intent_id": intent_id,
                    "venue": naked.venue,
                    "market_id": naked.market_id,
                    "outcome": naked.outcome,
                    "size": naked.size,
                    "reason": naked.reason,
                    "naked_exposure": True,
                },
            )

    # -- Step 5: results --------------------------------------------------

    async def _settle(
        self,
        *,
        intent: Intent,
        intent_id: str,
        strategy: str,
        plans: Sequence[_LegPlan],
        acks: dict[int, OrderAck | Exception],
        unwinds: list[RoutedLeg],
        naked: list[NakedLeg],
        unwind_cost: float,
        tif: TimeInForce,
        now: datetime,
    ) -> RoutedIntent:
        """Persist fills/positions, resolve reservations, build the result.

        Args:
            intent: The routed intent.
            intent_id: Its id.
            strategy: Strategy name.
            plans: The planned legs.
            acks: Placement results by leg index.
            unwinds: Unwind legs already placed (their fills are booked
                here, from the venue's own fill records).
            naked: Exposure that could not be unwound.
            unwind_cost: Realized USD cost of the unwind.
            tif: Time-in-force used.
            now: Aware UTC submission time.

        Returns:
            RoutedIntent: The complete outcome.
        """
        # EVERY venue read happens HERE, before the gate is taken (T21e).
        # `get_fills` is a network round trip in live mode, and the gate
        # below serializes every submit in the process — so a venue that
        # is slow to answer must delay this intent's booking only, not
        # everyone else's risk check. Nothing here writes.
        entry_fills: dict[int, list[Fill]] = {
            plan.index: await self._fills_for(
                plan.leg.venue,
                plan.client_order_id,
                now,
                _venue_order_id(acks.get(plan.index)),
            )
            for plan in plans
        }
        unwind_fills: dict[int, list[Fill]] = {
            unwind.index: await self._fills_for(
                unwind.venue,
                unwind.client_order_id,
                now - _FEE_LOOKBACK,
                unwind.venue_order_id,
            )
            for unwind in unwinds
        }

        legs: list[RoutedLeg] = []
        # ONE gated transaction: the `PENDING` -> `FILLED` + `Position`
        # hand-off is where a concurrent submit could otherwise read an
        # intent's exposure as neither an order nor a position, and where
        # `_upsert_position`'s select-then-write could otherwise lose a
        # whole fill to a concurrent one. See the class docstring for
        # what this gate does and does not cover.
        async with self._submit_gate(), self._sessions() as session:
            for plan in plans:
                ack = acks.get(plan.index)
                fills = entry_fills[plan.index]
                fee = math.fsum(f.fee for f in fills)
                reason = _reason_for(
                    self._adapters[plan.leg.venue], plan.client_order_id
                )
                status: LegStatus = (
                    "replayed" if plan.replayed else _leg_status(ack, reason)
                )
                await self._update_order_row(session, plan, ack, reason, status, now)
                if not plan.replayed:
                    await self._book_fills(session, plan, fills, intent, intent_id, now)
                legs.append(
                    RoutedLeg(
                        index=plan.index,
                        venue=plan.leg.venue,
                        market_id=plan.leg.market_id,
                        outcome=plan.leg.outcome,
                        side=plan.leg.side,
                        client_order_id=plan.client_order_id,
                        limit_price=plan.price,
                        requested_size=plan.size,
                        filled_size=_filled(ack),
                        avg_price=_avg_price(ack),
                        fee=fee,
                        status=status,
                        reason=reason,
                        order_row_id=plan.order_row_id,
                        venue_order_id=_venue_order_id(ack),
                    )
                )
                held = self._resolve_reservation(plan, ack, fills)
                await self._record_reservation(session, plan, held)

            for unwind in unwinds:
                entry_plan = next(p for p in plans if p.index == unwind.index)
                await self._book_unwind(
                    session,
                    entry_plan,
                    unwind,
                    unwind_fills[unwind.index],
                    intent_id,
                    now,
                )

            status_final = _intent_status(legs)
            await self._finalize_intent(
                session,
                intent_id=intent_id,
                status=status_final,
                unwinds=unwinds,
                naked=naked,
                unwind_cost=unwind_cost,
            )
            await session.commit()

        routed = RoutedIntent(
            intent_id=intent_id,
            strategy=strategy,
            mode=self._mode,
            status=status_final,
            atomicity=intent.atomicity,
            legs=tuple(legs),
            unwinds=tuple(unwinds),
            naked_legs=tuple(naked),
            unwind_cost_usd=unwind_cost,
        )
        self._log_intent(routed, tif)
        return routed

    async def _fills_for(
        self,
        venue: VenueId,
        client_order_id: str,
        since: datetime,
        venue_order_id: str | None = None,
    ) -> list[Fill]:
        """Return the venue's fills belonging to one order.

        Read back from `adapter.get_fills()` rather than from the ack, so
        the paper and live paths book fills the same way: the ack carries
        only aggregates (`filled_size`, `avg_fill_price`), while a
        `Trade` row needs each fill's own price and fee — and on Kalshi
        the fee is charged PER FILL, so an aggregate would understate it.

        THE MATCHING RULE, AND WHY IT HAS THREE ARMS. See
        `_fill_matches`. Matching on the client key alone worked in paper
        and could not work live: `PaperVenueAdapter` stamps
        `metadata["client_order_id"]` on every simulated fill, while
        `polymarket/adapter.py` emits `{"market", "asset_id"}`,
        `kalshi/adapter.py` emits `{"market_id", "outcome",
        "fee_source"}`, and in BOTH the `Fill.order_id` is the VENUE's
        order id. So live, every real fill missed, every order booked no
        `Trade`, no `Position` and a zero fee, and its whole capital
        reservation was released as though nothing had been spent — on a
        FULLY FILLED order.

        Args:
            venue: The venue to read from.
            client_order_id: The order's idempotency key.
            since: Aware UTC lower bound.
            venue_order_id: The venue's own order id, from the ack. This
                is the arm that makes the live path work; it is `None`
                only when placement never returned an ack, in which case
                there are no fills to find anyway.

        Returns:
            list[Fill]: Matching fills, oldest first.
        """
        adapter = self._adapters[venue]
        try:
            fills = await adapter.get_fills(since)
        except Exception:  # noqa: BLE001 - absence of fills is handled below
            logger.exception(
                "order",
                extra={
                    "event": "get_fills_failed",
                    "venue": venue,
                    "client_order_id": client_order_id,
                    "mode": self._mode,
                },
            )
            return []
        return [
            fill
            for fill in fills
            if _fill_matches(fill, client_order_id, venue_order_id)
        ]

    async def _fee_for(
        self, venue: VenueId, client_order_id: str, ack: OrderAck | None
    ) -> float:
        """Return total fees on one order, summed from its fills."""
        if ack is None or ack.filled_size <= 0.0:
            return 0.0
        fills = await self._fills_for(
            venue, client_order_id, ack.ts - _FEE_LOOKBACK, ack.order_id
        )
        return math.fsum(f.fee for f in fills)

    async def _update_order_row(
        self,
        session: AsyncSession,
        plan: _LegPlan,
        ack: OrderAck | Exception | None,
        reason: str | None,
        status: LegStatus,
        now: datetime,
    ) -> None:
        """Move the leg's `PENDING` row to its post-placement state."""
        if plan.order_row_id is None:
            return
        row = await session.get(OrderRow, plan.order_row_id)
        if row is None:
            return
        if plan.replayed:
            # The original submission owns this row; a replay must not
            # overwrite the outcome it already recorded.
            return
        if isinstance(ack, Exception):
            row.status = OrderStatus.FAILED
            row.error_message = f"{type(ack).__name__}: {ack}"
            row.remaining_size = 0.0
        elif ack is None:
            row.status = OrderStatus.FAILED
            row.error_message = "no acknowledgement"
            row.remaining_size = 0.0
        else:
            row.order_id = ack.order_id
            row.filled_size = ack.filled_size
            row.remaining_size = ack.remaining_size
            row.status = _order_status(ack, reason)
            if ack.filled_size > 0.0:
                row.filled_at = now
            if row.status in (OrderStatus.FAILED, OrderStatus.EXPIRED) and reason:
                row.error_message = reason
        self._log(
            "order_updated",
            intent_id=_intent_of(plan.client_order_id),
            plan=plan,
            status=status,
        )

    async def _book_fills(
        self,
        session: AsyncSession,
        plan: _LegPlan,
        fills: Sequence[Fill],
        intent: Intent,
        intent_id: str,
        now: datetime,
    ) -> None:
        """Write one `Trade` per fill and upsert the `Position`."""
        for ordinal, fill in enumerate(fills):
            await self._trade_row(
                session,
                plan=plan,
                fill=fill,
                trade_id=f"{plan.client_order_id}#{ordinal}",
                side=plan.leg.side,
                unwind=False,
            )
        if fills:
            await self._upsert_position(
                session,
                plan=plan,
                fills=fills,
                side=plan.leg.side,
                intent_id=intent_id,
                hold_to_resolution=intent.hold_to_resolution,
                now=now,
                # The tag of the intent that actually BOUGHT these
                # contracts, not of whichever intent opened the row
                # (T21e) — see `_upsert_position`.
                bucket=_bucket_tag(intent),
            )

    async def _book_unwind(
        self,
        session: AsyncSession,
        plan: _LegPlan,
        unwind: RoutedLeg,
        fills: Sequence[Fill],
        intent_id: str,
        now: datetime,
    ) -> None:
        """Write the unwind's `Trade` rows and reduce the `Position`.

        Args:
            session: Session to write in; the caller commits.
            plan: The ENTRY leg being unwound (the position identity and
                market row come from it).
            unwind: The unwind leg as placed.
            fills: The venue's fills for the unwind order. Read by
                `_settle` BEFORE it takes the submit gate — see the
                class docstring: no venue call happens under the gate.
            intent_id: The intent this unwind belongs to.
            now: Aware UTC booking time.
        """
        for ordinal, fill in enumerate(fills):
            await self._trade_row(
                session,
                plan=plan,
                fill=fill,
                trade_id=f"{unwind.client_order_id}#{ordinal}",
                side="SELL",
                unwind=True,
            )
        if fills:
            await self._upsert_position(
                session,
                plan=plan,
                fills=fills,
                side="SELL",
                intent_id=intent_id,
                hold_to_resolution=False,
                now=now,
            )

    async def _trade_row(
        self,
        session: AsyncSession,
        *,
        plan: _LegPlan,
        fill: Fill,
        trade_id: str,
        side: OrderSide,
        unwind: bool,
    ) -> None:
        """Insert one `Trade`, skipping a `trade_id` already recorded.

        The id is deterministic (`{client_order_id}#{ordinal}`), so an
        idempotent replay or a second reconciliation pass over the same
        fills cannot double-book them.
        """
        exists = await session.scalar(
            select(TradeRow.id).where(TradeRow.trade_id == trade_id)
        )
        if exists is not None:
            return
        trade = TradeRow(
            trade_id=trade_id,
            order_id=plan.order_row_id,
            market_id=plan.market_row_id,
            venue=plan.leg.venue,
            token_id=plan.token_id,
            outcome=plan.leg.outcome,
            side=_ORDER_SIDE[side],
            price=fill.price,
            size=fill.size,
            fee=fill.fee,
            liquidity=fill.liquidity,
            mode=self._mode,
            executed_at=fill.ts,
            extra_data={
                "unwind": unwind,
                "depth_source": fill.metadata.get("depth_source"),
                "fee_source": fill.metadata.get("fee_source"),
            },
        )
        session.add(trade)
        await session.flush()

    async def _upsert_position(
        self,
        session: AsyncSession,
        *,
        plan: _LegPlan,
        fills: Sequence[Fill],
        side: OrderSide,
        intent_id: str,
        hold_to_resolution: bool,
        now: datetime,
        bucket: str | None = None,
    ) -> None:
        """Fold fills into the open `Position` for this identity.

        Position identity is `(venue, market, outcome)` in THIS mode
        (PLAN.md D7's `f"{venue}:{market_id}:{outcome}"`, plus the
        paper/live separation D4 requires) — not `(intent, ...)`: two
        intents that buy the same outcome hold ONE position, exactly as
        the venue does. `intent_id` records which intent opened it, and
        keeps recording exactly that: it is NEVER rewritten by a later
        fold (see `bucket` below for what used to depend on it).

        THE PER-BUCKET LEDGER (`extra_data["bucket_notional"]`). Bucket
        exposure used to be attributed to whichever intent happened to
        OPEN this row, because `_bucket_open_notional` read the tag off
        `IntentRecord.extra_data["bucket"]` via `intent_id`. Since a
        later BUY folds into an existing row without touching
        `intent_id`, ONE untagged order — a $0.50 fill from any other
        strategy, or from `/trading`'s `strategy="api"` path, which
        builds an `Intent` with no metadata at all — was enough to make
        every subsequent `near_resolution` buy on that
        `(venue, market, outcome)` invisible to the cap, permanently.
        Near-resolution markets are exactly where several strategies
        converge on the same near-certain outcome, so this was not a
        corner case.

        The fix attributes exposure to the CONTRIBUTIONS that carry the
        tag rather than to the row's opener: every BUY adds its own
        `sum(price * size)` to `extra_data["bucket_notional"][bucket]`,
        and every SELL shrinks EVERY bucket entry by the same fraction
        of the position it closed. The invariant that keeps this
        agreeing with the account-wide `_risk_context` view is
        `sum(bucket_notional.values()) <= size * avg_entry_price`, with
        equality when every contract held was bought under some tag —
        both sides are the same fee-exclusive entry basis, and a
        proportional sell leaves `avg_entry_price` untouched, so both
        sides shrink by the same factor.

        The alternative (rewrite `intent_id` to the filling intent) was
        rejected: it would silently destroy "which intent opened this",
        which this docstring promises and `app/models/position.py`
        documents as an indexed lookup.

        The key is written on EVERY buy, empty dict included, so its
        ABSENCE is an unambiguous marker of a row written before this
        change — which `_bucket_open_notional` needs in order to fall
        back to the old opener-based attribution for such rows instead
        of reading them as "no bucket exposure".

        Args:
            session: Session to write in; the caller commits.
            plan: The leg whose fills these are.
            fills: The venue's own fill records, already matched to
                this order.
            side: `"BUY"` or `"SELL"`.
            intent_id: The intent these fills came from. Written only
                when this call CREATES the row.
            hold_to_resolution: Whether the intent intends to hold to
                settlement. Written only on creation, same as above.
            now: Aware UTC time of the booking.
            bucket: The filling intent's `metadata["bucket"]` tag, or
                `None` for an untagged intent. Credited on a BUY;
                ignored on a SELL, which reduces every bucket
                proportionally regardless of who is selling.
        """
        row = await session.scalar(
            select(PositionRow).where(
                PositionRow.mode == self._mode,
                PositionRow.venue == plan.leg.venue,
                PositionRow.market_id == plan.market_row_id,
                PositionRow.outcome == plan.leg.outcome,
                PositionRow.closed_at.is_(None),
            )
        )
        qty = math.fsum(f.size for f in fills)
        notional = math.fsum(f.price * f.size for f in fills)
        fee = math.fsum(f.fee for f in fills)
        if qty <= 0.0:
            return
        price = notional / qty

        if row is None:
            if side == "SELL":
                # Selling something we have no local row for should not
                # invent a negative position; the venue is the authority
                # and reconciliation will surface the discrepancy.
                logger.warning(
                    "order",
                    extra={
                        "event": "sell_without_position",
                        "mode": self._mode,
                        "venue": plan.leg.venue,
                        "market_id": plan.leg.market_id,
                        "outcome": plan.leg.outcome,
                        "size": qty,
                    },
                )
                return
            row = PositionRow(
                market_id=plan.market_row_id,
                venue=plan.leg.venue,
                mode=self._mode,
                intent_id=intent_id,
                token_id=plan.token_id,
                outcome=plan.leg.outcome,
                size=qty,
                avg_entry_price=price,
                total_cost=notional + fee,
                current_price=price,
                current_value=qty * price,
                hold_to_resolution=hold_to_resolution,
                opened_at=now,
                extra_data={
                    _BUCKET_NOTIONAL_KEY: (
                        {bucket: notional} if bucket is not None else {}
                    )
                },
            )
            # Marked at the price it was just bought at, so the opening
            # entry fee shows up as exactly what it is: a cost already
            # incurred against a position worth what it cost.
            _mark_position(row, price)
            session.add(row)
            await session.flush()
            return

        if side == "BUY":
            total = row.size + qty
            row.avg_entry_price = (
                (row.avg_entry_price * row.size + notional) / total if total > 0 else 0.0
            )
            row.size = total
            row.total_cost += notional + fee
            _credit_bucket(row, bucket, notional)
        else:
            closed = min(qty, row.size)
            # Pro-rate the per-bucket ledger by the fraction of the
            # position this sale did NOT close, BEFORE `row.size` is
            # reduced. `avg_entry_price` is unchanged by a sale, so
            # scaling every bucket by the same factor the whole basis
            # shrinks by is what keeps `sum(bucket_notional) <= size *
            # avg_entry_price` true. Which intent is selling is
            # irrelevant: contracts are fungible, the venue nets them,
            # and there is no lot the seller could be said to have
            # picked.
            _shrink_buckets(
                row, (row.size - closed) / row.size if row.size > 0.0 else 0.0
            )
            # REALIZED P&L IS FEE-INCLUSIVE ON BOTH SIDES. `total_cost`
            # is the fee-inclusive basis of what is still held, so
            # `total_cost / size` is what one contract actually cost —
            # entry fee included — and that, not the fee-EXCLUSIVE
            # `avg_entry_price`, is the basis a sale is measured against.
            #
            # Using `avg_entry_price` understated every realized loss by
            # exactly the pro-rated entry fee, and put this row in open
            # disagreement with `RoutedIntent.unwind_cost_usd`
            # (`_unwind_leg`), which has always included it: on T14's own
            # example (buy 10 @ 0.50 fee 0.10, sell 10 @ 0.50 fee 0.10)
            # the unwind cost was 0.20 — the figure the ledger agrees
            # with, 1000 -> 999.80 — while this column said −0.10. Two
            # numbers for the same event, written in the same commit.
            # They now agree: `realized_pnl == -unwind_cost_usd`.
            avg_cost = (
                row.total_cost / row.size
                if row.size > _SIZE_EPSILON
                else row.avg_entry_price
            )
            basis = closed * avg_cost
            exit_fee = fee * (closed / qty) if qty > 0.0 else fee
            self._book_realized(row, closed * price - exit_fee - basis, now)
            row.total_cost = max(0.0, row.total_cost - basis)
            row.size = max(0.0, row.size - closed)
            if row.size <= _SIZE_EPSILON:
                row.size = 0.0
                # A fully closed position holds nothing, so it costs
                # nothing. Subtracting only `closed * avg_entry_price`
                # left the entry fees stranded on this column forever.
                row.total_cost = 0.0
                row.closed_at = now
                # ...and it is in no bucket. The pro-rata shrink above
                # would leave a float residue here rather than an exact
                # zero; a closed row is out of `_bucket_open_notional`'s
                # `closed_at IS NULL` filter either way, but a ledger
                # that says a flat position holds bucket exposure is not
                # something to leave lying in the database.
                if _position_buckets(row) is not None:
                    _write_buckets(row, {})
        _mark_position(row, price)
        await session.flush()

    def _book_realized(self, row: PositionRow, delta: float, now: datetime) -> None:
        """Add one realization to a position's lifetime AND per-day P&L.

        `Position.realized_pnl` is cumulative over the position's whole
        life, which makes it useless for "how much have we lost TODAY?" —
        the question `check_order_limits`'s daily-loss cap actually asks.
        The same delta is therefore also added to
        `extra_data["realized_by_day"][YYYY-MM-DD]`, keyed by the UTC day
        it happened on, which `_risk_context` sums for today alone. It is
        written in the same transaction as the realization itself, so the
        two can never disagree.

        Only the most recent `_REALIZED_DAYS_KEPT` days are retained: the
        fence looks at today, T22's reporting reads the `trades` table,
        and an unbounded per-position dict in a JSON column is a slow
        leak nobody would notice.

        Args:
            row: The position being reduced.
            delta: Realized USD on this event; negative is a loss.
            now: Aware UTC time of the realization.
        """
        row.realized_pnl += delta
        extra = dict(row.extra_data or {})
        raw = extra.get("realized_by_day")
        by_day: dict[str, float] = (
            {str(k): float(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
        )
        key = now.strftime(_DAY_KEY_FORMAT)
        by_day[key] = by_day.get(key, 0.0) + delta
        for stale in sorted(by_day)[:-_REALIZED_DAYS_KEPT]:
            del by_day[stale]
        extra["realized_by_day"] = by_day
        row.extra_data = extra

    def _resolve_reservation(
        self,
        plan: _LegPlan,
        ack: OrderAck | Exception | None,
        fills: Sequence[Fill],
    ) -> ReservationId | None:
        """Settle, release, or KEEP this leg's capital reservation.

        THE RULE: capital is released only on a TERMINAL outcome. An
        order the venue is still working is money that is still
        committed, and `CapitalLedger.locked` is "the only thing in the
        system tracking capital committed to open orders"
        (`app/execution/ledger.py`). Releasing a resting GTC leg's
        reservation — which is what "no fills yet, so give it all back"
        amounted to — told the ledger the money was free while the venue
        could fill the order a second later, and nothing ever
        re-reserved it: not this router, and not `reconcile`, which does
        not touch the ledger at all.

        So:

        - **Still working at the venue** (`open`/`partially_filled` with
          a remainder): the reservation is KEPT. If part of it filled,
          that part is settled and a fresh reservation is taken for what
          is still resting, so the ledger charges what was spent without
          freeing what is still committed.
        - **Terminal** (`filled`, `cancelled`, `rejected`, an exception,
          or no ack at all): settled at what was actually spent, or
          released in full when nothing was.
        - **Replayed**: released in full. The ORIGINAL submission already
          spent the real capital; settling again would double-charge.

        KNOWN LIMIT, DELIBERATE AND SCOPED. A reservation kept for a
        resting order is released by `cancel()`, which finds it through
        `Order.extra_data["reservation_id"]`. It is NOT released by
        `app.execution.reconcile`, which resolves that same order in a
        different process and holds no ledger at all — the ledger is
        in-memory (`app/execution/ledger.py`: "no database, no session,
        no I/O"). Closing that gap is the persistent-capital seam PLAN.md
        D8 still owes, not something to improvise here. Erring toward
        capital that stays LOCKED is the safe side of that gap: the
        failure is a refused order, not an overspend.

        Args:
            plan: The leg, whose `reservation` is cleared or replaced.
            ack: The placement result.
            fills: The order's fills.

        Returns:
            ReservationId | None: The reservation STILL held against this
                order, to be recorded on its `orders` row so a later
                cancel can free it; `None` if nothing is held any more.
        """
        if plan.reservation is None:
            if plan.leg.side == "SELL" and fills:
                proceeds = math.fsum(f.price * f.size for f in fills)
                fee = math.fsum(f.fee for f in fills)
                self._credit(plan.leg.venue, max(0.0, proceeds - fee))
            return None
        reservation = plan.reservation
        plan.reservation = None
        if plan.replayed or not isinstance(ack, OrderAck):
            self._ledger.release(reservation)
            return None
        spent = math.fsum(f.price * f.size for f in fills) + math.fsum(
            f.fee for f in fills
        )
        if not _is_working(ack):
            if fills:
                self._ledger.settle(reservation, spent)
            else:
                self._ledger.release(reservation)
            return None

        # Still resting.
        if fills:
            # Part of it filled. Charge that, then re-commit exactly what
            # the venue is still working on.
            self._ledger.settle(reservation, spent)
            amount = self._reservation_amount(plan, size=ack.remaining_size)
            try:
                reservation = self._ledger.reserve(plan.leg.venue, amount)
            except LedgerError:
                logger.exception(
                    "order",
                    extra={
                        "event": "resting_reservation_failed",
                        "mode": self._mode,
                        "venue": plan.leg.venue,
                        "client_order_id": plan.client_order_id,
                        "remaining_size": ack.remaining_size,
                        "amount": amount,
                    },
                )
                return None
        else:
            # Nothing was spent, so the original reservation already
            # covers exactly what is still committed: keep it untouched.
            amount = self._ledger.reservation(reservation).amount
        plan.reservation = reservation
        self._log(
            "capital_held_for_resting_order",
            intent_id=_intent_of(plan.client_order_id),
            plan=plan,
            status="open",
            extra={"amount": amount, "remaining_size": ack.remaining_size},
        )
        return reservation

    async def _record_reservation(
        self,
        session: AsyncSession,
        plan: _LegPlan,
        reservation: ReservationId | None,
    ) -> None:
        """Record (or clear) the reservation still held against an order row.

        The ledger is in-memory and its ids are opaque, so a reservation
        held for a RESTING order has to be findable again from the order
        it belongs to — otherwise `cancel()` would take the exposure off
        the venue and leave the capital locked forever.

        Args:
            session: The settlement session (the caller commits).
            plan: The leg.
            reservation: The reservation still held, or `None`.
        """
        if plan.order_row_id is None or plan.replayed:
            # A replay's row belongs to the ORIGINAL submission, which may
            # still be holding a reservation against it. Clearing that
            # key here would strand the original's capital as
            # unreleasable — the same reason `_update_order_row` refuses
            # to touch a replayed row at all.
            return
        row = await session.get(OrderRow, plan.order_row_id)
        if row is None:
            return
        extra = dict(row.extra_data or {})
        if reservation is None:
            extra.pop("reservation_id", None)
        else:
            extra["reservation_id"] = reservation
        row.extra_data = extra

    def _release_recorded_reservation(self, row: OrderRow) -> None:
        """Release the reservation an order row still holds, if any.

        Called when an order reaches a terminal state through this
        router (a cancel). A reservation id that the ledger no longer
        knows is not an error: this process may have been restarted, in
        which case the in-memory ledger was rebuilt and never held it.

        Args:
            row: The `orders` row being retired.
        """
        extra = dict(row.extra_data or {})
        reservation = extra.pop("reservation_id", None)
        if reservation is None:
            return
        try:
            self._ledger.release(str(reservation))
        except UnknownReservation:
            logger.warning(
                "order",
                extra={
                    "event": "reservation_already_gone",
                    "mode": self._mode,
                    "venue": row.venue,
                    "client_order_id": row.client_order_id,
                    "reservation_id": str(reservation),
                },
            )
        row.extra_data = extra

    def _credit(self, venue: VenueId, amount: float) -> None:
        """Credit a venue's balance without letting a ledger error escape.

        `CapitalLedger.credit` raises `UnknownVenue` for a venue it was
        never seeded for. That is a real configuration problem, but by
        the time this is called the FILLS ARE REAL at the venue: raising
        here would abandon a half-executed intent mid-flight, un-booked
        and un-recorded. The failure is logged loudly and the caller
        completes, so the trades, the position and any naked exposure are
        still persisted.

        Args:
            venue: Venue whose balance receives the proceeds.
            amount: USD to credit, `>= 0`.
        """
        try:
            self._ledger.credit(venue, amount)
        except LedgerError:
            logger.exception(
                "order",
                extra={
                    "event": "credit_failed",
                    "mode": self._mode,
                    "venue": venue,
                    "amount": amount,
                },
            )

    async def _finalize_intent(
        self,
        session: AsyncSession,
        *,
        intent_id: str,
        status: RoutedStatus,
        unwinds: Sequence[RoutedLeg],
        naked: Sequence[NakedLeg],
        unwind_cost: float,
    ) -> None:
        """Write the intent's terminal status and its unwind record."""
        record = await session.get(IntentRecord, intent_id)
        if record is None:
            return
        record.status = status
        extra = dict(record.extra_data or {})
        if unwinds or naked:
            extra["unwind"] = {
                "attempted": len(unwinds),
                "completed": len([u for u in unwinds if u.status == "filled"]),
                "realized_cost_usd": unwind_cost,
                "naked_legs": [leg.as_dict() for leg in naked],
                "why": "no cross-venue atomicity: an all_or_none leg filled short, "
                "so the filled legs were sold back. Crossing the spread twice "
                "and paying two taker fees is a realized loss.",
            }
        if naked:
            extra["naked_exposure"] = True
        record.extra_data = extra
        await session.flush()

    # -- Rejection path ---------------------------------------------------

    async def _reject(
        self, intent: Intent, intent_id: str, strategy: str, rejection: "_Rejected"
    ) -> RoutedIntent:
        """Persist a pre-flight rejection and return it. Nothing was placed."""
        logger.warning(
            "order",
            extra={
                "event": "intent_rejected",
                "mode": self._mode,
                "intent_id": intent_id,
                "strategy": strategy,
                "reason": rejection.reason,
                "detail": rejection.detail,
            },
        )
        async with self._sessions() as session:
            record = await session.get(IntentRecord, intent_id)
            if record is None:
                record = IntentRecord(
                    id=intent_id,
                    kind=intent.kind,
                    strategy=strategy,
                    mode=self._mode,
                    status="rejected",
                    legs=[_raw_leg_json(leg) for leg in intent.legs],
                    score=dict(intent.metadata.get("score") or {}),
                    extra_data={
                        "atomicity": intent.atomicity,
                        "rejected_reason": rejection.reason,
                        "rejected_detail": rejection.detail,
                    },
                )
                session.add(record)
            else:
                record.status = "rejected"
                extra = dict(record.extra_data or {})
                extra["rejected_reason"] = rejection.reason
                extra["rejected_detail"] = rejection.detail
                record.extra_data = extra
            await session.commit()
        return RoutedIntent(
            intent_id=intent_id,
            strategy=strategy,
            mode=self._mode,
            status="rejected",
            atomicity=intent.atomicity,
            reason=rejection.reason,
        )

    # -- Logging ----------------------------------------------------------

    def _log(
        self,
        event: str,
        *,
        intent_id: str,
        plan: _LegPlan,
        status: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Emit one structured order line. Never carries a secret."""
        payload: dict[str, Any] = {
            "event": event,
            "mode": self._mode,
            "intent_id": intent_id,
            "venue": plan.leg.venue,
            "market_id": plan.leg.market_id,
            "outcome": plan.leg.outcome,
            "side": plan.leg.side,
            "client_order_id": plan.client_order_id,
            "price": plan.price,
            "size": plan.size,
            "status": status,
        }
        if extra:
            payload.update(extra)
        logger.info("order", extra=payload)

    def _log_intent(self, routed: RoutedIntent, tif: TimeInForce) -> None:
        """Emit the one-line summary of a completed submission."""
        logger.info(
            "order",
            extra={
                "event": "intent_routed",
                "mode": routed.mode,
                "intent_id": routed.intent_id,
                "strategy": routed.strategy,
                "status": routed.status,
                "atomicity": routed.atomicity,
                "tif": tif,
                "legs": len(routed.legs),
                "filled_size": routed.filled_size,
                "unwinds": len(routed.unwinds),
                "unwind_cost_usd": routed.unwind_cost_usd,
                "naked_legs": len(routed.naked_legs),
            },
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

#: How far back `get_fills()` is asked to look when collecting an
#: order's own fills. Generous on purpose: a venue's fill timestamp can
#: legitimately predate the local clock reading by a few seconds, and
#: matching is exact (by `client_order_id`), so a wider window cannot
#: pull in another order's fills — it can only avoid missing this one's.
_FEE_LOOKBACK = timedelta(minutes=5)

#: `strftime` pattern for the per-day realized-P&L keys in
#: `Position.extra_data["realized_by_day"]`. A UTC calendar date, which
#: is the same "day" `_risk_context` bounds with and the same one
#: `Settings.max_daily_loss_usd` is written against.
_DAY_KEY_FORMAT = "%Y-%m-%d"

#: How many days of per-position realized-P&L deltas are kept. The
#: daily-loss fence reads today; anything historical is the `trades`
#: table's job. Bounded so a long-lived position cannot grow an
#: unbounded JSON dict.
_REALIZED_DAYS_KEPT = 7


def _today_realized(extra_data: object, today: str) -> float:
    """Return the realized P&L one position booked on `today`, in USD.

    Args:
        extra_data: A `Position.extra_data` value, as read from the DB.
        today: UTC date key, `_DAY_KEY_FORMAT`.

    Returns:
        float: Today's realized P&L for that row; `0.0` if it booked
            none (including every row written before this bookkeeping
            existed — a row with a lifetime `realized_pnl` but no
            per-day record contributes NOTHING to today, which is the
            correct answer, not a missing one).
    """
    if not isinstance(extra_data, dict):
        return 0.0
    by_day = extra_data.get("realized_by_day")
    if not isinstance(by_day, dict):
        return 0.0
    try:
        return float(by_day.get(today, 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _bucket_tag(intent: Intent) -> str | None:
    """Return an intent's risk-bucket tag as a `str`, or `None`.

    `Intent.metadata` is free-form (`dict[str, Any]`), so the tag is
    coerced here — once — rather than at each of the three places that
    consume it (`_check_limits`, `_book_fills`, and through them
    `check_order_limits`/`_upsert_position`), which must all agree on
    the same string or the cap and its aggregate would be reading
    different buckets. A non-string tag becomes its `str()`, which will
    then be reported by `app.execution.fences.warn_unknown_bucket` as
    the unrecognized bucket it is.

    Args:
        intent: The intent being routed.

    Returns:
        str | None: The tag, or `None` when the intent carries none.
    """
    raw = intent.metadata.get("bucket")
    if raw is None:
        return None
    return raw if isinstance(raw, str) else str(raw)


def _position_buckets(row: PositionRow) -> dict[str, float] | None:
    """Return this position's `{bucket: usd}` ledger, or `None` if absent.

    `None` is NOT an empty ledger. It means the row was written before
    `_BUCKET_NOTIONAL_KEY` existed, so nothing is known about which
    buckets its contracts were bought under and
    `_bucket_open_notional` must fall back to the old opener-based
    attribution rather than reading the row as bucket-free.

    Args:
        row: The position row.

    Returns:
        dict[str, float] | None: A defensive copy of the ledger with
            float values, or `None` when the key is missing/malformed.
    """
    extra = row.extra_data
    if not isinstance(extra, dict) or _BUCKET_NOTIONAL_KEY not in extra:
        return None
    raw = extra[_BUCKET_NOTIONAL_KEY]
    if not isinstance(raw, dict):
        return None
    out: dict[str, float] = {}
    for key, value in raw.items():
        try:
            out[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return out


def _write_buckets(row: PositionRow, buckets: dict[str, float]) -> None:
    """Store `buckets` on `row.extra_data[_BUCKET_NOTIONAL_KEY]`.

    Rebinds `extra_data` to a NEW dict rather than mutating in place:
    the column is a plain SQLAlchemy JSON column with no
    `MutableDict`, so an in-place mutation is never flushed. Same
    discipline as `OrderRouter._book_realized`, which shares this
    column.

    Args:
        row: The position row to write to.
        buckets: The ledger to store; written verbatim (empty included).
    """
    extra = dict(row.extra_data or {})
    extra[_BUCKET_NOTIONAL_KEY] = buckets
    row.extra_data = extra


def _credit_bucket(row: PositionRow, bucket: str | None, notional: float) -> None:
    """Add `notional` USD to `bucket` on this position's ledger.

    Called on every BUY fold. The key is written even when `bucket` is
    `None` and even when the ledger stays empty, so that a row touched
    by this code is always distinguishable from a legacy one.

    Args:
        row: The position being added to.
        bucket: The buying intent's tag, or `None` for an untagged buy —
            which contributes nothing to any bucket, which is the whole
            point: an untagged $0.50 order must not carry, or hide,
            near-resolution exposure.
        notional: USD entry basis bought here (`sum(price * size)`,
            fee-exclusive, matching `size * avg_entry_price`).
    """
    buckets = _position_buckets(row) or {}
    if bucket is not None:
        buckets[bucket] = buckets.get(bucket, 0.0) + notional
    _write_buckets(row, buckets)


def _shrink_buckets(row: PositionRow, factor: float) -> None:
    """Scale every bucket entry by `factor` (the un-closed fraction).

    Args:
        row: The position being reduced.
        factor: `(size - closed) / size`, computed BEFORE `size` is
            reduced. `0.0` empties the ledger, which is what a full
            close means.
    """
    buckets = _position_buckets(row)
    if buckets is None:
        # A legacy row keeps its legacy (opener-based) attribution; it
        # is not converted here on the strength of one sale, because
        # this call has no idea what the row's earlier buys were tagged.
        return
    scaled = {key: value * factor for key, value in buckets.items() if value * factor > 0.0}
    _write_buckets(row, scaled)


def _mark_position(row: PositionRow, price: float) -> None:
    """Mark a position at `price` and recompute its unrealized P&L.

    `unrealized_pnl`/`unrealized_pnl_pct` are columns the API returns on
    every `GET /positions` row (`app/api/routes/trading.py`). Nothing
    wrote them, so they read as a permanent, confident `0.0` — a human
    reading "flat" on a position that is not. They are computed here,
    where `current_price`/`current_value` are already written, against
    the FEE-INCLUSIVE basis in `total_cost`, so they are consistent with
    `realized_pnl` (see `_upsert_position`): an open position marked at
    exactly what it was bought at shows the entry fee as an unrealized
    loss, because that is precisely what it is.

    The mark is the last price this router saw for the position, which is
    an honest lower-frequency mark and not a live quote; a mark-to-market
    pass that writes `current_price` from the book should call this too.

    Args:
        row: The position row to mark.
        price: The price to mark at, a probability in `[0.0, 1.0]`.
    """
    row.current_price = price
    row.current_value = row.size * price
    row.unrealized_pnl = row.current_value - row.total_cost
    row.unrealized_pnl_pct = (
        row.unrealized_pnl / row.total_cost if row.total_cost > 0.0 else 0.0
    )


class _Rejected(Exception):
    """Internal control-flow signal: this intent must not be placed.

    Carries a machine-readable `reason` (persisted on the intent row and
    returned on `RoutedIntent.reason`) alongside a human `detail`.
    """

    def __init__(self, reason: str, detail: str) -> None:
        """Record the machine reason and the human-readable detail."""
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def snap_to_tick(price: float, tick_size: float, side: OrderSide) -> float:
    """Snap a limit price onto the venue's tick grid, CONSERVATIVELY.

    Direction matters and is not symmetric: a BUY rounds DOWN and a SELL
    rounds UP, so snapping can only ever make the order LESS aggressive.
    Rounding a BUY up would reach a level the strategy never authorized
    paying for; in a simulation it would manufacture fills that the real
    limit could not have obtained.

    Why snap at all: `SimulatedFillEngine.fill()` RAISES for an off-tick
    limit once it has a `VenueMarket`, and a real venue rejects the order
    outright — so an unsnapped strategy price (arithmetic on a mid, a
    replayed off-grid price) would turn into a hard failure at the last
    possible moment. Snapping here also narrows the backtest/paper
    asymmetry the Phase 1 review flagged: the backtester fills off-grid
    because it passes no `market=`, and this at least makes the paper
    path fill the nearest grid price it is allowed to rather than
    nothing.

    Args:
        price: Desired limit, a probability in `[0.0, 1.0]`.
        tick_size: The market's minimum price increment, in `(0.0, 1.0]`.
        side: `"BUY"` (round down) or `"SELL"` (round up).

    Returns:
        float: The snapped limit, still in `[0.0, 1.0]`.
    """
    quantized = Decimal(str(round(price, _PRICE_DENOISE_PLACES)))
    tick = Decimal(str(tick_size))
    rounding = ROUND_FLOOR if side == "BUY" else ROUND_CEILING
    steps = (quantized / tick).to_integral_value(rounding=rounding)
    snapped = float(steps * tick)
    return min(1.0, max(0.0, snapped))


def token_id_for(market: VenueMarket, outcome: str) -> str:
    """Return the venue-native token id for one outcome, or `""`.

    `Order.token_id`/`Trade.token_id`/`Position.token_id` are
    `String(100) NOT NULL`, and Kalshi has NO per-outcome token-id
    concept: its adapter maps BOTH outcomes to the market ticker
    (`outcome_ids = {"YES": ticker, "NO": ticker}`), which is not a token
    id and is not even unique across outcomes. Writing the ticker there
    would be a WRONG value dressed as a right one — anything keying on
    `token_id` would silently merge the YES and NO sides of a Kalshi
    market. The empty string is the honest answer: it says "this venue
    has no such identifier", which is exactly true, and it is the value
    T15's own Kalshi order test uses.

    The rule is venue-agnostic rather than a `venue == "kalshi"` branch:
    an outcome id that is absent, or is merely the market id repeated, is
    not a token id.

    Args:
        market: The normalized market.
        outcome: Canonical outcome name.

    Returns:
        str: The token id, or `""` when the venue has none.
    """
    outcome_id = market.outcome_ids.get(outcome)
    if not outcome_id or outcome_id == market.market_id:
        return ""
    return str(outcome_id)


def _intent_id(intent: Intent) -> str:
    """Return the id every `client_order_id` for this intent derives from.

    `Intent` (PLAN.md D7) has no `id` field, so one is minted here unless
    the caller supplied `metadata["intent_id"]`. The length check is not
    cosmetic: `Order.client_order_id` is `String(64)` and holds
    `f"{intent_id}:{leg_index}:{attempt}"`. A UUID4 is 36 characters,
    leaving 28 for the suffix. An id past ~48 characters could push the
    key over 64 and TRUNCATE, at which point two distinct orders would
    collide on the venue's idempotency key — a silent double-fill or a
    silent no-op. Rejecting is the only safe answer.

    Args:
        intent: The intent being routed.

    Returns:
        str: The intent id.

    Raises:
        ValueError: If a caller-supplied id is empty or too long.
    """
    supplied = intent.metadata.get("intent_id") if intent.metadata else None
    if supplied is None:
        return str(uuid.uuid4())
    candidate = str(supplied)
    if not candidate:
        raise ValueError("intent metadata['intent_id'] must be non-empty")
    if len(candidate) > 48:
        raise ValueError(
            f"intent id {candidate!r} is {len(candidate)} characters; "
            "client_order_id is String(64) and must hold "
            "f'{intent_id}:{leg_index}:{attempt}' without truncating "
            "(see app/models/trade.py)"
        )
    return candidate


def _intent_of(client_order_id: str) -> str:
    """Return the intent id embedded in a `client_order_id`."""
    return client_order_id.split(":", 1)[0]


def _leg_size(leg: Leg) -> float | None:
    """Return a leg's size in contracts, converting from USD if needed."""
    if leg.size_contracts is not None:
        return leg.size_contracts
    if leg.size_usd is not None and leg.limit_price > 0.0:
        return leg.size_usd / leg.limit_price
    return None


def _leg_json(plan: _LegPlan) -> dict[str, Any]:
    """Return the JSON snapshot of a planned leg for `IntentRecord.legs`."""
    return {
        "index": plan.index,
        "venue": plan.leg.venue,
        "market_id": plan.leg.market_id,
        "outcome": plan.leg.outcome,
        "side": plan.leg.side,
        "limit_price": plan.price,
        "requested_limit_price": plan.leg.limit_price,
        "size_contracts": plan.size,
        "client_order_id": plan.client_order_id,
    }


def _raw_leg_json(leg: Leg) -> dict[str, Any]:
    """Return the JSON snapshot of a leg that never reached planning."""
    return {
        "venue": leg.venue,
        "market_id": leg.market_id,
        "outcome": leg.outcome,
        "side": leg.side,
        "limit_price": leg.limit_price,
        "size_contracts": leg.size_contracts,
        "size_usd": leg.size_usd,
    }


def _fill_matches(
    fill: Fill, client_order_id: str, venue_order_id: str | None
) -> bool:
    """Return `True` if `fill` belongs to this order, in EITHER mode.

    THE RULE, in priority order:

    1. If the fill carries a `metadata["client_order_id"]` AT ALL, that
       key decides — a match if it equals ours, and a NON-match if it
       does not. An adapter that labels its fills with the client key has
       told us exactly which order each one belongs to, and no weaker
       signal may override that.
    2. Otherwise, a match if the fill's `order_id` is the VENUE order id
       from this order's own acknowledgement. This is the live path:
       `polymarket/adapter.py::_parse_fill` and `kalshi/adapter.py::
       _parse_fill` both put the venue's order id in `Fill.order_id` and
       neither has ever heard of a client order id. An EMPTY
       `venue_order_id` never matches (both live parsers fall back to
       `""` when the payload has no id at all), or an unidentifiable
       fill would be attributed to every order at once.
    3. Otherwise, a match if `Fill.order_id` equals the client key —
       for an adapter that echoes the idempotency key back as its order
       id, which is what the fill engine does when it has no venue id to
       use.

    Args:
        fill: A fill returned by `adapter.get_fills()`.
        client_order_id: The order's idempotency key.
        venue_order_id: The venue's own order id from the ack, if any.

    Returns:
        bool: `True` if the fill belongs to this order.
    """
    labelled = fill.metadata.get("client_order_id")
    if labelled is not None:
        return str(labelled) == client_order_id
    if venue_order_id and fill.order_id == venue_order_id:
        return True
    return bool(fill.order_id) and fill.order_id == client_order_id


def _filled(ack: OrderAck | Exception | None) -> float:
    """Return an ack's filled size, or `0.0` for a failure/absence."""
    return ack.filled_size if isinstance(ack, OrderAck) else 0.0


def _venue_order_id(ack: OrderAck | Exception | None) -> str | None:
    """Return an ack's venue order id, or `None` for a failure/absence."""
    return ack.order_id if isinstance(ack, OrderAck) else None


def _is_working(ack: OrderAck) -> bool:
    """Return `True` if the venue is still working this order.

    "Working" means the venue could still fill it: the ack says `open`
    or `partially_filled` AND a remainder is outstanding. Every other
    status (`filled`, `cancelled`, `rejected`) is terminal — nothing
    more can happen to the order, so nothing more is committed to it.
    """
    return (
        ack.status in ("open", "partially_filled")
        and ack.remaining_size > _SIZE_EPSILON
    )


def _avg_price(ack: OrderAck | Exception | None) -> float | None:
    """Return an ack's average fill price, or `None`."""
    return ack.avg_fill_price if isinstance(ack, OrderAck) else None


def _reason_for(adapter: object, client_order_id: str) -> str | None:
    """Return an adapter's fill reason for an order, if it exposes one.

    Reached through `getattr` on purpose: `fill_reason` is a
    `PaperVenueAdapter` enrichment, not part of the `VenueAdapter`
    Protocol, and the router must behave identically without it. The
    reason is used for LOGGING and for choosing between `FAILED` and
    `EXPIRED` on a local row — never to change what was placed.
    """
    getter = getattr(adapter, "fill_reason", None)
    if getter is None:
        return None
    try:
        value = getter(client_order_id)
    except Exception:  # noqa: BLE001 - an enrichment must never break routing
        return None
    return str(value) if value is not None else None


def _leg_status(ack: OrderAck | Exception | None, reason: str | None) -> LegStatus:
    """Map a placement result onto a `LegStatus`."""
    if isinstance(ack, Exception) or ack is None:
        return "failed"
    if ack.status == "filled":
        return "filled"
    if ack.status == "partially_filled":
        return "partially_filled"
    if ack.status == "open":
        return "open"
    if ack.status == "cancelled":
        return "cancelled"
    return "failed" if reason in _STRUCTURAL_REASONS else "rejected"


def _order_status(ack: OrderAck, reason: str | None) -> OrderStatus:
    """Map an ack onto the persisted `OrderStatus`.

    An IOC that found no liquidity is `EXPIRED`, not `FAILED`: nothing
    was wrong with the order, the book simply was not there, and it can
    sensibly be re-submitted with a fresh attempt number. A structurally
    rejected order (`crossed_book`, `post_only` on a taker-only engine,
    insufficient position to sell) is `FAILED` — retrying it is a bug.
    """
    if ack.status == "filled":
        return OrderStatus.FILLED
    if ack.status == "partially_filled":
        return OrderStatus.PARTIALLY_FILLED
    if ack.status == "open":
        return OrderStatus.OPEN
    if ack.status == "cancelled":
        return OrderStatus.CANCELLED
    return OrderStatus.FAILED if reason in _STRUCTURAL_REASONS else OrderStatus.EXPIRED


def _intent_status(legs: Sequence[RoutedLeg]) -> RoutedStatus:
    """Derive the intent row's terminal status from its legs.

    - `"pending"`: a leg is still resting on the venue, so the outcome
      is not decided yet.
    - `"executed"`: at least one leg filled (even if it was then
      unwound — the fills genuinely happened and the trades are real).
    - `"expired"`: everything was placed and nothing filled.
    """
    if any(leg.status == "open" for leg in legs):
        return "pending"
    if any(leg.filled_size > 0.0 for leg in legs):
        return "executed"
    return "expired"
