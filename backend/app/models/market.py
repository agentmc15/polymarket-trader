"""Market models."""
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Float, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.position import Position
    from app.models.trade import Order, Trade


class Market(Base, TimestampMixin):
    """Market model representing a Polymarket prediction market."""

    __tablename__ = "markets"

    id: Mapped[int] = mapped_column(primary_key=True)
    condition_id: Mapped[str] = mapped_column(String(66), unique=True, index=True)
    question_id: Mapped[str | None] = mapped_column(String(66), nullable=True)

    # Market details
    question: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Token information
    token_ids: Mapped[dict] = mapped_column(JSONB, default=dict)
    outcomes: Mapped[list] = mapped_column(JSONB, default=list)

    # Market status
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    resolution_outcome: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Timing
    end_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Volume and liquidity
    volume_24h: Mapped[float] = mapped_column(Float, default=0.0)
    total_volume: Mapped[float] = mapped_column(Float, default=0.0)
    liquidity: Mapped[float] = mapped_column(Float, default=0.0)

    # Current prices
    outcome_prices: Mapped[dict] = mapped_column(JSONB, default=dict)

    # Metadata
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    icon_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata: Mapped[dict] = mapped_column(JSONB, default=dict)

    # Relationships
    prices: Mapped[list["MarketPrice"]] = relationship(back_populates="market", cascade="all, delete-orphan")
    trades: Mapped[list["Trade"]] = relationship(back_populates="market")
    orders: Mapped[list["Order"]] = relationship(back_populates="market")
    positions: Mapped[list["Position"]] = relationship(back_populates="market")

    __table_args__ = (
        Index("ix_markets_category", "category"),
        Index("ix_markets_is_active", "is_active"),
        Index("ix_markets_end_date", "end_date"),
    )


class MarketPrice(Base):
    """Time-series price data for markets."""

    __tablename__ = "market_prices"

    id: Mapped[int] = mapped_column(primary_key=True)
    market_id: Mapped[int] = mapped_column(index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # OHLCV data for each outcome
    outcome: Mapped[str] = mapped_column(String(50), nullable=False)
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[float] = mapped_column(Float, default=0.0)

    # Relationship
    market: Mapped["Market"] = relationship(back_populates="prices")

    __table_args__ = (
        Index("ix_market_prices_market_timestamp", "market_id", "timestamp"),
        Index("ix_market_prices_timestamp", "timestamp"),
    )
