"""Deterministic simulated fill engine (PLAN.md D5, T07).

THE BACKTESTER AND THE PAPER TRADER SHARE THIS ONE ENGINE. `engine.py`
replaces `_apply_slippage` with it (T08) and `PaperVenueAdapter` answers
`place_order` from it (T13), so an edge that survives backtest but not
paper is a data problem, never a "the two simulators disagree" problem
(PLAN.md D5). Every sign, unit, and rounding decision in this module is
therefore load-bearing in both places at once.

Units (GUARDRAILS.md §4): prices are probabilities in `[0.0, 1.0]` on
BOTH venues; sizes are CONTRACTS, each paying $1.00 at resolution; fees,
`total_fee`, and every other cash figure are USD. Kalshi's cents/
dollar-strings are converted at the adapter boundary and nowhere else —
nothing in this module converts a price or a size.

What this engine models
-----------------------
- **Taking liquidity only.** Every `Fill` it produces is
  `liquidity="taker"`, and fees are charged at the venue's taker rate.
  Maker fills — resting an order, earning queue priority, being filled by
  someone else's aggression — are the `OrderRouter`'s business in T14 and
  are NOT simulated here. A `post_only` order (which by definition must
  never take) therefore returns `"unfilled"` with a WARNING, rather than
  being silently filled at the touch as if it had crossed.
- **Depth-walking with partial fills.** The walk is delegated to
  `OrderBook.walk()` (T04) — the single depth-consuming primitive in the
  codebase — after filtering the book to the levels the order's limit
  price actually permits. Slippage falls out of the walk; it is never a
  fixed constant (PLAN.md R4: never silently fill the whole size at the
  top level).
- **Per-fill fees.** `FeeModel.fee()` is called ONCE PER FILL and the
  results are summed. This is not a stylistic choice: `KalshiFeeModel`
  applies its ceiling (6dp, then up to a whole cent for non-direct
  members) PER FILL, so a 3-level walk on Kalshi genuinely costs strictly
  more than one aggregate call on the same total size (measured: levels
  `(0.40, 100)`, `(0.41, 100)`, `(0.42, 50)` at rate 0.07 cost
  `1.68 + 1.70 + 0.86 = 4.24`, while one call for 250 contracts at 0.40
  costs `4.20`). Calling `fee()` once with an order's aggregate size
  understates cost by up to Nx on an N-level walk — i.e. it manufactures
  edge that does not exist.

What this engine does NOT model
-------------------------------
- Resting orders. A `"GTC"` residual is REPORTED as `remaining_size`; the
  engine never queues it. The router (T14) decides what to do with it.
- Latency. `latency_ms` is recorded on every `Fill` as
  `metadata["latency_ms"]` and is otherwise informational: it does not
  move `Fill.ts` and does not re-price the fill. The real defense against
  same-snapshot look-ahead is `BacktestConfig.fill_at="next"` (T08,
  PLAN.md D6), not a latency fudge.
- Queue position, adverse selection, or any stochastic effect. The
  engine is FULLY DETERMINISTIC: `rng_seed` is reserved for the day one
  of those is added and is NEVER consulted today, so two engines
  differing only in `rng_seed` return byte-identical results. Do not
  read a `rng_seed` argument as a promise of variation.
- Self-trade prevention, margin, or capital checks (the `CapitalLedger`
  in T14 owns those).

Why nothing fills: `FillResult.reason`
--------------------------------------
An `"unfilled"` result is never a bare zero. Every one carries a
machine-readable `reason` from `FillReason`, because the router (T14)
must distinguish "the book was too thin this second, try again" from
"this order is structurally rejected, retrying forever is a bug". The
engine also TALLIES those reasons (`unfilled_counts`,
`crossed_book_skips`) so a replay (T08) and the edge-decay report (T22)
can quote how much of a run was declined and why, instead of that
number existing only in a log line nobody aggregates.

Refusing to fabricate profit
----------------------------
Two refusals in this module exist purely because their absence
MANUFACTURES MONEY, not because they are tidy:

- **Crossed books are never filled.** A book whose best bid is strictly
  above its best ask (`bids=[(0.60, 100)]`, `asks=[(0.55, 100)]`) offers
  a riskless $5.00 on a 100-lot — buy the 0.55 ask, hit the 0.60 bid. It
  is not an opportunity, it is a bad row: stale, mis-keyed, or two
  venues' quotes merged. Walking it in a backtest accumulates pure
  fabricated profit, so `fill()` declines with
  `reason="crossed_book"` and counts the skip. A LOCKED book
  (`best_bid == best_ask`) is legal and fills normally — only a STRICT
  cross is refused.
- **A free fill must be declared.** A `FeeSchedule` whose `taker_rate`
  is `0.0` is legitimate (Polymarket's Geopolitics category really is
  0.0; a Kalshi fee waiver is real) — but it is also exactly the shape
  of a market-keying bug, and a fee-eaten edge with the fee removed
  looks profitable. So a zero taker rate is accepted only when
  `FeeSchedule.source` DECLARES it (`_ZERO_RATE_DECLARED_SOURCES`), and
  otherwise fills with a WARNING naming the venue and market plus an
  entry in `undeclared_zero_fee_markets`. It is the same invariant the
  missing-`FeeModel` path already defends (GUARDRAILS.md §1.5).
"""
import logging
import math
import random
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Literal, get_args

from app.strategies.base import MarketSnapshot, outcome_key
from app.utils.time import ensure_aware
from app.venues.base import FeeModel
from app.venues.types import (
    DEPTH_SOURCE_KEY,
    BookLevel,
    FeeSchedule,
    Fill,
    OrderBook,
    OrderRequest,
    VenueId,
    VenueMarket,
    WalkSide,
    _check_price,
    _check_size,
)

logger = logging.getLogger(__name__)

#: Outcome of a `fill()` call. `"filled"` = the whole order size was
#: obtained; `"partial"` = some but not all of it; `"unfilled"` = none of
#: it. `FillResult.__post_init__` enforces the correspondence with
#: `remaining_size` (see there) so the two can never disagree.
FillStatus = Literal["filled", "partial", "unfilled"]

#: WHY a `fill()` call produced no fill. Every `"unfilled"` result
#: carries exactly one of these and every `"filled"`/`"partial"` result
#: carries `None` (`FillResult.__post_init__` enforces both), so a caller
#: never has to infer the cause from a tuple of zeros.
#:
#: The distinction that matters to `OrderRouter` (T14) is RETRYABLE vs
#: STRUCTURAL. `"no_eligible_levels"`, `"below_min_size"` and
#: `"fok_insufficient_depth"` are market conditions — the same order may
#: fill against the next snapshot. `"post_only_taker_engine"`,
#: `"zero_size_order"` and `"crossed_book"` are not: re-sending the
#: identical order against the identical inputs produces the identical
#: refusal, so a router that retries them loops forever.
#:
#: - `"crossed_book"`: `best_bid > best_ask` — a data-quality fault, not
#:   an arbitrage (see the module docstring). Also tallied in
#:   `SimulatedFillEngine.crossed_book_skips`.
#: - `"post_only_taker_engine"`: the order must not take, and taking is
#:   all this engine models. NOT "no liquidity" — the book may be deep.
#: - `"zero_size_order"`: `order.size` is zero; nothing was requested.
#: - `"no_eligible_levels"`: the book held nothing at or better than the
#:   limit price (an empty side, or every level worse than the limit).
#: - `"below_min_size"`: the walk succeeded but obtained less than
#:   `VenueMarket.min_size`, which the venue would reject outright.
#: - `"fok_insufficient_depth"`: a `"FOK"` order could not be completed
#:   in full, so the venue kills it rather than partially filling.
FillReason = Literal[
    "crossed_book",
    "post_only_taker_engine",
    "zero_size_order",
    "no_eligible_levels",
    "below_min_size",
    "fok_insufficient_depth",
]

#: `OrderBook.metadata` key marking a book built from quotes that were
#: already crossed at the source (`synthesize_book` always sets it, to
#: `True` or `False`; a recorded book may set it too). `fill()` honours
#: an explicit `True` here even when its own `best_bid`/`best_ask`
#: comparison cannot see the cross, because a producer knows things the
#: finished book no longer shows — so the upstream signal is never
#: weakened by the local one.
CROSSED_QUOTES_KEY = "crossed_quotes"

#: `FillResult.metadata` key recording whether `order.price` was checked
#: against a real venue tick grid (and `filled_size` against a real
#: `min_size`). `False` means `fill()` was called without a
#: `VenueMarket`, so BOTH constraints went unenforced and the fill may
#: sit at a price no venue would have accepted. Labeled rather than
#: hidden, for the same reason `depth_source` is (GUARDRAILS.md §1.7):
#: T22 can then report what fraction of a run's fills were never
#: tick-checked instead of presenting them as venue-realistic.
TICK_VALIDATED_KEY = "tick_validated"

#: `Fill.metadata` key naming the `FeeSchedule.source` the fill's fee
#: rate came from, so a $0.00 fee is traceable to the thing that declared
#: it rather than being indistinguishable from an unconfigured default.
FEE_SOURCE_KEY = "fee_source"

#: `FeeSchedule.source` values that DECLARE a zero `taker_rate` as
#: deliberate. The rule (see `SimulatedFillEngine._resolve_schedule`) is
#: PROVENANCE-based, not value-based: `0.0` is a real rate on both
#: venues, so refusing it outright would be wrong — what must never
#: happen is a zero arriving from a source that never meant to assert
#: one.
#:
#: - `"fee_waiver"`: a waiver IS the assertion that the fee is zero.
#: - `"category_table"`: Polymarket's per-category table lists
#:   Geopolitics at exactly 0.0 (`app/venues/fees.py`), an explicit
#:   published rate.
#: - `"clob_market"`: a rate carried on the venue's own market payload —
#:   the venue itself said zero, and PLAN.md §3 makes it authoritative
#:   over the category table.
#:
#: Everything else — notably `"settings_default"`, and any source a
#: caller invents — is treated as UNDECLARED: a default, a fallback, or
#: a test stub that resolved to zero by accident is the exact shape of
#: the market-keying bug this guard exists for.
_ZERO_RATE_DECLARED_SOURCES: frozenset[str] = frozenset(
    {"fee_waiver", "category_table", "clob_market"}
)

#: Absolute/relative epsilon (contracts) for "is the order complete?",
#: deliberately the SAME `max(abs, rel * requested)` shape and the same
#: `1e-9` magnitudes `OrderBook.walk()` uses for its remainder check. A
#: different tolerance here would let `walk()` consider a request
#: satisfied while this engine still reported a dust `remaining_size`,
#: producing a `"partial"` that is really a fill — the exact drift D5
#: exists to prevent. See `app/venues/types.py` for why `1e-9` sits
#: safely between float64 chained-subtraction noise and any real venue
#: `min_size`.
_SIZE_EPSILON_ABS = 1e-9
_SIZE_EPSILON_REL = 1e-9

#: Tolerance (in probability units) when comparing a book level's price
#: against the order's limit price. A limit reached by arithmetic
#: (`0.40 + 0.01 == 0.41000000000000003`) must still match a level at
#: `0.41`, and a level price carrying the same representation noise must
#: still be reachable by an exactly-`0.41` limit. `1e-12` is ten orders
#: of magnitude below one tick (`0.01`) — far too small to ever admit a
#: genuinely worse price level, and far above float64 noise at these
#: magnitudes.
_PRICE_EPSILON = 1e-12

#: Decimal places at which a limit price is de-noised before the on-tick
#: check. Same rationale as `app/venues/fees.py::_ceil_decimal`: a float
#: that IS `0.41000000000000003` as a bit pattern would otherwise fail an
#: exact `Decimal` modulo against a `0.01` tick even though it is the
#: caller's `0.41`. 12dp is six orders past a cent tick and four past the
#: finest sub-cent tick either venue quotes.
_TICK_DENOISE_PLACES = 12

#: Price floor (probability) used as the DIVISOR in `synthesize_book`'s
#: `size = liquidity_fraction * volume_24h / price`. See that function's
#: docstring for why an unfloored `1 / price` fabricates the most depth
#: exactly where a real market is thinnest. `0.01` is one cent — the
#: standard minimum tick on both venues, i.e. the lowest price at which
#: either venue normally quotes at all.
_MIN_SYNTHETIC_PRICE = 0.01

#: Contracts below which a synthesized level is not a level at all and
#: the side is left EMPTY. A market that traded nothing in 24h
#: (`volume_24h = 0`) computes a size of exactly `0.0`; emitting
#: `BookLevel(price=0.51, size=0.0)` would make it read as quotable to
#: every consumer that tests `best_ask() is not None` rather than
#: `.size > 0` — a phantom quote. The threshold is `_SIZE_EPSILON_ABS`
#: rather than a bare `== 0.0` so that a dust size (a market with
#: `volume_24h = 1e-12`) is treated the same way; it is the same
#: magnitude `OrderBook.walk()` and this engine already call "no size".
_MIN_SYNTHETIC_SIZE = _SIZE_EPSILON_ABS


def _size_tolerance(size: float) -> float:
    """Return the completeness tolerance, in contracts, for `size`.

    Args:
        size: The requested order size in contracts.

    Returns:
        float: `max(_SIZE_EPSILON_ABS, _SIZE_EPSILON_REL * size)` —
            identical in shape to `OrderBook.walk()`'s own threshold.
    """
    return max(_SIZE_EPSILON_ABS, _SIZE_EPSILON_REL * size)


@dataclass(frozen=True)
class FillResult:
    """The outcome of simulating one `OrderRequest` against one book.

    Immutable, and self-checking: `__post_init__` enforces the
    status/size invariants rather than trusting the producer, so no
    caller (this engine, a future router, a test fixture) can construct a
    result that says `"filled"` while still reporting size outstanding.

    `fills` is declared as a `tuple` and coerced from any sequence in
    `__post_init__`, matching the immutability convention every
    collection-valued field in `app/venues/types.py` follows (frozen
    dataclasses whose collections are genuinely immutable at runtime).
    Callers may build one with a plain `list`; the field comes back a
    tuple, and `list(result.fills)` is always available.

    Attributes:
        fills: The individual `Fill`s, in the order levels were consumed
            (best price first). One per book level touched.
        filled_size: Contracts obtained, in `[0, order.size]`.
        remaining_size: Contracts NOT obtained, `>= 0`. Exactly `0.0`
            when `status == "filled"` (dust is snapped, never reported).
            For `"GTC"` this is the residual the router may rest; this
            engine does not rest it.
        avg_price: SIZE-WEIGHTED average fill price, a probability in
            `[0.0, 1.0]`, or `None` if nothing filled. This is
            `sum(price * size) / sum(size)` — NOT the mean of the level
            prices. For `100 @ 0.40` plus `50 @ 0.41` it is
            `(100 * 0.40 + 50 * 0.41) / 150 = 0.4033...`, not
            `(0.40 + 0.41) / 2 = 0.405`; the naive mean understates cost
            on every multi-level fill, which manufactures edge.
        total_fee: Sum of the per-fill fees, USD, `>= 0`. **This is a
            POSITIVE COST the caller must SUBTRACT SEPARATELY; it is NOT
            baked into `avg_price`.** A BUY's cash outlay is
            `filled_size * avg_price + total_fee` and a SELL's proceeds
            are `filled_size * avg_price - total_fee`; `avg_price` is the
            raw size-weighted execution price and nothing else. Netting
            the fee into the price as well would double-count it;
            assuming it is already netted would make every fill free.
            Summed per fill, never computed from the aggregate size (see
            the module docstring's Kalshi measurement).
        status: `"filled"`, `"partial"`, or `"unfilled"`.
        levels_consumed: How many book levels were consumed (`len(fills)`;
            `0` when nothing filled).
        reason: WHY nothing filled — a `FillReason` when
            `status == "unfilled"`, and `None` for every `"filled"` or
            `"partial"` result. `__post_init__` enforces that
            correspondence, so a caller may branch on `reason is None`
            and on the specific reason without defensive checks. See
            `FillReason` for the retryable-vs-structural split T14 needs.
        metadata: Free-form provenance for the SIMULATION (as distinct
            from `Fill.metadata`, which is provenance for one execution).
            Always carries `TICK_VALIDATED_KEY`; immutable
            (`MappingProxyType`) like every other mapping in the domain
            types, so a result cannot be re-labeled after the fact.
    """

    fills: tuple[Fill, ...]
    filled_size: float
    remaining_size: float
    avg_price: float | None
    total_fee: float
    status: FillStatus
    levels_consumed: int
    reason: FillReason | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Coerce `fills`/`metadata` and enforce every stated invariant.

        Raises:
            ValueError: If any size/fee is not finite and `>= 0`, if
                `avg_price` is outside `[0.0, 1.0]`, if `status` is not a
                `FillStatus`, if `levels_consumed` disagrees with
                `len(fills)`, if the per-fill sizes/fees do not add up to
                `filled_size`/`total_fee`, if the status/size
                correspondence is violated (`"filled"` with
                `remaining_size > 0`, `"partial"` with
                `remaining_size == 0` (T07 acceptance 2), `"partial"`/
                `"filled"` with nothing filled, or `"unfilled"` with
                something filled), or if `reason` disagrees with
                `status`: an `"unfilled"` result MUST name a
                `FillReason` (an unexplained zero is what T14 cannot
                route on) and a `"filled"`/`"partial"` one must not carry
                a reason.
        """
        object.__setattr__(self, "fills", tuple(self.fills))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
        _check_size(self.filled_size, field="filled_size")
        _check_size(self.remaining_size, field="remaining_size")
        _check_size(self.total_fee, field="total_fee")
        if self.avg_price is not None:
            _check_price(self.avg_price, field="avg_price")
        if self.status not in ("filled", "partial", "unfilled"):
            raise ValueError(f"status must be a FillStatus, got {self.status!r}")
        if self.levels_consumed != len(self.fills):
            raise ValueError(
                f"levels_consumed {self.levels_consumed} != len(fills) "
                f"{len(self.fills)}"
            )
        summed_size = math.fsum(f.size for f in self.fills)
        if not math.isclose(summed_size, self.filled_size, rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError(
                f"filled_size {self.filled_size} != sum of fill sizes {summed_size}"
            )
        summed_fee = math.fsum(f.fee for f in self.fills)
        if not math.isclose(summed_fee, self.total_fee, rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError(
                f"total_fee {self.total_fee} != sum of fill fees {summed_fee}"
            )
        # The invariant T07 acceptance 2 names, enforced at the type so it
        # holds for every producer, not only for `SimulatedFillEngine`.
        if self.status == "filled" and self.remaining_size > 0.0:
            raise ValueError("status 'filled' cannot carry remaining_size > 0")
        if self.status == "partial" and self.remaining_size == 0.0:
            raise ValueError("status 'partial' cannot carry remaining_size == 0")
        if self.status in ("filled", "partial") and self.filled_size <= 0.0:
            raise ValueError(f"status {self.status!r} requires filled_size > 0")
        if self.status == "unfilled" and self.filled_size > 0.0:
            raise ValueError("status 'unfilled' cannot carry filled_size > 0")
        if self.avg_price is None and self.filled_size > 0.0:
            raise ValueError("avg_price is required when filled_size > 0")
        if self.avg_price is not None and self.filled_size <= 0.0:
            raise ValueError("avg_price must be None when nothing filled")
        # An "unfilled" result with no reason is the exact defect this
        # field was added for: a bare tuple of zeros a caller cannot act
        # on. Enforced at the type so every producer must explain itself.
        if self.status == "unfilled":
            if self.reason is None:
                raise ValueError(
                    "status 'unfilled' requires a reason; an unexplained zero "
                    f"cannot be routed (expected one of {get_args(FillReason)})"
                )
            if self.reason not in get_args(FillReason):
                raise ValueError(
                    f"reason must be a FillReason, got {self.reason!r}"
                )
        elif self.reason is not None:
            raise ValueError(
                f"status {self.status!r} must not carry a reason, got "
                f"{self.reason!r}"
            )


class SimulatedFillEngine:
    """Simulate taking liquidity from an `OrderBook`, with real fees.

    See the module docstring for what is and is not modeled (in
    particular: taker-only, no resting, no queue simulation).

    An engine instance also ACCUMULATES the two data-quality signals a
    replay needs to report on itself: `unfilled_counts` (how many orders
    were declined, by `FillReason`) and `undeclared_zero_fee_markets`
    (which markets filled at a zero fee rate nobody declared). Both are
    per-instance and monotonic; T08 builds one engine per backtest run,
    so they read as that run's totals.

    Attributes:
        latency_ms: Informational execution latency, recorded on each
            `Fill` as `metadata["latency_ms"]`. Does not shift `Fill.ts`
            and does not re-price anything.
        rng_seed: Seed for the reserved, NEVER-CONSULTED RNG. The engine
            is fully deterministic: two engines differing only in
            `rng_seed` produce identical results for identical inputs.
            Kept (rather than dropped) so the day a stochastic effect is
            added it is already reproducible — see the module docstring.
    """

    def __init__(
        self,
        fee_models: dict[VenueId, FeeModel],
        schedules: Callable[[VenueId, str], FeeSchedule],
        latency_ms: int = 0,
        rng_seed: int | None = None,
    ) -> None:
        """Configure the engine.

        Args:
            fee_models: Venue -> `FeeModel`. A venue absent from this
                mapping cannot be simulated: `fill()` raises rather than
                falling back to a zero fee, because a silently free fill
                is exactly how a fee-eaten edge looks profitable
                (GUARDRAILS.md §1.5). Copied on construction.
            schedules: `(venue, market_id) -> FeeSchedule` resolver. Also
                called per fill, so a caller may key a fee waiver or a
                per-market rate off the market id. Its RETURN VALUE is
                validated on every call (`_resolve_schedule`) — it is an
                arbitrary caller-supplied callable, and a resolver that
                mis-keys a market and lands on a zero rate silently
                deletes the cost of every trade.
            latency_ms: Informational latency in MILLISECONDS, `>= 0`.
            rng_seed: Seed for the reserved RNG; `None` seeds from the
                system entropy. NEVER consulted — the engine is fully
                deterministic and this argument changes nothing about its
                output. See the module docstring.

        Raises:
            ValueError: If `latency_ms` is negative.
        """
        if latency_ms < 0:
            raise ValueError(f"latency_ms must be >= 0, got {latency_ms!r}")
        self._fee_models: dict[VenueId, FeeModel] = dict(fee_models)
        self._schedules = schedules
        self.latency_ms = latency_ms
        self.rng_seed = rng_seed
        # Reserved for future stochastic effects (queue position, adverse
        # selection). Held so that behavior is reproducible the day one is
        # added; never consulted today.
        self._rng = random.Random(rng_seed)
        # Data-quality tallies. Exposed read-only below so a run can
        # report "N orders declined: crossed quotes" as a NUMBER rather
        # than as a log line a report cannot aggregate.
        self._unfilled_counts: Counter[str] = Counter()
        self._undeclared_zero_fee: set[tuple[VenueId, str]] = set()

    @property
    def unfilled_counts(self) -> Mapping[str, int]:
        """Return how many results this engine declined, by `FillReason`.

        Monotonic over the engine's lifetime and never reset, so a caller
        that wants per-window figures should diff two readings or build a
        fresh engine.

        Returns:
            Mapping[str, int]: Read-only `FillReason -> count`. Reasons
                that have not occurred are absent (not zero-valued).
        """
        return MappingProxyType(dict(self._unfilled_counts))

    @property
    def crossed_book_skips(self) -> int:
        """Return how many fills were declined for a crossed book.

        Named explicitly, rather than left as a lookup in
        `unfilled_counts`, because it is not a trading outcome at all: it
        counts BAD INPUT ROWS. A replay reporting "N snapshots skipped:
        crossed quotes" (T08/T22) is reporting on its data, not on its
        strategy, and that number must be as easy to reach as the P&L.

        Returns:
            int: Count of `reason == "crossed_book"` results so far.
        """
        return self._unfilled_counts["crossed_book"]

    @property
    def undeclared_zero_fee_markets(self) -> frozenset[tuple[VenueId, str]]:
        """Return the markets that filled at an UNDECLARED zero fee rate.

        Empty is the expected state and is the machine-checkable form of
        "no fill in this run was silently free". A non-empty set names
        every `(venue, market_id)` whose resolved `FeeSchedule` carried
        `taker_rate == 0.0` from a `source` outside
        `_ZERO_RATE_DECLARED_SOURCES` — each of which also emitted a
        WARNING the first time it was seen.

        Returns:
            frozenset[tuple[VenueId, str]]: `(venue, market_id)` pairs.
        """
        return frozenset(self._undeclared_zero_fee)

    def fill(
        self,
        order: OrderRequest,
        book: OrderBook,
        now: datetime,
        *,
        market: VenueMarket | None = None,
    ) -> FillResult:
        """Simulate `order` against `book` and return what it would get.

        Algorithm, in order:

        1. Reject a book that is not this order's book (venue, market id,
           outcome). Filling against the wrong market's depth is a silent
           money error, so it raises rather than returning `"unfilled"`.
        2. If `market` is supplied, require the limit price to be on
           `market.tick_size` (a venue rejects an off-tick order, so
           simulating a fill for one would invent liquidity that could
           never have been reached). When it is NOT supplied, neither
           `tick_size` nor `min_size` is enforced and the result is
           stamped `metadata[TICK_VALIDATED_KEY] = False`.
        3. Decline a CROSSED book (`best_bid > best_ask`) ->
           `"unfilled"`, `reason="crossed_book"`, a WARNING, and a
           counted skip. A crossed book is a bad row, not free money; see
           the module docstring. A LOCKED book (`best_bid == best_ask`)
           is legal and proceeds normally.
        4. `post_only` -> `"unfilled"` plus a WARNING: this engine models
           taking liquidity only.
        5. Restrict the book to the levels the limit permits — BUY keeps
           `asks` with `price <= order.price`, SELL keeps `bids` with
           `price >= order.price` — and hand that restricted book to
           `OrderBook.walk()`, which consumes best-price-first and never
           over-fills. The walk is NOT re-implemented here (PLAN.md D5:
           one primitive, no divergent copies).
        6. Drop the whole fill if the walked quantity is below
           `market.min_size`, or — for `"FOK"` — below the full order
           size. `"IOC"`/`"GTC"` keep whatever was walked; a `"GTC"`
           residual is reported, never rested.
        7. Charge `FeeModel.fee(..., liquidity="taker")` ONCE PER LEVEL
           and sum (see the module docstring's Kalshi measurement),
           against a `FeeSchedule` whose shape and zero-rate provenance
           are validated first.

        Every early exit above returns `"unfilled"` with a `reason`
        naming which one it was; nothing here returns an unexplained
        zero.

        Args:
            order: The order to simulate. `order.price` is the LIMIT, not
                a market order — this engine has no market-order mode,
                because an unbounded walk down a synthesized book is how
                a backtest invents fills at prices that never existed.
            book: The book to fill against, for the same
                (venue, market_id, outcome). Its
                `metadata["depth_source"]` is propagated onto every
                resulting `Fill` (GUARDRAILS.md §1.7).
            now: Aware UTC timestamp stamped on every resulting `Fill`.
            market: The market's metadata, when the caller has it. It is
                the ONLY source of `tick_size`/`min_size` (they live on
                `VenueMarket`, PLAN.md D3) — when it is `None` those two
                venue constraints are not enforced, because the engine
                will not guess a microstructure it was not told (a
                replayed price is genuinely off-grid, and defaulting to a
                0.01 tick would make T08 raise constantly). That
                unverified assumption is LABELED, not hidden: the result
                carries `metadata[TICK_VALIDATED_KEY] = False`, so T22
                can report what share of a run's fills were never
                tick-checked. Callers that have a `VenueMarket` (T13/T14,
                via `get_market`) should pass it; the backtester (T08),
                which replays `MarketSnapshot`s, generally cannot.

        Returns:
            FillResult: What the order would have obtained, always
                carrying `metadata[TICK_VALIDATED_KEY]` and — when
                nothing filled — a `reason`.

        Raises:
            ValueError: If `now` is naive; if `book` is not this order's
                book; if `market` is given and `order.price` is off-tick;
                if no `FeeModel` is configured for `order.venue`; or if
                `schedules()` does not return a `FeeSchedule`.
            TypeError: If `now` is not a `datetime`.
        """
        ensure_aware(now)
        self._check_book_matches(order, book)
        tick_validated = market is not None
        if market is not None:
            _check_on_tick(order.price, market.tick_size)
        min_size = market.min_size if market is not None else 0.0

        if _is_crossed(book):
            # A crossed book is free money: buy the ask, hit the higher
            # bid. It is never a real opportunity — it is a stale,
            # mis-keyed, or merged quote row — so filling it would credit
            # a backtest with profit that never existed. Declining does
            # NOT raise: T08 replays real history and one bad row must
            # not abort the run. The skip is counted instead, so the run
            # can report its own data quality.
            best_bid = book.best_bid()
            best_ask = book.best_ask()
            logger.warning(
                "crossed book not filled: best_bid > best_ask is a data fault, "
                "not an arbitrage; declining to simulate a riskless fill",
                extra={
                    "client_order_id": order.client_order_id,
                    "venue": order.venue,
                    "market_id": order.market_id,
                    "outcome": order.outcome,
                    "best_bid": best_bid.price if best_bid is not None else None,
                    "best_ask": best_ask.price if best_ask is not None else None,
                    DEPTH_SOURCE_KEY: book.depth_source,
                },
            )
            return self._unfilled(
                order, "crossed_book", tick_validated=tick_validated
            )

        if order.post_only:
            # A post_only order must never take liquidity, and taking is
            # the only thing this engine models. Returning "unfilled"
            # (rather than raising) keeps a paper-trading run alive, but
            # it is loud: a maker strategy that appears to never fill in
            # paper mode must be traceable to this line, not to a
            # mysterious zero.
            logger.warning(
                "post_only order not simulated: SimulatedFillEngine models taking "
                "liquidity only; maker fills are the router's business (T14)",
                extra={
                    "client_order_id": order.client_order_id,
                    "venue": order.venue,
                    "market_id": order.market_id,
                },
            )
            return self._unfilled(
                order, "post_only_taker_engine", tick_validated=tick_validated
            )

        if order.size <= 0.0:
            # Nothing was requested, so nothing is missing: distinguished
            # from "no liquidity" so a router does not treat an empty
            # request as a market condition worth retrying.
            return self._unfilled(
                order, "zero_size_order", tick_validated=tick_validated
            )

        walk_side, eligible = self._restrict_to_limit(order, book)
        walked = eligible.walk(walk_side, order.size)
        filled_size = math.fsum(size for _, size in walked)
        tolerance = _size_tolerance(order.size)

        if filled_size <= 0.0:
            return self._unfilled(
                order, "no_eligible_levels", tick_validated=tick_validated
            )
        if filled_size < min_size - tolerance:
            # The venue would reject a fill smaller than its minimum, so
            # the honest simulation is that nothing trades — not that a
            # sub-minimum position quietly appears in the ledger.
            logger.debug(
                "fill dropped: walked size below venue min_size",
                extra={
                    "client_order_id": order.client_order_id,
                    "walked_size": filled_size,
                    "min_size": min_size,
                },
            )
            return self._unfilled(
                order, "below_min_size", tick_validated=tick_validated
            )
        if order.tif == "FOK" and filled_size < order.size - tolerance:
            # All-or-nothing: the book could not supply the whole size at
            # or better than the limit, so the venue kills the order.
            return self._unfilled(
                order, "fok_insufficient_depth", tick_validated=tick_validated
            )

        fills = self._build_fills(order, book, walked, now)
        # Size-weighted, not the mean of level prices (see FillResult).
        avg_price = math.fsum(price * size for price, size in walked) / filled_size
        # Clamp only against 1-ulp overshoot: every input price was already
        # validated into [0, 1] by `BookLevel`, so a weighted average of
        # them cannot genuinely leave the range.
        avg_price = min(1.0, max(0.0, avg_price))
        total_fee = math.fsum(f.fee for f in fills)

        remaining_size = max(0.0, order.size - filled_size)
        if remaining_size <= tolerance:
            # Snap dust: a residual of 1e-17 contracts is a float
            # artifact, not an unfilled order. Without this the result
            # would report "partial" for a fill that took the entire
            # requested size (T07 acceptance 2).
            remaining_size = 0.0
            status: FillStatus = "filled"
        else:
            status = "partial"

        return FillResult(
            fills=fills,
            filled_size=filled_size,
            remaining_size=remaining_size,
            avg_price=avg_price,
            total_fee=total_fee,
            status=status,
            levels_consumed=len(fills),
            metadata={TICK_VALIDATED_KEY: tick_validated},
        )

    def _unfilled(
        self, order: OrderRequest, reason: FillReason, *, tick_validated: bool
    ) -> FillResult:
        """Build — and TALLY — the "nothing traded" result for `order`.

        Every refusal in `fill()` goes through here, so the counters can
        never drift from the results that produced them.

        Args:
            order: The order that did not fill.
            reason: Why it did not (see `FillReason`).
            tick_validated: Whether a `VenueMarket` was available to
                enforce `tick_size`/`min_size`.

        Returns:
            FillResult: `status="unfilled"`, no fills, no fee, the entire
                order size still outstanding, and the reason attached.
        """
        self._unfilled_counts[reason] += 1
        return FillResult(
            fills=(),
            filled_size=0.0,
            remaining_size=order.size,
            avg_price=None,
            total_fee=0.0,
            status="unfilled",
            levels_consumed=0,
            reason=reason,
            metadata={TICK_VALIDATED_KEY: tick_validated},
        )

    def _check_book_matches(self, order: OrderRequest, book: OrderBook) -> None:
        """Raise unless `book` is the book for `order`'s (venue, market, outcome).

        Outcome names are compared on `outcome_key()` identity — the
        kit's single canonicalization for outcome IDENTITY
        (`app/strategies/base.py`, T21d) — rather than a bare
        `casefold()`, because the two venues disagree on casing for the
        same outcome (Polymarket `"Yes"`, Kalshi `"yes"`) AND a book or
        order can carry incidental surrounding whitespace (e.g. a
        scanner/adapter payload spelling one outcome `"Trump "`). A
        YES-vs-NO or genuine-label mismatch is still caught, which is
        the one that would book a position on the wrong side; only a
        casing or whitespace difference is now tolerated (T21f).

        Args:
            order: The order being simulated.
            book: The book it is being simulated against.

        Raises:
            ValueError: If venue, market id, or outcome disagree.
        """
        if order.venue != book.venue:
            raise ValueError(
                f"book venue {book.venue!r} does not match order venue {order.venue!r}"
            )
        if order.market_id != book.market_id:
            raise ValueError(
                f"book market_id {book.market_id!r} does not match order market_id "
                f"{order.market_id!r}"
            )
        if outcome_key(order.outcome) != outcome_key(book.outcome):
            raise ValueError(
                f"book outcome {book.outcome!r} does not match order outcome "
                f"{order.outcome!r}"
            )

    def _restrict_to_limit(
        self, order: OrderRequest, book: OrderBook
    ) -> tuple[WalkSide, OrderBook]:
        """Return the walk side and a copy of `book` limited to eligible levels.

        This is how the limit price is expressed WITHOUT writing a second
        walk loop: `OrderBook.walk()` has no limit-price parameter, so
        the ineligible levels are removed from the book first and the
        untouched, already-correct primitive does the consuming. Because
        `OrderBook` normalizes to best-price-first on construction, the
        eligible levels are always a prefix of the walk order, so
        filtering cannot reorder or skip depth.

        Args:
            order: The order supplying the side and the limit price.
            book: The full book.

        Returns:
            tuple[WalkSide, OrderBook]: The `walk()` side, and a book
                whose opposing side holds only levels at or better than
                the limit.
        """
        if order.side == "BUY":
            asks = tuple(
                lvl for lvl in book.asks if lvl.price <= order.price + _PRICE_EPSILON
            )
            return "buy", replace(book, asks=asks)
        bids = tuple(
            lvl for lvl in book.bids if lvl.price >= order.price - _PRICE_EPSILON
        )
        return "sell", replace(book, bids=bids)

    def _build_fills(
        self,
        order: OrderRequest,
        book: OrderBook,
        walked: Sequence[tuple[float, float]],
        now: datetime,
    ) -> tuple[Fill, ...]:
        """Turn walked `(price, size)` pairs into `Fill`s, fee'd per fill.

        The fee model is called once per pair. See the module docstring:
        on Kalshi the per-fill ceiling makes this strictly more expensive
        than one aggregate call, and that is the real cost.

        Args:
            order: The order being filled.
            book: The book walked (its `depth_source` is propagated).
            walked: `(price, size)` pairs from `OrderBook.walk()`.
            now: Aware UTC fill timestamp.

        Returns:
            tuple[Fill, ...]: One `Fill` per consumed level, in order.

        Raises:
            ValueError: If no `FeeModel` is configured for the venue, or
                if `schedules()` did not return a `FeeSchedule`.
        """
        fee_model = self._fee_models.get(order.venue)
        if fee_model is None:
            raise ValueError(
                f"no FeeModel configured for venue {order.venue!r}; refusing to "
                "simulate a fill with an unknown (implicitly free) fee"
            )
        schedule = self._resolve_schedule(order)
        metadata: dict[str, Any] = {
            # Propagated so a fill walked out of invented depth stays
            # labeled all the way into T22's report (GUARDRAILS.md §1.7).
            DEPTH_SOURCE_KEY: book.depth_source,
            "latency_ms": self.latency_ms,
            # `Fill` carries no market/outcome field; without these the
            # ledger cannot tell which position a fill belongs to.
            "market_id": order.market_id,
            "outcome": order.outcome,
            # Same labeling philosophy as `depth_source`: a $0.00 fee is
            # only defensible if you can see WHAT declared it.
            FEE_SOURCE_KEY: schedule.source,
        }
        return tuple(
            Fill(
                venue=order.venue,
                # The simulated venue mirrors the client's idempotency key
                # back as the order id — there is no venue-side id to
                # invent, and inventing a random one would make paper
                # fills unreconcilable against the order that caused them.
                order_id=order.client_order_id,
                price=price,
                size=size,
                fee=fee_model.fee(price, size, "taker", schedule),
                ts=now,
                liquidity="taker",
                metadata=metadata,
            )
            for price, size in walked
        )

    def _resolve_schedule(self, order: OrderRequest) -> FeeSchedule:
        """Call `schedules()` and refuse to trust its answer blindly.

        `schedules` is an arbitrary caller-supplied callable and its
        result multiplies every fee in the fill. Two failure modes are
        checked, and they are deliberately treated DIFFERENTLY:

        1. **Wrong type (including `None`) -> `ValueError`.** A resolver
           that returns nothing is broken, not permissive, and the old
           behavior was an `AttributeError` raised deep inside the fee
           model — a stack trace that blamed the wrong component. This
           mirrors the missing-`FeeModel` refusal above: an unknown fee
           is never simulated as a free one (GUARDRAILS.md §1.5).
        2. **A zero `taker_rate` -> allowed only if DECLARED, else a
           WARNING.** It must NOT raise: `0.0` is a real, published rate
           (Polymarket Geopolitics) and a Kalshi fee waiver is real, so
           raising would make legitimate markets unbacktestable — the
           same reason a crossed row is skipped rather than fatal. But it
           must not pass silently either, because a resolver that
           mis-keys a market and lands on a waiver produces exactly this
           value, and deleting the fee is how a fee-eaten edge starts
           looking profitable (measured on the canonical 150-lot walk:
           `$1.8047` becomes `$0.0000`). The rule is therefore
           PROVENANCE, not value: `FeeSchedule.source` must be one of
           `_ZERO_RATE_DECLARED_SOURCES` — a source that asserts the zero
           on purpose. Anything else (a `"settings_default"` that fell
           through, a stub) warns once per `(venue, market_id)` naming
           both, and is recorded in `undeclared_zero_fee_markets`, so a
           free fill is always an explicit, auditable decision rather
           than a default nobody noticed.

        A zero `maker_rate` is NOT checked: this engine only ever charges
        the taker rate, so the maker rate cannot affect a number it
        produces.

        Args:
            order: The order whose `(venue, market_id)` keys the lookup.

        Returns:
            FeeSchedule: The validated schedule to fee this fill with.

        Raises:
            ValueError: If `schedules()` returned anything other than a
                `FeeSchedule`.
        """
        schedule = self._schedules(order.venue, order.market_id)
        if not isinstance(schedule, FeeSchedule):
            raise ValueError(
                f"schedules() must return a FeeSchedule for venue {order.venue!r} "
                f"market {order.market_id!r}, got {type(schedule).__name__}; "
                "refusing to simulate a fill with an unknown (implicitly free) fee"
            )
        declared = schedule.source in _ZERO_RATE_DECLARED_SOURCES
        if schedule.taker_rate == 0.0 and not declared:
            key = (order.venue, order.market_id)
            if key not in self._undeclared_zero_fee:
                # Warn once per market: a replay calls this per snapshot,
                # and a warning repeated 100k times is as unreadable as
                # no warning at all. The set below keeps the signal.
                logger.warning(
                    "undeclared zero taker fee on venue %r market %r: FeeSchedule "
                    "source %r is not one of %s; this fill is FREE and that is "
                    "almost certainly a fee-resolution bug, not a real waiver",
                    order.venue,
                    order.market_id,
                    schedule.source,
                    sorted(_ZERO_RATE_DECLARED_SOURCES),
                    extra={
                        "venue": order.venue,
                        "market_id": order.market_id,
                        "fee_source": schedule.source,
                        "taker_rate": schedule.taker_rate,
                    },
                )
                self._undeclared_zero_fee.add(key)
        return schedule


def _is_crossed(book: OrderBook) -> bool:
    """Return whether `book`'s quotes are CROSSED (not merely locked).

    Crossed means `best_bid.price > best_ask.price` STRICTLY: someone is
    bidding more than someone else is asking, which is riskless profit
    and therefore a data fault rather than a market. LOCKED
    (`best_bid == best_ask`) is a legal, routinely-observed state — a
    zero spread, not free money — and is deliberately NOT treated as
    crossed; refusing to fill locked books would silently drop the
    tightest (and most tradeable) rows in the history.

    The comparison carries `_PRICE_EPSILON` so a lock reconstructed
    through float arithmetic is never misread as a one-ulp cross.

    An explicit `metadata[CROSSED_QUOTES_KEY] is True` also counts, even
    when this function's own comparison cannot see the cross. A producer
    knows things the finished book no longer shows — a recorded book
    (T21) assembled from two reads, or one whose crossing side was
    filtered out on the way in — so the upstream tag is treated as a
    superset of the local comparison, never overridden by it.

    Args:
        book: The book about to be walked.

    Returns:
        bool: `True` if the book must not be filled against.
    """
    if book.metadata.get(CROSSED_QUOTES_KEY) is True:
        return True
    bid = book.best_bid()
    ask = book.best_ask()
    if bid is None or ask is None:
        return False
    return bid.price > ask.price + _PRICE_EPSILON


def _check_on_tick(price: float, tick_size: float) -> None:
    """Raise unless `price` is an exact multiple of `tick_size`.

    Uses `Decimal` rather than a float modulo: `0.03 % 0.01` is
    `0.009999999999999998` in binary floating point, which no tolerance
    on a float modulo reads cleanly. `price` is first de-noised to
    `_TICK_DENOISE_PLACES` so a limit that arrived via arithmetic
    (`0.40 + 0.01`) is judged as the value the caller meant.

    Args:
        price: Limit price, a probability in [0.0, 1.0].
        tick_size: Minimum price increment, in (0.0, 1.0].

    Raises:
        ValueError: If `price` is not on the tick grid.
    """
    quantized = Decimal(str(round(price, _TICK_DENOISE_PLACES)))
    tick = Decimal(str(tick_size))
    if quantized % tick != 0:
        raise ValueError(
            f"limit price {price!r} is not a multiple of tick_size {tick_size!r}"
        )


def synthesize_book(
    snapshot: MarketSnapshot,
    liquidity_fraction: float,
    *,
    outcome: str = "YES",
) -> OrderBook:
    """Invent a one-level-per-side book from a top-of-book-only snapshot.

    THIS FUNCTION FABRICATES LIQUIDITY THAT WAS NEVER OBSERVED.
    `PriceHistory` stores only top-of-book, so until real `book_snapshots`
    accumulate (PLAN.md D10) a backtest has no depth to walk. Rather than
    pretend the top level is infinitely deep (which would fill any size at
    the touch and invent an edge at every capital level — PLAN.md R4),
    this synthesizes a single level per side whose size is derived from
    traded volume, and stamps the book `depth_source="synthetic"` so every
    `Fill` and every downstream report is labeled (GUARDRAILS.md §1.7).

    Size formula (T07 brief):
    `size_contracts = liquidity_fraction * volume_24h / price`.
    `volume_24h` is USD notional, so dividing by the level's price
    converts it to CONTRACTS — that division is a unit conversion, not a
    weighting. (PLAN.md D6 writes the same rule in shorthand, without the
    `/ price`; the brief's form is the dimensionally correct one, since
    `liquidity_fraction * volume_24h` is dollars and a book level is
    measured in contracts.)

    **The `1 / price` blow-up, and the floor applied to it.** As
    `price -> 0` the formula fabricates unbounded depth: at
    `volume_24h = 50_000`, `liquidity_fraction = 0.02`, a `0.001` price
    yields `1,000,000` contracts resting — a million dollars of notional
    conjured onto a market that traded fifty thousand — and `price = 0.0`
    is an outright `ZeroDivisionError`. That is fabricated depth exactly
    where a real market is thinnest (deep-out-of-the-money longshots are
    the *least* liquid, not the most), which would let a backtest fill an
    arbitrarily large tail position for free. So the DIVISOR is floored at
    `_MIN_SYNTHETIC_PRICE` (0.01, one cent — the standard minimum tick on
    both venues), bounding synthetic depth at
    `100 * liquidity_fraction * volume_24h` contracts. The level's own
    price is NOT altered — only the divisor — and when the floor bites the
    book records `metadata["synthetic_price_floor_applied"] = True` so a
    sweep can see which markets got the capped treatment.

    **Crossed quotes are propagated, never repaired.** A stale or
    mis-keyed `PriceHistory` row can carry `yes_bid=0.60, yes_ask=0.40`.
    `MarketSnapshot` accepts that (it is legitimate historical data —
    what was recorded IS what was recorded) and this function does not
    swap, average, or drop the quotes, because inventing a corrected
    price is a worse lie than the bad row. Instead the resulting book is
    tagged `metadata[CROSSED_QUOTES_KEY] = True` (the key is always
    present, `True` or `False`), which is the signal
    `SimulatedFillEngine.fill()` uses to decline the fill outright — a
    crossed synthetic book would otherwise be walked at the free-money
    spread, and every one of those dollars would be fabricated.

    **A zero-size side is no side.** `volume_24h = 0` (a market that
    traded nothing in 24h) or `liquidity_fraction = 0` makes the computed
    size `0.0`. That side is then OMITTED rather than emitted as
    `BookLevel(price=..., size=0.0)`: a zero-size level reads as quotable
    to every consumer that tests `best_ask() is not None` instead of
    `.size`, which is a phantom quote. A NEGATIVE `liquidity_fraction`
    raises instead of being clamped — it cannot come from data, only from
    a mis-set `settings.liquidity_fraction`, and a configuration error
    should be fixed rather than silently absorbed into an empty book.

    Args:
        snapshot: The top-of-book snapshot. `yes_bid`/`yes_ask` (or
            `no_bid`/`no_ask`) supply the prices, `volume_24h` the
            notional, `timestamp` the book `ts`, `venue`/`market_id` the
            identity. A side whose price is `None` yields NO level on
            that side — a missing NO quote is never derived as
            `1 - yes_*`, which would stack an invented price on top of
            invented depth.
        liquidity_fraction: Fraction of 24h notional assumed to be
            resting at the touch, `>= 0` (`settings.liquidity_fraction`,
            default 0.02). `0.0` is legal and means "assume no resting
            liquidity": both sides come back empty and every order goes
            `"unfilled"` with `reason="no_eligible_levels"`. Negative
            raises.
        outcome: `"YES"` or `"NO"` in any spelling `outcome_key()`
            resolves to that identity (case-insensitive, surrounding
            whitespace tolerated — T21f), selecting which pair of
            quotes to use. Recorded on the book VERBATIM (this
            function's own raw argument, not the `outcome_key()`
            result), so the caller's own outcome spelling is preserved
            for `SimulatedFillEngine.fill()`'s book/order match, which
            itself now also compares on `outcome_key()`. Keyword-only
            with a `"YES"` default so the brief's two-argument call still
            works; `MarketSnapshot` itself carries no outcome field,
            which is why this cannot be inferred.

    Returns:
        OrderBook: One bid level and one ask level (either may be
            absent — a missing quote or a zero computed size), tagged
            `depth_source="synthetic"` and `CROSSED_QUOTES_KEY`, along
            with the `liquidity_fraction` and `volume_24h` it was
            invented from.

    Raises:
        ValueError: If `liquidity_fraction` or `snapshot.volume_24h` is
            not finite and `>= 0`, if `outcome` is neither YES nor NO, or
            if a quoted price is not a finite probability in [0, 1].
    """
    _check_size(liquidity_fraction, field="liquidity_fraction")
    _check_size(snapshot.volume_24h, field="volume_24h")
    key = outcome_key(outcome)
    if key == "YES":
        bid_price, ask_price = snapshot.yes_bid, snapshot.yes_ask
    elif key == "NO":
        bid_price, ask_price = snapshot.no_bid, snapshot.no_ask
    else:
        raise ValueError(f"outcome must be 'YES' or 'NO', got {outcome!r}")

    notional_usd = liquidity_fraction * snapshot.volume_24h
    floor_applied = False

    def _level(price: float, field_name: str) -> tuple[BookLevel, ...]:
        """Build one synthetic level, flooring the divisor (not the price).

        Returns an EMPTY tuple — no level — when the computed size
        rounds to zero, so a side is absent rather than phantom.
        """
        nonlocal floor_applied
        _check_price(price, field=field_name)
        divisor = price
        if divisor < _MIN_SYNTHETIC_PRICE:
            divisor = _MIN_SYNTHETIC_PRICE
            floor_applied = True
        size = notional_usd / divisor
        if size < _MIN_SYNTHETIC_SIZE:
            return ()
        return (BookLevel(price=price, size=size),)

    bids = _level(bid_price, "yes_bid/no_bid") if bid_price is not None else ()
    asks = _level(ask_price, "yes_ask/no_ask") if ask_price is not None else ()

    # Judged on the QUOTES, not on the surviving levels: a side dropped
    # for zero size must not launder a crossed row into a clean-looking
    # one-sided book.
    crossed = (
        bid_price is not None
        and ask_price is not None
        and bid_price > ask_price + _PRICE_EPSILON
    )
    if crossed:
        logger.warning(
            "synthesized book from CROSSED quotes: bid %r > ask %r; tagging "
            "%s so the fill engine declines it (the quotes are NOT repaired)",
            bid_price,
            ask_price,
            CROSSED_QUOTES_KEY,
            extra={
                "venue": snapshot.venue,
                "market_id": snapshot.market_id,
                "outcome": outcome,
            },
        )

    return OrderBook(
        venue=snapshot.venue,
        market_id=snapshot.market_id,
        outcome=outcome,
        bids=bids,
        asks=asks,
        ts=snapshot.timestamp,
        metadata={
            DEPTH_SOURCE_KEY: "synthetic",
            "liquidity_fraction": liquidity_fraction,
            "volume_24h": snapshot.volume_24h,
            "synthetic_price_floor": _MIN_SYNTHETIC_PRICE,
            "synthetic_price_floor_applied": floor_applied,
            CROSSED_QUOTES_KEY: crossed,
        },
    )
