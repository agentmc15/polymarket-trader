"""Backtesting API endpoints."""
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.api.deps import AsyncSessionDep

router = APIRouter()


class BacktestRequest(BaseModel):
    """Backtest request schema."""

    strategy_id: str
    start_date: str
    end_date: str
    initial_capital: float = Field(default=10000, gt=0)
    parameters: dict[str, Any] = Field(default_factory=dict)


class BacktestResult(BaseModel):
    """Backtest result schema."""

    backtest_id: str
    status: str
    total_return: float | None = None
    sharpe_ratio: float | None = None
    max_drawdown: float | None = None
    win_rate: float | None = None
    total_trades: int | None = None


@router.get("")
async def list_backtests(
    session: AsyncSessionDep,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    """List all backtests.

    Args:
        session: Database session.
        skip: Number of records to skip.
        limit: Maximum number of records to return.

    Returns:
        dict: List of backtests.
    """
    # TODO: Implement backtest listing
    return {"backtests": [], "total": 0}


@router.post("")
async def run_backtest(
    request: BacktestRequest,
    session: AsyncSessionDep,
) -> BacktestResult:
    """Run a new backtest.

    Args:
        request: Backtest configuration.
        session: Database session.

    Returns:
        BacktestResult: Backtest results.
    """
    # TODO: Implement backtest execution
    return BacktestResult(
        backtest_id="placeholder",
        status="pending",
    )


@router.get("/{backtest_id}")
async def get_backtest(
    backtest_id: str,
    session: AsyncSessionDep,
) -> BacktestResult:
    """Get backtest details and results.

    Args:
        backtest_id: Backtest ID.
        session: Database session.

    Returns:
        BacktestResult: Backtest details.
    """
    # TODO: Implement backtest retrieval
    return BacktestResult(
        backtest_id=backtest_id,
        status="not_found",
    )


@router.get("/{backtest_id}/trades")
async def get_backtest_trades(
    backtest_id: str,
    session: AsyncSessionDep,
) -> dict:
    """Get trades from a backtest.

    Args:
        backtest_id: Backtest ID.
        session: Database session.

    Returns:
        dict: List of backtest trades.
    """
    # TODO: Implement backtest trades
    return {"backtest_id": backtest_id, "trades": []}


@router.get("/strategies")
async def list_strategies(
    session: AsyncSessionDep,
) -> dict:
    """List available strategies for backtesting.

    Args:
        session: Database session.

    Returns:
        dict: List of strategies.
    """
    # TODO: Implement strategy listing
    return {"strategies": []}
