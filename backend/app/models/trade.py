"""Trade and Order models.

`venue`/`mode` mass-assignment warning (Phase 0 security audit; see also
`app.models.intent`'s module docstring, which carries the full writeup):
`Order.mode`/`Trade.mode`/`Position.mode` are the ONLY thing that
distinguishes a PAPER row from a LIVE (real-money) row in these shared
tables (PLAN.md D4, GUARDRAILS.md §1.2/§4) — a caller that can set `mode`
from untrusted input can mislabel a real fill as simulated or vice versa.
Whichever task first builds a DB-writing path (T14's `OrderRouter`, T16's
API routes) MUST construct `Order`/`Trade`/`Position` field-by-field from
a validated pydantic schema, never `Order(**request.model_dump())` or any
other splat of caller-controlled data — SQLAlchemy's stock
`_declarative_constructor` (see `app.models.base.Base`) accepts any kwarg
naming a mapped column, `mode` and `venue` included, and does nothing to
validate its value beyond the CHECK constraints added below (which run at
flush/commit, not at Python-object construction time — a defense-in-depth
backstop, not a substitute for validating before you ever call
`Order(...)`).
"""
import enum
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, JSONDict, TimestampMixin

if TYPE_CHECKING:
    from app.models.market import Market


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


#: Allowed values for every `venue` column added in this module (T15,
#: PLAN.md D3). Mirrors `app.venues.types.VenueId` exactly — NOT imported
#: from there to avoid a runtime dependency from `app.models` onto
#: `app.venues` (none exists today; keep it that way), but the two lists
#: must be kept in sync by hand. Enforced by a `CheckConstraint` on every
#: table below as defense in depth alongside pydantic-layer validation
#: (see the mass-assignment warning in this module's docstring) — adding
#: a third venue means updating BOTH this tuple's callers (a new
#: migration altering each CHECK constraint) and `VenueId`.
_VENUE_VALUES = ("polymarket", "kalshi")

#: Allowed values for every `mode` column added in this module (T15,
#: PLAN.md D4/D13). Mirrors `app.venues.registry.TradingMode` exactly,
#: same not-imported rationale as `_VENUE_VALUES` above. `mode` is the
#: ONLY column separating a paper (simulated) row from a live (real
#: money) row in these shared tables — GUARDRAILS.md: "a query that
#: forgets to filter on it mixes simulated and real fills in one P&L" —
#: so it is indexed on every table that carries it, and every read path
#: (reporting, P&L, position listing) MUST filter on `mode` explicitly;
#: never assume a table only ever holds one mode's rows.
_MODE_VALUES = ("paper", "live")


class Order(Base, TimestampMixin):
    """Order model representing a trading order.

    `mode` (`"paper"` or `"live"`) is load-bearing: every reader of this
    table (P&L, position reconciliation, the `/orders` API) MUST filter
    on `mode` explicitly, since paper and live orders share this one
    table (PLAN.md D4). See this module's docstring for the
    mass-assignment risk `mode` carries.
    """

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: Venue-assigned order id. Nullable: a `PENDING` order is persisted
    #: BEFORE the venue call is made (T14 crash-safety — a row with no
    #: ack can be reconciled later), so `order_id` is only known once the
    #: venue has acknowledged it. Still unique (a real venue order id
    #: never repeats), and SQL's standard "NULLs are not equal to each
    #: other" semantics mean any number of not-yet-acked orders can
    #: coexist with `order_id IS NULL`.
    order_id: Mapped[str | None] = mapped_column(
        String(100), unique=True, index=True, nullable=True
    )
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), index=True)

    #: `"polymarket"` or `"kalshi"` (see `_VENUE_VALUES`). No default —
    #: every order must state its venue explicitly; capital is per-venue
    #: (PLAN.md R6) and inferring this would risk sizing/settling against
    #: the wrong venue's ledger.
    venue: Mapped[str] = mapped_column(String(16), index=True)
    #: Idempotency key the venue sees (PLAN.md D4): `OrderRouter` (T14)
    #: mints this as `f"{intent_id}:{leg_index}:{attempt}"`. UNIQUE, so a
    #: retried submission with the SAME `client_order_id` raises
    #: `sqlalchemy.exc.IntegrityError` on insert — this is a LEGITIMATE,
    #: expected outcome (idempotent retry), not a crash: T14 must catch
    #: `IntegrityError` on this insert and look up the existing `Order`
    #: row rather than let it propagate. Length arithmetic: `String(64)`
    #: assumes `intent_id` is a UUID4 string (`str(uuid.uuid4())`, 36
    #: chars) — see `app.models.intent.IntentRecord.id` for the full
    #: budget (36 + 2 separators + leg_index + attempt digits, comfortably
    #: under 64). A future `intent_id` scheme that runs longer must either
    #: shorten itself or this column (and `intent_id` below) must be
    #: widened in a follow-up migration; it must NOT silently truncate.
    client_order_id: Mapped[str] = mapped_column(String(64), unique=True)
    #: The `Intent` (see `app.models.intent.IntentRecord`) this order's
    #: leg belongs to. Nullable because a `Signal`-derived single-leg
    #: order predates `Intent` persistence in some callers; index for the
    #: "all orders for this intent" lookup `OrderRouter`/reconciliation need.
    intent_id: Mapped[str | None] = mapped_column(String(64), index=True)

    # Order details
    token_id: Mapped[str] = mapped_column(String(100), nullable=False)
    #: Outcome being traded, canonicalized via
    #: `app.strategies.base.normalize_outcome()` before it ever reaches
    #: this column (e.g. `"YES"`/`"NO"`, or a bundle's named outcome
    #: unchanged) — see that function's docstring for why an uncanonicalized
    #: casing would silently desync a position's identity.
    outcome: Mapped[str] = mapped_column(String(50))
    side: Mapped[OrderSide] = mapped_column(Enum(OrderSide), nullable=False)
    order_type: Mapped[OrderType] = mapped_column(Enum(OrderType), default=OrderType.GTC)
    status: Mapped[OrderStatus] = mapped_column(Enum(OrderStatus), default=OrderStatus.PENDING)
    #: `"paper"` or `"live"` (see `_MODE_VALUES` and this module's
    #: docstring). No default — every order must state which world it
    #: belongs to explicitly.
    mode: Mapped[str] = mapped_column(String(8), index=True)

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
    extra_data: Mapped[dict] = mapped_column("extra_data", JSONDict, default=dict)

    # Relationships
    market: Mapped["Market"] = relationship(back_populates="orders")
    trades: Mapped[list["Trade"]] = relationship(back_populates="order")

    __table_args__ = (
        Index("ix_orders_status", "status"),
        Index("ix_orders_created_at", "created_at"),
        CheckConstraint(
            "venue IN ('polymarket', 'kalshi')", name="ck_orders_venue_valid"
        ),
        CheckConstraint("mode IN ('paper', 'live')", name="ck_orders_mode_valid"),
    )


class Trade(Base, TimestampMixin):
    """Trade model representing an executed trade.

    `mode` (`"paper"` or `"live"`) is load-bearing: every reader of this
    table (P&L, position reconciliation) MUST filter on `mode`
    explicitly, since paper and live fills share this one table (PLAN.md
    D4). See `app.models.trade`'s module docstring for the
    mass-assignment risk `mode` carries.
    """

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    trade_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"), nullable=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), index=True)

    #: `"polymarket"` or `"kalshi"` (see `_VENUE_VALUES`). No default —
    #: same rationale as `Order.venue`.
    venue: Mapped[str] = mapped_column(String(16), index=True)

    # Trade details
    token_id: Mapped[str] = mapped_column(String(100), nullable=False)
    #: Outcome traded, canonicalized via
    #: `app.strategies.base.normalize_outcome()` — see `Order.outcome`.
    outcome: Mapped[str] = mapped_column(String(50))
    side: Mapped[OrderSide] = mapped_column(Enum(OrderSide), nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    size: Mapped[float] = mapped_column(Float, nullable=False)
    fee: Mapped[float] = mapped_column(Float, default=0.0)
    #: `"maker"` or `"taker"` (`app.venues.types.Liquidity`), or `None`
    #: when the venue payload does not report it. Fee sign/amount depends
    #: on this (PLAN.md: Polymarket makers never pay; Kalshi maker rate
    #: may differ from taker) — second-verifier lens: never assume taker.
    liquidity: Mapped[str | None] = mapped_column(String(8), nullable=True)
    #: `"paper"` or `"live"` (see `_MODE_VALUES`). No default — same
    #: rationale as `Order.mode`.
    mode: Mapped[str] = mapped_column(String(8), index=True)

    # Counterparty
    maker_address: Mapped[str | None] = mapped_column(String(42), nullable=True)
    taker_address: Mapped[str | None] = mapped_column(String(42), nullable=True)

    # Transaction
    #: On-chain transaction hash. Nullable: Kalshi trades have no
    #: on-chain settlement, so a Kalshi `Trade` row never has one
    #: (Polymarket trades still do).
    tx_hash: Mapped[str | None] = mapped_column(String(66), nullable=True)
    block_number: Mapped[int | None] = mapped_column(nullable=True)
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Metadata
    extra_data: Mapped[dict] = mapped_column("extra_data", JSONDict, default=dict)

    # Relationships
    market: Mapped["Market"] = relationship(back_populates="trades")
    order: Mapped["Order"] = relationship(back_populates="trades")

    __table_args__ = (
        Index("ix_trades_executed_at", "executed_at"),
        Index("ix_trades_maker_address", "maker_address"),
        Index("ix_trades_taker_address", "taker_address"),
        CheckConstraint(
            "venue IN ('polymarket', 'kalshi')", name="ck_trades_venue_valid"
        ),
        CheckConstraint("mode IN ('paper', 'live')", name="ck_trades_mode_valid"),
        CheckConstraint(
            "liquidity IN ('maker', 'taker') OR liquidity IS NULL",
            name="ck_trades_liquidity_valid",
        ),
    )
