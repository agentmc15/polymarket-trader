"""Trade history model for backtesting."""
from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import DateTime, Enum, Float, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class TradeSide(str, PyEnum):
    """Trade side enum."""

    BUY = "BUY"
    SELL = "SELL"


class TradeOutcome(str, PyEnum):
    """Trade outcome enum (YES/NO token)."""

    YES = "YES"
    NO = "NO"


class TradeHistory(Base):
    """Historical trade data for backtesting and analysis.

    Stores all trades that occurred on markets for use in
    backtesting strategies and analyzing market behavior.
    """

    __tablename__ = "trade_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    market_id: Mapped[str] = mapped_column(String(66), nullable=False, index=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    # Trade details
    side: Mapped[TradeSide] = mapped_column(Enum(TradeSide), nullable=False)
    outcome: Mapped[TradeOutcome] = mapped_column(Enum(TradeOutcome), nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    size: Mapped[float] = mapped_column(Float, nullable=False)

    # Counterparty addresses
    maker_address: Mapped[str | None] = mapped_column(String(42), nullable=True, index=True)
    taker_address: Mapped[str | None] = mapped_column(String(42), nullable=True, index=True)

    # Transaction hash for on-chain reference
    tx_hash: Mapped[str | None] = mapped_column(String(66), nullable=True, unique=True)

    __table_args__ = (
        Index("ix_trade_history_market_timestamp", "market_id", "timestamp"),
        Index("ix_trade_history_timestamp_desc", timestamp.desc()),
        Index("ix_trade_history_maker_taker", "maker_address", "taker_address"),
    )
