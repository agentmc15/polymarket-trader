"""Tracked trader model for whale copy-trading."""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class TrackedTrader(Base):
    """Model for tracking whale traders for copy-trading.

    Stores information about traders being monitored for
    copy-trading strategies, including their performance metrics.
    """

    __tablename__ = "tracked_traders"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Trader identification
    address: Mapped[str] = mapped_column(String(42), unique=True, nullable=False, index=True)
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # Performance metrics
    total_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    win_rate: Mapped[float] = mapped_column(Float, default=0.0)
    total_trades: Mapped[int] = mapped_column(default=0)

    # Copy trading settings
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    copy_multiplier: Mapped[float] = mapped_column(Float, default=1.0)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index("ix_tracked_traders_pnl", "total_pnl"),
        Index("ix_tracked_traders_win_rate", "win_rate"),
        Index("ix_tracked_traders_active_pnl", "is_active", "total_pnl"),
    )
