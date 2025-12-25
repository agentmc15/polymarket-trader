"""Strategy model."""
from sqlalchemy import Boolean, Enum, Float, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

import enum


class StrategyType(str, enum.Enum):
    """Strategy type enum."""

    ARBITRAGE = "ARBITRAGE"
    MOMENTUM = "MOMENTUM"
    MEAN_REVERSION = "MEAN_REVERSION"
    MARKET_MAKING = "MARKET_MAKING"
    COPY_TRADING = "COPY_TRADING"
    CUSTOM = "CUSTOM"


class Strategy(Base, TimestampMixin):
    """Strategy model representing a trading strategy."""

    __tablename__ = "strategies"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Strategy configuration
    strategy_type: Mapped[StrategyType] = mapped_column(Enum(StrategyType), nullable=False)
    parameters: Mapped[dict] = mapped_column(JSONB, default=dict)
    default_parameters: Mapped[dict] = mapped_column(JSONB, default=dict)

    # Risk settings
    max_position_size: Mapped[float] = mapped_column(Float, default=1000.0)
    max_daily_loss: Mapped[float] = mapped_column(Float, default=100.0)
    stop_loss_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit_pct: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Status
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_backtested: Mapped[bool] = mapped_column(Boolean, default=False)

    # Performance metrics
    total_trades: Mapped[int] = mapped_column(default=0)
    win_rate: Mapped[float] = mapped_column(Float, default=0.0)
    avg_profit: Mapped[float] = mapped_column(Float, default=0.0)
    sharpe_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_drawdown: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Code reference
    module_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    class_name: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Metadata
    metadata: Mapped[dict] = mapped_column(JSONB, default=dict)
