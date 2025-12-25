"""Position model."""
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Float, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.market import Market


class Position(Base, TimestampMixin):
    """Position model representing holdings in a market."""

    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(primary_key=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), index=True)

    # Position details
    token_id: Mapped[str] = mapped_column(String(100), nullable=False)
    outcome: Mapped[str] = mapped_column(String(50), nullable=False)
    size: Mapped[float] = mapped_column(Float, nullable=False)

    # Cost basis
    avg_entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    total_cost: Mapped[float] = mapped_column(Float, nullable=False)

    # Current value
    current_price: Mapped[float] = mapped_column(Float, default=0.0)
    current_value: Mapped[float] = mapped_column(Float, default=0.0)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    unrealized_pnl_pct: Mapped[float] = mapped_column(Float, default=0.0)

    # Realized P&L
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)

    # Timing
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Metadata
    metadata: Mapped[dict] = mapped_column(JSONB, default=dict)

    # Relationships
    market: Mapped["Market"] = relationship(back_populates="positions")

    __table_args__ = (
        Index("ix_positions_token_id", "token_id"),
        Index("ix_positions_opened_at", "opened_at"),
    )
