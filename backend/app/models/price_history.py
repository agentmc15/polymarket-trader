"""Price history model for backtesting (TimescaleDB hypertable)."""
from datetime import datetime

from sqlalchemy import DateTime, Float, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class PriceHistory(Base):
    """Time-series price history for backtesting.

    This table is designed to be a TimescaleDB hypertable for efficient
    time-series queries. The hypertable conversion should be done in
    the migration after table creation.
    """

    __tablename__ = "price_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    market_id: Mapped[str] = mapped_column(String(66), nullable=False, index=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    # Current prices
    yes_price: Mapped[float] = mapped_column(Float, nullable=False)
    no_price: Mapped[float] = mapped_column(Float, nullable=False)

    # Order book data
    yes_bid: Mapped[float | None] = mapped_column(Float, nullable=True)
    yes_ask: Mapped[float | None] = mapped_column(Float, nullable=True)
    no_bid: Mapped[float | None] = mapped_column(Float, nullable=True)
    no_ask: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Spread (yes_ask - yes_bid)
    spread: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Volume metrics
    volume: Mapped[float] = mapped_column(Float, default=0.0)
    volume_24h: Mapped[float] = mapped_column(Float, default=0.0)

    # Open interest
    open_interest: Mapped[float | None] = mapped_column(Float, nullable=True)

    __table_args__ = (
        Index("ix_price_history_market_timestamp", "market_id", "timestamp"),
        Index("ix_price_history_timestamp_desc", timestamp.desc()),
        # Unique constraint to prevent duplicate entries
        Index(
            "uq_price_history_market_timestamp",
            "market_id",
            "timestamp",
            unique=True,
        ),
    )
