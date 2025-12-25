"""Trader models for tracking and copy trading."""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class Trader(Base, TimestampMixin):
    """Trader model for tracking external traders."""

    __tablename__ = "traders"

    id: Mapped[int] = mapped_column(primary_key=True)
    address: Mapped[str] = mapped_column(String(42), unique=True, index=True)

    # Profile
    username: Mapped[str | None] = mapped_column(String(100), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Statistics
    total_trades: Mapped[int] = mapped_column(default=0)
    winning_trades: Mapped[int] = mapped_column(default=0)
    losing_trades: Mapped[int] = mapped_column(default=0)
    win_rate: Mapped[float] = mapped_column(Float, default=0.0)

    # P&L
    total_profit: Mapped[float] = mapped_column(Float, default=0.0)
    total_volume: Mapped[float] = mapped_column(Float, default=0.0)
    avg_position_size: Mapped[float] = mapped_column(Float, default=0.0)

    # Timing
    first_trade_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_trade_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Tracking
    is_tracked: Mapped[bool] = mapped_column(Boolean, default=False)
    track_priority: Mapped[int] = mapped_column(default=0)

    # Metadata
    metadata: Mapped[dict] = mapped_column(JSONB, default=dict)

    # Relationships
    followers: Mapped[list["TraderFollow"]] = relationship(back_populates="trader", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_traders_win_rate", "win_rate"),
        Index("ix_traders_total_profit", "total_profit"),
        Index("ix_traders_is_tracked", "is_tracked"),
    )


class TraderFollow(Base, TimestampMixin):
    """TraderFollow model for copy trading configuration."""

    __tablename__ = "trader_follows"

    id: Mapped[int] = mapped_column(primary_key=True)
    trader_id: Mapped[int] = mapped_column(index=True)
    trader_address: Mapped[str] = mapped_column(String(42), index=True)

    # Copy trading settings
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    copy_percentage: Mapped[float] = mapped_column(Float, default=100.0)
    max_position_size: Mapped[float] = mapped_column(Float, default=1000.0)
    min_position_size: Mapped[float] = mapped_column(Float, default=10.0)

    # Filters
    copy_buys: Mapped[bool] = mapped_column(Boolean, default=True)
    copy_sells: Mapped[bool] = mapped_column(Boolean, default=True)
    excluded_markets: Mapped[list] = mapped_column(JSONB, default=list)
    included_categories: Mapped[list] = mapped_column(JSONB, default=list)

    # Delay settings
    delay_seconds: Mapped[int] = mapped_column(default=0)
    max_slippage: Mapped[float] = mapped_column(Float, default=0.02)

    # Statistics
    total_copied_trades: Mapped[int] = mapped_column(default=0)
    total_profit: Mapped[float] = mapped_column(Float, default=0.0)

    # Metadata
    metadata: Mapped[dict] = mapped_column(JSONB, default=dict)

    # Relationships
    trader: Mapped["Trader"] = relationship(back_populates="followers")

    __table_args__ = (
        Index("ix_trader_follows_is_active", "is_active"),
    )
