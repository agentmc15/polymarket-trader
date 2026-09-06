"""Position model."""
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, JSONDict, TimestampMixin

if TYPE_CHECKING:
    from app.models.market import Market


class Position(Base, TimestampMixin):
    """Position model representing holdings in a market.

    `mode` (`"paper"` or `"live"`) is load-bearing: paper and live
    positions share this one table (PLAN.md D4), so every reader (P&L,
    the `/positions` API, reconciliation) MUST filter on `mode`
    explicitly — never assume this table holds only one mode's rows. See
    `app.models.trade`'s module docstring for the mass-assignment risk
    `mode`/`venue` carry (whichever task first writes to this table field
    by field from a validated schema, never a splat of caller input).
    """

    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(primary_key=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), index=True)

    #: `"polymarket"` or `"kalshi"`. No default — every position must
    #: state its venue explicitly; capital and settlement are per-venue
    #: (PLAN.md R6).
    venue: Mapped[str] = mapped_column(String(16), index=True)
    #: `"paper"` or `"live"`. No default — see class docstring.
    mode: Mapped[str] = mapped_column(String(8), index=True)
    #: The `Intent` (`app.models.intent.IntentRecord`) that opened this
    #: position, or `None` for a position predating `Intent` persistence
    #: (e.g. a bare `Signal`-derived single-leg order). Indexed for the
    #: "positions opened by this intent" lookup.
    intent_id: Mapped[str | None] = mapped_column(String(64), index=True)

    # Position details
    token_id: Mapped[str] = mapped_column(String(100), nullable=False)
    #: Outcome held, canonicalized via
    #: `app.strategies.base.normalize_outcome()` before it ever reaches
    #: this column (e.g. `"YES"`/`"NO"`, or a bundle's named outcome
    #: unchanged) — the same identity this position is keyed by
    #: downstream as `f"{venue}:{market_id}:{outcome}"`.
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

    #: If `True`, this position is held to market resolution rather than
    #: closed early (PLAN.md D8: the riskless cross-venue complement form
    #: is buy-and-hold-to-resolution). Defaults `False` (an ordinary
    #: directional position, closeable any time).
    hold_to_resolution: Mapped[bool] = mapped_column(Boolean, default=False)

    # Timing
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: When this position was settled at market resolution (as opposed to
    #: closed early by an offsetting trade), or `None` if not yet settled.
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: The resolved outcome this position settled against (e.g.
    #: `"YES"`/`"NO"`), or `None` before settlement. Set once, at
    #: settlement — not to be confused with `outcome` above, which is
    #: which side this position HOLDS, not which side WON.
    settlement_outcome: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Metadata
    extra_data: Mapped[dict] = mapped_column("extra_data", JSONDict, default=dict)

    # Relationships
    market: Mapped["Market"] = relationship(back_populates="positions")

    __table_args__ = (
        Index("ix_positions_token_id", "token_id"),
        Index("ix_positions_opened_at", "opened_at"),
        CheckConstraint(
            "venue IN ('polymarket', 'kalshi')", name="ck_positions_venue_valid"
        ),
        CheckConstraint("mode IN ('paper', 'live')", name="ck_positions_mode_valid"),
    )
