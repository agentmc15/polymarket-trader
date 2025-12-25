"""Backtesting API endpoints.

Provides REST endpoints for running backtests, viewing results,
and managing backtest history.
"""
from datetime import datetime
from enum import Enum
from typing import Any
import logging

from celery.result import AsyncResult
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, func, desc

from app.api.deps import AsyncSessionDep
from app.models.backtest_run import BacktestRun, BacktestRunStatus
from app.strategies import (
    STRATEGIES,
    STRATEGY_CATEGORIES,
    get_default_config,
    list_strategies,
)
from app.services.backtesting import SlippageModel
from app.tasks.backtesting import run_backtest_task
from app.tasks import celery_app


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
    """Request schema for starting a backtest."""

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


class TradeMetrics(BaseModel):
    """Trade statistics."""

    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0


class RiskMetrics(BaseModel):
    """Risk statistics."""

    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    volatility: float = 0.0
    var_95: float = 0.0


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

    # Progress
    progress: float = 0.0
    error_message: str | None = None

    # Timestamps
    created_at: datetime
    completed_at: datetime | None = None

    class Config:
        """Pydantic config."""

        from_attributes = True


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
        trade_metrics = TradeMetrics(
            total_trades=backtest.total_trades,
            win_rate=backtest.win_rate or 0.0,
        )

        risk_metrics = RiskMetrics(
            sharpe_ratio=backtest.sharpe_ratio or 0.0,
            max_drawdown=backtest.max_drawdown or 0.0,
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
        progress=progress,
        created_at=backtest.created_at,
        completed_at=backtest.completed_at,
    )


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
