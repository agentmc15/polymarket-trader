"""Backtest models."""
from datetime import datetime

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.trade import OrderSide

import enum


class BacktestStatus(str, enum.Enum):
    """Backtest status enum."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class Backtest(Base, TimestampMixin):
    """Backtest model representing a strategy backtest."""

    __tablename__ = "backtests"

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_id: Mapped[int] = mapped_column(ForeignKey("strategies.id"), index=True)

    # Configuration
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    start_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    initial_capital: Mapped[float] = mapped_column(Float, nullable=False)
    parameters: Mapped[dict] = mapped_column(JSONB, default=dict)

    # Status
    status: Mapped[BacktestStatus] = mapped_column(Enum(BacktestStatus), default=BacktestStatus.PENDING)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Results
    final_capital: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_return: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_return_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    annualized_return: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Risk metrics
    sharpe_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    sortino_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_drawdown: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_drawdown_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    volatility: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Trade statistics
    total_trades: Mapped[int] = mapped_column(default=0)
    winning_trades: Mapped[int] = mapped_column(default=0)
    losing_trades: Mapped[int] = mapped_column(default=0)
    win_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_win: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    profit_factor: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Timing
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Equity curve and additional data
    equity_curve: Mapped[list] = mapped_column(JSONB, default=list)
    metadata: Mapped[dict] = mapped_column(JSONB, default=dict)

    # Relationships
    trades: Mapped[list["BacktestTrade"]] = relationship(back_populates="backtest", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_backtests_status", "status"),
        Index("ix_backtests_created_at", "created_at"),
    )


class BacktestTrade(Base):
    """Backtest trade model representing a simulated trade."""

    __tablename__ = "backtest_trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    backtest_id: Mapped[int] = mapped_column(ForeignKey("backtests.id"), index=True)

    # Trade details
    market_condition_id: Mapped[str] = mapped_column(String(66), nullable=False)
    token_id: Mapped[str] = mapped_column(String(100), nullable=False)
    side: Mapped[OrderSide] = mapped_column(Enum(OrderSide), nullable=False)

    # Execution
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    size: Mapped[float] = mapped_column(Float, nullable=False)
    fee: Mapped[float] = mapped_column(Float, default=0.0)

    # Timing
    entry_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    exit_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # P&L
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl_pct: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Signal info
    signal_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    signal_strength: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Metadata
    metadata: Mapped[dict] = mapped_column(JSONB, default=dict)

    # Relationships
    backtest: Mapped["Backtest"] = relationship(back_populates="trades")

    __table_args__ = (
        Index("ix_backtest_trades_entry_time", "entry_time"),
    )
