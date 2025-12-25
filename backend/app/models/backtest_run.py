"""Backtest run model for storing backtest results."""
from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import DateTime, Enum, Float, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class BacktestRunStatus(str, PyEnum):
    """Backtest run status enum."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class BacktestRun(Base):
    """Model for storing backtest execution results.

    Stores comprehensive results from strategy backtests including
    performance metrics, equity curves, and individual trades.
    """

    __tablename__ = "backtest_runs"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Strategy configuration
    strategy_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    strategy_config: Mapped[dict] = mapped_column(JSONB, default=dict)

    # Backtest parameters
    start_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    initial_capital: Mapped[float] = mapped_column(Float, nullable=False)
    fee_rate: Mapped[float] = mapped_column(Float, default=0.0)

    # Status
    status: Mapped[BacktestRunStatus] = mapped_column(
        Enum(BacktestRunStatus), default=BacktestRunStatus.PENDING, index=True
    )

    # Results - Final values
    final_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_return: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Results - Risk metrics
    sharpe_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_drawdown: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Results - Trade statistics
    win_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_trades: Mapped[int] = mapped_column(default=0)

    # Results - Detailed data (stored as JSON)
    equity_curve: Mapped[list] = mapped_column(JSONB, default=list)
    trades_list: Mapped[list] = mapped_column(JSONB, default=list)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_backtest_runs_created_at", "created_at"),
        Index("ix_backtest_runs_strategy_status", "strategy_name", "status"),
        Index("ix_backtest_runs_sharpe", "sharpe_ratio"),
        Index("ix_backtest_runs_total_return", "total_return"),
    )
