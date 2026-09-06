"""Backtesting API endpoints.

Provides REST endpoints for running backtests, viewing results,
and managing backtest history.
"""
import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from celery.result import AsyncResult
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import desc, func, select

from app.api.deps import AsyncSessionDep
from app.models.backtest_run import BacktestRun, BacktestRunStatus
from app.services.backtesting import (
    DEFAULT_CAPITAL_LEVELS,
    PerformanceMetrics,
    calculate_metrics,
)
from app.strategies import (
    STRATEGIES,
    STRATEGY_CATEGORIES,
    get_default_config,
    list_strategies,
)
from app.tasks import celery_app
from app.tasks.backtesting import run_backtest_task, run_sweep_task

logger = logging.getLogger(__name__)
router = APIRouter()

# Store Celery task IDs for backtests
_backtest_task_ids: dict[int, str] = {}


# ============================================================================
# Pydantic Models
# ============================================================================


class SlippageModelEnum(str, Enum):
    """Slippage model options."""

    NONE = "none"
    FIXED = "fixed"
    VOLUME_BASED = "volume_based"
    SPREAD_BASED = "spread_based"


class BacktestRequest(BaseModel):
    """Request schema for starting a backtest.

    UNKNOWN BODY KEYS ARE A 422, NEVER A SILENT DEFAULT (T33). This is
    the model the `slippage_bps`/`slippage_value` bug landed on: the
    frontend posted `slippage_bps`, pydantic's DEFAULT `extra="ignore"`
    discarded the key, `slippage_value` fell back to `0.001`, and every
    backtest ever run used the default slippage no matter what the form
    said — no error, no warning, wrong numbers. (`CLAUDE.md`'s
    `TRADING_KILL_SWITCH_PATH` against `Settings`' `KILL_SWITCH_PATH`
    was the same failure with a worse blast radius: an operator halting
    trading during an incident would have halted nothing.)

    `extra="forbid"` turns both of those into a 422 that NAMES the
    offending field. It is set here, explicitly and identically, on
    every model that parses a request body — `SweepRequest` below,
    `bots.BotConfig`, `links.ApproveRequest`/`RejectRequest`,
    `trading.OrderRequest` — and on NO response model: a response model
    serializes outward and has no caller input to reject, so the setting
    there would be meaningless at best and, for a model ever fed back
    through `model_validate`, actively harmful.
    `tests/api/test_request_models_forbid_extras.py` re-derives the
    request set from the live route table and fails if a new body model
    ships without it.
    """

    model_config = ConfigDict(extra="forbid")

    strategy: str = Field(
        ...,
        description="Strategy name from available strategies",
        examples=["catalyst_momentum"],
    )
    strategy_config: dict[str, Any] = Field(
        default_factory=dict,
        description="Strategy-specific configuration overrides",
    )
    start_date: datetime = Field(
        ...,
        description="Backtest start date",
        examples=["2024-01-01T00:00:00Z"],
    )
    end_date: datetime = Field(
        ...,
        description="Backtest end date",
        examples=["2024-06-01T00:00:00Z"],
    )
    initial_capital: float = Field(
        default=10000.0,
        gt=0,
        description="Starting capital in dollars",
    )
    fee_rate: float = Field(
        default=0.0,
        ge=0,
        lt=1,
        description="Trading fee rate (0.001 = 0.1%)",
    )
    slippage_model: SlippageModelEnum = Field(
        default=SlippageModelEnum.FIXED,
        description="Slippage simulation model",
    )
    slippage_value: float = Field(
        default=0.001,
        ge=0,
        description="Slippage parameter value",
    )
    markets: list[str] | None = Field(
        default=None,
        description="Optional list of market IDs to include",
    )

    @field_validator("strategy")
    @classmethod
    def validate_strategy(cls, v: str) -> str:
        """Validate strategy exists."""
        if v not in STRATEGIES:
            available = ", ".join(sorted(STRATEGIES.keys()))
            raise ValueError(f"Unknown strategy '{v}'. Available: {available}")
        return v

    @field_validator("end_date")
    @classmethod
    def validate_end_date(cls, v: datetime, info) -> datetime:
        """Validate end_date is after start_date."""
        if "start_date" in info.data and v <= info.data["start_date"]:
            raise ValueError("end_date must be after start_date")
        return v


class BacktestResponse(BaseModel):
    """Response schema for backtest creation."""

    id: int = Field(..., description="Backtest ID")
    status: str = Field(..., description="Current status")
    message: str = Field(..., description="Status message")


class SweepRequest(BacktestRequest):
    """Request schema for a capital sweep (PLAN.md D12, T22).

    Extends `BacktestRequest` with `capital_levels` — every other field
    (dates, fee/slippage config, market filter) is held fixed across the
    sweep, exactly as `run_sweep`'s `base_config` is (T22 sweeps CAPITAL,
    not strategy behavior).
    """

    #: Restated rather than inherited (T33): this line is what a reader
    #: greps for, and it keeps the rule if this model ever stops
    #: extending `BacktestRequest`. See that model's docstring.
    model_config = ConfigDict(extra="forbid")

    capital_levels: list[float] | None = Field(
        default=None,
        description=(
            "Capital levels to sweep, USD (default: "
            f"{', '.join(str(int(c)) for c in DEFAULT_CAPITAL_LEVELS)})"
        ),
    )

    @field_validator("capital_levels")
    @classmethod
    def validate_capital_levels(cls, v: list[float] | None) -> list[float] | None:
        """Reject an explicitly empty or non-positive level list."""
        if v is None:
            return v
        if not v:
            raise ValueError("capital_levels must be non-empty when provided")
        if any(level <= 0 for level in v):
            raise ValueError("every capital_levels entry must be positive")
        return v


class SweepResponse(BaseModel):
    """Response schema for sweep creation."""

    id: int = Field(..., description="Parent backtest ID (strategy_name=sweep:<name>)")
    status: str = Field(..., description="Current status")
    message: str = Field(..., description="Status message")


class TradeMetrics(BaseModel):
    """Trade statistics for one COMPLETED run.

    `None` MEANS "NOT COMPUTED", AND IS THE ONLY HONEST WAY TO SAY IT
    (T33/T36, GUARDRAILS.md §1.7). Before T33 every field here defaulted
    to `0.0` and `get_backtest_status` populated two of them, so a
    metric that had never been computed left this endpoint as a `0.0`
    indistinguishable from a measured zero — which is exactly why the
    frontend refused to render seven of these nine fields at all. The
    unpopulated fields are now nullable and are filled from
    `app.services.backtesting.metrics.calculate_metrics` re-run over the
    run's own persisted `equity_curve`/`trades_list` (see
    `_recompute_metrics`); when that is not possible they stay `None`
    rather than becoming a zero.

    `total_trades` is the only field that keeps a non-null type: it
    comes from the `BacktestRun` COLUMN, always a real (possibly `0`)
    count the run itself recorded, and for a sweep PARENT row (whose
    `equity_curve`/`trades_list` are empty because the results live on
    its children) the column is the only meaningful `total_trades`
    there is. `win_rate` (T36) is also column-backed but IS nullable:
    the column itself is `NULL` on exactly that same sweep-parent row,
    and reporting `0.0` there would claim a measured 0% win rate that
    was never computed.

    Attributes:
        profit_factor: Gross profit / gross loss. `999.99` is
            `calculate_metrics`' finite stand-in for an INFINITE ratio
            (there were winning trades and no losing ones), not a
            measured value — do not plot it as one.
    """

    total_trades: int = 0
    winning_trades: int | None = None
    losing_trades: int | None = None
    win_rate: float | None = None
    profit_factor: float | None = None
    avg_win: float | None = None
    avg_loss: float | None = None
    largest_win: float | None = None
    largest_loss: float | None = None


class RiskMetrics(BaseModel):
    """Risk statistics for one COMPLETED run.

    Same `None`-means-not-computed contract as `TradeMetrics`, same
    reason. `sharpe_ratio`/`max_drawdown` (T36) are column-backed, same
    as `win_rate` above, and nullable for the same reason: the columns
    are `NULL` on a sweep parent, and `0.0` would claim a measured zero
    Sharpe ratio / drawdown that was never computed. The other three are
    recomputed by `_recompute_metrics` and are `None` when the persisted
    equity curve cannot support the calculation.

    Attributes:
        sortino_ratio: `calculate_metrics` leaves this at `0.0` when
            downside volatility is zero (no losing day in the window) —
            an undefined ratio, not a measured zero. It is reported as
            given rather than re-derived here; `metrics.py` owns that
            convention.
        var_95: The 5th percentile of DAILY returns, a fraction (e.g.
            `-0.02` = -2%). Negative for any run that had a losing day.
    """

    sharpe_ratio: float | None = None
    sortino_ratio: float | None = None
    max_drawdown: float | None = None
    max_drawdown_pct: float | None = None
    volatility: float | None = None
    var_95: float | None = None


class BacktestStatusResponse(BaseModel):
    """Response schema for backtest status and results."""

    id: int
    strategy_name: str
    strategy_config: dict[str, Any]
    status: str
    start_date: datetime
    end_date: datetime
    initial_capital: float
    fee_rate: float

    # Results (populated when completed)
    final_value: float | None = None
    total_return: float | None = None
    total_return_pct: float | None = None

    # Metrics
    trade_metrics: TradeMetrics | None = None
    risk_metrics: RiskMetrics | None = None

    #: Trustworthiness and coverage payload (`BacktestRun.report`,
    #: migration `003`): `depth_source`, `fill_at`, the intent counters,
    #: settlement/unrealized counts, and the survivorship `coverage`
    #: census. GUARDRAILS.md §1.7 requires a result produced from
    #: synthesized depth, same-snapshot fills, or a mostly-unresolved
    #: market population to be labeled as such wherever it is shown —
    #: this endpoint is one of those places. An EMPTY dict means the run
    #: predates report capture, NOT that its coverage was zero.
    report: dict[str, Any] = Field(default_factory=dict)

    # Progress
    progress: float = 0.0
    error_message: str | None = None

    # Timestamps
    created_at: datetime
    completed_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class EquityCurvePoint(BaseModel):
    """Single point on equity curve."""

    timestamp: datetime
    equity: float
    drawdown: float = 0.0


class EquityCurveResponse(BaseModel):
    """Response schema for equity curve data."""

    backtest_id: int
    points: list[EquityCurvePoint]
    initial_capital: float
    final_value: float


class TradeRecord(BaseModel):
    """Individual trade record."""

    timestamp: datetime
    market_id: str
    outcome: str
    side: str
    price: float
    size: float
    fee: float = 0.0
    pnl: float | None = None
    signal_confidence: float = 0.0


class TradesResponse(BaseModel):
    """Response schema for trade list."""

    backtest_id: int
    trades: list[TradeRecord]
    total_count: int


class StrategyInfo(BaseModel):
    """Strategy information."""

    name: str
    description: str
    version: str
    category: str
    default_config: dict[str, Any]


class StrategiesResponse(BaseModel):
    """Response schema for strategy list."""

    strategies: list[StrategyInfo]
    categories: dict[str, list[str]]


class BacktestListItem(BaseModel):
    """Backtest summary for list view."""

    id: int
    strategy_name: str
    status: str
    start_date: datetime
    end_date: datetime
    initial_capital: float
    final_value: float | None
    total_return: float | None
    sharpe_ratio: float | None
    max_drawdown: float | None
    total_trades: int
    created_at: datetime


class BacktestListResponse(BaseModel):
    """Response schema for backtest list."""

    backtests: list[BacktestListItem]
    total: int
    skip: int
    limit: int


# ============================================================================
# Metric recomputation (T33)
# ============================================================================


@dataclass(frozen=True)
class _PersistedTrade:
    """One `BacktestRun.trades_list` row, in the shape metrics code reads.

    `app.services.backtesting.metrics._calculate_trade_metrics` reaches
    for `t.pnl`/`t.fee`/`t.price`/`t.size` by ATTRIBUTE, each behind a
    `hasattr` guard. Handing it the persisted DICTS would therefore not
    raise — every `hasattr` would simply be `False`, the function would
    take its "no closing trades" branch, and it would return zeros for
    every field. A zero that means "I was handed the wrong type" is the
    precise failure `TradeMetrics`' nullability exists to avoid, so the
    dicts are converted into real objects here.

    `slippage` is deliberately absent: `_populate_run_from_result` does
    not persist it, and the `hasattr` guard means its absence costs only
    `total_slippage`, which this endpoint does not report.

    Attributes:
        pnl: Realized P&L in USD, or `None` for an OPENING fill (a row
            with no `pnl` is not a closed round trip and is excluded
            from every win/loss statistic).
        fee: Fee paid on this fill, USD.
        price: Fill price, a probability in `[0, 1]`.
        size: Fill size in contracts.
    """

    pnl: float | None
    fee: float
    price: float
    size: float


@dataclass(frozen=True)
class _RecomputedMetrics:
    """`calculate_metrics` re-run over one run's persisted results.

    Attributes:
        metrics: The recomputed metrics.
        has_closing_trades: Whether `trades_list` held at least one row
            with a `pnl` — i.e. whether the win/loss statistics
            (`profit_factor`, `avg_win`/`avg_loss`, `largest_win`/
            `largest_loss`) were computed from anything at all. When
            `False`, `calculate_metrics` returns `0.0` for each of them
            by construction, and the endpoint reports `None` instead.
    """

    metrics: PerformanceMetrics
    has_closing_trades: bool


def _persisted_equity_curve(run: BacktestRun) -> list[tuple[datetime, float]]:
    """Return `run.equity_curve` in `calculate_metrics`' input shape.

    Tolerates both persisted shapes for the same reason
    `get_backtest_equity_curve` does: `_populate_run_from_result` writes
    `{"timestamp": iso, "equity": float}` dicts, but older rows may hold
    `[timestamp, equity]` pairs. An unparseable point is SKIPPED rather
    than defaulted to zero — a fabricated `(epoch, 0.0)` point would
    invent a drawdown of the entire book.

    Args:
        run: The persisted backtest row.

    Returns:
        list[tuple[datetime, float]]: `(timestamp, equity)` pairs, in
            stored order.
    """
    points: list[tuple[datetime, float]] = []
    for point in run.equity_curve or []:
        if isinstance(point, dict):
            raw_ts, raw_equity = point.get("timestamp"), point.get("equity")
        elif isinstance(point, list | tuple) and len(point) >= 2:
            raw_ts, raw_equity = point[0], point[1]
        else:
            continue

        if isinstance(raw_ts, str):
            try:
                timestamp = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
            except ValueError:
                continue
        elif isinstance(raw_ts, datetime):
            timestamp = raw_ts
        else:
            continue

        if not isinstance(raw_equity, int | float):
            continue
        points.append((timestamp, float(raw_equity)))
    return points


def _persisted_trades(run: BacktestRun) -> list[_PersistedTrade]:
    """Return `run.trades_list` as `_PersistedTrade` objects.

    Args:
        run: The persisted backtest row.

    Returns:
        list[_PersistedTrade]: One entry per persisted fill; malformed
            rows are skipped.
    """
    trades: list[_PersistedTrade] = []
    for trade in run.trades_list or []:
        if not isinstance(trade, dict):
            continue
        pnl = trade.get("pnl")
        trades.append(
            _PersistedTrade(
                pnl=float(pnl) if isinstance(pnl, int | float) else None,
                fee=float(trade.get("fee") or 0.0),
                price=float(trade.get("price") or 0.0),
                size=float(trade.get("size") or 0.0),
            )
        )
    return trades


def _recompute_metrics(run: BacktestRun) -> _RecomputedMetrics | None:
    """Re-run `calculate_metrics` over one run's own persisted results.

    WHY RECOMPUTE RATHER THAN READ. `app.tasks.backtesting.
    _populate_run_from_result` already calls `calculate_metrics` and
    then keeps only SIX of its values (`final_value`, `total_return`,
    `sharpe_ratio`, `max_drawdown`, `win_rate`, `total_trades`) — the
    other eleven `TradeMetrics`/`RiskMetrics` fields are computed at run
    time and thrown away, which is why this endpoint could not report
    them. The inputs those eleven are derived from ARE persisted in
    full, though (`equity_curve` and `trades_list`), so running the same
    function over the same inputs reproduces the same numbers, for every
    row already in the table as well as for new ones — no migration, no
    second definition of any metric.

    Args:
        run: The persisted backtest row.

    Returns:
        _RecomputedMetrics | None: `None` when the row's equity curve has
            fewer than two usable points, which is exactly the input
            `calculate_metrics` refuses (it returns an all-zero
            `PerformanceMetrics`, and reporting those zeros as measured
            values is the thing being fixed). A sweep PARENT row is the
            common case: its results live on its children, so its own
            `equity_curve` is empty and it can honestly report nothing.
    """
    curve = _persisted_equity_curve(run)
    if len(curve) < 2:
        return None
    trades = _persisted_trades(run)
    return _RecomputedMetrics(
        metrics=calculate_metrics(curve, trades, run.initial_capital),
        has_closing_trades=any(trade.pnl is not None for trade in trades),
    )


# ============================================================================
# Endpoints
# ============================================================================


@router.get("/strategies", response_model=StrategiesResponse)
async def list_available_strategies() -> StrategiesResponse:
    """List all available trading strategies for backtesting.

    Returns:
        StrategiesResponse: Available strategies with configurations.
    """
    strategies_list = list_strategies()

    strategy_infos = [
        StrategyInfo(
            name=s["name"],
            description=s["description"],
            version=s["version"],
            category=s["category"],
            default_config=get_default_config(s["name"]),
        )
        for s in strategies_list
    ]

    return StrategiesResponse(
        strategies=strategy_infos,
        categories=STRATEGY_CATEGORIES,
    )


@router.post("", response_model=BacktestResponse)
async def start_backtest(
    request: BacktestRequest,
    session: AsyncSessionDep,
) -> BacktestResponse:
    """Start a new backtest using Celery task queue.

    Args:
        request: Backtest configuration.
        session: Database session.

    Returns:
        BacktestResponse: Backtest ID and initial status.
    """
    # Create backtest record
    backtest = BacktestRun(
        strategy_name=request.strategy,
        strategy_config=request.strategy_config,
        start_date=request.start_date,
        end_date=request.end_date,
        initial_capital=request.initial_capital,
        fee_rate=request.fee_rate,
        status=BacktestRunStatus.PENDING,
    )

    session.add(backtest)
    await session.commit()
    await session.refresh(backtest)

    backtest_id = backtest.id

    # Prepare request data for Celery task (must be JSON serializable)
    request_data = {
        "strategy_name": request.strategy,
        "strategy_config": request.strategy_config,
        "start_date": request.start_date.isoformat(),
        "end_date": request.end_date.isoformat(),
        "initial_capital": request.initial_capital,
        "fee_rate": request.fee_rate,
        "slippage_model": request.slippage_model.value,
        "slippage_value": request.slippage_value,
        "markets": request.markets,
    }

    # Start Celery task
    task = run_backtest_task.delay(backtest_id, request_data)

    # Store task ID for progress tracking
    _backtest_task_ids[backtest_id] = task.id

    logger.info(f"Started backtest {backtest_id} with Celery task {task.id}")

    return BacktestResponse(
        id=backtest_id,
        status="PENDING",
        message=f"Backtest queued. Task ID: {task.id}",
    )


@router.post("/sweep", response_model=SweepResponse)
async def start_sweep(
    request: SweepRequest,
    session: AsyncSessionDep,
) -> SweepResponse:
    """Start a capital sweep using Celery (PLAN.md D12, T22).

    Creates a PARENT `BacktestRun` (`strategy_name=f"sweep:{name}"`) that
    `run_sweep_task` fills in with the aggregate `EdgeDecayReport`
    (`report["edge_decay"]`) plus the ids of one CHILD `BacktestRun` per
    capital level, each populated identically to a standalone run.

    Args:
        request: Sweep configuration (`BacktestRequest` + `capital_levels`).
        session: Database session.

    Returns:
        SweepResponse: Parent backtest ID and initial status.
    """
    levels = request.capital_levels or list(DEFAULT_CAPITAL_LEVELS)

    backtest = BacktestRun(
        strategy_name=f"sweep:{request.strategy}",
        strategy_config=request.strategy_config,
        start_date=request.start_date,
        end_date=request.end_date,
        initial_capital=max(levels),
        fee_rate=request.fee_rate,
        status=BacktestRunStatus.PENDING,
    )

    session.add(backtest)
    await session.commit()
    await session.refresh(backtest)

    backtest_id = backtest.id

    request_data = {
        "strategy_name": request.strategy,
        "strategy_config": request.strategy_config,
        "start_date": request.start_date.isoformat(),
        "end_date": request.end_date.isoformat(),
        "fee_rate": request.fee_rate,
        "slippage_model": request.slippage_model.value,
        "slippage_value": request.slippage_value,
        "markets": request.markets,
        "capital_levels": levels,
    }

    task = run_sweep_task.delay(backtest_id, request_data)
    _backtest_task_ids[backtest_id] = task.id

    logger.info(f"Started sweep {backtest_id} with Celery task {task.id}")

    return SweepResponse(
        id=backtest_id,
        status="PENDING",
        message=f"Sweep queued. Task ID: {task.id}",
    )


@router.get("/{backtest_id}", response_model=BacktestStatusResponse)
async def get_backtest_status(
    backtest_id: int,
    session: AsyncSessionDep,
) -> BacktestStatusResponse:
    """Get backtest status and results.

    Args:
        backtest_id: Backtest ID.
        session: Database session.

    Returns:
        BacktestStatusResponse: Backtest details and results.

    Raises:
        HTTPException: If backtest not found.
    """
    query = select(BacktestRun).where(BacktestRun.id == backtest_id)
    result = await session.execute(query)
    backtest = result.scalar_one_or_none()

    if not backtest:
        raise HTTPException(status_code=404, detail="Backtest not found")

    # Get progress from Celery task if still running
    progress = 0.0
    if backtest.status == BacktestRunStatus.COMPLETED:
        progress = 1.0
    elif backtest.status in (BacktestRunStatus.PENDING, BacktestRunStatus.RUNNING):
        # Check Celery task state for progress
        task_id = _backtest_task_ids.get(backtest_id)
        if task_id:
            task_result = AsyncResult(task_id, app=celery_app)
            if task_result.state == "PROGRESS":
                task_meta = task_result.info or {}
                progress = task_meta.get("progress", 0.0)
            elif task_result.state == "SUCCESS":
                progress = 1.0

    # Build trade metrics
    trade_metrics = None
    risk_metrics = None

    if backtest.status == BacktestRunStatus.COMPLETED:
        # T33/T36: the four fields `BacktestRun` persists as columns
        # still come from the columns (they are what the run itself
        # recorded), and, as of T36, come through AS PERSISTED — a
        # `NULL` column (a sweep parent) reports `None`, never a
        # coerced `0.0`. The other eleven are recomputed from the same
        # persisted `equity_curve`/`trades_list` the run's own metrics
        # came from, and are likewise `None` when that is not possible.
        # See `_recompute_metrics` and `TradeMetrics`'/`RiskMetrics`'
        # docstrings.
        recomputed = _recompute_metrics(backtest)
        derived = recomputed.metrics if recomputed is not None else None
        closed = recomputed.has_closing_trades if recomputed is not None else False

        trade_metrics = TradeMetrics(
            total_trades=backtest.total_trades,
            win_rate=backtest.win_rate,
            winning_trades=derived.winning_trades if derived else None,
            losing_trades=derived.losing_trades if derived else None,
            profit_factor=derived.profit_factor if derived and closed else None,
            avg_win=derived.avg_win if derived and closed else None,
            avg_loss=derived.avg_loss if derived and closed else None,
            largest_win=derived.largest_win if derived and closed else None,
            largest_loss=derived.largest_loss if derived and closed else None,
        )

        risk_metrics = RiskMetrics(
            sharpe_ratio=backtest.sharpe_ratio,
            max_drawdown=backtest.max_drawdown,
            sortino_ratio=derived.sortino_ratio if derived else None,
            max_drawdown_pct=derived.max_drawdown_pct if derived else None,
            volatility=derived.volatility if derived else None,
            var_95=derived.var_95 if derived else None,
        )

    return BacktestStatusResponse(
        id=backtest.id,
        strategy_name=backtest.strategy_name,
        strategy_config=backtest.strategy_config,
        status=backtest.status.value,
        start_date=backtest.start_date,
        end_date=backtest.end_date,
        initial_capital=backtest.initial_capital,
        fee_rate=backtest.fee_rate,
        final_value=backtest.final_value,
        total_return=backtest.total_return,
        total_return_pct=(backtest.total_return * 100) if backtest.total_return else None,
        trade_metrics=trade_metrics,
        risk_metrics=risk_metrics,
        report=dict(backtest.report or {}),
        progress=progress,
        created_at=backtest.created_at,
        completed_at=backtest.completed_at,
    )


@router.get("/{backtest_id}/edge-decay")
async def get_backtest_edge_decay(
    backtest_id: int,
    session: AsyncSessionDep,
) -> dict[str, Any]:
    """Get the `EdgeDecayReport` for a completed sweep (PLAN.md D12, T22).

    GUARDRAILS.md §1.7: the returned payload carries `depth_source`/
    `fill_at` on every row and the `sweep_ceiling_note`/
    `unmeasurable_note` caveats — a caller displaying `edge_dies_at`
    without also showing those is not showing the whole number.

    Args:
        backtest_id: The PARENT sweep's backtest ID (the one returned by
            `POST /api/v1/backtests/sweep`, `strategy_name=f"sweep:
            {name}"`).
        session: Database session.

    Returns:
        dict[str, Any]: `edge_decay_report_to_dict()`'s shape.

    Raises:
        HTTPException: 404 if the backtest does not exist, or if it
            carries no `edge_decay` report (not a sweep, or not yet
            completed).
    """
    query = select(BacktestRun).where(BacktestRun.id == backtest_id)
    result = await session.execute(query)
    backtest = result.scalar_one_or_none()

    if not backtest:
        raise HTTPException(status_code=404, detail="Backtest not found")

    edge_decay: dict[str, Any] | None = (backtest.report or {}).get("edge_decay")
    if edge_decay is None:
        raise HTTPException(
            status_code=404,
            detail="This backtest has no edge-decay report — it is not a "
            "sweep, or the sweep has not completed yet",
        )

    return edge_decay


@router.get("/{backtest_id}/equity-curve", response_model=EquityCurveResponse)
async def get_backtest_equity_curve(
    backtest_id: int,
    session: AsyncSessionDep,
    downsample: int = Query(default=500, ge=10, le=5000, description="Max points to return"),
) -> EquityCurveResponse:
    """Get equity curve data for a backtest.

    Args:
        backtest_id: Backtest ID.
        session: Database session.
        downsample: Maximum number of points to return.

    Returns:
        EquityCurveResponse: Equity curve data.

    Raises:
        HTTPException: If backtest not found.
    """
    query = select(BacktestRun).where(BacktestRun.id == backtest_id)
    result = await session.execute(query)
    backtest = result.scalar_one_or_none()

    if not backtest:
        raise HTTPException(status_code=404, detail="Backtest not found")

    if backtest.status != BacktestRunStatus.COMPLETED:
        raise HTTPException(status_code=400, detail="Backtest not completed")

    equity_curve = backtest.equity_curve or []

    # Downsample if needed
    if len(equity_curve) > downsample:
        step = len(equity_curve) // downsample
        equity_curve = equity_curve[::step]

    # Calculate drawdowns
    points = []
    peak = backtest.initial_capital

    for point in equity_curve:
        # Handle both dict and list formats
        if isinstance(point, dict):
            ts = point.get("timestamp")
            equity = point.get("equity", 0)
        else:
            ts, equity = point[0], point[1]

        # Parse timestamp if string
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))

        peak = max(peak, equity)
        drawdown = (peak - equity) / peak if peak > 0 else 0

        points.append(EquityCurvePoint(
            timestamp=ts,
            equity=equity,
            drawdown=drawdown,
        ))

    return EquityCurveResponse(
        backtest_id=backtest_id,
        points=points,
        initial_capital=backtest.initial_capital,
        final_value=backtest.final_value or backtest.initial_capital,
    )


@router.get("/{backtest_id}/trades", response_model=TradesResponse)
async def get_backtest_trades(
    backtest_id: int,
    session: AsyncSessionDep,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1000),
) -> TradesResponse:
    """Get trade list from a backtest.

    Args:
        backtest_id: Backtest ID.
        session: Database session.
        skip: Number of trades to skip.
        limit: Maximum trades to return.

    Returns:
        TradesResponse: List of trades.

    Raises:
        HTTPException: If backtest not found.
    """
    query = select(BacktestRun).where(BacktestRun.id == backtest_id)
    result = await session.execute(query)
    backtest = result.scalar_one_or_none()

    if not backtest:
        raise HTTPException(status_code=404, detail="Backtest not found")

    trades_list = backtest.trades_list or []
    total_count = len(trades_list)

    # Apply pagination
    paginated_trades = trades_list[skip:skip + limit]

    # Convert to response format
    trades = []
    for trade in paginated_trades:
        if isinstance(trade, dict):
            ts = trade.get("timestamp")
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))

            trades.append(TradeRecord(
                timestamp=ts,
                market_id=trade.get("market_id", ""),
                outcome=trade.get("outcome", ""),
                side=trade.get("side", ""),
                price=trade.get("price", 0),
                size=trade.get("size", 0),
                fee=trade.get("fee", 0),
                pnl=trade.get("pnl"),
                signal_confidence=trade.get("signal_confidence", 0),
            ))

    return TradesResponse(
        backtest_id=backtest_id,
        trades=trades,
        total_count=total_count,
    )


@router.get("", response_model=BacktestListResponse)
async def list_backtests(
    session: AsyncSessionDep,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    strategy: str | None = Query(default=None, description="Filter by strategy name"),
    status: str | None = Query(default=None, description="Filter by status"),
) -> BacktestListResponse:
    """List all backtests with pagination.

    Args:
        session: Database session.
        skip: Number of records to skip.
        limit: Maximum records to return.
        strategy: Optional strategy filter.
        status: Optional status filter.

    Returns:
        BacktestListResponse: List of backtests.
    """
    # Build query
    query = select(BacktestRun)

    if strategy:
        query = query.where(BacktestRun.strategy_name == strategy)

    if status:
        try:
            status_enum = BacktestRunStatus(status.upper())
            query = query.where(BacktestRun.status == status_enum)
        except ValueError:
            pass

    # Get total count
    count_query = select(func.count(BacktestRun.id))
    if strategy:
        count_query = count_query.where(BacktestRun.strategy_name == strategy)
    if status:
        try:
            status_enum = BacktestRunStatus(status.upper())
            count_query = count_query.where(BacktestRun.status == status_enum)
        except ValueError:
            pass

    count_result = await session.execute(count_query)
    total = count_result.scalar() or 0

    # Get paginated results
    query = query.order_by(desc(BacktestRun.created_at)).offset(skip).limit(limit)
    result = await session.execute(query)
    backtests = result.scalars().all()

    items = [
        BacktestListItem(
            id=b.id,
            strategy_name=b.strategy_name,
            status=b.status.value,
            start_date=b.start_date,
            end_date=b.end_date,
            initial_capital=b.initial_capital,
            final_value=b.final_value,
            total_return=b.total_return,
            sharpe_ratio=b.sharpe_ratio,
            max_drawdown=b.max_drawdown,
            total_trades=b.total_trades,
            created_at=b.created_at,
        )
        for b in backtests
    ]

    return BacktestListResponse(
        backtests=items,
        total=total,
        skip=skip,
        limit=limit,
    )


@router.delete("/{backtest_id}")
async def delete_backtest(
    backtest_id: int,
    session: AsyncSessionDep,
) -> dict[str, str]:
    """Delete a backtest record.

    Args:
        backtest_id: Backtest ID.
        session: Database session.

    Returns:
        dict: Confirmation message.

    Raises:
        HTTPException: If backtest not found or still running.
    """
    query = select(BacktestRun).where(BacktestRun.id == backtest_id)
    result = await session.execute(query)
    backtest = result.scalar_one_or_none()

    if not backtest:
        raise HTTPException(status_code=404, detail="Backtest not found")

    if backtest.status == BacktestRunStatus.RUNNING:
        raise HTTPException(status_code=400, detail="Cannot delete running backtest")

    await session.delete(backtest)
    await session.commit()

    return {"message": f"Backtest {backtest_id} deleted"}
