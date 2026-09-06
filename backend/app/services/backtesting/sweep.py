"""Capital sweep and `EdgeDecayReport` (PLAN.md D12, T22).

`run_sweep` answers the question the rest of the backtester deliberately
does not: not "is this edge profitable at the size I happened to
configure", but "at what capital does this edge stop being viable". It
runs a FRESH `Backtester` per capital level (never one mutated run
scaled after the fact -- PLAN.md D5's shared fill engine already walks
real depth per level, and reusing state across levels would let one
level's positions/cash leak into the next) and reports the SAME shape
of trustworthiness labels every other backtest number carries
(GUARDRAILS.md §1.7): `depth_source`, `fill_at`, `tick_unvalidated_fills`
and `unmarked_positions` ride on every row, not just the report as a
whole, because a row's own `net_return`/`annualized` is itself a
backtest number that must never be quoted unlabeled.

**Two money-correctness traps this module was built to avoid (see
NOTES.md `### T22` for the full analysis):**

1. **"No edge" and "not measurable" are different.** `MarketSnapshot.book`
   is a single field, so an N-leg bundle intent can carry a recorded
   book for at most one leg, and `Backtester._book_for` returns `None`
   for any non-YES/NO outcome with no recorded book -- meaning a real
   multi-outcome bundle intent can NEVER execute in a synthetic-depth
   backtest. That produces EXACTLY the same signature as a genuine
   "no edge at any size" result: zero trades at every level. Reporting
   it as "no edge" would be the same class of error GUARDRAILS.md §1.7
   exists to prevent, one level up. `CapitalRow.zero_trades_cause`
   exists to tell the two apart: `"no_signal"` means the strategy
   itself declined to trade (a real "no edge" reading); anything else
   (`"structural: ..."`) means the intent was generated but could never
   be FILLED by this backtester, which is a measurement limitation, not
   an edge finding. `EdgeDecayReport.unmeasurable_note` is set whenever
   `edge_dies_at` is anchored to a structurally-zero-trade row, so that
   note -- not `edge_dies_at` alone -- is what a caller must read before
   concluding "this strategy has no edge".

2. **"I ran out of money" and "the book ran out" are different, and
   they run in OPPOSITE directions across a capital sweep (T28).**
   `pct_intents_downsized` measures a fill that fell short of what the
   STRATEGY asked for, which is the SUM of two unrelated causes: the
   engine's own `min(portfolio_value * max_position_pct, cash * 0.99)`
   cap, and the book running out of contracts. The capital cap binds
   LESS as capital grows; depth exhaustion binds MORE. Summed into one
   column and swept over capital, the capital-cap term dominates and
   the series runs DOWNWARD -- on the repo's own `--synthetic` demo it
   read `16.7% -> 4.8% -> 1.0% -> 0.0% -> 0.0%`, i.e. "downsizing
   improves as you deploy more capital", the precise inverse of the
   depth-exhaustion signal PLAN.md D12/R4 exist to surface.

   The fix is not to redefine `pct_intents_downsized` (its docstring
   was already accurate, and the field is consumed by name in the API
   payload and the frontend's `EdgeDecayTable`; silently changing what
   a shipped number MEANS is the one kind of rename no consumer can
   see). It is DECOMPOSED instead, by two engine-truth counters that
   ride on `BacktestResult` itself:

   - `pct_intents_capital_capped` -- of the intents the engine actually
     sized, the share whose legs were scaled down by the capital cap.
     This is the term that falls with capital.
   - `pct_intents_depth_limited` -- of the same denominator, the share
     with at least one leg whose walk CONSUMED THE BOOK AND STILL CAME
     UP SHORT. This is PLAN.md R4's tripwire number, and the one that
     rises with capital.

   Both are counted from the leg PLANS inside
   `Backtester._execute_intent`, before atomicity is applied, which is
   what makes them see something `pct_intents_downsized` structurally
   cannot: an `all_or_none` intent killed BECAUSE a leg came up short
   produces no `TradeRecord` at all, so its depth failure was
   previously invisible to any trade-derived metric. For the repo's
   flagship `binary_complement_arbitrage` (`atomicity="all_or_none"`,
   `partial_tolerance=0.0`) that is EVERY depth failure it can have:
   a committed trade always filled its ordered size exactly, so a
   trade-derived depth metric on that strategy is identically zero.
   `depth_blocked_intents` reports that subset, which is also the exact
   overlap with `fill_rate` -- the two are never summed, and the
   overlap is published as a number rather than left to be inferred.

Units (GUARDRAILS.md §4): `CapitalRow.capital` is USD; `net_return`/
`annualized`/`pct_intents_downsized`/`pct_intents_capital_capped`/
`pct_intents_depth_limited`/`capital_utilization`/`fill_rate` are
dimensionless fractions; `avg_slippage_bps` is basis points.
"""
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any

from app.config import settings
from app.services.backtesting.engine import (
    BacktestConfig,
    Backtester,
    BacktestResult,
    FillAt,
    ResultDepthSource,
    TradeRecord,
)
from app.services.backtesting.metrics import calculate_metrics
from app.strategies import get_strategy
from app.strategies.base import (
    BaseStrategy,
    Intent,
    MarketSnapshot,
    Signal,
    outcome_key,
)

logger = logging.getLogger(__name__)

#: Default capital levels for a sweep (PLAN.md D12).
DEFAULT_CAPITAL_LEVELS: tuple[float, ...] = (500.0, 2_000.0, 10_000.0, 50_000.0, 250_000.0)

#: `Intent.metadata` key `_RequestTrackingStrategy` stamps with a
#: `{(venue, outcome_key): requested_contracts}` mapping. Internal to
#: this module; never read by the engine or by any strategy.
_SWEEP_REQUESTED_KEY = "_sweep_requested_contracts"

#: Tolerance for "filled < requested" comparisons, contracts. Guards
#: against float noise from the `size_usd / total_price` division
#: reproducing a slightly different value than the engine's own.
_SIZE_TOLERANCE = 1e-6


class _RequestTrackingStrategy(BaseStrategy):
    """Wrap a strategy to recover each intent's REQUESTED per-leg size.

    Mechanism: `on_market_data` keeps a reference to the `Intent` it just
    returned (the engine calls this strategy from a single coroutine, one
    snapshot at a time, so there is no concurrent-access risk). The
    engine's very next step for that same cycle -- `Backtester._size_intent`
    -- always calls `calculate_position_size` next, whether or not its
    return value ends up mattering for sizing (an intent whose legs
    already declare `size_contracts` ignores the returned budget, but the
    call still happens). This wrapper's `calculate_position_size`
    delegates to the wrapped strategy for the real return value, then --
    before returning -- computes the SAME per-leg contract count
    `Backtester._leg_sizes` would compute from that same information
    (explicit `size_contracts` when every leg declares one, else
    `budget / sum(leg.limit_price)` applied equally to every leg, exactly
    mirroring `_leg_sizes`'s own two branches) and stamps it onto the
    intent's `metadata` dict under `_SWEEP_REQUESTED_KEY`. Because
    `Intent`/`Leg` are ordinary mutable dataclasses and this is the SAME
    object instance that later flows into `Backtester._commit_leg`
    (`TradeRecord.metadata = dict(intent.metadata)`), the stamp survives
    onto every trade the intent produces.

    NOT captured: `Backtester._leg_sizes`'s own scale-down when the
    computed notional exceeds `min(portfolio_value * max_position_pct,
    cash * 0.99)`. Replicating that step here would duplicate live
    portfolio state `_leg_sizes` already owns (PLAN.md D5: one engine, no
    divergent copies of its money math). `pct_intents_downsized`
    therefore measures "filled short of what the STRATEGY asked for",
    which is the sum of book-depth-driven and capital-cap-driven
    shortfall, not book depth alone -- see the module docstring.

    That sum is why this wrapper is no longer the row's only shortfall
    signal (T28): `CapitalRow.pct_intents_capital_capped` and
    `pct_intents_depth_limited` come from the engine's own per-intent
    counters and separate the two causes, over a denominator that
    includes intents which committed nothing at all. This wrapper is
    kept, unchanged, because `pct_intents_downsized` is a shipped field
    read by name from the API payload and the frontend, and the union
    it reports ("how often did I fail to get the size I asked for, for
    any reason") is still a real operational number -- it was only ever
    wrong as the SOLE number.

    A strategy that returns a `Signal` (not an `Intent`) is passed
    through untouched: nothing is stamped, so trades produced from a
    `Signal` are excluded from `pct_intents_downsized`'s numerator and
    denominator rather than reported as 0% downsized.
    """

    def __init__(self, inner: BaseStrategy) -> None:
        """Wrap `inner`, proxying its identity and config.

        Args:
            inner: The strategy `run_sweep` was asked to sweep.
        """
        super().__init__(inner.config)
        self._inner = inner
        self.name = inner.name
        self.description = inner.description
        self.version = inner.version
        self._pending_intent: Intent | None = None

    def on_market_data(self, snapshot: MarketSnapshot) -> Signal | Intent | None:
        """Delegate to the wrapped strategy, remembering any `Intent`.

        Args:
            snapshot: Current market state snapshot.

        Returns:
            Signal | Intent | None: Exactly what `inner` returned.
        """
        result = self._inner.on_market_data(snapshot)
        self._pending_intent = result if isinstance(result, Intent) else None
        return result

    def calculate_position_size(
        self,
        signal: Signal,
        portfolio_value: float,
        positions: dict[str, Any],
    ) -> float:
        """Delegate for the real budget, then stamp the requested size.

        Args:
            signal: The sizing probe (a real `Signal`, or one the engine
                built from `intent.legs[0]` for an `Intent`-returning
                strategy).
            portfolio_value: Current total portfolio value.
            positions: Current open positions.

        Returns:
            float: Exactly what `inner.calculate_position_size` returned.
        """
        budget = self._inner.calculate_position_size(signal, portfolio_value, positions)
        intent = self._pending_intent
        self._pending_intent = None
        if intent is not None:
            self._stamp_requested(intent, budget)
        return budget

    @staticmethod
    def _stamp_requested(intent: Intent, budget: float) -> None:
        """Record each leg's pre-cap requested contract count on `intent`.

        Mirrors `Backtester._leg_sizes`'s first step exactly (explicit
        `size_contracts` honored verbatim when every leg declares one,
        else an equal contract count per leg derived from the USD
        budget) -- see the class docstring for what is deliberately NOT
        mirrored (the engine's own capital-cap scale-down).

        Args:
            intent: The intent about to be sized/executed.
            budget: USD budget from `calculate_position_size`.
        """
        explicit = [leg.size_contracts for leg in intent.legs]
        if all(size is not None for size in explicit):
            sizes = [float(size) for size in explicit if size is not None]
        else:
            total_price = math.fsum(leg.limit_price for leg in intent.legs)
            if budget <= 0.0 or total_price <= 0.0:
                return
            contracts = budget / total_price
            sizes = [contracts] * len(intent.legs)
        requested = {
            (leg.venue, outcome_key(leg.outcome)): size
            for leg, size in zip(intent.legs, sizes, strict=True)
        }
        intent.metadata[_SWEEP_REQUESTED_KEY] = requested

    def on_trade_executed(self, trade: dict[str, Any]) -> None:
        """Delegate so the wrapped strategy's own stats stay accurate."""
        self._inner.on_trade_executed(trade)

    def on_position_closed(self, position: dict[str, Any], pnl: float) -> None:
        """Delegate so the wrapped strategy's own stats stay accurate."""
        self._inner.on_position_closed(position, pnl)

    def reset(self) -> None:
        """Reset both this wrapper and the wrapped strategy."""
        super().reset()
        self._pending_intent = None
        self._inner.reset()

    async def initialize(self) -> None:
        """Initialize the wrapped strategy."""
        await self._inner.initialize()
        self._is_initialized = True

    async def cleanup(self) -> None:
        """Clean up the wrapped strategy."""
        await self._inner.cleanup()
        self._is_initialized = False

    def validate_config(self) -> bool:
        """Delegate config validation to the wrapped strategy."""
        return self._inner.validate_config()

    def get_default_config(self) -> dict[str, Any]:
        """Delegate default config to the wrapped strategy."""
        return self._inner.get_default_config()

    def get_stats(self) -> dict[str, Any]:
        """Return the WRAPPED strategy's stats, not this proxy's own."""
        return self._inner.get_stats()


@dataclass
class CapitalRow:
    """One capital level's backtest outcome, self-labeled (GUARDRAILS.md §1.7).

    Attributes:
        capital: The `initial_capital` this row was run at, USD.
        net_return: `BacktestResult.total_return` for this level.
        annualized: `PerformanceMetrics.annualized_return` for this
            level's equity curve.
        fill_rate: `intents_executed / intents_generated`, or `0.0` when
            nothing was generated.
        avg_slippage_bps: Size-weighted `(avg_fill - limit) / limit *
            1e4` over BUY fills only. `0.0` when there were none.
        pct_intents_downsized: Share of TRACKABLE executed intents whose
            filled contracts fell short of what the strategy requested
            (see `_RequestTrackingStrategy`). An intent is "trackable"
            only when at least one of its BUY trades carries the
            wrapper's stamp; a `Signal`-based strategy or one whose legs
            declare `size_usd` produces no trackable intents and this is
            `0.0` for it -- read `downsize_trackable_intents` before
            trusting a `0.0` here as "never downsized".

            **This is the UNION of two causes and it is NOT the
            depth signal** (T28). It sums capital-cap-driven and
            book-depth-driven shortfall, and because the capital cap
            binds less as capital grows, the union typically FALLS
            across a sweep even while depth exhaustion rises. Read
            `pct_intents_depth_limited` for PLAN.md R4's number and
            `pct_intents_capital_capped` for its counterpart; this
            field is kept at its original meaning so the shipped API
            payload and frontend column do not silently change under
            their consumers.
        sized_intents: Intents the engine actually sized and planned
            against a book (`BacktestResult.intents_sized`) -- the
            denominator for the two fields below. An intent that could
            not be sized at all (no budget, or a notional under
            `settings.min_trade_usd`) never reached the book and is
            evidence about neither capital nor depth, so it is excluded
            from both.
        pct_intents_capital_capped: Share of `sized_intents` whose legs
            the engine scaled DOWN to respect
            `min(portfolio_value * max_position_pct, cash * 0.99)` --
            "how often did I run out of money". Expected to FALL toward
            zero as capital grows; a sweep where it does not is a sweep
            whose top level is still cash-constrained.
        pct_intents_depth_limited: Share of `sized_intents` with at
            least one leg whose walk consumed the book and still came up
            short (`0 < filled < ordered`) -- "how often did the book
            run out". **This is PLAN.md R4's tripwire number**, and it is
            expected to RISE with capital for any strategy whose order
            size scales with capital. A flat non-zero series instead
            means the strategy's order size is capital-invariant (as
            `binary_complement_arbitrage`'s fixed `min_position_size`
            contracts are), and a flat ZERO series means the fixture's
            synthesized depth is never smaller than the order --
            R4's "the fixture is too deep".

            Legs that filled NOTHING are deliberately excluded: a zero
            fill reports `"no_eligible_levels"` whether the side was
            empty (depth) or every level was worse than the limit (a
            price move under `fill_at="next"`), and the engine cannot
            separate those. They are visible in `fill_rate` and
            `rejection_reasons` instead. See
            `Backtester._leg_ran_out_of_book`.
        depth_blocked_intents: How many of the depth-limited intents
            committed NOTHING -- an `all_or_none` intent killed by a
            short leg. These leave no `TradeRecord`, which is why no
            trade-derived metric can see them, and they are also the
            exact subset where this row's depth signal overlaps its
            `fill_rate`. The two are never summed; this count is
            published so the overlap is a number, not an inference.
        capital_utilization: Mean over the equity curve of
            `(equity - cash) / equity`, where `cash` is APPROXIMATED by
            replaying `BacktestResult.trades`' own cash effect
            (`price*size +/- fee`) against `initial_capital` in
            timestamp order. This does NOT reflect `pending_settlements`
            or resolved P&L (`BacktestResult` carries no cash-over-time
            series to read exactly), so a row with `positions_settled >
            0` may read slightly off from the engine's own internal
            cash figure -- see `coverage`/`unrealized_notional_at_end`
            on the underlying result for the settlement-heavy case.
        trades: `BacktestResult.total_trades` for this level.
        depth_source: This level's own `BacktestResult.depth_source` --
            NOT necessarily the same as the report's aggregate, since a
            level with zero trades reports `"synthetic"` by default
            (the conservative label) regardless of what other levels
            walked.
        fill_at: This level's own `BacktestResult.fill_at`.
        tick_unvalidated_fills: This level's own
            `BacktestResult.tick_unvalidated_fills` -- fills committed
            without a `VenueMarket`, so neither `tick_size` nor
            `min_size` was enforced (T08 carry-forward 2).
        unmarked_positions: This level's own
            `BacktestResult.unmarked_positions` (T21d). Non-empty means
            part of this row's equity curve/`net_return` is a position
            marked at its ENTRY PRICE for want of any observed price --
            i.e. the equity curve is partly not a market number at all.
        rejection_reasons: This level's own
            `BacktestResult.rejection_reasons` (`FillReason -> count`),
            surfaced so a zero-trade row's cause is inspectable directly,
            not just through `zero_trades_cause`'s summary string.
        zero_trades_cause: `None` when `trades > 0`. Otherwise
            `"no_signal"` when the strategy generated no intent at all
            (a genuine "no edge at this size" reading), or a
            `"structural: ..."` string naming the dominant
            `rejection_reasons`/expiration cause when intents WERE
            generated but never executed -- e.g. a multi-outcome bundle
            intent that can never get a book for a non-YES/NO leg
            (T22 carry-forward 2). A `"structural: ..."` row must never
            be read as evidence the edge is zero; see
            `EdgeDecayReport.unmeasurable_note`.
        downsize_trackable_intents: How many executed intents had at
            least one BUY trade carrying the requested-size stamp --
            the denominator behind `pct_intents_downsized`.
    """

    capital: float
    net_return: float
    annualized: float
    fill_rate: float
    avg_slippage_bps: float
    pct_intents_downsized: float
    capital_utilization: float
    trades: int
    depth_source: ResultDepthSource
    fill_at: FillAt
    tick_unvalidated_fills: int
    unmarked_positions: tuple[str, ...]
    rejection_reasons: dict[str, int] = field(default_factory=dict)
    zero_trades_cause: str | None = None
    downsize_trackable_intents: int = 0
    sized_intents: int = 0
    pct_intents_capital_capped: float = 0.0
    pct_intents_depth_limited: float = 0.0
    depth_blocked_intents: int = 0


@dataclass
class EdgeDecayReport:
    """A capital sweep's full result (PLAN.md D12).

    Attributes:
        rows: One `CapitalRow` per capital level, ascending by capital.
        edge_dies_at: The smallest level whose `annualized` fell below
            `settings.min_viable_annualized`, or `None` if no tested
            level did. `None` is NOT "the edge survives at any size" --
            only the levels actually tested were measured; see
            `sweep_ceiling_note`.
        depth_source: Aggregate across every row -- the common value
            when every row agrees, else `"mixed"`. Read each row's OWN
            `depth_source` before quoting one level's number alone.
        fill_at: The `fill_at` every row in this sweep was run under
            (`run_sweep` uses one `base_config` for all levels).
        sweep_ceiling_note: The PLAN.md D12 caveat, ALWAYS populated
            (never merely implied by `edge_dies_at is None`): the sweep
            ceiling is not proof of behavior beyond the levels tested,
            in either direction. Callers (the CLI, the API) must PRINT
            or DISPLAY this, not just persist it.
        unmeasurable_note: `None` unless `edge_dies_at` is anchored to a
            row whose `zero_trades_cause` is structural (not
            `"no_signal"`) -- i.e. `edge_dies_at` reflects a level this
            backtester could never measure, not a shrinking edge. See
            the module docstring's trap 1.
    """

    rows: list[CapitalRow]
    edge_dies_at: float | None
    depth_source: ResultDepthSource
    fill_at: FillAt
    sweep_ceiling_note: str
    unmeasurable_note: str | None = None


def _zero_trades_cause(result: BacktestResult) -> str | None:
    """Explain WHY a zero-trade result had zero trades.

    Args:
        result: The completed backtest result.

    Returns:
        str | None: `None` when trades occurred. `"no_signal"` when the
            strategy never generated an intent (a genuine no-edge
            reading). Otherwise a `"structural: ..."` string naming the
            dominant rejection reason or, absent any recorded rejection,
            the expiration count -- a result that was generated but
            never executed for a reason that has nothing to do with the
            edge's size (T22 carry-forward 2).
    """
    if result.total_trades > 0:
        return None
    if result.intents_generated == 0:
        return "no_signal"
    if result.rejection_reasons:
        reason, count = max(result.rejection_reasons.items(), key=lambda kv: kv[1])
        return f"structural: {reason} ({count}/{result.intent_rejections} rejected)"
    if result.intent_expirations > 0:
        return f"structural: pending_ttl_expired ({result.intent_expirations})"
    return "structural: intents generated but none executed (no reason recorded)"


def _avg_slippage_bps(trades: list[TradeRecord]) -> float:
    """Size-weighted `(avg_fill - limit) / limit * 1e4` over BUY fills.

    Args:
        trades: A result's trade list.

    Returns:
        float: Weighted average slippage in basis points, or `0.0` when
            there were no BUY fills.
    """
    weighted = 0.0
    weight = 0.0
    for trade in trades:
        if trade.side != "BUY":
            continue
        # TradeRecord.slippage is defined as `price - limit_price` for a
        # BUY, so the limit is exactly recoverable from the two fields
        # already on the record.
        limit_price = trade.price - trade.slippage
        if limit_price <= 0.0 or trade.size <= 0.0:
            continue
        bps = (trade.slippage / limit_price) * 10_000.0
        weighted += bps * trade.size
        weight += trade.size
    return weighted / weight if weight > 0.0 else 0.0


def _pct_intents_downsized(trades: list[TradeRecord]) -> tuple[float, int]:
    """Share of trackable executed intents whose fill fell short of ask.

    Args:
        trades: A result's trade list.

    Returns:
        tuple[float, int]: `(pct_downsized, trackable_count)`. `(0.0, 0)`
            when no intent carried a recoverable requested size (see
            `_RequestTrackingStrategy`).
    """
    by_intent: dict[str, list[TradeRecord]] = {}
    for trade in trades:
        if trade.intent_id is None or trade.side != "BUY":
            continue
        by_intent.setdefault(trade.intent_id, []).append(trade)

    trackable = 0
    downsized = 0
    for intent_trades in by_intent.values():
        requested_map: dict[tuple[str, str], float] | None = None
        for trade in intent_trades:
            candidate = trade.metadata.get(_SWEEP_REQUESTED_KEY)
            if isinstance(candidate, dict):
                requested_map = candidate
                break
        if not requested_map:
            continue
        trackable += 1

        filled_by_leg: dict[tuple[str, str], float] = {}
        for trade in intent_trades:
            key = (trade.venue, outcome_key(trade.outcome))
            filled_by_leg[key] = filled_by_leg.get(key, 0.0) + trade.size

        if any(
            filled_by_leg.get(key, 0.0) < requested - _SIZE_TOLERANCE
            for key, requested in requested_map.items()
        ):
            downsized += 1

    if trackable == 0:
        return 0.0, 0
    return downsized / trackable, trackable


def _capital_utilization(result: BacktestResult) -> float:
    """Mean over the equity curve of `(equity - cash) / equity`.

    `cash` is approximated by replaying each BUY/SELL trade's own cash
    effect against `initial_capital` in timestamp order -- see
    `CapitalRow.capital_utilization` for what this omits.

    Args:
        result: The completed backtest result.

    Returns:
        float: The mean fraction of equity NOT sitting in spendable
            cash, or `0.0` when the equity curve is empty.
    """
    if not result.equity_curve:
        return 0.0

    trades_sorted = sorted(
        (t for t in result.trades if t.side in ("BUY", "SELL")),
        key=lambda t: t.timestamp,
    )
    cash = result.initial_capital
    idx = 0
    n = len(trades_sorted)
    total = 0.0
    count = 0
    for timestamp, equity in result.equity_curve:
        while idx < n and trades_sorted[idx].timestamp <= timestamp:
            trade = trades_sorted[idx]
            if trade.side == "BUY":
                cash -= trade.price * trade.size + trade.fee
            else:
                cash += trade.price * trade.size - trade.fee
            idx += 1
        if equity > 0.0:
            total += (equity - cash) / equity
            count += 1
    return total / count if count > 0 else 0.0


def _aggregate_depth_source(sources: list[ResultDepthSource]) -> ResultDepthSource:
    """Combine every row's `depth_source` into one report-level label.

    Args:
        sources: Each row's own `depth_source`.

    Returns:
        ResultDepthSource: The common value when every row agrees,
            `"mixed"` when they disagree, `"synthetic"` (the
            conservative default) when there are no rows.
    """
    unique = set(sources)
    if not unique:
        return "synthetic"
    if len(unique) == 1:
        return next(iter(unique))
    return "mixed"


def _build_row(capital: float, result: BacktestResult) -> CapitalRow:
    """Build one `CapitalRow` from a completed backtest at one level.

    Args:
        capital: The capital level this result was run at.
        result: The completed backtest result.

    Returns:
        CapitalRow: The row for this level.
    """
    metrics = calculate_metrics(
        result.equity_curve, result.trades, result.initial_capital
    )
    pct_downsized, trackable = _pct_intents_downsized(result.trades)
    return CapitalRow(
        capital=capital,
        net_return=result.total_return,
        annualized=metrics.annualized_return,
        fill_rate=(
            result.intents_executed / result.intents_generated
            if result.intents_generated
            else 0.0
        ),
        avg_slippage_bps=_avg_slippage_bps(result.trades),
        pct_intents_downsized=pct_downsized,
        capital_utilization=_capital_utilization(result),
        trades=result.total_trades,
        depth_source=result.depth_source,
        fill_at=result.fill_at,
        tick_unvalidated_fills=result.tick_unvalidated_fills,
        unmarked_positions=result.unmarked_positions,
        rejection_reasons=dict(result.rejection_reasons),
        zero_trades_cause=_zero_trades_cause(result),
        downsize_trackable_intents=trackable,
        sized_intents=result.intents_sized,
        pct_intents_capital_capped=(
            result.intents_capital_capped / result.intents_sized
            if result.intents_sized
            else 0.0
        ),
        pct_intents_depth_limited=(
            result.intents_depth_limited / result.intents_sized
            if result.intents_sized
            else 0.0
        ),
        depth_blocked_intents=result.intents_depth_blocked,
    )


def _find_edge_dies_at(rows: list[CapitalRow]) -> tuple[float | None, str | None]:
    """Find the smallest level whose annualized return dies (PLAN.md D12).

    Args:
        rows: Rows in ASCENDING capital order.

    Returns:
        tuple[float | None, str | None]: `(edge_dies_at, cause)`. `cause`
            is the triggering row's `zero_trades_cause` when `trades ==
            0` there, else `None` -- i.e. `cause` is only ever non-`None`
            when the "death" is actually a zero-trade row, which is what
            `EdgeDecayReport.unmeasurable_note` keys off.
    """
    threshold = settings.min_viable_annualized
    for row in rows:
        if row.annualized < threshold:
            cause = row.zero_trades_cause if row.trades == 0 else None
            return row.capital, cause
    return None, None


async def run_sweep(
    strategy_name: str,
    strategy_config: dict[str, Any],
    base_config: BacktestConfig,
    data_source_factory: Callable[[], Any],
    capital_levels: list[float] | None = None,
    *,
    on_level_result: Callable[[float, BacktestResult], Awaitable[None]] | None = None,
) -> EdgeDecayReport:
    """Run one backtest per capital level and report how the edge decays.

    Args:
        strategy_name: Registry name (`app.strategies.STRATEGIES`).
        strategy_config: Strategy configuration overrides, applied
            identically at every level (PLAN.md D12 sweeps CAPITAL, not
            strategy behavior).
        base_config: Template `BacktestConfig`; every level's config is
            `dataclasses.replace(base_config, initial_capital=level)` --
            every OTHER field (dates, `fill_at`, slippage, ...) is held
            fixed across the sweep.
        data_source_factory: Called ONCE PER LEVEL to build a fresh data
            source (`DataReplayer`/`InMemoryDataReplayer`/any async
            iterator of `ReplayItem`) -- never reused across levels, so
            one level's stream position can never leak into the next.
        capital_levels: Levels to test, USD. Defaults to
            `DEFAULT_CAPITAL_LEVELS`. Sorted ascending regardless of
            input order, since `edge_dies_at` is defined as "the
            SMALLEST level" (PLAN.md D12).
        on_level_result: Optional async hook, awaited with `(level,
            BacktestResult)` right after each level's backtest completes
            -- so a caller (the Celery sweep task) can persist the FULL
            per-level result (equity curve, trades, `build_report()`)
            without re-running the backtest a second time just to get
            it. Never consulted by `run_sweep` itself.

    Returns:
        EdgeDecayReport: One row per level plus the decay/ceiling labels.

    Raises:
        ValueError: If `capital_levels` is an empty list.
    """
    levels = sorted({float(c) for c in (capital_levels or DEFAULT_CAPITAL_LEVELS)})
    if not levels:
        raise ValueError("capital_levels must be non-empty")

    rows: list[CapitalRow] = []
    for level in levels:
        config = replace(base_config, initial_capital=level)
        strategy = _RequestTrackingStrategy(get_strategy(strategy_name, strategy_config))
        backtester = Backtester(config, strategy)
        data_source = data_source_factory()
        logger.info(
            "sweep: running level",
            extra={"strategy": strategy_name, "capital": level},
        )
        result = await backtester.run(data_source)
        if on_level_result is not None:
            await on_level_result(level, result)
        rows.append(_build_row(level, result))

    edge_dies_at, triggering_cause = _find_edge_dies_at(rows)
    depth_source = _aggregate_depth_source([row.depth_source for row in rows])
    fill_at = rows[0].fill_at if rows else base_config.fill_at
    top_level = levels[-1]
    threshold = settings.min_viable_annualized

    # PLAN.md D12: the caveat is PRINTED, not merely stored -- callers
    # (the CLI, the API) must surface this string, not just persist it.
    if edge_dies_at is None:
        sweep_ceiling_note = (
            f"No tested level pushed the annualized return below "
            f"settings.min_viable_annualized ({threshold:.0%}); the top "
            f"level tested was ${top_level:,.0f}. The sweep ceiling is "
            "NOT proof the edge survives above that size -- only the "
            "levels actually tested were measured."
        )
    else:
        sweep_ceiling_note = (
            f"Annualized return fell below settings.min_viable_annualized "
            f"({threshold:.0%}) at ${edge_dies_at:,.0f}. Levels above "
            f"${top_level:,.0f} were not tested; the sweep ceiling is not "
            "proof of behavior beyond the levels tested, in either "
            "direction."
        )

    unmeasurable_note: str | None = None
    if triggering_cause is not None and triggering_cause != "no_signal":
        unmeasurable_note = (
            f"edge_dies_at=${edge_dies_at:,.0f} is anchored to a ZERO-TRADE "
            f"row whose cause is '{triggering_cause}', not a shrinking "
            "edge -- this strategy/fixture could not be MEASURED by this "
            "backtester at that capital level. Do not read edge_dies_at "
            "here as evidence the edge is zero (T22 carry-forward 2)."
        )

    return EdgeDecayReport(
        rows=rows,
        edge_dies_at=edge_dies_at,
        depth_source=depth_source,
        fill_at=fill_at,
        sweep_ceiling_note=sweep_ceiling_note,
        unmeasurable_note=unmeasurable_note,
    )


def capital_row_to_dict(row: CapitalRow) -> dict[str, Any]:
    """Return a JSON-serializable dict for one `CapitalRow`.

    Args:
        row: The row to serialize.

    Returns:
        dict[str, Any]: Primitives/lists/dicts only.
    """
    return {
        "capital": row.capital,
        "net_return": row.net_return,
        "annualized": row.annualized,
        "fill_rate": row.fill_rate,
        "avg_slippage_bps": row.avg_slippage_bps,
        "pct_intents_downsized": row.pct_intents_downsized,
        "capital_utilization": row.capital_utilization,
        "trades": row.trades,
        "depth_source": row.depth_source,
        "fill_at": row.fill_at,
        "tick_unvalidated_fills": row.tick_unvalidated_fills,
        "unmarked_positions": list(row.unmarked_positions),
        "rejection_reasons": dict(row.rejection_reasons),
        "zero_trades_cause": row.zero_trades_cause,
        "downsize_trackable_intents": row.downsize_trackable_intents,
        "sized_intents": row.sized_intents,
        "pct_intents_capital_capped": row.pct_intents_capital_capped,
        "pct_intents_depth_limited": row.pct_intents_depth_limited,
        "depth_blocked_intents": row.depth_blocked_intents,
    }


def edge_decay_report_to_dict(report: EdgeDecayReport) -> dict[str, Any]:
    """Return a JSON-serializable dict for a full `EdgeDecayReport`.

    Args:
        report: The report to serialize.

    Returns:
        dict[str, Any]: Primitives/lists/dicts only -- safe to write
            straight into a JSON/JSONB column or `json.dump`.
    """
    return {
        "rows": [capital_row_to_dict(row) for row in report.rows],
        "edge_dies_at": report.edge_dies_at,
        "depth_source": report.depth_source,
        "fill_at": report.fill_at,
        "sweep_ceiling_note": report.sweep_ceiling_note,
        "unmeasurable_note": report.unmeasurable_note,
    }
