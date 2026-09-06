"""Normalized, venue-agnostic domain types (PLAN.md D3).

Every price in this module is a probability in ``[0.0, 1.0]`` on every
venue: Kalshi's fixed-point dollar strings (and legacy integer cents) are
converted to this ``[0,1]`` float scale at the concrete adapter boundary
(``app/venues/kalshi/``, T12) and nowhere else. Every size is contracts,
where one contract pays ``$1.00`` at resolution. Every timestamp is an
aware UTC ``datetime`` — see ``app.utils.time.ensure_aware``.

These are frozen (immutable) dataclasses: a value read from a venue (a
book, a fill, a balance) should never be mutated in place after
construction — code that wants a changed value builds a new instance
(``dataclasses.replace``). `frozen=True` only blocks *rebinding* a field
(``book.venue = "kalshi"`` raises); it does nothing to stop a caller
mutating a *mutable value held by* a field in place
(``book.bids.append(...)``). So every field that holds a collection is
additionally coerced in ``__post_init__`` (via ``object.__setattr__``,
the standard escape hatch for frozen dataclasses) into a genuinely
immutable runtime type — ``tuple`` for sequences, ``types.MappingProxyType``
over a private copy for mappings — so depth/outcomes/ids can never be
fabricated after the validator has already approved the value. Callers
may still construct these with a plain ``list``/``dict``; the field just
comes back immutable.
"""
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, Literal, get_args

from app.utils.time import ensure_aware

#: The two real-API, liquid prediction-market venues this kit supports
#: (PLAN.md D3). Not an open-ended registry — see `app/venues/registry.py`.
VenueId = Literal["polymarket", "kalshi"]

#: Lifecycle status of a `VenueMarket`.
MarketStatus = Literal["open", "closed", "resolved"]

#: Side of an `OrderRequest`.
OrderSide = Literal["BUY", "SELL"]

#: Normalized time-in-force. Concrete adapters translate at the venue
#: boundary, e.g. Kalshi: `"GTC" -> "good_till_canceled"`,
#: `"IOC" -> "immediate_or_cancel"`, `"FOK" -> "fill_or_kill"`.
TimeInForce = Literal["GTC", "IOC", "FOK"]

#: Which side of a fill/fee a `Fill` was on.
Liquidity = Literal["maker", "taker"]

#: Where an `OrderBook`'s depth actually came from.
#: `"recorded"` = every level was observed on the venue (a real
#: `get_book` response, or a stored `book_snapshots` row).
#: `"synthetic"` = the depth beyond top-of-book was INVENTED by
#: `app.execution.fill_engine.synthesize_book` from a top-of-book-only
#: history row, and was never observed. GUARDRAILS.md §1.7 requires any
#: result computed against a synthetic book to be labeled as such
#: wherever it is shown, which is why this travels on the book itself
#: (and, via `SimulatedFillEngine`, onto every `Fill` produced from it)
#: instead of being tracked out-of-band by whoever happens to remember.
DepthSource = Literal["recorded", "synthetic"]

#: `OrderBook.metadata` key carrying the `DepthSource`. Defaulted to
#: `"recorded"` on construction (see `OrderBook.__post_init__`), so the
#: tag is ALWAYS present downstream and a consumer never has to guess
#: what a missing key meant.
DEPTH_SOURCE_KEY = "depth_source"

#: Direction to walk an `OrderBook` in `OrderBook.depth_at`/`OrderBook.walk`:
#: `"buy"` consumes `asks` (a buyer takes liquidity from resting sellers),
#: `"sell"` consumes `bids` (a seller takes liquidity from resting buyers).
WalkSide = Literal["buy", "sell"]

#: Absolute epsilon (contracts) for `OrderBook.walk`'s remainder check.
#: Sizes here are CONTRACTS (each pays $1.00 at resolution); the smallest
#: meaningful unit of a contract is bounded below by `VenueMarket.min_size`,
#: and neither real venue this kit supports trades in fractions anywhere
#: close to `1e-9` of a contract (Polymarket/Kalshi minimums are on the
#: order of whole or low-decimal contracts). `1e-9` is chosen to sit
#: comfortably between those two floors: many orders of magnitude above
#: IEEE-754 float64 noise from chained subtraction across a handful of
#: book levels (empirically ~1e-17 to ~1e-16 for level sizes near 1.0 —
#: see DEFECT 3, `test_walk_does_not_leave_a_phantom_dust_level`), and
#: many orders of magnitude below any `min_size` a venue could plausibly
#: report. A pure absolute epsilon alone breaks down for very large
#: requested sizes, where float noise scales with magnitude, so it is
#: combined below with a relative term.
_WALK_EPSILON_ABS = 1e-9

#: Relative epsilon (fraction of the requested size) for the same check,
#: combined with `_WALK_EPSILON_ABS` as `max(abs, rel * requested)` so the
#: guard scales for both very large requests (where float noise grows
#: with magnitude and a fixed absolute epsilon would eventually be too
#: small to catch it) and very small ones (where a pure relative epsilon
#: alone would be too small to matter, which is exactly why it is not
#: used alone).
_WALK_EPSILON_REL = 1e-9


def _check_price(value: float, *, field: str) -> None:
    """Raise `ValueError` if `value` is not a finite probability in [0.0, 1.0].

    `math.isfinite` is checked explicitly rather than relied on as a side
    effect of the range comparison: `0.0 <= nan <= 1.0` and
    `0.0 <= inf <= 1.0` both already evaluate `False` (so NaN/Infinity are
    in fact rejected today), but that is an accident of how Python
    evaluates chained comparisons with NaN, not a stated rule — spelling
    it out makes the intent readable and keeps this in sync with
    `_check_size`, where the equivalent accident does NOT hold (see
    there).

    Args:
        value: The price to validate.
        field: Name of the field being validated, used in the message.

    Raises:
        ValueError: If `value` is not finite or is outside `[0.0, 1.0]`.
    """
    if not (math.isfinite(value) and 0.0 <= value <= 1.0):
        raise ValueError(f"{field} must be a finite value in [0.0, 1.0], got {value!r}")


def _check_size(value: float, *, field: str) -> None:
    """Raise `ValueError` if `value` is not a finite number >= 0.

    Despite the name, this guards every "must be zero or positive, and
    never NaN/Infinity" float in this module, not only contract sizes:
    contract sizes, USD amounts (`Fill.fee`, `Balance.available/locked`),
    and dimensionless fee rates (`FeeSchedule.taker_rate`/`maker_rate`)
    all share the identical rule, so they share this one check.

    `value < 0` alone is NOT enough here, unlike `_check_price`'s range
    check: `float('nan') < 0` and `float('inf') < 0` are both `False`, so
    a naive negativity guard lets NaN and +Infinity through silently.
    `json.loads` accepts the literal tokens `NaN`/`Infinity`/`-Infinity`
    by default, so a malformed venue payload can carry them straight into
    these fields with no adversary involved — `math.isfinite` closes that.

    Args:
        value: The value to validate.
        field: Name of the field being validated, used in the message.

    Raises:
        ValueError: If `value` is not finite or is negative.
    """
    if not (math.isfinite(value) and value >= 0.0):
        raise ValueError(f"{field} must be a finite value >= 0, got {value!r}")


@dataclass(frozen=True)
class FeeSchedule:
    """Per-market fee schedule sourced from a venue payload, a category
    table, or `Settings` (GUARDRAILS.md §1.5: fees are never literals in
    strategy code).

    NOTE ON PLACEMENT: this dataclass is intentionally defined here, in
    `types.py` (T04), rather than in `app/venues/fees.py` (T05), so that
    `VenueMarket.fee` below has a concrete type before T05 exists. T05
    ("Fee models and cost settings") should `from app.venues.types import
    FeeSchedule` and build `FeeModel`/`PolymarketFeeModel`/`KalshiFeeModel`
    (see `app.venues.base.FeeModel`) around this exact class — it must not
    redefine a second, incompatible `FeeSchedule`.

    Units: `taker_rate`/`maker_rate` are dimensionless fee rates (e.g.
    `0.05` for 5%), consumed by `FeeModel.fee()` (T05) with the formula
    `fee = size_contracts * rate * price * (1 - price)`, in USD.

    Attributes:
        taker_rate: Fee rate applied to taker fills, >= 0.
        maker_rate: Fee rate applied to maker fills, >= 0.
        source: Where this rate came from, e.g. `"clob_market"`,
            `"category_table"`, `"fee_waiver"`, `"settings_default"`.
            Never blank — callers need to know whether a per-market rate
            overrode the category default.
    """

    taker_rate: float
    maker_rate: float
    source: str

    def __post_init__(self) -> None:
        """Validate fee rates are finite, non-negative, and `source` is set."""
        _check_size(self.taker_rate, field="taker_rate")
        _check_size(self.maker_rate, field="maker_rate")
        if not self.source:
            raise ValueError("source must be non-empty")


@dataclass(frozen=True)
class BookLevel:
    """One price level of an order book.

    Attributes:
        price: Level price, a probability in [0.0, 1.0].
        size: Contracts resting at this level, >= 0.
    """

    price: float
    size: float

    def __post_init__(self) -> None:
        """Validate `price` and `size`."""
        _check_price(self.price, field="price")
        _check_size(self.size, field="size")


@dataclass(frozen=True)
class OrderBook:
    """Normalized order book snapshot for one (market, outcome).

    This class is NORMALIZED ON CONSTRUCTION: `__post_init__` sorts `bids`
    descending by price (best bid first) and `asks` ascending by price
    (best ask first), regardless of the order they were supplied in.
    Venues may legitimately return levels in any order (or none at all);
    normalizing here — rather than raising or documenting an ordering
    requirement callers must uphold — is what a normalized domain type is
    for. Because of this, `best_bid()`/`best_ask()`/`mid()`/`depth_at()`/
    `walk()` all hold their stated contracts (best-price-first, etc.)
    for ANY input order; callers never need to sort before constructing
    one. `bids`/`asks` are also coerced to `tuple[BookLevel, ...]` here
    (immutable — see module docstring), so the sort result and the
    immutability guarantee are established in the same step.

    `walk()` is the depth-walking primitive `SimulatedFillEngine` (T07)
    is built on.

    Attributes:
        venue: Venue this book was read from.
        market_id: Venue-native market identifier.
        outcome: Outcome name (e.g. `"YES"`, `"NO"`).
        bids: Bid levels, sorted best (highest price) first.
        asks: Ask levels, sorted best (lowest price) first.
        ts: Aware UTC timestamp this snapshot was read at.
        metadata: Free-form provenance for this book. Always carries
            `DEPTH_SOURCE_KEY` (defaulted to `"recorded"` — see
            `__post_init__` and `DepthSource`); a synthesized book
            (`app.execution.fill_engine.synthesize_book`, T07) sets it to
            `"synthetic"` and adds the parameters it invented the depth
            from. Immutable (`types.MappingProxyType` over a private
            copy, coerced in `__post_init__`) like every other mapping in
            this module, so a book cannot be re-labeled `"recorded"`
            after the fact.
    """

    venue: VenueId
    market_id: str
    outcome: str
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    ts: datetime
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate `ts`, normalize `bids`/`asks`, and stamp `depth_source`.

        Each `BookLevel` already validated its own `price`/`size` at
        construction, so there is nothing further to check on `bids`/
        `asks` here beyond sorting and immutability.

        `metadata[DEPTH_SOURCE_KEY]` defaults to `"recorded"` rather than
        being left absent: every book that is not built by
        `synthesize_book` IS a real, observed book, and defaulting here
        means a downstream consumer (a `Fill`, T22's edge-decay report)
        can read the tag unconditionally instead of treating "key
        missing" as a third, unlabeled state — which is exactly how a
        synthetic-depth result would end up shown as if it were real
        (GUARDRAILS.md §1.7).

        Raises:
            ValueError: If `metadata[DEPTH_SOURCE_KEY]` is present but is
                not one of `DepthSource`'s values. A typo there
                ("syntetic") would silently mislabel a fabricated-depth
                result as a real one, so it is rejected loudly.
        """
        ensure_aware(self.ts)
        object.__setattr__(
            self, "bids", tuple(sorted(self.bids, key=lambda lvl: lvl.price, reverse=True))
        )
        object.__setattr__(
            self, "asks", tuple(sorted(self.asks, key=lambda lvl: lvl.price))
        )
        metadata = dict(self.metadata)
        depth_source = metadata.setdefault(DEPTH_SOURCE_KEY, "recorded")
        if depth_source not in get_args(DepthSource):
            raise ValueError(
                f"{DEPTH_SOURCE_KEY} must be one of {get_args(DepthSource)}, "
                f"got {depth_source!r}"
            )
        object.__setattr__(self, "metadata", MappingProxyType(metadata))

    @property
    def depth_source(self) -> str:
        """Return this book's `DepthSource` tag.

        Returns:
            str: `"recorded"` or `"synthetic"` (see `DepthSource`). Always
                present — `__post_init__` defaults and validates it.
        """
        return str(self.metadata[DEPTH_SOURCE_KEY])

    def best_bid(self) -> BookLevel | None:
        """Return the highest bid level, or `None` if `bids` is empty."""
        return self.bids[0] if self.bids else None

    def best_ask(self) -> BookLevel | None:
        """Return the lowest ask level, or `None` if `asks` is empty."""
        return self.asks[0] if self.asks else None

    def mid(self) -> float | None:
        """Return the midpoint of the best bid and best ask.

        Returns:
            float | None: `(best_bid.price + best_ask.price) / 2`, or
                `None` if either side is empty — a midpoint needs both.
        """
        bid = self.best_bid()
        ask = self.best_ask()
        if bid is None or ask is None:
            return None
        return (bid.price + ask.price) / 2.0

    def depth_at(self, price: float, side: WalkSide) -> float:
        """Return total size available at prices at least as good as `price`.

        Args:
            price: Limit price, a probability in [0.0, 1.0].
            side: `"buy"` sums `asks` levels with `level.price <= price`
                (contracts obtainable by paying up to `price` per
                contract); `"sell"` sums `bids` levels with
                `level.price >= price` (contracts sellable while
                accepting no less than `price` per contract).

        Returns:
            float: Total size, in contracts, at or better than `price`.

        Raises:
            ValueError: If `side` is not `"buy"`/`"sell"`.
        """
        if side == "buy":
            return sum(level.size for level in self.asks if level.price <= price)
        if side == "sell":
            return sum(level.size for level in self.bids if level.price >= price)
        raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")

    def walk(self, side: WalkSide, size: float) -> list[tuple[float, float]]:
        """Simulate consuming `size` contracts from the book, best price first.

        This never raises for an illiquid book and never returns more
        total size than requested: it consumes levels in order (best
        first, guaranteed by `OrderBook`'s normalization on construction —
        see the class docstring), taking the full level size until the
        level is exhausted or `size` is satisfied, and returns whatever
        the book actually had if it runs dry before `size` is filled.
        Slippage falls out of this walk naturally — it is not a fixed
        constant.

        The remainder check is `remaining <= max(_WALK_EPSILON_ABS,
        _WALK_EPSILON_REL * size)`, not `remaining <= 0`: naive sequential
        float subtraction across levels can leave a tiny nonzero residual
        even when the request was, in real arithmetic, exactly satisfied
        (e.g. `0.1 + 0.2 == 0.30000000000000004` in IEEE-754), and without
        this tolerance that dust would be taken as a genuine remaining
        size and produce one more, phantom, near-zero-size level from the
        next level in the book — a position at a real price that could
        never actually be filled or closed downstream. See the module
        docstring for how the epsilon is chosen.

        Args:
            side: `"buy"` walks `asks` (ascending price = best-first);
                `"sell"` walks `bids` (descending price = best-first).
            size: Contracts to fill; must be finite and >= 0.

        Returns:
            list[tuple[float, float]]: `(price, size)` pairs, in the
                order levels were consumed, with the last pair sized down
                to whatever remainder was still needed. The sum of the
                returned sizes equals `min(size, total depth on that
                side)`.

        Raises:
            ValueError: If `size` is not finite, is negative, or `side`
                is not `"buy"`/`"sell"`.
        """
        if not (math.isfinite(size) and size >= 0.0):
            # Explicit here (not left to DEFECT 2's field-level checks)
            # because `size` is a bare method argument, not a validated
            # dataclass field: `size < 0` alone would silently pass NaN
            # through (`nan < 0` is `False`), and `min(level.size, nan)`
            # always returns `level.size`, so a NaN `size` would drain
            # every level in the book instead of filling nothing.
            raise ValueError(f"size must be a finite value >= 0, got {size!r}")
        if side == "buy":
            levels = self.asks
        elif side == "sell":
            levels = self.bids
        else:
            raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")

        threshold = max(_WALK_EPSILON_ABS, _WALK_EPSILON_REL * size)
        remaining = size
        filled: list[tuple[float, float]] = []
        for level in levels:
            if remaining <= threshold:
                break
            take = min(level.size, remaining)
            if take > 0:
                filled.append((level.price, take))
                remaining -= take
        return filled


@dataclass(frozen=True)
class VenueMarket:
    """Normalized market metadata, venue-agnostic.

    `question`/`rules_text` are untrusted venue text (GUARDRAILS.md §6):
    displayed and scored, never executed or interpreted as instructions.

    Attributes:
        venue: `"polymarket"` or `"kalshi"`.
        market_id: Venue-native market identifier (Polymarket
            `condition_id`/CLOB market id; Kalshi `ticker`).
        event_id: Venue-native parent event identifier, if the venue
            groups markets into events; `None` otherwise.
        question: The market's question text.
        outcomes: Outcome names, e.g. `("YES", "NO")`. Immutable (a
            `tuple`, coerced in `__post_init__`) — see the module
            docstring.
        outcome_ids: Outcome name -> venue-native outcome/token identifier
            (e.g. Polymarket CLOB `token_id`). Immutable
            (`types.MappingProxyType` over a private copy, coerced in
            `__post_init__`) — a caller cannot mutate a token id in place
            after this market has been validated.
        rules_text: Resolution rules text.
        resolution_source: Named resolution source/authority, if the
            venue states one; `None` otherwise.
        close_time: Aware UTC time trading closes.
        expected_settle_time: Aware UTC expected settlement time, if
            known; `None` otherwise.
        status: `"open"`, `"closed"`, or `"resolved"`.
        result: Venue-native resolved outcome string, `None` if
            unresolved.
        tick_size: Minimum price increment, a probability in (0.0, 1.0].
        min_size: Minimum order size in contracts, >= 0.
        fee: The market's `FeeSchedule`.
        raw: The unmodified venue payload this was parsed from, kept for
            debugging and for fields not yet normalized. Immutable
            (`types.MappingProxyType` over a private copy, coerced in
            `__post_init__`).
    """

    venue: VenueId
    market_id: str
    event_id: str | None
    question: str
    outcomes: tuple[str, ...]
    outcome_ids: Mapping[str, str]
    rules_text: str
    resolution_source: str | None
    close_time: datetime
    expected_settle_time: datetime | None
    status: MarketStatus
    result: str | None
    tick_size: float
    min_size: float
    fee: FeeSchedule
    raw: Mapping[str, Any]

    def __post_init__(self) -> None:
        """Validate timestamps/tick_size/min_size, then normalize
        `outcomes`/`outcome_ids`/`raw` to immutable runtime types.
        """
        ensure_aware(self.close_time)
        if self.expected_settle_time is not None:
            ensure_aware(self.expected_settle_time)
        if not (math.isfinite(self.tick_size) and 0.0 < self.tick_size <= 1.0):
            raise ValueError(f"tick_size must be in (0.0, 1.0], got {self.tick_size!r}")
        _check_size(self.min_size, field="min_size")
        object.__setattr__(self, "outcomes", tuple(self.outcomes))
        # `dict(self.outcome_ids)`/`dict(self.raw)` copy into a private
        # dict before wrapping so the proxy cannot be mutated indirectly
        # through a reference the caller kept to the original mapping.
        object.__setattr__(
            self, "outcome_ids", MappingProxyType(dict(self.outcome_ids))
        )
        object.__setattr__(self, "raw", MappingProxyType(dict(self.raw)))


@dataclass(frozen=True)
class OrderRequest:
    """A normalized order to place on one venue.

    `client_order_id` is the idempotency key: `OrderRouter` (T14)
    generates it as `f"{intent_id}:{leg_index}:{attempt}"` (PLAN.md D4);
    concrete adapters (T11/T12) must pass it through to the venue
    unchanged so a retried request cannot double-fill.

    Attributes:
        venue: `"polymarket"` or `"kalshi"`.
        market_id: Venue-native market identifier.
        outcome: Outcome being traded, e.g. `"YES"`/`"NO"`.
        side: `"BUY"` or `"SELL"`.
        price: Limit price, a probability in [0.0, 1.0].
        size: Order size in contracts, >= 0.
        tif: Normalized time-in-force (see `TimeInForce`).
        client_order_id: Caller-supplied idempotency key, non-empty.
        post_only: If `True`, the order must not take liquidity. Default
            `False`.
    """

    venue: VenueId
    market_id: str
    outcome: str
    side: OrderSide
    price: float
    size: float
    tif: TimeInForce
    client_order_id: str
    post_only: bool = False

    def __post_init__(self) -> None:
        """Validate `price`, `size`, and `client_order_id`."""
        _check_price(self.price, field="price")
        _check_size(self.size, field="size")
        if not self.client_order_id:
            raise ValueError("client_order_id must be non-empty")


@dataclass(frozen=True)
class OrderAck:
    """Venue's acknowledgement of a `place_order` call.

    PLAN.md D3 names this type but does not enumerate its fields; this
    shape is inferred from both venues' real order-response payloads
    (Kalshi V2 `POST /portfolio/events/orders` returns `order_id`,
    `client_order_id`, `fill_count`, `remaining_count`,
    `average_fill_price`, `average_fee_paid`, `ts_ms` — PLAN.md §3; the
    Polymarket CLOB order response is analogous). T11/T12 should treat
    this as a starting point, not gospel — extend it if a real payload
    needs a field this doesn't have.

    Attributes:
        venue: `"polymarket"` or `"kalshi"`.
        order_id: Venue-native order identifier.
        client_order_id: The idempotency key this ack answers.
        status: Venue-native order status right after placement.
        filled_size: Contracts already filled, >= 0.
        remaining_size: Contracts still resting/working, >= 0.
        avg_fill_price: Size-weighted average fill price so far, a
            probability in [0.0, 1.0], or `None` if nothing has filled.
        ts: Aware UTC time of this acknowledgement.
    """

    venue: VenueId
    order_id: str
    client_order_id: str
    status: Literal["open", "filled", "partially_filled", "cancelled", "rejected"]
    filled_size: float
    remaining_size: float
    avg_fill_price: float | None
    ts: datetime

    def __post_init__(self) -> None:
        """Validate sizes, optional price, and timestamp."""
        _check_size(self.filled_size, field="filled_size")
        _check_size(self.remaining_size, field="remaining_size")
        if self.avg_fill_price is not None:
            _check_price(self.avg_fill_price, field="avg_fill_price")
        ensure_aware(self.ts)


@dataclass(frozen=True)
class Fill:
    """One executed fill on a venue.

    Attributes:
        venue: `"polymarket"` or `"kalshi"`.
        order_id: The venue-native order this fill belongs to.
        price: Fill price, a probability in [0.0, 1.0].
        size: Fill size in contracts, >= 0.
        fee: Fee charged for this fill, in USD, >= 0. Neither venue
            currently models a maker rebate (PLAN.md §3: Polymarket
            makers pay 0; Kalshi `maker_rate` defaults to 0).
        ts: Aware UTC fill timestamp.
        liquidity: `"maker"` or `"taker"`.
        metadata: Free-form context for this fill. `SimulatedFillEngine`
            (T07) records `depth_source` (propagated from the book the
            fill was walked out of, so a fabricated-depth fill stays
            labeled all the way into a report — GUARDRAILS.md §1.7),
            `latency_ms`, `market_id`, and `outcome` here; `Fill` itself
            carries no market/outcome field, so that context would
            otherwise be lost between the engine and the ledger.
            Immutable (`types.MappingProxyType` over a private copy,
            coerced in `__post_init__`).
    """

    venue: VenueId
    order_id: str
    price: float
    size: float
    fee: float
    ts: datetime
    liquidity: Liquidity
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate `price`, `size`, `fee`, `ts`; freeze `metadata`."""
        _check_price(self.price, field="price")
        _check_size(self.size, field="size")
        _check_size(self.fee, field="fee")
        ensure_aware(self.ts)
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True)
class Balance:
    """Venue account balance, USD. Capital is per venue (GUARDRAILS.md
    §1.6) — never summed across venues to size an order.

    Attributes:
        venue: `"polymarket"` or `"kalshi"`.
        available: Free USD balance, >= 0.
        locked: USD reserved against open orders/positions, >= 0.
    """

    venue: VenueId
    available: float
    locked: float

    def __post_init__(self) -> None:
        """Validate `available` and `locked` are finite and non-negative."""
        _check_size(self.available, field="available")
        _check_size(self.locked, field="locked")


@dataclass(frozen=True)
class Position:
    """An open position in one (market, outcome) on one venue.

    Attributes:
        venue: `"polymarket"` or `"kalshi"`.
        market_id: Venue-native market identifier.
        outcome: Outcome held, e.g. `"YES"`/`"NO"`.
        size: Contracts held, >= 0 (neither venue supports naked shorts —
            PLAN.md §3 — so a negative position never occurs).
        avg_price: Size-weighted average entry price, a probability in
            [0.0, 1.0].
    """

    venue: VenueId
    market_id: str
    outcome: str
    size: float
    avg_price: float

    def __post_init__(self) -> None:
        """Validate `size` and `avg_price`."""
        _check_size(self.size, field="size")
        _check_price(self.avg_price, field="avg_price")
