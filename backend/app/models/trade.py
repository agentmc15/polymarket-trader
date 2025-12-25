"""Trade and Order models."""
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.market import Market

import enum


class OrderSide(str, enum.Enum):
    """Order side enum."""

    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, enum.Enum):
    """Order status enum."""

    PENDING = "PENDING"
    OPEN = "OPEN"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


class OrderType(str, enum.Enum):
    """Order type enum."""

    GTC = "GTC"  # Good Till Cancelled
    GTD = "GTD"  # Good Till Date
    FOK = "FOK"  # Fill Or Kill


class Order(Base, TimestampMixin):
    """Order model representing a trading order."""

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), index=True)

    # Order details
    token_id: Mapped[str] = mapped_column(String(100), nullable=False)
    side: Mapped[OrderSide] = mapped_column(Enum(OrderSide), nullable=False)
    order_type: Mapped[OrderType] = mapped_column(Enum(OrderType), default=OrderType.GTC)
    status: Mapped[OrderStatus] = mapped_column(Enum(OrderStatus), default=OrderStatus.PENDING)

    # Pricing
    price: Mapped[float] = mapped_column(Float, nullable=False)
    size: Mapped[float] = mapped_column(Float, nullable=False)
    filled_size: Mapped[float] = mapped_column(Float, default=0.0)
    remaining_size: Mapped[float] = mapped_column(Float, nullable=False)

    # Timing
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    filled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Transaction details
    tx_hash: Mapped[str | None] = mapped_column(String(66), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Metadata
    metadata: Mapped[dict] = mapped_column(JSONB, default=dict)

    # Relationships
    market: Mapped["Market"] = relationship(back_populates="orders")
    trades: Mapped[list["Trade"]] = relationship(back_populates="order")

    __table_args__ = (
        Index("ix_orders_status", "status"),
        Index("ix_orders_created_at", "created_at"),
    )


class Trade(Base, TimestampMixin):
    """Trade model representing an executed trade."""

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    trade_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"), nullable=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), index=True)

    # Trade details
    token_id: Mapped[str] = mapped_column(String(100), nullable=False)
    side: Mapped[OrderSide] = mapped_column(Enum(OrderSide), nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    size: Mapped[float] = mapped_column(Float, nullable=False)
    fee: Mapped[float] = mapped_column(Float, default=0.0)

    # Counterparty
    maker_address: Mapped[str | None] = mapped_column(String(42), nullable=True)
    taker_address: Mapped[str | None] = mapped_column(String(42), nullable=True)

    # Transaction
    tx_hash: Mapped[str] = mapped_column(String(66), nullable=False)
    block_number: Mapped[int | None] = mapped_column(nullable=True)
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Metadata
    metadata: Mapped[dict] = mapped_column(JSONB, default=dict)

    # Relationships
    market: Mapped["Market"] = relationship(back_populates="trades")
    order: Mapped["Order"] = relationship(back_populates="trades")

    __table_args__ = (
        Index("ix_trades_executed_at", "executed_at"),
        Index("ix_trades_maker_address", "maker_address"),
        Index("ix_trades_taker_address", "taker_address"),
    )
