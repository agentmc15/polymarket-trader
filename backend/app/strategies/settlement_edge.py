"""Settlement-edge strategy: a capital-lockup trade, NOT an information edge.

PLAN.md D10(c) / R2. This strategy does not find a market inefficiency.
Once `outcome_determined` is true (the market's own price already says
one side is ~certain, and the scheduled event date has passed), buying
that side at 0.98 is not a bet that the crowd is wrong — it is LENDING
$0.98 against an outcome the market already believes, to collect the
$0.02 residual. That $0.02 exists for exactly two reasons, and both are
COSTS, not mispricings: (a) somebody has to wait out the venue's
settlement/redemption process before the $1.00 actually arrives, and (b)
there is a real, if small, chance the market's belief is WRONG — a
disputed UMA proposal, a Kalshi determination that goes the other way, a
market voided or annulled. Size this like a savings account paying a
few points of yield with a tail risk of total loss, never like an
arbitrage: a strategy that finds a genuine inefficiency should get MORE
confident as its edge grows, but here a large residual (an ask far below
0.98) is usually a sign the market itself is NOT yet confident, i.e. that
`outcome_determined` fired on noise, not that the trade is better.

ITS RISK IS SETTLEMENT RISK, WHICH IS NOT PRICE RISK (PLAN.md R2). Being
right about the outcome and being PAID for it are different events.
Polymarket resolves through UMA's optimistic oracle: a proposed outcome
sits in a challenge window (order of a couple of hours) during which
ANYONE can dispute it, and a disputed market can then take days to
settle — this is exactly what `in_dispute_window` encodes, and it is
why `allow_dispute_window` defaults `False`: during that window, the
"determined" outcome is precisely the thing under contest, and trading
through it is buying a lawsuit, not a near-certainty. Kalshi's own
determination has an analogous settlement timer. `min_viable_annualized`
(from `Settings`, never a literal here) is what keeps this trade honest:
it is the one number standing between "yes, the residual clears the
real cost of the wait" and "no, tie up capital for weeks to earn three
basis points". A strategy that is systematically short a small, rare,
total loss is the classic shape that looks profitable for a long run of
history and then is not — `min_viable_annualized` is the discipline that
keeps this one from being sized as if that could not happen here.

`outcome_determined` (price >= 0.97 or <= 0.03, scheduled close passed)
deserves SCEPTICISM, not trust, for the same reason: 0.97 is the
market's OWN estimate of the chance it is wrong, not a certificate that
it is right. A market sitting at 0.97 one hour after its scheduled close
is telling you "the crowd thinks there is about a 3% chance this goes
the other way, plus however much of the residual is really just the
time-value of the wait" — not "this is over". This strategy trades that
number at face value only because `min_viable_annualized` demands the
residual be big enough to be worth the wait AFTER fees, never because
0.97 is treated as 1.00.

THE VENUE ASYMMETRY IN THIS BUCKET IS DRIVEN BY FILL FRAGMENTATION, NOT
BY A FLAT PER-CONTRACT PENALTY — measured against this repo's own fee
models (`app/venues/fees.py`), not assumed. Polymarket's
`size * rate * price * (1 - price)` fee is FLAT IN FILL COUNT and
LINEAR in size: summing it over any number of fills that add up to the
same total size gives the identical total (100 contracts at p=0.98
costs `100 * 0.05 * 0.98 * 0.02 = $0.098` in one fill AND in a hundred
1-contract fills — the same $0.098 either way; it also collapses at the
tails vs the middle, `$0.098` at p=0.98 vs `100 * 0.05 * 0.5 * 0.5 =
$1.25` at p=0.50). Kalshi's per-fill $0.01 floor
(`KalshiFeeModel`'s `round_net_to_cents`) is the OPPOSITE: it is charged
PER FILL, so it is HOW an order fills, not merely the price it fills
at, that decides the cost — three numbers at the identical price
(p=0.995, default 0.07 Kalshi rate vs 0.05 Polymarket rate), same 100
contracts, three different fragmentations:

    one 1-contract fill:    Kalshi $0.01000  |  Polymarket $0.00024875  (ratio ~40x)
    one 100-contract fill:  Kalshi $0.04000  |  Polymarket $0.02487500  (ratio ~1.6x)
    a hundred 1-ct fills:   Kalshi $1.00000  |  Polymarket $0.02487500  (ratio ~40x)

The SAME $0.01-per-fill number that looks like "a ~40x tax" at a single
contract almost disappears (~1.6x) when the identical 100 contracts
fill in ONE level, and roars back to ~40x — on the SAME notional — the
moment that fill fragments into a hundred pieces. Polymarket's total is
IDENTICAL in all three rows; Kalshi's is not. So there is no single
"Kalshi is Nx more expensive" number to quote: the near-resolution
bucket is structurally cheap on Polymarket regardless of how an order
fills, and on Kalshi it is the THINNESS of the book — how many separate
fills a real order takes to fill, not the headline price — that decides
whether a trade that clears easily becomes marginal or negative. This
is exactly why `evaluate()` prices the fee over the ACTUAL fills the
order would take (`_priced_fills`), never over an assumed size — see
its docstring for the worked 0.98-vs-0.995 arithmetic under both a
single deep fill and a fragmented book, and
`tests/strategies/test_settlement_edge.py` for the exact numbers.

DELIBERATELY DELETED (this task, PLAN.md D10): every "ambiguity"/
"clarity" keyword heuristic from the pre-T20 version of this file.
Scanning `rules_text` for words like "approximately" or "official" and
treating the result as a probability estimate was never validated
against anything and had no place pretending to answer a question this
strategy no longer asks — this strategy does not read rules text for
edge cases at all; it reads the market's OWN price and clock.
"""
import math
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from app.config import settings
from app.strategies.base import BaseStrategy, Intent, Leg, MarketSnapshot, Signal
from app.venues.base import FeeModel
from app.venues.fees import (
    KalshiFeeModel,
    PolymarketFeeModel,
    category_fee_schedule,
    default_kalshi_schedule,
)
from app.venues.types import FeeSchedule, OrderBook, VenueId

#: Hours in a 365-day year. Kept as a local literal (not imported from
#: `app.services.scoring.HOURS_PER_YEAR` or
#: `app.strategies.cross_venue_arbitrage.HOURS_PER_YEAR`) for the same
#: layering reason both of those already state: a strategy module must
#: not depend on `app.services` (the dependency runs the other way —
#: `app.services.scanner` imports strategies, never the reverse), and
#: every annualization constant in this kit is a plain unit conversion
#: kept in sync by hand rather than coupling unrelated modules over it.
HOURS_PER_YEAR = 8760.0

DEFAULT_CONFIG: dict[str, Any] = {
    # `outcome_determined` fires when `yes_price >= threshold` (buy YES)
    # or `yes_price <= 1 - threshold` (buy NO), i.e. the market's own
    # price already treats one side as ~certain. PLAN.md D10(c)'s 0.97.
    "outcome_determined_threshold": 0.97,
    # CONTRACTS (not dollars): (a) the depth `evaluate()` REQUESTS from
    # `snapshot.book` via `_priced_fills` (the ACTUAL filled size may be
    # less, on a thin book -- see that function); (b) the intended
    # divisor for the FIXED per-redemption `settings.redemption_gas_usd`
    # -- this strategy has exactly ONE leg, so unlike
    # `binary_complement_arbitrage`'s `2 * redemption_gas_usd` (one
    # redemption per leg), this amortizes a single redemption's gas, over
    # whatever size actually filled; and (c) the requested contract count
    # the emitted `Leg` is sized at (capped at the ACTUAL filled size).
    "min_position_size": 100.0,
    # Hard cap on the leg size in contracts, applied before the engine's
    # own cash/`max_position_pct` scaling.
    "max_position_size": 1000.0,
    # Skip an `in_dispute_window` market unless explicitly set `True`.
    # Defaults `False` (PLAN.md R2): during the challenge/settlement-
    # timer window the "determined" outcome is precisely what is under
    # contest, so trading through it needs a deliberate, informed
    # opt-in, never a strategy default.
    "allow_dispute_window": False,
}


def _fee_inputs(venue: VenueId, category: str | None) -> tuple[FeeModel, FeeSchedule]:
    """Return the `(FeeModel, FeeSchedule)` to price a taker fill on `venue`.

    GUARDRAILS.md §1.5: never a literal fee rate. Polymarket's rate comes
    from `category_fee_schedule(category)` (itself honoring
    `settings.polymarket_taker_fee_overrides` before the published
    category table); Kalshi's from `Settings` via
    `default_kalshi_schedule()`. Mirrors
    `binary_complement_arbitrage._fee_inputs` exactly (duplicated, not
    imported — see this module's `HOURS_PER_YEAR` comment for why
    strategy modules do not reach into each other's private helpers).

    Args:
        venue: `"polymarket"` or `"kalshi"`.
        category: The market's category (Polymarket only; ignored for
            Kalshi).

    Returns:
        tuple[FeeModel, FeeSchedule]: The model to call `.fee()` on and
            the schedule to pass it.
    """
    if venue == "kalshi":
        return KalshiFeeModel(), default_kalshi_schedule()
    return PolymarketFeeModel(), category_fee_schedule(category)


def _priced_fills(
    snapshot: MarketSnapshot,
    *,
    outcome: str,
    top_of_book: float,
    size: float,
    outcome_books: Mapping[str, OrderBook] | None = None,
) -> list[tuple[float, float]]:
    """Return the `(price, qty)` fills that buying `size` contracts of
    `outcome` would actually take.

    THIS IS THE ONE SIZE BASIS `evaluate()` prices EVERYTHING against —
    the ask, the fee, and (via the filled total) the gas divisor all
    come from this same list, never from a separately-assumed size. A
    prior version priced the fee at a bare `fee_model.fee(ask, 1.0,
    ...)` while gas was amortized over `size` (100): on Polymarket that
    is harmless (its fee is linear in size and flat in fill count, so
    pricing "per contract" and multiplying is exact), but on Kalshi
    `fee(ask, 1.0, ...)` prices the fee as if the ENTIRE order filled as
    100 separate 1-contract fills — the worst possible fragmentation,
    each paying the $0.01 floor in full — regardless of what the book
    actually offered. That silently assumed maximum fragmentation on one
    venue while assuming a single redemption on the other, which is
    worth a large chunk of the residual at these prices (see the module
    docstring's "VENUE ASYMMETRY" table and `evaluate()`'s docstring for
    the worked numbers this defect produced vs. the fix below).

    Walks the book for THIS outcome (best-first, via `OrderBook.walk`,
    the repo's one depth primitive — never re-implemented here) —
    preferring `outcome_books[outcome]` when supplied, falling back to
    `snapshot.book` otherwise (see the `outcome_books` arg below for
    why). Kalshi's own fee ceiling is charged PER FILL, not on an
    order's blended average (`app.venues.fees.KalshiFeeModel`'s own
    docstring: "callers that need an order-level total must call
    `fee()` once per `Fill` and sum the results, never call it once with
    an order's aggregate size") — keeping the per-level breakdown here,
    rather than collapsing it to a weighted-average ask the way
    `binary_complement_arbitrage._sizing_ask` does, is what lets
    `evaluate()` honor that.

    THREE CASES, not two — a prior version documented only the first
    two and silently collapsed the third into the first, which is
    exactly the defect this paragraph now forecloses:

    1. NO BOOK WAS OBSERVED for this outcome at all: `outcome_books` is
       `None` or has no entry for `outcome`, AND `snapshot.book` is
       either absent or is some OTHER outcome's book (case-insensitive
       match on `.outcome`). Falls back to a SINGLE fill of `size`
       contracts at `top_of_book`. This is the OPTIMISTIC end — the
       fewest possible fills, and therefore the least Kalshi-floor
       exposure a real order of this size could ever pay — and it is a
       deliberate, explicit choice for the case where there is no real
       depth to walk (a bare `MarketSnapshot`, e.g. `on_market_data`'s
       backtest path, or a scan that never fetched this side's book):
       it means an intent built with no book information reads as "as
       good as it could possibly be", never as a phantom worst case.
    2. A BOOK WAS OBSERVED FOR THIS OUTCOME AND IT IS EMPTY: the book
       resolved above matches `outcome`, but `book.walk("buy", size)`
       returns no fills — `asks` is `()`, or every ask level's `size` is
       `0.0`. This is the ONE case that must NEVER fall back to
       `top_of_book`: a `depth_source="recorded"` book that positively
       asserts zero resting liquidity is not "no information", it is
       "no fill", and an empty ask side is the NORMAL state of a
       near-resolution market whose offers have been withdrawn —
       precisely this strategy's target population. Returns `[]`
       (`evaluate()` then reads `filled_size <= 0.0` and emits no
       intent), never the optimistic single fill.
    3. A BOOK WAS OBSERVED FOR THIS OUTCOME AND HAS REAL DEPTH: the
       normal case. Returns whatever `book.walk("buy", size)` actually
       consumes, best price first, `(price, qty)` per level.

    Args:
        snapshot: Supplies the `outcome_books`-absent fallback, `book`.
        outcome: `"YES"` or `"NO"` — must match the resolved book's
            `.outcome` (case-insensitively) for the walk to be used.
        top_of_book: Fallback ask price for the case-1 single-fill.
        size: Contracts to price. Must be `>= 0`.
        outcome_books: Every outcome's REAL book for this market, keyed
            by canonical outcome name, when the caller already fetched
            one per outcome (`app.services.scanner.near_resolution_pass`
            does — see that function). Preferred over `snapshot.book`
            because a `MarketSnapshot` carries AT MOST one outcome's
            book (`"YES"`, for a binary market — see
            `app.services.scanner._snapshot_from_market`'s docstring),
            so pricing the NO side of a binary market off `snapshot.book`
            alone would always mismatch and fall into case 1 even when
            the real NO book was fetched and has genuine depth. `None`
            (the default, e.g. `on_market_data`'s bare-snapshot path)
            falls back to `snapshot.book` exactly as before.

    Returns:
        list[tuple[float, float]]: `(price, qty)` pairs summing to AT
            MOST `size` (less, if the book is thinner than `size`, or
            `[]` in case 2 above). Empty when `size <= 0` or case 2.
    """
    if size <= 0.0:
        return []
    book = outcome_books.get(outcome) if outcome_books is not None else None
    if book is None:
        book = snapshot.book
    if book is None or book.outcome.casefold() != outcome.casefold():
        # Case 1: no book observed for this outcome -- the optimistic
        # single-fill fallback.
        return [(top_of_book, size)]
    # Cases 2 and 3: a book WAS observed and DOES match this outcome --
    # walk it for real, however much (including nothing) it offers.
    # `OrderBook.walk` already returns `[]` for an empty/zero-size ask
    # side, so case 2 falls out of this call with no separate branch.
    return book.walk("buy", size)


def _determined_side(
    snapshot: MarketSnapshot, threshold: float
) -> tuple[str, float] | None:
    """Return `(outcome, top_of_book_ask)` if the market's price is ~certain.

    `outcome_determined` also requires the scheduled close to have
    passed (PLAN.md D10(c)) — checked here as
    `snapshot.timestamp >= snapshot.end_date`, the only proxy available
    from a bare `MarketSnapshot` (see `evaluate()`'s docstring for why
    the richer, venue-differentiated `in_dispute_window` check is
    supplied by the CALLER instead of derived here).

    Args:
        snapshot: Snapshot to classify.
        threshold: `self.config["outcome_determined_threshold"]`.

    Returns:
        tuple[str, float] | None: `("YES", yes_ask-or-price)` if
            `yes_price >= threshold`; `("NO", no_ask-or-price)` if
            `yes_price <= 1 - threshold`; `None` if the close has not
            passed, or if the price is not at either tail.
    """
    if snapshot.end_date is None or snapshot.timestamp < snapshot.end_date:
        return None
    if snapshot.yes_price >= threshold:
        top = snapshot.yes_ask if snapshot.yes_ask is not None else snapshot.yes_price
        return "YES", top
    if snapshot.yes_price <= 1.0 - threshold:
        top = snapshot.no_ask if snapshot.no_ask is not None else snapshot.no_price
        return "NO", top
    return None


class SettlementEdgeStrategy(BaseStrategy):
    """Buy the ~certain side of a near-resolution market — a lockup, not an edge.

    See the module docstring for the full framing (PLAN.md R2): this is
    NOT an information edge, its risk is settlement risk, and
    `outcome_determined` deserves scepticism, not trust.

    Two entrypoints share one core (`evaluate()`):

    - `on_market_data()` — the `BaseStrategy` interface the backtest
      engine and any generic caller use. It can only ever approximate
      `in_dispute_window` (a bare `MarketSnapshot` carries no
      `VenueMarket.status`/`expected_settle_time` to tell a genuinely
      resolved-but-disputed market apart from a merely-closed one) and
      always passes `in_dispute_window=False` — i.e. it assumes NOT in
      dispute when it cannot tell. This is the one place this module
      is optimistic rather than conservative, and it is safe only
      because backtest replay data is itself collected from
      not-yet-resolved snapshots (`app/services/data_collector.py`), so
      the disputed case this approximation would mis-classify does not
      occur in that data in practice.
    - `evaluate()` — the richer entrypoint
      `app.services.scanner.near_resolution_pass` (T20) calls directly,
      supplying the real, venue-differentiated `in_dispute_window` (from
      `VenueMarket.status`/`close_time`/`expected_settle_time`) and the
      real settlement-time estimate (`expected_resolution_ts`) it
      computed from the same `VenueMarket`. This is the production path
      for this strategy; `on_market_data` exists for interface
      compliance and backtest replay, not as this strategy's primary use.
    """

    name = "settlement_edge"
    description = "Buy the ~certain side of a near-resolution market (capital lockup, PLAN.md R2)"
    version = "2.0.0"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """Initialize with merged config."""
        merged_config = {**DEFAULT_CONFIG, **(config or {})}
        super().__init__(merged_config)
        self._opportunities_found = 0
        self._total_theoretical_profit = 0.0

    def on_market_data(self, snapshot: MarketSnapshot) -> Signal | Intent | None:
        """Backtest/generic entrypoint. See class docstring's caveat.

        Args:
            snapshot: Current market state.

        Returns:
            Intent | None: See `evaluate()`; always evaluated with
                `in_dispute_window=False` (unknowable from a bare
                snapshot) and an estimated `expected_resolution_ts`.
        """
        return self.evaluate(snapshot, in_dispute_window=False)

    def evaluate(
        self,
        snapshot: MarketSnapshot,
        *,
        in_dispute_window: bool,
        expected_resolution_ts: datetime | None = None,
        outcome_books: Mapping[str, OrderBook] | None = None,
    ) -> Intent | None:
        """Evaluate one near-resolution market for a capital-lockup trade.

        THE ARITHMETIC (PLAN.md D10(c)), worked at Kalshi's default 0.07
        rate, `min_position_size=100` contracts (the ONE size basis
        `_priced_fills` prices the ask, the fee, AND (via the filled
        total) the gas divisor against — see that function's docstring
        for why an earlier version priced the fee and the gas at two
        different sizes and what that cost), `settlement_delay_hours`
        floor of 24h, and `hours_to_resolution=30` (so `floor_hours =
        max(30, 24) = 30`). Shown at BOTH ends of how the same 100
        contracts can fill — one deep level (best case: one $0.01-ish
        Kalshi ceiling for the whole order) and a hundred 1-contract
        levels (worst case: that ceiling paid ONCE PER FILL) — because
        which one a real book offers is exactly what decides the trade
        here (see the module docstring's "VENUE ASYMMETRY" section):

            ask=0.98, ONE 100-contract fill:
                       fee = ceil_cents(1*0.07*0.98*0.02*100) = ceil_cents($0.1372) = $0.14 total,  $0.0014/contract
                       gas = 0.05 / 100 = $0.0005/contract
                       residual = 1 - 0.98 - 0.0014 - 0.0005 = $0.0181/contract
                       fraction = 0.0181 / 0.98 = 1.8469%
                       annualized = 0.018469 * (8760/30) = 5.393 (539.3%) -> SIGNAL

            ask=0.98, a hundred 1-contract fills:
                       fee = 100 * ceil_cents(1*0.07*0.98*0.02) = 100 * $0.01 = $1.00 total,  $0.01/contract
                       gas = $0.0005/contract (unchanged -- the SAME 100 contracts, one redemption)
                       residual = 1 - 0.98 - 0.01 - 0.0005 = $0.0095/contract
                       fraction = 0.0095 / 0.98 = 0.9694%
                       annualized = 0.009694 * (8760/30) = 2.831 (283.1%) -> STILL SIGNALS
                       (smaller than the one-fill case, but 0.98's residual
                       survives even the worst-case fragmentation)

            ask=0.995, ONE 100-contract fill:
                       fee = ceil_cents(1*0.07*0.995*0.005*100) = ceil_cents($0.034825) = $0.04 total,  $0.0004/contract
                       gas = $0.0005/contract
                       residual = 1 - 0.995 - 0.0004 - 0.0005 = $0.0041/contract
                       fraction = 0.0041 / 0.995 = 0.4121%
                       annualized = 0.004121 * (8760/30) = 1.203 (120.3%) -> SIGNAL

            ask=0.995, a hundred 1-contract fills:
                       fee = 100 * $0.01 = $1.00 total,  $0.01/contract (the SAME per-fill $0.01 as 0.98's
                       worst case -- Kalshi's floor does not care what the price was)
                       gas = $0.0005/contract
                       residual = 1 - 0.995 - 0.01 - 0.0005 = -$0.0055/contract  (NEGATIVE)
                       -> annualized is negative -> None

            So at 0.98 the trade clears REGARDLESS of how thin the book
            is; at 0.995 the SAME hundred-fill fragmentation that only
            dented 0.98's residual is enough, on its own, to erase
            0.995's much smaller residual entirely. The fee per fill did
            not change between the two prices ($0.01 either way, once
            fragmented) -- what changed is the residual (`1 - ask`) it
            is measured against, which shrank from $0.02 to $0.005. This
            is "fees eat it", literally: at 0.995, fragmentation alone
            (before gas) already exceeds the entire gross residual.

        Args:
            snapshot: Current market state.
            in_dispute_window: Whether the underlying `VenueMarket` is
                inside its dispute/settlement-timer window right now
                (Polymarket: `close_time` passed and `status !=
                "resolved"`; Kalshi: `expected_settle_time` passed and
                `status != "resolved"` -- both computed by the caller,
                which has the `VenueMarket` this method never sees).
                When `True`, this method returns `None` unless
                `self.config["allow_dispute_window"]` is `True` (PLAN.md
                R2) -- checked BEFORE pricing, so a disputed market never
                even reaches the fee/annualization arithmetic.
            expected_resolution_ts: The caller's own estimate of when
                this market will actually settle (aware UTC). `None`
                (the `on_market_data` path) falls back to
                `snapshot.end_date + settings.settlement_delay_hours` --
                the same assumed gap `near_resolution_pass` uses for a
                venue (Polymarket) whose real settlement time is not
                itself known.
            outcome_books: Every outcome's REAL book for this market,
                keyed by canonical outcome name, forwarded verbatim to
                `_priced_fills` (see that function's docstring for why
                this must be preferred over `snapshot.book`, which
                carries at most one outcome's book). `None` (the
                `on_market_data` path) falls back to `snapshot.book`.

        Returns:
            Intent | None: A single-leg, `hold_to_resolution=True`
                `kind="single"` `Intent` buying the ~certain side at its
                sizing ask, iff `outcome_determined`, not blocked by
                `in_dispute_window`, and the fee/gas-net annualized
                return (floored at `max(hours_to_resolution,
                settings.settlement_delay_hours)`, never the smaller
                `settings.min_hours_for_annualization`  -- settlement
                delay is the REAL lockup here, and annualizing over the
                shorter, generic floor would overstate the return, see
                `app.services.scanner`'s module docstring) clears
                `settings.min_viable_annualized`. `None` otherwise --
                including when `snapshot.end_date` is unknown (no close
                time to measure "already past" against) or the
                certain side has no price to trade at all.
        """
        threshold = float(self.config["outcome_determined_threshold"])
        determined = _determined_side(snapshot, threshold)
        if determined is None:
            return None
        if in_dispute_window and not self.config["allow_dispute_window"]:
            return None
        outcome, top_of_book = determined
        if top_of_book is None or top_of_book <= 0.0:
            return None

        resolution_ts = expected_resolution_ts
        if resolution_ts is None:
            if snapshot.end_date is None:
                return None
            resolution_ts = snapshot.end_date + timedelta(
                hours=settings.settlement_delay_hours
            )
        hours_to_resolution = (
            resolution_ts - snapshot.timestamp
        ).total_seconds() / 3600.0
        if hours_to_resolution <= 0.0:
            # The estimated settlement point is itself already behind
            # us -- there is no forward-looking return left to annualize.
            return None

        size = float(self.config["min_position_size"])
        fills = _priced_fills(
            snapshot,
            outcome=outcome,
            top_of_book=top_of_book,
            size=size,
            outcome_books=outcome_books,
        )
        filled_size = math.fsum(qty for _, qty in fills)
        if filled_size <= 0.0:
            return None

        fee_model, schedule = _fee_inputs(snapshot.venue, snapshot.category)
        # ONE fee call PER FILL, summed -- never `fee_model.fee(ask, 1.0,
        # ...)` scaled up. Kalshi's ceiling is charged per fill (see
        # `_priced_fills`'s docstring); this is the only way to price
        # what an order on THIS book would actually pay.
        total_fee = math.fsum(
            fee_model.fee(price, qty, "taker", schedule) for price, qty in fills
        )
        total_cost = math.fsum(price * qty for price, qty in fills)
        ask = total_cost / filled_size
        if ask <= 0.0:
            return None
        fee_per_contract = total_fee / filled_size
        # Amortized over the SAME `filled_size` the fee was just priced
        # against -- one fixed redemption, spread over the position this
        # order actually ends up with, not the size it merely asked for.
        gas_per_contract = settings.redemption_gas_usd / filled_size

        residual = 1.0 - ask - fee_per_contract - gas_per_contract
        net_edge_fraction = residual / ask

        floor_hours = max(hours_to_resolution, settings.settlement_delay_hours)
        annualized_return = net_edge_fraction * (HOURS_PER_YEAR / floor_hours)

        if annualized_return < settings.min_viable_annualized:
            return None

        self._opportunities_found += 1
        self._total_theoretical_profit += residual

        leg_size = min(filled_size, float(self.config["max_position_size"]))
        confidence = max(0.0, min(net_edge_fraction / 0.05, 1.0))

        return Intent(
            kind="single",
            legs=[
                Leg(
                    market_id=snapshot.market_id,
                    outcome=outcome,
                    side="BUY",
                    limit_price=ask,
                    size_contracts=leg_size,
                    venue=snapshot.venue,
                )
            ],
            hold_to_resolution=True,
            atomicity="best_effort",
            confidence=confidence,
            expected_resolution_ts=resolution_ts,
            metadata={
                "strategy": self.name,
                "bucket": "near_resolution",
                "ask": ask,
                "fee": fee_per_contract,
                "filled_size": filled_size,
                "fill_count": len(fills),
                "gas_per_contract": gas_per_contract,
                "edge": residual,
                "net_edge_fraction": net_edge_fraction,
                "annualized_return": annualized_return,
                "hours_to_resolution": hours_to_resolution,
                "in_dispute_window": in_dispute_window,
                "fee_schedule_source": schedule.source,
            },
        )

    def calculate_position_size(
        self,
        signal: Signal,
        portfolio_value: float,
        positions: dict[str, Any],  # noqa: ARG002 - interface parity, see below
    ) -> float:
        """Return a dollar sizing fallback (interface compliance only).

        Not consulted by the backtest engine for this strategy's own
        intents: `evaluate()`/`on_market_data` always set
        `Leg.size_contracts` directly. Implemented anyway because
        `BaseStrategy` requires it and other callers (e.g. a UI sizing
        probe) may still invoke it directly. Mirrors
        `binary_complement_arbitrage.calculate_position_size`.

        Args:
            signal: A `Signal` describing the leg to size (its `.price`
                is the leg's limit price).
            portfolio_value: Current total portfolio value.
            positions: Current positions (unused; kept for interface
                parity with `BaseStrategy.calculate_position_size`).

        Returns:
            float: Position size in dollars, `>= 0`.
        """
        price = signal.price if signal.price > 0.0 else 0.5
        max_by_pct = portfolio_value * 0.10
        dollar_cap = float(self.config["max_position_size"]) * price
        position_size = min(max_by_pct, dollar_cap)
        position_size = max(position_size, float(self.config["min_position_size"]) * price)
        position_size *= signal.confidence
        return min(position_size, portfolio_value * 0.5)

    def reset(self) -> None:
        """Reset strategy state."""
        super().reset()
        self._opportunities_found = 0
        self._total_theoretical_profit = 0.0

    def get_stats(self) -> dict[str, Any]:
        """Get strategy statistics."""
        stats = super().get_stats()
        stats.update({
            "opportunities_found": self._opportunities_found,
            "total_theoretical_profit": self._total_theoretical_profit,
        })
        return stats
