"""Backtesting engine for strategy evaluation (PLAN.md D5/D6/D7, T08).

Extended IN PLACE — the public names re-exported by
`app/services/backtesting/__init__.py` (`Backtester`, `BacktestConfig`,
`BacktestResult`, `Portfolio`, `Position`, `SlippageModel`,
`TradeRecord`) are load-bearing for `app/api/routes/backtesting.py` and
`app/tasks/backtesting.py` and are preserved exactly (PLAN.md §2).

What T08 changed, and why each change is a money-correctness fix
----------------------------------------------------------------
- **Fills come from `SimulatedFillEngine`, not a slippage constant.**
  The backtester and the paper adapter now share one fill model, one fee
  model and one set of rounding rules (PLAN.md D5), so an edge that
  survives backtest but not paper is a data problem rather than "the two
  simulators disagree". Depth is WALKED: an order larger than the touch
  no longer fills entirely at the touch price.
- **`fill_at="next"` is the default.** An intent generated on snapshot N
  for market M is queued and executed against M's NEXT snapshot's book
  (PLAN.md D6). Filling on the same snapshot that produced the signal is
  look-ahead: the strategy saw that price and traded on it in the same
  instant, which no live system can do. `fill_at="same"` is retained for
  diagnostics ONLY, and every trade it produces is stamped
  `metadata["diagnostic_same_snapshot"] = True` so a result built that
  way can never be quoted as a clean number (GUARDRAILS.md §1.7).
- **Multi-leg intents execute as multi-leg.** The pre-T08 bridge that
  executed only `intent.legs[0]` (turning a hedged complement into a bare
  directional bet) is GONE. `all_or_none` computes every leg's fill first
  and commits either all of them or none.
- **The result reports on its own trustworthiness.** `depth_source`,
  `fill_at`, `crossed_book_skips`, `undeclared_zero_fee_markets`,
  `tick_unvalidated_fills` and `unfilled_counts` ride on `BacktestResult`
  because a number produced from synthesized depth, free fills, or a run
  that silently skipped 40% of its snapshots for crossed quotes is not
  the same number as one produced from recorded books — and the
  difference must be visible where the number is, not only in a log line
  nobody aggregates.

Units (GUARDRAILS.md §4): prices are probabilities in `[0.0, 1.0]`;
`Position.size`, `TradeRecord.size` and every `Leg`/order size are
CONTRACTS (each pays $1.00 at resolution); cash, `cost_basis`, fees and
P&L are USD. Every datetime is aware UTC.
"""
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Literal

from app.config import settings
from app.execution.fill_engine import (
    TICK_VALIDATED_KEY,
    FillReason,
    FillResult,
    SimulatedFillEngine,
    synthesize_book,
)
from app.services.backtesting.data_replay import ResolutionEvent
from app.strategies.base import (
    BaseStrategy,
    Intent,
    Leg,
    MarketSnapshot,
    Signal,
    SignalType,
    normalize_outcome,
    outcome_key,
)
from app.utils.time import ensure_aware, utcnow
from app.venues.base import FeeModel
from app.venues.fees import (
    KalshiFeeModel,
    PolymarketFeeModel,
    category_fee_schedule,
    default_kalshi_schedule,
)
from app.venues.types import (
    FeeSchedule,
    OrderBook,
    OrderRequest,
    OrderSide,
    VenueId,
)

logger = logging.getLogger(__name__)

#: When a fill is simulated relative to the snapshot that produced the
#: signal. `"next"` (the default) is the only look-ahead-free setting;
#: `"same"` is a diagnostic (see the module docstring).
FillAt = Literal["same", "next"]

#: Where a run's depth came from, aggregated over every book the engine
#: walked. `"mixed"` when both `"recorded"` and `"synthetic"` books were
#: used in one run (T08 carry-forward 7).
ResultDepthSource = Literal["synthetic", "recorded", "mixed"]

#: `TradeRecord.metadata` key stamped on every trade produced under
#: `fill_at="same"`. Its presence means the fill used the very snapshot
#: the signal was computed from, i.e. the trade is a DIAGNOSTIC and the
#: run it belongs to must be labeled wherever it is shown
#: (GUARDRAILS.md §1.7).
DIAGNOSTIC_SAME_SNAPSHOT_KEY = "diagnostic_same_snapshot"

#: `TradeRecord.metadata` key stamped on a position closed by market
#: RESOLUTION rather than by a trade. A settlement is not a fill: it
#: consumed no liquidity, it paid $1.00 or $0.00 per contract, and its
#: cash is credited only after `settlement_delay_hours`.
SETTLEMENT_KEY = "settlement"

#: `TradeRecord.side` for a settlement. Distinct from `"BUY"`/`"SELL"`
#: because a settlement is not an execution — a report that counted it as
#: a round-trip fill would misstate both the fill count and the average
#: execution price.
SETTLE_SIDE = "SETTLE"

#: `Backtester._reject` reason for an intent with a leg whose `venue`
#: matches none of the venues ever observed for its `market_id`, even
#: though that market_id HAS been seen on some OTHER venue this run
#: (Phase-1 remediation FIX 2). STRUCTURAL, in the same sense
#: `FillReason`'s `"post_only_taker_engine"`/`"zero_size_order"`/
#: `"crossed_book"` are: retrying the identical leg against a later
#: snapshot reproduces the identical mismatch. Deliberately NOT one of
#: `FillReason`'s six values — this defect never reaches the fill engine
#: at all, it is caught before any book is even looked up — so it must
#: not be confused with (or counted alongside) a market-condition
#: rejection like `"no_eligible_levels"`, which is exactly what this
#: used to read as.
VENUE_MISMATCH_REASON = "venue_mismatch"

#: Resolution coverage below which a result is flagged
#: `low_resolution_coverage` (PLAN.md D6). Under this level most of the
#: run's positions were valued by MARKING them to a last quote rather
#: than by being paid out, so the P&L is an estimate wearing the clothes
#: of a settled number.
_MIN_RESOLUTION_COVERAGE = 0.8

#: Minimum USD notional the engine will attempt for one intent. Below
#: this an order is not worth simulating (and on a real venue would sit
#: under `min_size` for any realistic price), so the intent is rejected
#: with `"below_min_size"` rather than filled as dust. Preserves the
#: pre-T08 `available < 10` floor.
#:
#: T10 retry (GUARDRAILS.md): this floor now lives on `Settings` as
#: `settings.min_trade_usd` (documented there, overridable like
#: `redemption_gas_usd` and the other cost settings) instead of being a
#: bare module constant a strategy author has no way to discover —
#: which is exactly how `binary_complement_arbitrage` shipped a default
#: `min_position_size` that could never clear it. `_MIN_TRADE_USD` is
#: kept here, set to the value read at import time, purely so any
#: external reference to this name keeps resolving; it is a SNAPSHOT,
#: not a live proxy, so the actual sizing check below reads
#: `settings.min_trade_usd` directly rather than this constant — only
#: that read honors a `settings.min_trade_usd` changed after import
#: (e.g. via `monkeypatch.setattr(settings, ...)` in a test).
_MIN_TRADE_USD = settings.min_trade_usd

#: Fraction of cash the engine is willing to commit to a single intent,
#: leaving a buffer for the fees charged on top of the notional.
#: Preserves the pre-T08 `self.portfolio.cash * 0.99`.
_CASH_BUFFER = 0.99

#: Contracts tolerance for "did this leg fill enough?" and "is this
#: position fully closed?". Same magnitude the fill engine and
#: `OrderBook.walk()` use, so the three cannot disagree about whether a
#: dust residual counts.
_SIZE_EPSILON = 1e-9


def position_key(venue: VenueId, market_id: str, outcome: str) -> str:
    """Build the engine's one and only position/price identity.

    T21d (NOTES.md). `f"{venue}:{market_id}:{outcome}"` (PLAN.md D7) was
    spelled out by hand at five sites — `Position.position_id`,
    `_process_snapshot`'s `_current_prices` writes, `_process_resolution`'s
    post-resolution marks, the SELL-leg position lookup in `_leg_plans`,
    and `_record_fill`'s id for a new position — and each one used
    whatever casing its own source happened to carry. That is how a
    position `outcome="TRUMP"` came to key `polymarket:M:TRUMP` while the
    payload that could price it keyed `polymarket:M:Trump`, leaving
    `Portfolio.total_equity` to fall through to `pos.entry_price` and
    mark the position at its ENTRY PRICE forever. Routing every one of
    those sites through this function is what makes the two agree:
    `outcome_key()` (`app.strategies.base`) strips whitespace and folds
    case for EVERY label, not just `"YES"`/`"NO"`.

    The result is byte-identical to the old hand-spelled id for a
    `"YES"`/`"NO"` position, so no identity written down before T21d
    changes.

    Args:
        venue: `"polymarket"` or `"kalshi"`.
        market_id: Venue-native market identifier.
        outcome: Outcome name in any spelling.

    Returns:
        str: `f"{venue}:{market_id}:{outcome_key(outcome)}"`.
    """
    return f"{venue}:{market_id}:{outcome_key(outcome)}"


class SlippageModel(str, Enum):
    """Slippage model types.

    **Only `NONE` and `FIXED` still have meaning.** Slippage is no longer
    a constant applied to a signal price: it falls out of walking the
    book in `SimulatedFillEngine` (PLAN.md D5), so a "model" that invents
    a spread- or volume-derived number on top of a walked fill would be
    double-counting an effect the walk already produced.

    - `NONE`: the leg's limit price is used verbatim.
    - `FIXED`: `BacktestConfig.slippage_value` is added to a BUY limit
      (subtracted from a SELL limit) as a CONSERVATIVE PAD — it makes the
      order reach further down the book and therefore pay more, it does
      not move the fill price directly. The fill price is still whatever
      the book gives.
    - `VOLUME_BASED` / `SPREAD_BASED`: retained so existing callers and
      the API's `SlippageModelEnum` keep importing, but they map to
      `NONE` with a WARNING at `Backtester` construction. They are not
      silently honored, because a caller who asked for spread-based
      slippage and got none must be told.
    """

    NONE = "none"
    FIXED = "fixed"  # Fixed percentage
    VOLUME_BASED = "volume_based"  # Based on trade size vs volume
    SPREAD_BASED = "spread_based"  # Based on bid-ask spread


@dataclass
class BacktestConfig:
    """Configuration for a backtest run.

    Attributes:
        start_date: Backtest start timestamp. MUST be aware UTC.
        end_date: Backtest end timestamp. MUST be aware UTC.
        initial_capital: Starting capital in USD.
        fee_rate: **DEPRECATED.** Fees now come from `FeeModel` via
            `SimulatedFillEngine` (GUARDRAILS.md §1.5), keyed off the
            venue and the market's category. This field is kept only so
            pre-T08 callers (`app/tasks/backtesting.py`, the API's
            `BacktestRequest`) keep constructing. When it is passed
            explicitly and `> 0` it is applied **IN ADDITION** to the
            venue fee, as `notional * fee_rate` on every fill — the old
            caller's fee is therefore not silently lost, but it is also
            not mistaken for the venue's real fee. New callers should
            leave it at `0.0`.
        slippage_model: See `SlippageModel` — only `NONE` and `FIXED`
            still have meaning.
        slippage_value: For `SlippageModel.FIXED`, the conservative pad
            added to a BUY limit / subtracted from a SELL limit, in
            probability units.
        markets_filter: Optional list of market IDs to include.
        max_position_pct: Maximum notional for one intent as a fraction
            of portfolio value.
        fill_at: `"next"` (default) fills an intent against the NEXT
            snapshot of each leg's market; `"same"` fills against the
            snapshot that produced the signal and is a DIAGNOSTIC ONLY
            (see the module docstring and `DIAGNOSTIC_SAME_SNAPSHOT_KEY`).
        liquidity_fraction: Fraction of 24h notional assumed resting at
            the touch when a book must be synthesized from a
            top-of-book-only snapshot. `None` (default) uses
            `settings.liquidity_fraction`.
        partial_tolerance: For `atomicity="all_or_none"`, the fraction of
            a leg's requested size that may go unfilled and still count
            as a filled leg. Default `0.0` — every leg must fill in full.
        pending_ttl: How long a `fill_at="next"` intent waits for the
            next snapshot of each of its markets before expiring. Default
            one hour.
        redemption_gas_usd: Polygon cost of redeeming a settled position,
            **USD PER POSITION — not per contract** (PLAN.md §3). Getting
            that unit wrong is not a rounding error: per-contract, a
            1,000-contract position would be charged $50 instead of
            $0.05, making every large position look catastrophic and
            every small one free. `None` (default) uses
            `settings.redemption_gas_usd`.
        settlement_delay_hours: Hours between a market resolving and its
            proceeds becoming SPENDABLE cash. The money is counted in
            equity immediately (it exists) but cannot fund a new intent
            until it is released, which is what stops a backtest from
            compounding capital it does not yet have. `None` (default)
            uses `settings.settlement_delay_hours`.
    """

    start_date: datetime
    end_date: datetime
    initial_capital: float = 10000.0
    fee_rate: float = 0.0
    slippage_model: SlippageModel = SlippageModel.FIXED
    slippage_value: float = 0.001  # 0.1% default
    markets_filter: list[str] | None = None
    max_position_pct: float = 0.20
    fill_at: FillAt = "next"
    liquidity_fraction: float | None = None
    partial_tolerance: float = 0.0
    pending_ttl: timedelta = timedelta(hours=1)
    redemption_gas_usd: float | None = None
    settlement_delay_hours: float | None = None

    def __post_init__(self) -> None:
        """Validate configuration, timezone-awareness first.

        `ensure_aware` runs BEFORE the `start_date >= end_date`
        comparison on purpose: comparing a naive datetime against an
        aware one raises an opaque `TypeError` from deep inside
        `datetime`, and the caller's actual mistake (a naive date) must
        be named (GUARDRAILS.md §4, PLAN.md R9).

        Raises:
            TypeError: If `start_date`/`end_date` is not a `datetime`.
            ValueError: If either is naive, if `start_date` is not before
                `end_date`, if `initial_capital` is not positive, if
                `fee_rate` is outside `[0, 1)`, if `max_position_pct` is
                outside `(0, 1]`, if `fill_at` is not a `FillAt`, if
                `liquidity_fraction` is negative or not finite, if
                `partial_tolerance` is outside `[0, 1]`, or if
                `pending_ttl` is not positive.
        """
        ensure_aware(self.start_date)
        ensure_aware(self.end_date)
        if self.start_date >= self.end_date:
            raise ValueError("start_date must be before end_date")
        if self.initial_capital <= 0:
            raise ValueError("initial_capital must be positive")
        if not 0 <= self.fee_rate < 1:
            raise ValueError("fee_rate must be between 0 and 1")
        if not 0 < self.max_position_pct <= 1:
            raise ValueError("max_position_pct must be between 0 and 1")
        if self.fill_at not in ("same", "next"):
            raise ValueError(
                f"fill_at must be 'same' or 'next', got {self.fill_at!r}"
            )
        if self.liquidity_fraction is not None and not (
            math.isfinite(self.liquidity_fraction) and self.liquidity_fraction >= 0.0
        ):
            raise ValueError(
                "liquidity_fraction must be finite and >= 0, got "
                f"{self.liquidity_fraction!r}"
            )
        if not 0.0 <= self.partial_tolerance <= 1.0:
            raise ValueError(
                f"partial_tolerance must be in [0, 1], got {self.partial_tolerance!r}"
            )
        if self.pending_ttl <= timedelta(0):
            raise ValueError(f"pending_ttl must be positive, got {self.pending_ttl!r}")
        if self.redemption_gas_usd is not None and not (
            math.isfinite(self.redemption_gas_usd) and self.redemption_gas_usd >= 0.0
        ):
            raise ValueError(
                "redemption_gas_usd must be finite and >= 0, got "
                f"{self.redemption_gas_usd!r}"
            )
        if self.settlement_delay_hours is not None and not (
            math.isfinite(self.settlement_delay_hours)
            and self.settlement_delay_hours >= 0.0
        ):
            raise ValueError(
                "settlement_delay_hours must be finite and >= 0, got "
                f"{self.settlement_delay_hours!r}"
            )

    @property
    def effective_redemption_gas_usd(self) -> float:
        """Return the per-POSITION redemption cost actually applied.

        Returns:
            float: `redemption_gas_usd` when set, else
                `settings.redemption_gas_usd` (PLAN.md D11).
        """
        if self.redemption_gas_usd is None:
            return float(settings.redemption_gas_usd)
        return self.redemption_gas_usd

    @property
    def effective_settlement_delay(self) -> timedelta:
        """Return the settlement delay actually applied.

        Returns:
            timedelta: `settlement_delay_hours` when set, else
                `settings.settlement_delay_hours`.
        """
        hours = (
            float(settings.settlement_delay_hours)
            if self.settlement_delay_hours is None
            else self.settlement_delay_hours
        )
        return timedelta(hours=hours)

    @property
    def effective_liquidity_fraction(self) -> float:
        """Return the liquidity fraction actually used for synthesis.

        Returns:
            float: `liquidity_fraction` when set, else
                `settings.liquidity_fraction` (PLAN.md D11 — a cost is
                configuration, never a literal).
        """
        if self.liquidity_fraction is None:
            return float(settings.liquidity_fraction)
        return self.liquidity_fraction


@dataclass
class Position:
    """An open position in the portfolio.

    Attributes:
        market_id: Venue-native market identifier.
        outcome: Position outcome — the DISPLAY label (`"YES"`, `"NO"`,
            or a bundle's named outcome such as `"Trump"`, in the
            venue's own casing). Passed through `normalize_outcome()` in
            `__post_init__` (Phase-1 remediation FIX 1) so a
            Polymarket-cased `"Yes"` and a Kalshi-cased `"YES"` are one
            label and surrounding whitespace is stripped. `position_id`
            below does NOT read this directly — it goes through
            `outcome_key()`, which is what makes a non-binary label's
            identity casing-safe too (T21d).
        token_id: Token identifier.
        entry_price: Average entry price, a probability in [0.0, 1.0].
        size: Position size in CONTRACTS.
        entry_time: When the position was opened (aware UTC).
        stop_loss: Optional stop loss price.
        take_profit: Optional take profit price.
        cost_basis: Total USD paid to acquire the position, fees included.
        metadata: Additional position metadata.
        venue: `"polymarket"` or `"kalshi"` — part of `position_id`
            (PLAN.md D7), because the same `market_id` on two venues is
            two different positions.
    """

    market_id: str
    outcome: str
    token_id: str
    entry_price: float
    size: float
    entry_time: datetime
    stop_loss: float | None = None
    take_profit: float | None = None
    cost_basis: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)
    venue: VenueId = "polymarket"

    def __post_init__(self) -> None:
        """Normalize `outcome` casing and calculate cost basis if not provided."""
        self.outcome = normalize_outcome(self.outcome)
        if self.cost_basis == 0.0:
            self.cost_basis = self.entry_price * self.size

    @property
    def position_id(self) -> str:
        """Unique identifier for this position.

        Returns:
            str: `f"{venue}:{market_id}:{outcome_key(outcome)}"`
                (PLAN.md D7), via `position_key()`. Built from the
                outcome's IDENTITY, never its display label, so
                `"polymarket:M:Yes"`/`"polymarket:M:YES"` and
                `"polymarket:M:TRUMP"`/`"polymarket:M:Trump"`/
                `"polymarket:M:Trump "` can never each exist as a
                distinct position id for what is one position (T21d
                defects 5a/5b).
        """
        return position_key(self.venue, self.market_id, self.outcome)

    def current_value(self, current_price: float) -> float:
        """Calculate current position value."""
        return current_price * self.size

    def unrealized_pnl(self, current_price: float) -> float:
        """Calculate unrealized P&L."""
        return self.current_value(current_price) - self.cost_basis

    def unrealized_pnl_pct(self, current_price: float) -> float:
        """Calculate unrealized P&L percentage."""
        if self.cost_basis == 0:
            return 0.0
        return self.unrealized_pnl(current_price) / self.cost_basis


@dataclass
class Portfolio:
    """Portfolio state during backtest.

    Attributes:
        cash: SPENDABLE cash balance, USD. Settlement proceeds are NOT
            here until they are released — see `pending_settlements`.
        positions: Open positions by `position_id`.
        equity_history: List of (timestamp, equity) tuples.
        realized_pnl: Total realized profit/loss, USD. A settlement's
            P&L lands here at RESOLUTION time, not at release time: the
            gain is real the moment the market resolves; only the cash is
            delayed.
        peak_value: Maximum portfolio value seen (for drawdown).
        pending_settlements: `(available_at, amount_usd)` entries for
            resolved positions whose proceeds have not yet cleared
            (PLAN.md D6: capital stays locked for
            `settlement_delay_hours`). Counted in `total_equity` — the
            money exists — but deliberately absent from `cash`, which is
            what sizes new intents. That split is the whole mechanism: a
            backtest that credited settlement instantly would compound
            capital days before a real account could, inventing returns
            that no live system could have earned. An amount may be
            NEGATIVE: a losing position still pays redemption gas.
    """

    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    equity_history: list[tuple[datetime, float]] = field(default_factory=list)
    realized_pnl: float = 0.0
    peak_value: float = 0.0
    pending_settlements: list[tuple[datetime, float]] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Initialize peak value."""
        if self.peak_value == 0.0:
            self.peak_value = self.cash

    @property
    def pending_settlement_total(self) -> float:
        """Return the USD sitting in unreleased settlement proceeds."""
        return math.fsum(amount for _, amount in self.pending_settlements)

    def total_equity(self, prices: dict[str, float]) -> float:
        """Calculate total portfolio equity.

        Includes unreleased settlement proceeds: that money has been
        earned and is merely in transit, so excluding it would make the
        equity curve dip at every resolution and recover a day later —
        a drawdown the account never actually experienced.

        THE `entry_price` FALLBACK IS NOT A SAFE DEFAULT (T21d defect
        5c). A position with no entry in `prices` is marked at what it
        COST, which reads as a position that has not moved — the most
        innocuous-looking number this method could produce, and a
        fiction. This stays a pure function of `(positions, prices)` and
        does not log, because it is called several times per snapshot
        and has no idea whether a missing key is a transient (this
        market did not tick) or permanent (the key can never match)
        condition. `Backtester._note_unmarked_positions` is what makes
        the permanent case loud: it checks the same `position_id`s
        against the same dict once per snapshot of the position's own
        market, and again at end of run, and surfaces every one it finds
        on `BacktestResult.unmarked_positions`.

        Args:
            prices: Dict mapping position_id to current price.

        Returns:
            Total portfolio value (spendable cash + open positions
                marked to `prices` + unreleased settlements).
        """
        positions_value = sum(
            pos.current_value(prices.get(pos.position_id, pos.entry_price))
            for pos in self.positions.values()
        )
        return self.cash + positions_value + self.pending_settlement_total

    def update_equity_history(
        self,
        timestamp: datetime,
        prices: dict[str, float],
    ) -> None:
        """Record current equity in history."""
        equity = self.total_equity(prices)
        self.equity_history.append((timestamp, equity))

        # Update peak for drawdown tracking
        if equity > self.peak_value:
            self.peak_value = equity

    def current_drawdown(self, prices: dict[str, float]) -> float:
        """Calculate current drawdown from peak."""
        equity = self.total_equity(prices)
        if self.peak_value == 0:
            return 0.0
        return (self.peak_value - equity) / self.peak_value


@dataclass
class TradeRecord:
    """Record of an executed trade.

    Attributes:
        timestamp: When the trade was executed (aware UTC).
        market_id: Venue-native market identifier.
        outcome: YES or NO.
        token_id: Token traded.
        side: BUY or SELL.
        price: Size-weighted execution price from the book walk.
        size: Contracts filled.
        fee: Total fee paid, USD — the venue fee plus any deprecated
            `BacktestConfig.fee_rate` component.
        slippage: `price - limit_price` for a BUY (`limit_price - price`
            for a SELL): how much worse than the intent's limit the walk
            actually was.
        pnl: Realized P&L (closing trades only).
        signal_confidence: Original intent confidence.
        metadata: Additional trade metadata. Carries
            `DIAGNOSTIC_SAME_SNAPSHOT_KEY` under `fill_at="same"` and
            `MARK_TO_MARKET_KEY` on an end-of-run mark.
        venue: `"polymarket"` or `"kalshi"`.
        intent_id: The intent this trade belongs to, or `None` for an
            engine-initiated exit.
        requested_size: Contracts this leg was asked for BEFORE the
            engine's own `max_position_pct`/cash cap — `Leg.size_contracts`
            when every leg declared one, else the equal-contract count
            `_leg_sizes` derived from the strategy's USD budget. `None`
            for an engine-initiated trade (a settlement or an
            end-of-run mark) that no intent sized.
        ordered_size: Contracts actually SENT to the fill engine for
            this leg — `requested_size` after the capital cap, and after
            a SELL's cap at the position it can close. `None` for an
            engine-initiated trade.

            The three sizes on this record are deliberately distinct
            (T28): `requested_size` is what the strategy asked for,
            `ordered_size` is what capital allowed, and `size` is what
            the book delivered. `ordered_size < requested_size` is
            capital-cap-driven downsizing; `size < ordered_size` is
            book-depth-driven downsizing. Collapsing the two into one
            "downsized" number makes a capital sweep read backwards,
            because the capital cap binds LESS as capital grows while
            depth exhaustion binds MORE (PLAN.md R4).
    """

    timestamp: datetime
    market_id: str
    outcome: str
    token_id: str
    side: str
    price: float
    size: float
    fee: float = 0.0
    slippage: float = 0.0
    pnl: float | None = None
    signal_confidence: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)
    venue: VenueId = "polymarket"
    intent_id: str | None = None
    requested_size: float | None = None
    ordered_size: float | None = None

    @property
    def total_cost(self) -> float:
        """Total cost including fees."""
        return (self.price * self.size) + self.fee

    @property
    def is_profitable(self) -> bool:
        """Check if trade was profitable."""
        return self.pnl is not None and self.pnl > 0


@dataclass
class CoverageReport:
    """Survivorship-coverage census for one run (PLAN.md D6).

    Survivorship bias in a prediction-market backtest is not subtle: the
    markets that RESOLVE inside a replay window are systematically
    shorter-dated and more decisive than the ones that do not, and a
    result whose P&L came mostly from marked-to-quote positions is
    describing a different population than one whose P&L came from
    payouts. This report makes that population visible next to the
    number, rather than leaving the reader to assume every position was
    settled.

    Attributes:
        markets_seen: Distinct markets the run observed — any market
            that produced a snapshot OR a resolution event.
        markets_resolved: Markets that produced a `ResolutionEvent`
            inside the window, i.e. whose positions were PAID rather
            than marked.
        markets_closed_unresolved: `markets_seen - markets_resolved`.
            Named for the population it matters for — markets whose
            replay ended without an answer — and it deliberately
            includes both "still open" and "closed but no resolution
            data was recorded", because a snapshot carries no close
            state (that would be look-ahead) and the run genuinely
            cannot tell the two apart from the stream alone.
        snapshots_per_market: `market_id -> snapshot count`. A market
            with three snapshots across a six-month window is not
            evidence, and this is where that shows up.
        resolution_coverage: `markets_resolved / markets_seen`, or `0.0`
            when nothing was seen.
        low_resolution_coverage: `resolution_coverage < 0.8`. A `True`
            here means the run's return is mostly an ESTIMATE, and must
            be labeled as such wherever it is shown (GUARDRAILS.md §1.7).
    """

    markets_seen: int = 0
    markets_resolved: int = 0
    markets_closed_unresolved: int = 0
    snapshots_per_market: dict[str, int] = field(default_factory=dict)
    resolution_coverage: float = 0.0
    low_resolution_coverage: bool = True


@dataclass
class BacktestResult:
    """Results from a completed backtest.

    The trailing block of fields exists so a result REPORTS ON ITS OWN
    TRUSTWORTHINESS (GUARDRAILS.md §1.7). A number produced from
    synthesized depth, from same-snapshot fills, from fills that were
    never tick-checked, from a market whose fee resolved to an undeclared
    zero, or from a run that declined 40% of its orders for crossed
    quotes is not the same number as one produced without those — and the
    difference has to travel WITH the number.

    Attributes:
        config: Backtest configuration used.
        strategy_name: Name of strategy tested.
        strategy_config: Strategy configuration.
        start_time: When the backtest process started (aware UTC).
        end_time: When it completed (aware UTC).
        initial_capital: Starting capital, USD.
        final_value: Final portfolio value, USD.
        total_return: Total return (final/initial - 1).
        equity_curve: List of (timestamp, equity) tuples.
        trades: List of all executed trades, in execution order.
        positions_final: Open positions at end.
        snapshots_processed: Number of market snapshots processed.
        signals_generated: Number of signals/intents generated (kept as
            the pre-T08 name; equal to `intents_generated`).
        errors: List of any errors encountered.
        intents_generated: Intents the strategy produced (a `Signal` is
            normalized to a one-leg intent and counts as one).
        intents_executed: Intents that committed at least one fill.
        intent_rejections: Intents that committed nothing — an
            `all_or_none` intent with a leg short of
            `partial_tolerance`, a `best_effort` intent whose every leg
            was unfilled, or an intent too small/too poor to size.
        intent_expirations: `fill_at="next"` intents that never got the
            next snapshot of every leg's market within `pending_ttl` (or
            were still waiting when the replay ended).
        rejection_reasons: `FillReason -> count` for rejected intents,
            using the fill engine's own six-value vocabulary rather than
            a parallel one.
        intents_sized: Intents that got past `_leg_sizes` — a size
            vector was computed and legs were planned against a book.
            The denominator for the two counters below: an intent that
            could not be sized at all (no budget, notional under
            `settings.min_trade_usd`) never reached the book, so it is
            evidence about neither capital nor depth.
        intents_capital_capped: Of `intents_sized`, how many had at
            least one leg scaled DOWN by the engine's own
            `min(portfolio_value * max_position_pct, cash * 0.99)` cap.
            This is the "I ran out of money" count, and it falls to zero
            as capital grows.
        intents_depth_limited: Of `intents_sized`, how many had at least
            one leg whose walk CONSUMED THE BOOK AND STILL CAME UP
            SHORT — `0 < filled_size < ordered_size`. This is the "the
            book ran out" count, and it is the one a capital sweep
            exists to surface (PLAN.md R4). It is counted from the
            leg PLANS, before atomicity is applied, so an `all_or_none`
            intent that was rejected BECAUSE a leg came up short is
            counted here even though it produced no `TradeRecord` at all.

            Deliberately NOT counted: a leg that filled NOTHING. A
            `FillResult` with `filled_size == 0` reports
            `"no_eligible_levels"` whether the side was empty (real
            depth exhaustion) or every level was simply worse than the
            limit (a price move under `fill_at="next"`, which has
            nothing to do with depth), and this engine cannot tell those
            apart from the result alone. Counting them would re-pollute
            the metric in a new dimension; they are visible instead in
            `rejection_reasons`/`unfilled_counts` and in the sweep's
            `fill_rate`.
        intents_depth_blocked: Of `intents_depth_limited`, how many
            committed NOTHING — an `all_or_none` intent killed by a
            short leg. These are the depth failures that leave no trace
            in `trades`, and they are also the exact subset on which
            this counter overlaps `intents_executed`/`fill_rate`. The
            two are never summed; this field exists so the overlap is a
            number rather than an inference.
        depth_source: `"recorded"`, `"synthetic"`, or `"mixed"` across
            every book this run walked. Defaults to `"synthetic"` when
            the run walked no book at all — the conservative label, since
            claiming `"recorded"` for depth that was never observed is
            exactly the hidden assumption GUARDRAILS.md §1.7 forbids.
        fill_at: The `BacktestConfig.fill_at` this result was produced
            under, denormalized here so a report can label a
            same-snapshot (diagnostic) run without re-reading the config.
        crossed_book_skips: Orders declined because the book was CROSSED
            (`best_bid > best_ask`). A data-quality metric, not a trading
            outcome: a run that skipped a large share of its orders for
            this reason is telling you its `PriceHistory` rows are bad.
        undeclared_zero_fee_markets: `(venue, market_id)` pairs whose fee
            schedule resolved to a zero taker rate from a source that
            never declared one — i.e. fills that were FREE, which is
            almost always a fee-resolution bug. Empty is the expected
            state.
        tick_unvalidated_fills: Fill results committed without a
            `VenueMarket`, so neither `tick_size` nor `min_size` was
            enforced. Replayed prices are routinely off any fixed tick
            grid, so the backtester deliberately does not pass a market
            (T08 carry-forward 2) — which makes this count the whole
            fill population, and says so rather than implying
            venue-realistic prices.
        unfilled_counts: `FillReason -> count` over every order the fill
            engine declined during the run.
        positions_settled: Positions closed by market RESOLUTION (paid
            $1.00 or $0.00 per contract), as opposed to by a trade.
        unrealized_at_end: Positions still open when the replay ended.
            They ARE included in `final_value`, marked to their last
            known price — and they are listed here separately because a
            marked position is not a realized one (PLAN.md D6). A result
            whose return is dominated by this list is a forecast, not a
            track record.
        unrealized_notional_at_end: The USD those positions were marked
            at, so the reader does not have to re-mark them to see how
            much of `final_value` is an estimate.
        unmarked_positions: Position ids that were held at some point
            with NO price available for them, and were therefore valued
            at their ENTRY PRICE by `Portfolio.total_equity` (T21d
            defect 5c). Empty is the expected state, and a non-empty
            tuple means part of `equity_curve`/`final_value` is not a
            market number at all — an entry-priced position looks
            exactly like one that has not moved, which is why this is
            reported rather than left to be inferred. Each id is also
            logged once at WARNING when it is first noticed.
        coverage: The run's survivorship census — see `CoverageReport`.
    """

    config: BacktestConfig
    strategy_name: str
    strategy_config: dict[str, Any]
    start_time: datetime
    end_time: datetime
    initial_capital: float
    final_value: float
    total_return: float
    equity_curve: list[tuple[datetime, float]]
    trades: list[TradeRecord]
    positions_final: dict[str, Position]
    snapshots_processed: int = 0
    signals_generated: int = 0
    errors: list[str] = field(default_factory=list)
    intents_generated: int = 0
    intents_executed: int = 0
    intent_rejections: int = 0
    intent_expirations: int = 0
    rejection_reasons: dict[str, int] = field(default_factory=dict)
    intents_sized: int = 0
    intents_capital_capped: int = 0
    intents_depth_limited: int = 0
    intents_depth_blocked: int = 0
    depth_source: ResultDepthSource = "synthetic"
    fill_at: FillAt = "next"
    crossed_book_skips: int = 0
    undeclared_zero_fee_markets: tuple[tuple[str, str], ...] = ()
    tick_unvalidated_fills: int = 0
    unfilled_counts: dict[str, int] = field(default_factory=dict)
    positions_settled: int = 0
    unrealized_at_end: list[Position] = field(default_factory=list)
    unrealized_notional_at_end: float = 0.0
    unmarked_positions: tuple[str, ...] = ()
    coverage: CoverageReport = field(default_factory=CoverageReport)

    @property
    def total_trades(self) -> int:
        """Total number of trades executed."""
        return len(self.trades)

    @property
    def winning_trades(self) -> int:
        """Number of profitable trades."""
        return sum(1 for t in self.trades if t.is_profitable)

    @property
    def losing_trades(self) -> int:
        """Number of losing trades."""
        return sum(1 for t in self.trades if t.pnl is not None and t.pnl < 0)


@dataclass
class PendingIntent:
    """An intent queued under `fill_at="next"`, waiting for fresh books.

    The queue is what makes the default configuration look-ahead free:
    the intent was computed from snapshot N's prices, so it may only be
    filled against a book the strategy had NOT yet seen when it decided.

    Keying: an intent waits on `(venue, market_id)` for EVERY leg
    (`waiting`), because a cross-venue intent's two legs live on two
    independent snapshot streams and neither one arriving is sufficient.
    Each key is satisfied by the FIRST snapshot of that market to arrive
    after the intent was queued, and that snapshot — not a later, fresher
    one — is the book the leg fills against (`leg_snapshots`), so the
    result does not depend on how far apart the two streams happen to be.

    Attributes:
        intent: The intent to execute.
        created_ts: The timestamp of the snapshot that produced it
            (aware UTC). `pending_ttl` is measured from here.
        intent_id: Deterministic per-run id, used to build each leg's
            `client_order_id` as `f"{intent_id}:{leg_index}:0"` (PLAN.md
            D4's idempotency-key shape).
        size_usd: The USD budget the strategy sized this intent at.
        waiting: `(venue, market_id)` keys still awaiting their next
            snapshot.
        leg_snapshots: `(venue, market_id) -> MarketSnapshot`, the
            snapshot each already-satisfied key will fill against.
        signal: The originating `Signal` when the strategy emitted one,
            kept so `stop_loss`/`take_profit` (which a `Leg` cannot
            carry) survive the queue.
    """

    intent: Intent
    created_ts: datetime
    intent_id: str = ""
    size_usd: float = 0.0
    waiting: set[tuple[VenueId, str]] = field(default_factory=set)
    leg_snapshots: dict[tuple[VenueId, str], MarketSnapshot] = field(
        default_factory=dict
    )
    signal: Signal | None = None


@dataclass
class _LegPlan:
    """One leg's simulated outcome, computed BEFORE anything is committed.

    `all_or_none` atomicity is only meaningful if every leg's fill is
    known before any of them is booked, so `_execute_intent` builds the
    full list of these first and commits from it afterwards.

    Attributes:
        leg: The leg this plan is for.
        index: Leg index within the intent (part of `client_order_id`).
        size: Contracts requested for this leg, post-capital-cap. This
            is what `_atomicity_blocker` measures a fill against.
        requested_size: Contracts this leg asked for BEFORE the engine's
            `max_position_pct`/cash cap. `requested_size > size` is
            exactly "the capital cap bound on this leg" (T28).
        ordered_size: Contracts actually placed on the `OrderRequest` —
            `size`, further capped for a SELL at the position it can
            close (neither venue supports naked shorts, PLAN.md D8).
            Distinct from `size` so a position-capped SELL is not
            mistaken for a book-depth shortfall (T28): the only honest
            denominator for "did the book supply what I ordered" is the
            size that was ORDERED.
        limit_price: The padded limit price actually sent.
        snapshot: The snapshot whose book this leg was filled against.
        result: What the fill engine returned, or `None` when no book
            existed at all for this leg.
    """

    leg: Leg
    index: int
    size: float
    requested_size: float
    ordered_size: float
    limit_price: float
    snapshot: MarketSnapshot | None
    result: FillResult | None


class Backtester:
    """Backtesting engine for strategy evaluation.

    Simulates trading a strategy against historical market data with
    depth-walked fills (`SimulatedFillEngine`), real venue fee models,
    multi-leg atomicity, and next-snapshot execution.

    Example:
        ```python
        config = BacktestConfig(
            start_date=datetime(2024, 1, 1, tzinfo=UTC),
            end_date=datetime(2024, 6, 1, tzinfo=UTC),
            initial_capital=10000,
        )
        strategy = get_strategy("catalyst_momentum")
        backtester = Backtester(config, strategy)
        result = await backtester.run(data_replayer)
        ```
    """

    def __init__(
        self,
        config: BacktestConfig,
        strategy: BaseStrategy,
        progress_callback: Callable[[float], None] | None = None,
    ) -> None:
        """Initialize backtester.

        Args:
            config: Backtest configuration.
            strategy: Strategy to test.
            progress_callback: Optional callback for progress updates (0-1).
        """
        self.config = config
        self.strategy = strategy
        self.progress_callback = progress_callback

        # Initialize portfolio
        self.portfolio = Portfolio(cash=config.initial_capital)

        # Tracking
        self.trades: list[TradeRecord] = []
        self.errors: list[str] = []
        self.snapshots_processed = 0
        self.signals_generated = 0
        self.intents_generated = 0
        self.intents_executed = 0
        self.intent_rejections = 0
        self.intent_expirations = 0
        self.intents_sized = 0
        self.intents_capital_capped = 0
        self.intents_depth_limited = 0
        self.intents_depth_blocked = 0
        self.tick_unvalidated_fills = 0
        self.positions_settled = 0
        self._rejection_reasons: dict[str, int] = {}
        # Coverage census (PLAN.md D6). Markets are counted by
        # `market_id` alone — the type `CoverageReport` publishes — which
        # assumes a market id is unique within one replay; it is, since a
        # replay streams one venue's `PriceHistory`.
        self._snapshots_per_market: dict[str, int] = {}
        self._markets_seen: set[str] = set()
        self._markets_resolved: set[str] = set()
        # (venue, market_id) pairs that have already settled. Their books
        # are refused from here on: a quote printed after resolution is
        # not tradeable information, and filling against one would credit
        # the run with a trade no live account could have made.
        self._resolved_keys: set[tuple[VenueId, str]] = set()
        self._current_prices: dict[str, float] = {}
        # T21d defect 5c: position ids that were held while having NO
        # entry in `_current_prices`, i.e. positions `total_equity`
        # marked at their entry price. Insertion-ordered so the report
        # lists them in the order they were first noticed; the value is
        # the reason first recorded, used to log each one exactly once
        # instead of on every snapshot.
        self._unmarked_positions: dict[str, str] = {}
        # T21d defect 5b: position ids whose stop-loss/take-profit check
        # was skipped for want of a price. Logged once each rather than
        # on every tick of a long replay.
        self._exit_checks_skipped: set[str] = set()
        # Latest snapshot per (venue, market_id) — the engine never looks
        # at anything but the snapshot stream it has already consumed, so
        # this cache can only ever hold the PAST.
        self._snapshots: dict[tuple[VenueId, str], MarketSnapshot] = {}
        # Market category, for the Polymarket per-category taker rate.
        self._market_categories: dict[str, str | None] = {}
        # market_id -> every venue a snapshot for that market_id has
        # actually been observed on this run (Phase-1 remediation FIX 2).
        # Distinguishes "this market has never printed a snapshot at all"
        # (an ordinary, retryable market-condition state) from "this
        # leg's venue is simply wrong" (STRUCTURAL — the market exists,
        # just elsewhere): `Signal.to_intent()` used to hard-default
        # every leg to `venue="polymarket"` regardless of which venue's
        # snapshot produced the signal, so a signal generated from a
        # Kalshi snapshot silently produced a Polymarket-venued leg that
        # then failed as `"no_eligible_levels"` or quietly expired at
        # `pending_ttl` — both of which read as ordinary illiquidity
        # rather than naming the true defect. See `_venue_mismatch`.
        self._market_venues: dict[str, set[VenueId]] = {}
        self._pending: list[PendingIntent] = []
        self._depth_sources: set[str] = set()
        self._intent_seq = 0

        fee_models: dict[VenueId, FeeModel] = {
            "polymarket": PolymarketFeeModel(),
            "kalshi": KalshiFeeModel(),
        }
        self._fill_engine = SimulatedFillEngine(
            fee_models=fee_models,
            schedules=self._resolve_fee_schedule,
        )

        if config.slippage_model in (
            SlippageModel.VOLUME_BASED,
            SlippageModel.SPREAD_BASED,
        ):
            # Never silently honored as NONE: a caller who asked for
            # spread-based slippage and is getting a depth walk instead
            # must be told, or they will read the result as containing an
            # effect it does not contain.
            logger.warning(
                "slippage_model %r is no longer modeled: slippage now falls out of "
                "walking the book (PLAN.md D5); treating it as SlippageModel.NONE",
                config.slippage_model.value,
                extra={"slippage_model": config.slippage_model.value},
            )
        if config.fill_at == "same":
            logger.warning(
                "fill_at='same' is a DIAGNOSTIC: fills use the very snapshot the "
                "signal was computed from, which is look-ahead. Every resulting "
                "trade is stamped %s and the result must be labeled wherever it "
                "is shown (GUARDRAILS.md 1.7)",
                DIAGNOSTIC_SAME_SNAPSHOT_KEY,
            )

    # ------------------------------------------------------------------
    # Fees and books
    # ------------------------------------------------------------------

    def _resolve_fee_schedule(self, venue: VenueId, market_id: str) -> FeeSchedule:
        """Resolve the `FeeSchedule` for one (venue, market) during replay.

        GUARDRAILS.md §1.5: a fee is never a literal. Polymarket's rate
        comes from the market's own category via
        `app.venues.fees.category_fee_schedule` (which itself honors
        `settings.polymarket_taker_fee_overrides`, labeling the schedule
        `source="settings_override"` rather than `"category_table"` when
        an override supplied the rate — Phase-1 remediation FIX 3, so an
        operator-zeroed category is never mistaken for the published
        table's genuine zero); Kalshi's comes from `Settings` via
        `default_kalshi_schedule`. A replayed snapshot carries no CLOB
        fee payload, so the per-market `maker_base_fee`/`taker_base_fee`
        override PLAN.md §3 describes is not available here — the
        category table is the best-declared source a replay has.

        Args:
            venue: The venue the order is on.
            market_id: The venue-native market id.

        Returns:
            FeeSchedule: `source="category_table"` for Polymarket (a
                DECLARED zero source, so the genuinely-zero Geopolitics
                rate does not trip the fill engine's undeclared-free-fill
                guard) unless `settings.polymarket_taker_fee_overrides`
                supplied the rate, in which case `source=
                "settings_override"` (NOT declared, so an operator-
                zeroed category IS flagged); `source="settings_default"`
                for Kalshi.
        """
        if venue == "kalshi":
            return default_kalshi_schedule()
        return category_fee_schedule(self._market_categories.get(market_id))

    def _book_for(
        self, snapshot: MarketSnapshot, venue: VenueId, market_id: str, outcome: str
    ) -> OrderBook | None:
        """Return the book to fill one (venue, market, outcome) against.

        Prefers a RECORDED book carried on the snapshot; falls back to
        synthesizing one from top-of-book quotes plus
        `liquidity_fraction * volume_24h` (PLAN.md D6). Every book used
        contributes its `depth_source` to the run's aggregate label.

        Args:
            snapshot: The snapshot supplying the book or the quotes.
            venue: Venue of the leg being filled.
            market_id: Market of the leg being filled.
            outcome: Outcome of the leg being filled.

        Returns:
            OrderBook | None: `None` when the snapshot is for a different
                market/venue, or when `outcome` is neither YES nor NO and
                so cannot be synthesized (a multi-outcome bundle leg on a
                venue whose snapshot carries only YES/NO quotes).
        """
        if (venue, market_id) in self._resolved_keys:
            # The market has already paid out. Any quote still printing
            # on it is post-resolution noise, and a fill against it would
            # be a trade no live account could have made.
            logger.debug(
                "no book: market already resolved",
                extra={"venue": venue, "market_id": market_id},
            )
            return None
        book = snapshot.book
        if (
            book is not None
            and book.venue == venue
            and book.market_id == market_id
            # T21d: the same identity every KEYING site uses, so a
            # trailing space in one payload's label cannot hide a book
            # that is plainly for this outcome.
            and outcome_key(book.outcome) == outcome_key(outcome)
        ):
            self._depth_sources.add(book.depth_source)
            return book
        if snapshot.venue != venue or snapshot.market_id != market_id:
            return None
        if outcome_key(outcome) not in ("YES", "NO"):
            # T21 carry-forward 1 (NOTES.md): a multi-outcome bundle leg
            # (e.g. a named candidate) has no binary complement to
            # synthesize FROM -- `synthesize_book` only knows how to
            # invent a YES or a NO side from top-of-book quotes, and
            # there is no principled way to fabricate depth for an
            # arbitrary outcome label without ever having observed one.
            # This is therefore an EXPLICIT, LABELED skip (a recorded
            # `BookSnapshot`, T21, is the only way this leg ever fills),
            # never a silent YES/NO guess.
            logger.debug(
                "no book: outcome is neither YES nor NO and no recorded "
                "book was found; synthesis is undefined for a "
                "non-binary outcome",
                extra={"venue": venue, "market_id": market_id, "outcome": outcome},
            )
            return None
        synthesized = synthesize_book(
            snapshot, self.config.effective_liquidity_fraction, outcome=outcome
        )
        self._depth_sources.add(synthesized.depth_source)
        return synthesized

    def _price_for_outcome(
        self, snapshot: MarketSnapshot, outcome: str
    ) -> float | None:
        """Resolve a mark-to-market price for one outcome of `snapshot`'s market.

        T21 carry-forward 2 (NOTES.md): `_process_snapshot`'s
        `_current_prices` cache used to be written under ONLY the two
        literal keys `"YES"`/`"NO"` — correct for a binary market, but a
        multi-outcome bundle position's key
        (`f"{venue}:{market_id}:Trump"`) could never be found there, so
        `Portfolio.total_equity`'s `self._current_prices.get(
        pos.position_id, pos.entry_price)` fallback silently marked it
        at its ENTRY PRICE FOREVER — the same SHAPE of bug
        `app.strategies.base.normalize_outcome`'s docstring already
        fixed for outcome CASING, but for outcome IDENTITY (a label that
        is not `"YES"`/`"NO"` at all, not merely differently cased).
        `_check_position_exits` had the identical gap (its stop-loss/
        take-profit check read only `snapshot.yes_price`/`no_price`) and
        is fixed by calling this same method.

        Resolution order, cheapest/most-authoritative first:

        1. `"YES"`/`"NO"` (via `outcome_key`, so any casing and any
           surrounding whitespace matches): `snapshot.yes_price`/
           `snapshot.no_price` — BYTE IDENTICAL to pre-T21 behavior for
           every binary position.
        2. `snapshot.book`, if it is FOR this outcome (`outcome_key`
           match on `book.outcome`, mirroring `_book_for`'s own
           comparison): its `mid()`, or — when only one side is quoted —
           whichever of `best_ask()`/`best_bid()` exists (an ask-only or
           bid-only book still has SOME price, and refusing to mark a
           position because the book is one-sided is worse than a
           one-sided estimate).
        3. `snapshot.orderbook["outcomes"][name]` (`outcome_key` key
           match) — the SAME payload shape
           `app.strategies.multi_outcome_bundle_arbitrage` already reads
           prices from: either a bare `float` or a dict of quotes. See
           `_payload_price` for how a dict is read, and why this engine
           reads one MORE leniently than that strategy does.
        4. `None` — no source has a price for this outcome, or two
           payload labels that are the same outcome disagree about its
           price (see below). This is NEVER replaced with a guess (not
           the complement, not `0.5`): an unresolvable price must leave
           the position UNMARKED (its existing `_current_prices` entry,
           or `entry_price` if it never had one) rather than fabricate a
           P&L number, and a caller that needs to ACT on "no price
           available" (the stop-loss/take-profit check) must branch on
           `None` itself rather than receive a silent default. An
           unmarked position is reported, not swallowed — see
           `_note_unmarked_positions`.

        AMBIGUOUS PAYLOAD LABELS (T21d defect 9). Because the match at
        step 3 is case-insensitive, a payload carrying BOTH `"Trump"`
        and `"trump"` has two entries that are one outcome under
        `outcome_key`. The old code returned whichever came first in
        dict order — so `_price_for_outcome(s, "trump")` answered with
        `"Trump"`'s price. Dict order is not a pricing rule. Every
        matching entry is now resolved and, if they do not agree on a
        single price, the outcome is treated as unpriceable and logged:
        picking one of two contradictory prices for a real position is
        the failure mode this whole task exists to remove.

        Args:
            snapshot: The snapshot to price against. Must share this
                outcome's market — callers filter by `market_id`/`venue`
                before calling this (as `_process_snapshot`/
                `_check_position_exits` both already do).
            outcome: The outcome to price, any casing/spelling
                `app.strategies.base.outcome_key` accepts.

        Returns:
            float | None: The resolved price, or `None` if unresolvable.
        """
        key = outcome_key(outcome)
        if key == "YES":
            return snapshot.yes_price
        if key == "NO":
            return snapshot.no_price

        book = snapshot.book
        if book is not None and outcome_key(book.outcome) == key:
            mid = book.mid()
            if mid is not None:
                return mid
            best_ask = book.best_ask()
            if best_ask is not None:
                return best_ask.price
            best_bid = book.best_bid()
            if best_bid is not None:
                return best_bid.price

        outcomes_payload = (
            snapshot.orderbook.get("outcomes") if snapshot.orderbook else None
        )
        if isinstance(outcomes_payload, dict):
            matched: dict[str, float] = {}
            for name, value in outcomes_payload.items():
                if outcome_key(str(name)) != key:
                    continue
                price = self._payload_price(value)
                if price is not None:
                    matched[str(name)] = price
            if len(set(matched.values())) > 1:
                logger.warning(
                    "ambiguous outcome payload: two labels are the same "
                    "outcome but quote different prices; leaving it unpriced",
                    extra={
                        "venue": snapshot.venue,
                        "market_id": snapshot.market_id,
                        "outcome_key": key,
                        "labels": sorted(matched),
                    },
                )
                return None
            if matched:
                return next(iter(matched.values()))

        return None

    @staticmethod
    def _payload_price(value: Any) -> float | None:
        """Read one `orderbook["outcomes"]` entry as a price.

        The entry is either a bare number or a dict of quotes. T21d
        defect 8: the dict form used to be read as
        `value.get("ask", value.get("price"))`, which returns `None`
        whenever `"ask"` is PRESENT AND NULL — `dict.get`'s default is
        consulted only for a MISSING key, never for a null value. A
        one-sided book serialized as `{"ask": null, "bid": 0.60}` is an
        ordinary venue payload, and it made this method answer `None`
        with a perfectly good bid in hand, which left the position
        marked at its entry price.

        Each candidate key is therefore tried in turn and a null one
        falls through to the next: `"ask"` first (what it would cost to
        buy the outcome now, the conservative mark for a long position),
        then `"price"` (a last-traded or mid quote), then `"bid"`. A
        one-sided book yields a one-sided estimate, exactly as the
        `snapshot.book` branch above already does.

        `app.strategies.multi_outcome_bundle_arbitrage` reads the same
        payload with the original expression and is deliberately NOT
        changed to match: the consequences diverge. A strategy that
        cannot read an ask refuses to TRADE, which is safe; an engine
        that cannot read one still has to report an equity number, and
        falling back to entry price is not a refusal — it is a fiction.

        Args:
            value: One `orderbook["outcomes"]` value: a number, or a
                dict carrying some of `"ask"`/`"price"`/`"bid"`.

        Returns:
            float | None: The price, or `None` if the entry carries no
                usable number at all.
        """
        if isinstance(value, dict):
            for field_name in ("ask", "price", "bid"):
                quote = value.get(field_name)
                if quote is None:
                    continue
                try:
                    return float(quote)
                except (TypeError, ValueError):
                    return None
            return None
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _other_outcomes(self, snapshot: MarketSnapshot) -> set[str]:
        """Return every non-YES/NO outcome label `snapshot` can price.

        Used by `_process_snapshot` to mark `_current_prices` for
        outcomes beyond the two hardcoded `"YES"`/`"NO"` keys (T21
        carry-forward 2) — a recorded book's own outcome, and every
        named outcome in a multi-outcome bundle's `orderbook["outcomes"]`
        payload.

        Args:
            snapshot: The snapshot to inspect.

        Returns:
            set[str]: Outcome labels, in whatever casing their source
                used (`_price_for_outcome`/`outcome_key` handle casing
                at lookup/mark time, not here). Two labels that are the
                same outcome under `outcome_key` both appear here and
                both resolve to the same cache key — which is what makes
                `_price_for_outcome`'s ambiguity check the single place
                that decides what such a payload is worth.
        """
        outcomes: set[str] = set()
        book = snapshot.book
        if book is not None and outcome_key(book.outcome) not in ("YES", "NO"):
            outcomes.add(book.outcome)
        payload = snapshot.orderbook.get("outcomes") if snapshot.orderbook else None
        if isinstance(payload, dict):
            for name in payload:
                if outcome_key(str(name)) not in ("YES", "NO"):
                    outcomes.add(str(name))
        return outcomes

    def _pad_limit(self, price: float, side: OrderSide) -> float:
        """Apply the `SlippageModel.FIXED` conservative pad to a limit.

        The pad makes the order reach FURTHER into the book (a BUY is
        willing to pay more, a SELL to accept less), so the effect on the
        result is to make fills more expensive — never to hand the walk a
        better price than the book actually offered.

        Args:
            price: The leg's limit price, a probability in [0.0, 1.0].
            side: `"BUY"` or `"SELL"`.

        Returns:
            float: The padded limit, clamped to [0.0, 1.0].
        """
        if self.config.slippage_model is not SlippageModel.FIXED:
            return min(1.0, max(0.0, price))
        pad = self.config.slippage_value
        padded = price + pad if side == "BUY" else price - pad
        return min(1.0, max(0.0, padded))

    # ------------------------------------------------------------------
    # Run loop
    # ------------------------------------------------------------------

    async def run(self, data_source: Any) -> BacktestResult:
        """Run the backtest.

        The stream carries `MarketSnapshot`s interleaved with
        `ResolutionEvent`s (T09); each item is dispatched on its type.

        Args:
            data_source: `DataReplayer`/`InMemoryDataReplayer`, or any
                async iterator of `ReplayItem`.

        Returns:
            BacktestResult with complete backtest outcomes, including the
            self-reported data-quality and coverage fields described on
            that class.
        """
        start_time = utcnow()

        # Reset strategy
        self.strategy.reset()
        await self.strategy.initialize()

        # Process each stream item
        try:
            async for item in data_source:
                # Check market filter
                if (
                    self.config.markets_filter
                    and item.market_id not in self.config.markets_filter
                ):
                    continue

                if isinstance(item, ResolutionEvent):
                    await self._process_resolution(item)
                    self._report_progress(item.resolved_at)
                    continue

                await self._process_snapshot(item)
                self.snapshots_processed += 1
                self._report_progress(item.timestamp)

        except Exception as e:
            logger.error(f"Backtest error: {e}")
            self.errors.append(str(e))

        # An intent still queued when the stream ends never got the next
        # snapshot it was waiting for, which is the same outcome as a TTL
        # expiry and is counted as one.
        self._expire_all_pending()

        # `end_date` is the run's valuation point, so every settlement
        # still in transit is released there: holding it back would
        # understate `final_value` by money the account has genuinely
        # earned (it is already inside `total_equity`, so the release
        # moves it between buckets rather than creating it).
        self._release_settlements(self.config.end_date, force=True)

        # Positions still open are MARKED to their last known price into
        # `final_value` and reported separately (PLAN.md D6). They are
        # deliberately NOT liquidated: booking a synthetic exit trade
        # would turn an estimate into a fabricated fill and inflate the
        # trade count and win rate with round trips that never happened.
        # T21d defect 5c: the last chance to notice a position that
        # never found a price — including one opened on the final
        # snapshot, whose market is never revisited. Every id caught
        # here is inside `final_value` at its ENTRY PRICE.
        self._note_unmarked_positions(None, "no price by end of run")

        unrealized = list(self.portfolio.positions.values())
        unrealized_notional = math.fsum(
            pos.current_value(
                self._current_prices.get(pos.position_id, pos.entry_price)
            )
            for pos in unrealized
        )

        # Calculate final equity
        final_value = self.portfolio.total_equity(self._current_prices)
        total_return = (final_value / self.config.initial_capital) - 1

        # Cleanup
        await self.strategy.cleanup()

        return BacktestResult(
            config=self.config,
            strategy_name=self.strategy.name,
            strategy_config=self.strategy.config,
            start_time=start_time,
            end_time=utcnow(),
            initial_capital=self.config.initial_capital,
            final_value=final_value,
            total_return=total_return,
            equity_curve=self.portfolio.equity_history.copy(),
            trades=self.trades.copy(),
            positions_final=self.portfolio.positions.copy(),
            snapshots_processed=self.snapshots_processed,
            signals_generated=self.signals_generated,
            errors=self.errors.copy(),
            intents_generated=self.intents_generated,
            intents_executed=self.intents_executed,
            intent_rejections=self.intent_rejections,
            intent_expirations=self.intent_expirations,
            rejection_reasons=dict(self._rejection_reasons),
            intents_sized=self.intents_sized,
            intents_capital_capped=self.intents_capital_capped,
            intents_depth_limited=self.intents_depth_limited,
            intents_depth_blocked=self.intents_depth_blocked,
            depth_source=self._aggregate_depth_source(),
            fill_at=self.config.fill_at,
            crossed_book_skips=self._fill_engine.crossed_book_skips,
            undeclared_zero_fee_markets=tuple(
                sorted(self._fill_engine.undeclared_zero_fee_markets)
            ),
            tick_unvalidated_fills=self.tick_unvalidated_fills,
            unfilled_counts=dict(self._fill_engine.unfilled_counts),
            positions_settled=self.positions_settled,
            unrealized_at_end=unrealized,
            unrealized_notional_at_end=unrealized_notional,
            unmarked_positions=tuple(self._unmarked_positions),
            coverage=self._build_coverage(),
        )

    def _note_unmarked_positions(
        self, market: tuple[VenueId, str] | None, reason: str
    ) -> None:
        """Record and log every open position that has no price to mark to.

        T21d defect 5c, and the reason defects 5a and 5b went unnoticed
        for a whole task cycle. `Portfolio.total_equity` falls back to
        `pos.entry_price` for a position id missing from
        `_current_prices`, and `_check_position_exits` `continue`s past a
        position it cannot price — both silently. A backtest whose
        positions mark at entry price reports an equity curve that has
        no relationship to the market, and before this method nothing on
        the result, in the log, or in `errors` said so: `grep -rn
        "unmarked\\|unpriced\\|mark_miss" app/` returned nothing at all.

        Called once per snapshot for that snapshot's own market (the
        point at which that market has just had its chance to price
        every outcome it knows about) and once over EVERY open position
        at end of run, which catches a position opened on the final
        snapshot that no later snapshot ever revisited.

        Each position is logged at WARNING exactly once, naming the
        position id, and every id recorded here is published on
        `BacktestResult.unmarked_positions`.

        Args:
            market: Restrict the check to `(venue, market_id)`, or
                `None` to check every open position.
            reason: Short phrase recorded and logged with the position,
                describing which check found it.
        """
        for position_id, pos in self.portfolio.positions.items():
            if market is not None and (pos.venue, pos.market_id) != market:
                continue
            if position_id in self._current_prices:
                continue
            if position_id in self._unmarked_positions:
                continue
            self._unmarked_positions[position_id] = reason
            logger.warning(
                "position could not be marked to market and is valued at its "
                "ENTRY PRICE; the equity curve for it is not a market number",
                extra={
                    "position_id": position_id,
                    "venue": pos.venue,
                    "market_id": pos.market_id,
                    "outcome": pos.outcome,
                    "entry_price": pos.entry_price,
                    "size": pos.size,
                    "reason": reason,
                },
            )

    def _report_progress(self, timestamp: datetime) -> None:
        """Invoke `progress_callback` for a stream item at `timestamp`.

        Args:
            timestamp: The item's aware UTC time.
        """
        if self.progress_callback is None:
            return
        total_duration = (
            self.config.end_date - self.config.start_date
        ).total_seconds()
        elapsed = (timestamp - self.config.start_date).total_seconds()
        self.progress_callback(min(elapsed / total_duration, 1.0))

    def _build_coverage(self) -> CoverageReport:
        """Assemble the run's survivorship census.

        Returns:
            CoverageReport: See that class. `resolution_coverage` is
                `0.0` for a run that saw no markets at all, and
                `low_resolution_coverage` is therefore `True` — an empty
                run has proved nothing, and the honest label for it is
                not "fully covered".
        """
        seen = len(self._markets_seen)
        resolved = len(self._markets_resolved & self._markets_seen)
        coverage = resolved / seen if seen else 0.0
        return CoverageReport(
            markets_seen=seen,
            markets_resolved=resolved,
            markets_closed_unresolved=seen - resolved,
            snapshots_per_market=dict(self._snapshots_per_market),
            resolution_coverage=coverage,
            low_resolution_coverage=coverage < _MIN_RESOLUTION_COVERAGE,
        )

    def _aggregate_depth_source(self) -> ResultDepthSource:
        """Collapse the run's observed book depth sources into one label.

        Returns:
            ResultDepthSource: `"mixed"` when both recorded and
                synthetic books were walked, the single observed source
                when only one was, and `"synthetic"` when NO book was
                walked at all — the conservative default, because
                labeling a run `"recorded"` on the strength of zero
                recorded books is precisely the hidden assumption
                GUARDRAILS.md §1.7 forbids.
        """
        if len(self._depth_sources) > 1:
            return "mixed"
        if self._depth_sources == {"recorded"}:
            return "recorded"
        return "synthetic"

    def _venue_mismatch(self, intent: Intent) -> bool:
        """Return whether `intent` has a leg whose venue is provably wrong.

        "Provably" means `leg.market_id` has been observed on some OTHER
        venue in this run and never on `leg.venue` itself — a brand-new
        market that simply has not printed a snapshot yet is NOT
        flagged; that is an ordinary, retryable "no book yet" state, not
        this defect (Phase-1 remediation FIX 2).

        Args:
            intent: The intent about to be sized/queued/executed.

        Returns:
            bool: `True` if at least one leg is mismatched.
        """
        return any(
            leg.market_id in self._market_venues
            and leg.venue not in self._market_venues[leg.market_id]
            for leg in intent.legs
        )

    async def _process_snapshot(self, snapshot: MarketSnapshot) -> None:
        """Process a single market snapshot.

        Order of operations is load-bearing:

        1. Record the snapshot (prices, book cache, category).
        2. Check position exits against it.
        3. Advance the `fill_at="next"` queue — intents generated on an
           EARLIER snapshot fill here, against this one's book.
        4. Ask the strategy for a new intent, and either queue it (next)
           or execute it immediately (same).
        5. Mark the equity curve.

        Step 3 precedes step 4 so an intent never fills against the
        snapshot that produced it, and step 2 precedes step 3 so an exit
        is not evaluated against a position opened microseconds earlier in
        the same snapshot.

        Args:
            snapshot: Market data snapshot to process.
        """
        key = (snapshot.venue, snapshot.market_id)
        self._snapshots[key] = snapshot
        self._market_categories[snapshot.market_id] = snapshot.category
        self._markets_seen.add(snapshot.market_id)
        self._market_venues.setdefault(snapshot.market_id, set()).add(snapshot.venue)
        self._snapshots_per_market[snapshot.market_id] = (
            self._snapshots_per_market.get(snapshot.market_id, 0) + 1
        )
        # Settlement proceeds that have matured by now become spendable
        # BEFORE the strategy is asked to size anything, so a strategy is
        # never denied capital that had already cleared.
        self._release_settlements(snapshot.timestamp)
        self._current_prices[
            position_key(snapshot.venue, snapshot.market_id, "YES")
        ] = snapshot.yes_price
        self._current_prices[
            position_key(snapshot.venue, snapshot.market_id, "NO")
        ] = snapshot.no_price
        # T21 carry-forward 2: also mark every OTHER outcome this
        # snapshot can price (a recorded book's own outcome, or a
        # multi-outcome bundle's `orderbook["outcomes"]` payload) — not
        # just the two literal YES/NO keys above — so a non-binary
        # position is never left marked at its entry price forever for
        # want of a cache key. See `_price_for_outcome`'s docstring.
        #
        # T21d defect 5a: this key goes through `position_key` — the
        # SAME function `Position.position_id` uses — rather than being
        # spelled out from the PAYLOAD's casing. Written by hand it
        # produced `polymarket:M:Trump` for a position that keyed
        # `polymarket:M:TRUMP`, and the two never met.
        for other_outcome in self._other_outcomes(snapshot):
            price = self._price_for_outcome(snapshot, other_outcome)
            if price is not None:
                self._current_prices[
                    position_key(snapshot.venue, snapshot.market_id, other_outcome)
                ] = price

        # T21d defect 5c: every position on THIS market has just had its
        # one chance to be marked from this snapshot. Any that still has
        # no price is marked at its entry price by `total_equity`, which
        # is indistinguishable from a position that has not moved — so
        # say so here, once, rather than let the equity curve report a
        # number nothing downstream can question.
        self._note_unmarked_positions(
            (snapshot.venue, snapshot.market_id), "no price on this market's snapshot"
        )

        # Check for position exits (stop loss, take profit)
        await self._check_position_exits(snapshot)

        # Fill anything that was waiting for THIS market's next snapshot.
        for pending in self._advance_pending(snapshot):
            await self._execute_intent(
                pending.intent,
                snapshot,
                size_usd=pending.size_usd,
                intent_id=pending.intent_id,
                leg_snapshots=pending.leg_snapshots,
                signal=pending.signal,
                same_snapshot=False,
            )

        # Generate a signal or intent from the strategy (PLAN.md D7:
        # `on_market_data` may return either; a `Signal` is normalized to
        # a one-leg `Intent` via `to_intent()` and the original `Signal`
        # is carried alongside it so `stop_loss`/`take_profit` — which a
        # `Leg` cannot represent — are not lost).
        result = self.strategy.on_market_data(snapshot)

        signal: Signal | None = None
        intent: Intent | None = None

        if isinstance(result, Intent):
            intent = result
        elif isinstance(result, Signal) and result.type != SignalType.HOLD:
            signal = result
            # Phase-1 remediation FIX 2/FIX 4: `to_intent()` has no
            # snapshot of its own to pull `venue`/`end_date` from, so the
            # engine — which DOES have `snapshot` here — injects both.
            # Without `venue=snapshot.venue`, every `Signal`-returning
            # strategy's leg would default to `Leg.venue`'s own
            # `"polymarket"` default regardless of which venue's data
            # produced the signal.
            intent = result.to_intent(
                venue=snapshot.venue, expected_resolution_ts=snapshot.end_date
            )
        # else: result is None, or a HOLD Signal -> no intent to execute.

        if intent is not None:
            self.signals_generated += 1
            self.intents_generated += 1
            self._intent_seq += 1
            intent_id = f"bt-{self._intent_seq}"

            if self._venue_mismatch(intent):
                # STRUCTURAL (FIX 2): retrying this leg against a later
                # snapshot reproduces the identical mismatch, so this is
                # never counted as a market-condition rejection like
                # "no_eligible_levels" -- doing so would read as "no
                # liquidity" when the real defect is "the venue is
                # wrong". Rejected immediately, before sizing/queueing,
                # so it can never bleed out silently as an ordinary
                # `pending_ttl` expiry either.
                logger.error(
                    "intent rejected: a leg's venue matches none of the "
                    "venues ever observed for its market_id, though that "
                    "market_id HAS been seen on another venue -- this is "
                    "a strategy/engine defect, not a market condition",
                    extra={
                        "intent_id": intent_id,
                        "intent_kind": intent.kind,
                        "leg_venues": [leg.venue for leg in intent.legs],
                        "leg_markets": [leg.market_id for leg in intent.legs],
                    },
                )
                self._reject(intent, intent_id, VENUE_MISMATCH_REASON)
            else:
                size_usd = self._size_intent(intent, signal, snapshot)

                if self.config.fill_at == "next":
                    self._pending.append(
                        PendingIntent(
                            intent=intent,
                            created_ts=snapshot.timestamp,
                            intent_id=intent_id,
                            size_usd=size_usd,
                            waiting={
                                (leg.venue, leg.market_id) for leg in intent.legs
                            },
                            signal=signal,
                        )
                    )
                else:
                    await self._execute_intent(
                        intent,
                        snapshot,
                        size_usd=size_usd,
                        intent_id=intent_id,
                        leg_snapshots=None,
                        signal=signal,
                        same_snapshot=True,
                    )

        # Update equity history
        self.portfolio.update_equity_history(
            snapshot.timestamp,
            self._current_prices,
        )

    def _size_intent(
        self,
        intent: Intent,
        signal: Signal | None,
        snapshot: MarketSnapshot,
    ) -> float:
        """Return the USD budget for `intent`.

        Precedence:

        1. Any leg-declared `size_usd` (summed). A strategy that already
           sized its legs is not second-guessed.
        2. `BaseStrategy.calculate_position_size`. That hook's signature
           takes a `Signal`, so when the strategy emitted an `Intent`
           directly a SIZING PROBE is built from `intent.legs[0]`. The
           probe is never executed and never becomes a trade — it exists
           only to let a strategy's own sizing logic (Kelly, fixed
           fraction, confidence scaling) see the trade it is sizing. This
           is NOT the deleted pre-T08 leg-to-signal execution bridge,
           which turned a multi-leg intent into a single-leg ORDER and
           silently dropped the hedge.

        Legs that declare `size_contracts` are honored directly in
        `_leg_sizes` and do not consult this budget.

        Args:
            intent: The intent being sized.
            signal: The originating `Signal`, when there was one.
            snapshot: The snapshot the intent was generated on.

        Returns:
            float: USD budget, `>= 0`.
        """
        declared = sum(leg.size_usd for leg in intent.legs if leg.size_usd is not None)
        if declared > 0.0 and signal is None:
            return declared

        probe = signal
        if probe is None:
            leg = intent.legs[0]
            token_id = (
                snapshot.token_id
                if leg.market_id == snapshot.market_id
                else leg.market_id
            )
            probe = Signal(
                type=SignalType.BUY if leg.side == "BUY" else SignalType.SELL,
                market_id=leg.market_id,
                token_id=token_id,
                outcome=leg.outcome,
                price=leg.limit_price,
                size=leg.size_usd or 0.0,
                confidence=intent.confidence,
                timestamp=snapshot.timestamp,
                metadata=dict(intent.metadata),
            )

        portfolio_value = self.portfolio.total_equity(self._current_prices)
        positions_dict = {
            pid: {
                "market_id": p.market_id,
                "size": p.size,
                "entry_price": p.entry_price,
                "metadata": p.metadata,
            }
            for pid, p in self.portfolio.positions.items()
        }
        size = self.strategy.calculate_position_size(
            probe, portfolio_value, positions_dict
        )
        return max(0.0, float(size))

    # ------------------------------------------------------------------
    # Pending queue (fill_at="next")
    # ------------------------------------------------------------------

    def _advance_pending(self, snapshot: MarketSnapshot) -> list[PendingIntent]:
        """Advance the pending queue against one arriving snapshot.

        TTL is checked BEFORE the snapshot is applied, so an intent whose
        `pending_ttl` has already elapsed expires even if this very
        snapshot would have completed it — "if `pending_ttl` elapses
        first it expires" is the whole point of the setting.

        Args:
            snapshot: The snapshot that just arrived.

        Returns:
            list[PendingIntent]: Intents whose every leg now has a next
                snapshot, in queue order. They are removed from the queue.
        """
        key = (snapshot.venue, snapshot.market_id)
        ready: list[PendingIntent] = []
        still_waiting: list[PendingIntent] = []

        for pending in self._pending:
            if snapshot.timestamp - pending.created_ts > self.config.pending_ttl:
                self._expire(pending, snapshot.timestamp)
                continue
            if key in pending.waiting:
                pending.waiting.discard(key)
                pending.leg_snapshots[key] = snapshot
            if pending.waiting:
                still_waiting.append(pending)
            else:
                ready.append(pending)

        self._pending = still_waiting
        return ready

    def _expire(self, pending: PendingIntent, now: datetime | None) -> None:
        """Count and log one expired pending intent.

        Args:
            pending: The intent that never filled.
            now: The replay time at which it expired, or `None` when the
                stream simply ended.
        """
        self.intent_expirations += 1
        logger.debug(
            "pending intent expired without a next snapshot",
            extra={
                "intent_id": pending.intent_id,
                "intent_kind": pending.intent.kind,
                "created_ts": pending.created_ts.isoformat(),
                "expired_at": now.isoformat() if now is not None else None,
                "waiting_on": sorted(f"{v}:{m}" for v, m in pending.waiting),
            },
        )

    def _expire_pending_for_market(
        self, venue: VenueId, market_id: str, now: datetime
    ) -> None:
        """Expire every queued intent with a leg on a market that just settled.

        A `fill_at="next"` intent is waiting for a book that no longer
        exists: the market has paid out. Filling it against the next
        printed quote would be a trade no live account could have made,
        and — worse — an intent to BUY a market whose winner is already
        known is a guaranteed profit the engine would be inventing. So
        the intent EXPIRES rather than fills, and is counted as an
        expiry so the run's `intents_generated` still balances.

        Args:
            venue: Venue of the resolving market.
            market_id: The resolving market.
            now: Aware UTC resolution time, for the log.
        """
        key = (venue, market_id)
        survivors: list[PendingIntent] = []
        for pending in self._pending:
            touches = any(
                (leg.venue, leg.market_id) == key for leg in pending.intent.legs
            )
            if touches:
                self._expire(pending, now)
            else:
                survivors.append(pending)
        self._pending = survivors

    def _expire_all_pending(self) -> None:
        """Expire every intent still queued when the replay ends."""
        for pending in self._pending:
            self._expire(pending, None)
        self._pending = []

    # ------------------------------------------------------------------
    # Resolution settlement
    # ------------------------------------------------------------------

    def _release_settlements(self, now: datetime, *, force: bool = False) -> None:
        """Move matured settlement proceeds from escrow into spendable cash.

        Args:
            now: Current replay time (aware UTC).
            force: Release everything regardless of maturity. Used once,
                at `end_date`, where the run stops and holding money back
                would understate `final_value`.
        """
        if not self.portfolio.pending_settlements:
            return
        remaining: list[tuple[datetime, float]] = []
        released = 0.0
        for available_at, amount in self.portfolio.pending_settlements:
            if force or available_at <= now:
                released += amount
            else:
                remaining.append((available_at, amount))
        if released:
            self.portfolio.cash += released
            logger.debug(
                "settlement proceeds released",
                extra={"amount_usd": released, "at": now.isoformat()},
            )
        self.portfolio.pending_settlements = remaining

    async def _process_resolution(self, event: ResolutionEvent) -> None:
        """Settle every open position on a market that just resolved.

        Order of operations:

        1. Census: the market is counted as seen and resolved.
        2. Release any settlement proceeds that matured by now.
        3. Expire queued intents touching this market (see
           `_expire_pending_for_market`) — they can no longer fill.
        4. Settle each open position on the market.
        5. Mark the market resolved, so no later quote on it is
           tradeable, and mark its outcome prices to 1.00/0.00.
        6. Record an equity point at `resolved_at`.

        Args:
            event: The resolution to apply.
        """
        self._markets_seen.add(event.market_id)
        self._markets_resolved.add(event.market_id)
        self._release_settlements(event.resolved_at)
        self._expire_pending_for_market(
            event.venue, event.market_id, event.resolved_at
        )

        settling = [
            position_id
            for position_id, pos in self.portfolio.positions.items()
            if pos.venue == event.venue and pos.market_id == event.market_id
        ]
        for position_id in settling:
            self._settle_position(position_id, event)

        self._resolved_keys.add((event.venue, event.market_id))
        # A resolved market's outcomes are worth exactly 1.00 and 0.00.
        # Leaving the last traded quote in `_current_prices` would mark
        # any position the engine could not settle (an outcome the event
        # does not name) at a stale, pre-resolution price.
        for outcome in ("YES", "NO"):
            won = outcome.casefold() == event.winning_outcome.casefold()
            self._current_prices[
                position_key(event.venue, event.market_id, outcome)
            ] = 1.0 if won else 0.0

        self.portfolio.update_equity_history(
            event.resolved_at, self._current_prices
        )

    def _settle_position(self, position_id: str, event: ResolutionEvent) -> None:
        """Pay out one position at resolution and escrow its proceeds.

        Payout is `$1.00 per contract` for the winning outcome and
        `$0.00` for every other one, minus `redemption_gas_usd` **once
        for the whole POSITION** — it models the Polygon transaction that
        redeems the position, not a per-contract cost (PLAN.md §3). The
        gas is charged on losing positions too: the conservative
        assumption, and the one that keeps a complement pair's total cost
        equal to `2 x gas` regardless of which leg won.

        Note what that arithmetic implies and do not paper over it: at
        the default $0.05, a two-leg complement pays $0.10 of gas. A
        complement bought at 0.45 + 0.48 grosses only $0.07 per contract,
        so ONE contract of that "arbitrage" loses money. It turns
        profitable only at size, because the gas is fixed per position
        and the edge scales with contracts. That is a real property of
        the strategy, not an artifact of this model.

        P&L is realized at `resolved_at`; only the CASH waits for
        `settlement_delay_hours`.

        Args:
            position_id: The position to settle.
            event: The resolution event supplying the winner and time.
        """
        pos = self.portfolio.positions.pop(position_id, None)
        if pos is None:
            return

        won = pos.outcome.casefold() == event.winning_outcome.casefold()
        payout_per_contract = 1.0 if won else 0.0
        gas = self.config.effective_redemption_gas_usd
        # Per POSITION, not per contract.
        net_proceeds = (pos.size * payout_per_contract) - gas
        pnl = net_proceeds - pos.cost_basis

        self.portfolio.realized_pnl += pnl
        available_at = event.resolved_at + self.config.effective_settlement_delay
        self.portfolio.pending_settlements.append((available_at, net_proceeds))
        self.positions_settled += 1

        trade = TradeRecord(
            timestamp=event.resolved_at,
            market_id=pos.market_id,
            outcome=pos.outcome,
            token_id=pos.token_id,
            side=SETTLE_SIDE,
            price=payout_per_contract,
            size=pos.size,
            fee=gas,
            slippage=0.0,
            pnl=pnl,
            metadata={
                **pos.metadata,
                SETTLEMENT_KEY: True,
                "winning_outcome": event.winning_outcome,
                "won": won,
                "redemption_gas_usd": gas,
                "cash_available_at": available_at.isoformat(),
            },
            venue=pos.venue,
        )
        self.trades.append(trade)

        self.strategy.on_position_closed(
            {
                "market_id": pos.market_id,
                "token_id": pos.token_id,
                "entry_price": pos.entry_price,
                "exit_price": payout_per_contract,
                "size": pos.size,
                "entry_time": pos.entry_time,
                "exit_time": event.resolved_at,
            },
            pnl,
        )

    # ------------------------------------------------------------------
    # Intent execution
    # ------------------------------------------------------------------

    async def _execute_intent(
        self,
        intent: Intent,
        snapshot: MarketSnapshot,
        *,
        size_usd: float = 0.0,
        intent_id: str = "bt",
        leg_snapshots: dict[tuple[VenueId, str], MarketSnapshot] | None = None,
        signal: Signal | None = None,
        same_snapshot: bool = False,
    ) -> bool:
        """Execute one `Intent`, honoring its atomicity.

        `all_or_none`: every leg's fill is simulated FIRST, against its
        own leg's book, and nothing is committed unless every leg both
        filled and reached `size * (1 - partial_tolerance)`. A leg short
        of that rejects the WHOLE intent — which is the entire point of a
        complement or cross-venue arbitrage: half of a hedge is a
        directional bet, and booking it would report a riskless trade the
        strategy never asked for.

        `best_effort`: whatever filled is committed; only an intent where
        NOTHING filled counts as a rejection.

        Cost basis is PER LEG, and each leg lands in its own position
        keyed `f"{venue}:{market_id}:{outcome}"`, so a complement holds
        two positions rather than one netted pseudo-position.

        Args:
            intent: The intent to execute.
            snapshot: The snapshot that TRIGGERED execution (the
                generating snapshot under `fill_at="same"`, the last
                arriving next-snapshot under `"next"`). Used for the
                trade timestamp only.
            size_usd: USD budget from `_size_intent`.
            intent_id: Deterministic id used to build `client_order_id`s.
            leg_snapshots: Per-leg execution snapshots (the pending
                queue's). `None` means use the latest snapshot known for
                each leg's market, which under `fill_at="same"` is the
                generating snapshot itself.
            signal: The originating `Signal`, for `stop_loss`/
                `take_profit`.
            same_snapshot: Whether this is a diagnostic same-snapshot
                fill; stamps `DIAGNOSTIC_SAME_SNAPSHOT_KEY` on every
                resulting trade.

        Returns:
            bool: `True` if at least one fill was committed.
        """
        sized = self._leg_sizes(intent, size_usd)
        if sized is None:
            self._reject(intent, intent_id, "below_min_size")
            return False
        requested_sizes, sizes = sized

        # T28: the two causes of a short fill are counted SEPARATELY,
        # from the leg plans rather than from the committed trades, so
        # that an `all_or_none` intent killed by a short leg — which
        # produces no `TradeRecord` at all — still registers as depth
        # exhaustion instead of vanishing into `fill_rate`.
        self.intents_sized += 1
        if any(
            allowed < asked - _SIZE_EPSILON
            for asked, allowed in zip(requested_sizes, sizes, strict=True)
        ):
            self.intents_capital_capped += 1

        plans = self._plan_legs(
            intent, sizes, requested_sizes, intent_id, leg_snapshots or {}
        )

        depth_limited = any(self._leg_ran_out_of_book(plan) for plan in plans)
        if depth_limited:
            self.intents_depth_limited += 1

        if intent.atomicity == "all_or_none":
            blocker = self._atomicity_blocker(plans)
            if blocker is not None:
                if depth_limited:
                    self.intents_depth_blocked += 1
                self._reject(intent, intent_id, blocker)
                return False

        committed = 0
        for plan in plans:
            if plan.result is None or plan.result.filled_size <= 0.0:
                continue
            self._commit_leg(
                intent, plan, snapshot, intent_id, signal, same_snapshot
            )
            committed += 1

        if committed == 0:
            if depth_limited:
                self.intents_depth_blocked += 1
            reason: FillReason = "no_eligible_levels"
            for plan in plans:
                if plan.result is not None and plan.result.reason is not None:
                    reason = plan.result.reason
                    break
            self._reject(intent, intent_id, reason)
            return False

        self.intents_executed += 1
        return True

    def _leg_sizes(
        self, intent: Intent, size_usd: float
    ) -> tuple[list[float], list[float]] | None:
        """Return each leg's requested and capital-allowed contract size.

        Sizing rule (T08 brief): when the strategy gives a USD budget for
        the intent, it buys the SAME NUMBER OF CONTRACTS on every leg —
        `contracts = size_usd / sum(leg limit prices)`. For a complement
        with asks 0.45 and 0.48 that is `size_usd / 0.93` of each side,
        which is the only split that actually hedges: buying equal
        DOLLARS of each would leave more contracts on the cheaper side
        and a naked directional residual on the other.

        Legs that all declare `size_contracts` are honored verbatim
        instead. The whole vector is then scaled down, preserving the
        equal-contract relationship, to respect `max_position_pct` and
        available cash.

        BOTH vectors are returned, not just the scaled one (T28). The
        difference between them is the ONLY record that the engine's own
        capital cap bound on this intent, and it has to be separable from
        the book's own shortfall: the capital cap binds LESS as capital
        grows, while depth exhaustion binds MORE, so a single "downsized"
        number built from their sum runs backwards across a capital
        sweep and reads as though downsizing improves with scale
        (PLAN.md R4, D12).

        Args:
            intent: The intent being sized.
            size_usd: USD budget from `_size_intent`.

        Returns:
            tuple[list[float], list[float]] | None: `(requested,
                allowed)` contracts per leg — `requested` before the
                capital cap, `allowed` after it (the two are the SAME
                list values when the cap did not bind). `None` when the
                intent cannot be sized at all (no budget, a zero total
                limit price, or a notional under
                `settings.min_trade_usd`).
        """
        explicit = [leg.size_contracts for leg in intent.legs]
        if all(size is not None for size in explicit):
            sizes = [float(size) for size in explicit if size is not None]
        else:
            total_price = math.fsum(leg.limit_price for leg in intent.legs)
            if size_usd <= 0.0 or total_price <= 0.0:
                return None
            contracts = size_usd / total_price
            sizes = [contracts] * len(intent.legs)

        requested = list(sizes)

        notional = math.fsum(
            size * self._pad_limit(leg.limit_price, leg.side)
            for size, leg in zip(sizes, intent.legs, strict=True)
        )
        if notional <= 0.0:
            return None

        portfolio_value = self.portfolio.total_equity(self._current_prices)
        cap = min(
            portfolio_value * self.config.max_position_pct,
            self.portfolio.cash * _CASH_BUFFER,
        )
        if notional > cap:
            if cap <= 0.0:
                return None
            scale = cap / notional
            sizes = [size * scale for size in sizes]
            notional = cap

        if notional < settings.min_trade_usd:
            return None
        return requested, sizes

    @staticmethod
    def _leg_ran_out_of_book(plan: _LegPlan) -> bool:
        """Whether this leg's walk consumed the book and came up short.

        The strict definition of book-depth exhaustion (T28):
        `0 < filled_size < ordered_size`. An IOC walk that stops with
        size outstanding AFTER obtaining some contracts can only have
        stopped because no eligible level remained — that is depth, and
        nothing else.

        A leg that filled NOTHING is deliberately excluded. Its
        `FillResult` reports `"no_eligible_levels"` whether the side was
        empty (depth) or every level was simply worse than the limit
        (a price move, which under `fill_at="next"` is the common case),
        and the result carries nothing that separates the two. Counting
        those would swap one contaminated metric for another; they are
        reported instead through `rejection_reasons`/`unfilled_counts`.

        Args:
            plan: One leg's simulated outcome.

        Returns:
            bool: `True` when the book was walked and ran out.
        """
        result = plan.result
        if result is None:
            return False
        return (
            result.filled_size > 0.0
            and result.filled_size < plan.ordered_size - _SIZE_EPSILON
        )

    def _plan_legs(
        self,
        intent: Intent,
        sizes: list[float],
        requested_sizes: list[float],
        intent_id: str,
        leg_snapshots: dict[tuple[VenueId, str], MarketSnapshot],
    ) -> list[_LegPlan]:
        """Simulate every leg WITHOUT committing anything.

        A SELL leg is capped at the size of the position it would be
        closing: neither venue supports naked shorts (PLAN.md D8), so
        selling contracts the portfolio does not hold would fabricate
        proceeds. A SELL with no position becomes a zero-size order,
        which the fill engine declines as `"zero_size_order"` — and under
        `all_or_none` that correctly rejects the whole intent rather than
        booking half of it.

        Args:
            intent: The intent being executed.
            sizes: Contracts per leg from `_leg_sizes`, post-capital-cap.
            requested_sizes: The same vector BEFORE the capital cap, also
                from `_leg_sizes`, carried through onto each plan (and
                from there onto each `TradeRecord`) so the capital-cap
                and book-depth causes of a short fill stay separable
                (T28).
            intent_id: Id used to build each `client_order_id`.
            leg_snapshots: Per-leg execution snapshots; falls back to the
                latest snapshot known for the leg's market.

        Returns:
            list[_LegPlan]: One plan per leg, in leg order.
        """
        plans: list[_LegPlan] = []
        for index, (leg, size, asked) in enumerate(
            zip(intent.legs, sizes, requested_sizes, strict=True)
        ):
            key = (leg.venue, leg.market_id)
            leg_snapshot = leg_snapshots.get(key) or self._snapshots.get(key)
            limit = self._pad_limit(leg.limit_price, leg.side)

            order_size = size
            if leg.side == "SELL":
                held = self.portfolio.positions.get(
                    position_key(leg.venue, leg.market_id, leg.outcome)
                )
                order_size = min(size, held.size if held is not None else 0.0)
            order_size = max(0.0, order_size)

            book = (
                self._book_for(leg_snapshot, leg.venue, leg.market_id, leg.outcome)
                if leg_snapshot is not None
                else None
            )
            if leg_snapshot is None or book is None:
                plans.append(
                    _LegPlan(
                        leg=leg,
                        index=index,
                        size=size,
                        requested_size=asked,
                        ordered_size=order_size,
                        limit_price=limit,
                        snapshot=leg_snapshot,
                        result=None,
                    )
                )
                continue

            order = OrderRequest(
                venue=leg.venue,
                market_id=leg.market_id,
                outcome=leg.outcome,
                side=leg.side,
                price=limit,
                size=order_size,
                tif="IOC",
                client_order_id=f"{intent_id}:{index}:0",
            )
            # NO `market=` argument: a replayed price is routinely off any
            # fixed tick grid, so passing a `VenueMarket` would make the
            # run raise constantly (T08 carry-forward 2). The consequence
            # — neither tick_size nor min_size enforced — is LABELED on
            # the result as `tick_unvalidated_fills`, not hidden.
            result = self._fill_engine.fill(order, book, leg_snapshot.timestamp)
            plans.append(
                _LegPlan(
                    leg=leg,
                    index=index,
                    size=size,
                    requested_size=asked,
                    ordered_size=order_size,
                    limit_price=limit,
                    snapshot=leg_snapshot,
                    result=result,
                )
            )
        return plans

    def _atomicity_blocker(self, plans: list[_LegPlan]) -> FillReason | None:
        """Return the reason an `all_or_none` intent must not execute.

        Args:
            plans: Every leg's simulated outcome.

        Returns:
            FillReason | None: `None` when every leg filled to within
                `partial_tolerance`. Otherwise the blocking reason, drawn
                from the fill engine's own six-value vocabulary rather
                than a parallel one (T08 carry-forward 4). A leg that
                filled but fell SHORT of its tolerance reports
                `"fok_insufficient_depth"` — the vocabulary's existing
                term for "the book could not supply the whole size", which
                is exactly what happened.
        """
        required_fraction = 1.0 - self.config.partial_tolerance
        for plan in plans:
            if plan.result is None:
                return "no_eligible_levels"
            if plan.result.status == "unfilled":
                # `FillResult` guarantees an "unfilled" names a reason;
                # the fallback only satisfies the type checker.
                return plan.result.reason or "no_eligible_levels"
            required = plan.size * required_fraction
            if plan.result.filled_size < required - _SIZE_EPSILON:
                return "fok_insufficient_depth"
        return None

    def _reject(self, intent: Intent, intent_id: str, reason: FillReason | str) -> None:
        """Count and log one intent that committed nothing.

        Args:
            intent: The rejected intent.
            intent_id: Its deterministic id.
            reason: Why — one of `FillReason`'s six values, or
                `VENUE_MISMATCH_REASON` for a defect caught upstream of
                the fill engine entirely.
        """
        self.intent_rejections += 1
        self._rejection_reasons[reason] = self._rejection_reasons.get(reason, 0) + 1
        logger.debug(
            "intent rejected without executing any leg",
            extra={
                "intent_id": intent_id,
                "intent_kind": intent.kind,
                "atomicity": intent.atomicity,
                "leg_count": len(intent.legs),
                "reason": reason,
            },
        )

    def _commit_leg(
        self,
        intent: Intent,
        plan: _LegPlan,
        snapshot: MarketSnapshot,
        intent_id: str,
        signal: Signal | None,
        same_snapshot: bool,
    ) -> None:
        """Book one leg's fills into the portfolio.

        Args:
            intent: The intent this leg belongs to.
            plan: The leg's simulated outcome (`plan.result` is filled).
            snapshot: The triggering snapshot (trade timestamp).
            intent_id: The intent's id, recorded on the trade.
            signal: The originating `Signal`, for stop/target levels.
            same_snapshot: Whether to stamp the diagnostic marker.
        """
        result = plan.result
        if result is None or result.avg_price is None:
            return

        # `Fill` carries no market/outcome field; the fill engine records
        # them in `Fill.metadata` and that is the contract to read
        # (T08 carry-forward 6).
        head = result.fills[0]
        venue: VenueId = head.venue
        market_id = str(head.metadata.get("market_id", plan.leg.market_id))
        outcome = str(head.metadata.get("outcome", plan.leg.outcome))
        position_id = position_key(venue, market_id, outcome)

        if not result.metadata.get(TICK_VALIDATED_KEY, False):
            self.tick_unvalidated_fills += 1

        size = result.filled_size
        exec_price = result.avg_price
        notional = size * exec_price
        fee = result.total_fee + self._legacy_fee(notional)

        metadata = dict(intent.metadata)
        if same_snapshot:
            metadata[DIAGNOSTIC_SAME_SNAPSHOT_KEY] = True

        if plan.leg.side == "BUY":
            self._book_buy(
                position_id=position_id,
                venue=venue,
                market_id=market_id,
                outcome=outcome,
                token_id=self._token_id_for(plan, market_id),
                exec_price=exec_price,
                size=size,
                cost=notional + fee,
                timestamp=snapshot.timestamp,
                signal=signal,
                metadata=metadata,
            )
            pnl: float | None = None
        else:
            pnl = self._book_sell(
                position_id=position_id,
                size=size,
                proceeds=notional - fee,
            )

        slippage = (
            exec_price - plan.leg.limit_price
            if plan.leg.side == "BUY"
            else plan.leg.limit_price - exec_price
        )
        trade = TradeRecord(
            timestamp=snapshot.timestamp,
            market_id=market_id,
            outcome=outcome,
            token_id=self._token_id_for(plan, market_id),
            side=plan.leg.side,
            price=exec_price,
            size=size,
            fee=fee,
            slippage=slippage,
            pnl=pnl,
            signal_confidence=intent.confidence,
            metadata=metadata,
            venue=venue,
            intent_id=intent_id,
            requested_size=plan.requested_size,
            ordered_size=plan.ordered_size,
        )
        self.trades.append(trade)

        self.strategy.on_trade_executed(
            {
                "market_id": market_id,
                "token_id": trade.token_id,
                "side": plan.leg.side,
                "price": exec_price,
                "size": size,
                "fee": fee,
                "timestamp": snapshot.timestamp,
            }
        )

    def _token_id_for(self, plan: _LegPlan, market_id: str) -> str:
        """Return the token id to record for one leg.

        `Leg` carries no `token_id`, so the leg's own snapshot supplies
        it when the leg is for that snapshot's market; otherwise the
        market id stands in.

        Args:
            plan: The leg's plan (its `snapshot` may be `None`).
            market_id: The leg's market id.

        Returns:
            str: Token identifier, never empty.
        """
        snapshot = plan.snapshot
        if snapshot is not None and snapshot.market_id == market_id:
            return snapshot.token_id
        return market_id

    def _legacy_fee(self, notional: float) -> float:
        """Return the deprecated `BacktestConfig.fee_rate` component.

        Applied IN ADDITION to the venue fee so a pre-T08 caller that
        passed a fee rate does not silently lose it (see
        `BacktestConfig.fee_rate`).

        Args:
            notional: `price * size` for the fill, USD.

        Returns:
            float: `notional * config.fee_rate`, `0.0` by default.
        """
        if self.config.fee_rate <= 0.0:
            return 0.0
        return notional * self.config.fee_rate

    def _book_buy(
        self,
        *,
        position_id: str,
        venue: VenueId,
        market_id: str,
        outcome: str,
        token_id: str,
        exec_price: float,
        size: float,
        cost: float,
        timestamp: datetime,
        signal: Signal | None,
        metadata: dict[str, Any],
    ) -> None:
        """Open or add to a position and deduct its cost from cash.

        Args:
            position_id: `f"{venue}:{market_id}:{outcome}"`.
            venue: Fill venue.
            market_id: Fill market.
            outcome: Fill outcome.
            token_id: Token identifier for the record.
            exec_price: Size-weighted fill price.
            size: Contracts acquired.
            cost: Total USD outlay including fees.
            timestamp: Fill time (aware UTC).
            signal: Originating signal, for stop/target levels.
            metadata: Position metadata.
        """
        existing = self.portfolio.positions.get(position_id)
        if existing is not None:
            total_size = existing.size + size
            existing.cost_basis += cost
            existing.size = total_size
            existing.entry_price = (
                existing.cost_basis / total_size if total_size > 0 else exec_price
            )
        else:
            self.portfolio.positions[position_id] = Position(
                market_id=market_id,
                outcome=outcome,
                token_id=token_id,
                entry_price=exec_price,
                size=size,
                entry_time=timestamp,
                stop_loss=signal.stop_loss if signal is not None else None,
                take_profit=signal.take_profit if signal is not None else None,
                cost_basis=cost,
                metadata=metadata,
                venue=venue,
            )
        self.portfolio.cash -= cost

    def _book_sell(
        self,
        *,
        position_id: str,
        size: float,
        proceeds: float,
    ) -> float | None:
        """Reduce (or remove) a position and credit its net proceeds.

        Args:
            position_id: The position being reduced.
            size: Contracts sold.
            proceeds: Net USD received (notional minus fees).

        Returns:
            float | None: Realized P&L on the sold portion, or `None` if
                there was no such position (nothing is booked).
        """
        pos = self.portfolio.positions.get(position_id)
        if pos is None or pos.size <= 0.0:
            return None
        sold = min(size, pos.size)
        cost_share = pos.cost_basis * (sold / pos.size)
        pnl = proceeds - cost_share
        self.portfolio.cash += proceeds
        self.portfolio.realized_pnl += pnl
        if sold >= pos.size - _SIZE_EPSILON:
            del self.portfolio.positions[position_id]
        else:
            pos.size -= sold
            pos.cost_basis -= cost_share
        return pnl

    # ------------------------------------------------------------------
    # Exits
    # ------------------------------------------------------------------

    async def _close_position(
        self,
        position_id: str,
        timestamp: datetime,
        price: float,
    ) -> None:
        """Close (or partially close) an existing position by SELLING it.

        A genuine exit is WALKED out of the bid side of the position's
        book, so a stop-loss on an illiquid market no longer liquidates
        instantly at the last trade price. The sell limit is
        `min(price, best_bid)` — the caller's target, but never demanding
        more than the book is bidding — with the `SlippageModel.FIXED`
        pad subtracted so a marketable exit can reach a level or two
        deeper. When the walk fills only part of the position, the
        remainder STAYS OPEN; when there is no book or no bid at all,
        nothing is closed and that is logged, because selling into an
        empty book is not an outcome a real account can have.

        There is deliberately NO mark-to-market variant. A position that
        is not sold and not settled is reported as
        `BacktestResult.unrealized_at_end` and marked into `final_value`
        (PLAN.md D6) — booking a synthetic exit trade for it would turn
        an estimate into a fabricated fill and pad the trade count and
        win rate with round trips that never happened.

        Args:
            position_id: Position to close.
            timestamp: Current timestamp (aware UTC).
            price: Target exit price, a probability in [0.0, 1.0].
        """
        pos = self.portfolio.positions.get(position_id)
        if pos is None:
            return

        metadata = dict(pos.metadata)
        snapshot = self._snapshots.get((pos.venue, pos.market_id))
        book = (
            self._book_for(snapshot, pos.venue, pos.market_id, pos.outcome)
            if snapshot is not None
            else None
        )
        best_bid = book.best_bid() if book is not None else None
        if book is None or best_bid is None:
            logger.debug(
                "exit skipped: no bid to sell into",
                extra={"position_id": position_id},
            )
            return
        limit = self._pad_limit(min(price, best_bid.price), "SELL")
        order = OrderRequest(
            venue=pos.venue,
            market_id=pos.market_id,
            outcome=pos.outcome,
            side="SELL",
            price=limit,
            size=pos.size,
            tif="IOC",
            client_order_id=f"exit:{position_id}:{timestamp.isoformat()}",
        )
        result = self._fill_engine.fill(order, book, timestamp)
        if result.filled_size <= 0.0 or result.avg_price is None:
            logger.debug(
                "exit produced no fill",
                extra={"position_id": position_id, "reason": result.reason},
            )
            return
        if not result.metadata.get(TICK_VALIDATED_KEY, False):
            self.tick_unvalidated_fills += 1
        size = result.filled_size
        exec_price = result.avg_price
        notional = size * exec_price
        fee = result.total_fee + self._legacy_fee(notional)

        entry_price = pos.entry_price
        entry_time = pos.entry_time
        token_id = pos.token_id
        market_id = pos.market_id
        outcome = pos.outcome
        venue = pos.venue

        pnl = self._book_sell(
            position_id=position_id,
            size=size,
            proceeds=notional - fee,
        )

        trade = TradeRecord(
            timestamp=timestamp,
            market_id=market_id,
            outcome=outcome,
            token_id=token_id,
            side="SELL",
            price=exec_price,
            size=size,
            fee=fee,
            slippage=price - exec_price,
            pnl=pnl,
            metadata=metadata,
            venue=venue,
        )
        self.trades.append(trade)

        self.strategy.on_position_closed(
            {
                "market_id": market_id,
                "token_id": token_id,
                "entry_price": entry_price,
                "exit_price": exec_price,
                "size": size,
                "entry_time": entry_time,
                "exit_time": timestamp,
            },
            pnl if pnl is not None else 0.0,
        )

    async def _check_position_exits(self, snapshot: MarketSnapshot) -> None:
        """Check for stop loss and take profit exits.

        The stop-loss comparison is `<=` for BOTH outcomes and the
        take-profit comparison `>=` for both: a NO position's price IS
        `snapshot.no_price`, so "the price fell to my stop" is the same
        test on either side — the outcome-specific part is which price is
        read, which it already is. T08 changes only the position id.

        T21 carry-forward 2 (NOTES.md): this used to read
        `snapshot.yes_price` for anything not case-insensitively `"yes"`
        — so a multi-outcome bundle position (outcome `"Trump"`, say)
        was checked against `snapshot.no_price`, a price for a DIFFERENT
        outcome entirely. `_price_for_outcome` resolves the position's
        OWN outcome instead (falling back to `snapshot.yes_price`/
        `no_price` for an actual YES/NO position, byte-identical to the
        prior behavior); when it cannot resolve a price at all, the exit
        check is skipped for that position this tick rather than guessed
        at — the same "never fabricate a number" rule the rest of this
        module follows.

        Args:
            snapshot: Current market snapshot.
        """
        positions_to_close: list[tuple[str, float, str]] = []

        for position_id, pos in self.portfolio.positions.items():
            if pos.market_id != snapshot.market_id or pos.venue != snapshot.venue:
                continue

            # Get current price for this position's own outcome.
            current_price = self._price_for_outcome(snapshot, pos.outcome)
            if current_price is None:
                # T21d defects 5b/5c: this `continue` used to be
                # completely silent, and a position it skipped had its
                # stop-loss and take-profit NEVER EVALUATED — every
                # tick, for the whole run. A protective order that is
                # not being checked is worse than one that does not
                # exist, because the result still reports it as set.
                if (
                    pos.stop_loss is not None or pos.take_profit is not None
                ) and position_id not in self._exit_checks_skipped:
                    self._exit_checks_skipped.add(position_id)
                    logger.warning(
                        "stop-loss/take-profit NOT evaluated: no price could "
                        "be resolved for this position's outcome",
                        extra={
                            "position_id": position_id,
                            "venue": pos.venue,
                            "market_id": pos.market_id,
                            "outcome": pos.outcome,
                            "stop_loss": pos.stop_loss,
                            "take_profit": pos.take_profit,
                        },
                    )
                continue

            if pos.stop_loss is not None and current_price <= pos.stop_loss:
                positions_to_close.append((position_id, current_price, "stop_loss"))
            elif pos.take_profit is not None and current_price >= pos.take_profit:
                positions_to_close.append((position_id, current_price, "take_profit"))

        # Close positions that hit exits
        for position_id, exit_price, reason in positions_to_close:
            logger.debug(f"Closing {position_id} due to {reason} at {exit_price}")
            await self._close_position(position_id, snapshot.timestamp, exit_price)
