"""Trading-related API endpoints (PLAN.md D4/D7/D13, T16).

`POST /orders` builds a one-leg `Intent` from the validated
`OrderRequest` and routes it through `OrderRouter.submit()` — the ONLY
path from an intent to a venue order (PLAN.md D4). `DELETE /orders/{id}`
routes through `OrderRouter.cancel()`, the same singleton's only path to
a venue cancellation. Neither this module nor any other file outside
`app/execution/`/`app/venues/` calls `adapter.place_order`/
`cancel_order` directly — `tests/test_fences.py` enforces that
structurally.

MASS ASSIGNMENT (Phase 0 security audit; `app.models.intent`/
`app.models.trade` carry the full writeup, and `OrderRouter` itself
constructs every persisted row field-by-field). Nothing in this module
builds a `Leg`/`Intent` via `Model(**request.model_dump())`; every field
is read individually off the validated `OrderRequest`, and `mode` is
never accepted from the caller at all — it is `Settings.trading_mode`,
read inside `OrderRouter`/`get_router()`, never a request field.

MODE FILTERING (GUARDRAILS.md §4 / PLAN.md D4): `orders`/`positions`
share their tables between paper and live rows, so every read below
filters on `settings.trading_mode` explicitly — never assume either
table holds only one mode's rows.
"""
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AsyncSessionDep, RouterDep
from app.config import settings
from app.execution.fences import kill_switch_engaged
from app.execution.router import UnknownOrder
from app.models.market import Market as MarketRow
from app.models.position import Position as PositionRow
from app.models.trade import Order as OrderRow
from app.models.trade import OrderStatus
from app.strategies.base import Intent, Leg
from app.venues.types import VenueId

router = APIRouter()


class OrderRequest(BaseModel):
    """A one-leg order request.

    Normalized into a single-`Leg` `Intent` (`kind="single"`, PLAN.md
    D7) and routed through `OrderRouter.submit()`.

    `outcome` (not a venue-native token id) is how the side of the
    market is selected: Kalshi has NO per-outcome token concept at all
    (`app.execution.router.token_id_for` — both outcomes map to the same
    market ticker), so a token id could never disambiguate YES from NO
    on that venue. `OrderRouter` derives the venue-native token id FROM
    the canonical outcome, not the other way around.
    """

    #: T33: an unknown body key is a 422 naming the field, never a
    #: silently discarded value (see `app.api.routes.backtesting.
    #: BacktestRequest`'s docstring). Every field here except `venue` is
    #: required, so a misspelling of one of those already failed loudly
    #: on the MISSING field. `venue` is the exception and the reason
    #: this matters on an order path: it defaults to `"polymarket"`, so
    #: a body saying `"exchange": "kalshi"` (or `"venu"`) was accepted
    #: and routed to the WRONG VENUE with real money, silently. It also
    #: closes the mass-assignment shape this module's docstring
    #: describes: a body carrying `mode`, `client_order_id` or
    #: `venue_order_id` is now refused outright rather than accepted and
    #: ignored.
    model_config = ConfigDict(extra="forbid")

    market_id: str = Field(..., description="Venue-native market identifier.")
    outcome: str = Field(..., description='Outcome to trade, e.g. "YES"/"NO".')
    side: Literal["BUY", "SELL"]
    size: float = Field(gt=0, description="Order size in contracts.")
    price: float = Field(gt=0, le=1, description="Limit price, a probability in (0, 1].")
    venue: VenueId = "polymarket"


class OrderResponse(BaseModel):
    """Response for `POST /orders` — the routed intent's single leg.

    `client_order_id`/`id` are `None` when `status == "rejected"`: a
    pre-flight rejection (risk limits, insufficient capital, an
    unroutable venue) happens before any `Order` row is ever persisted,
    so there is nothing to report a client order id or row id FOR.
    """

    intent_id: str
    status: str
    client_order_id: str | None = None
    id: int | None = Field(default=None, description="The persisted `orders.id`, once written.")
    leg_status: str | None = None
    filled_size: float = 0.0
    avg_price: float | None = None
    fee: float = 0.0
    reason: str | None = Field(default=None, description="Why the intent was rejected, if it was.")


class CancelResponse(BaseModel):
    """Response for `DELETE /orders/{order_id}`."""

    id: int
    status: str
    cancelled: bool
    reason: str | None = None


class OrderOut(BaseModel):
    """One persisted order, filtered to the active trading mode."""

    id: int
    client_order_id: str
    intent_id: str | None
    venue: str
    market_id: str
    token_id: str
    outcome: str
    side: str
    order_type: str
    status: str
    price: float
    size: float
    filled_size: float
    remaining_size: float
    venue_order_id: str | None
    mode: str
    error_message: str | None


class OrderListResponse(BaseModel):
    """Response for `GET /orders`."""

    orders: list[OrderOut]


class PositionOut(BaseModel):
    """One OPEN position, filtered to the active trading mode.

    `GET /positions`/`GET /positions/{venue}/{market_id}` only ever
    return positions with `closed_at IS NULL` (see `_list_positions`),
    so `closed_at`/`settlement_outcome` would be `None` on every row
    returned here and are omitted rather than included as permanent
    dead weight.
    """

    id: int
    venue: str
    market_id: str
    outcome: str
    size: float
    avg_entry_price: float
    total_cost: float
    current_price: float
    current_value: float
    unrealized_pnl: float
    unrealized_pnl_pct: float
    realized_pnl: float
    hold_to_resolution: bool
    opened_at: datetime


class PositionListResponse(BaseModel):
    """Response for `GET /positions` and `GET /positions/{venue}/{market_id}`."""

    positions: list[PositionOut]


class TradingModeResponse(BaseModel):
    """Response for `GET /trading/mode`."""

    mode: str
    kill_switch: bool


def _order_out(row: OrderRow, condition_id: str) -> OrderOut:
    """Build an `OrderOut` field-by-field from a persisted `Order` row."""
    return OrderOut(
        id=row.id,
        client_order_id=row.client_order_id,
        intent_id=row.intent_id,
        venue=row.venue,
        market_id=condition_id,
        token_id=row.token_id,
        outcome=row.outcome,
        side=row.side.value,
        order_type=row.order_type.value,
        status=row.status.value,
        price=row.price,
        size=row.size,
        filled_size=row.filled_size,
        remaining_size=row.remaining_size,
        venue_order_id=row.order_id,
        mode=row.mode,
        error_message=row.error_message,
    )


def _position_out(row: PositionRow, condition_id: str) -> PositionOut:
    """Build a `PositionOut` field-by-field from a persisted `Position` row."""
    return PositionOut(
        id=row.id,
        venue=row.venue,
        market_id=condition_id,
        outcome=row.outcome,
        size=row.size,
        avg_entry_price=row.avg_entry_price,
        total_cost=row.total_cost,
        current_price=row.current_price,
        current_value=row.current_value,
        unrealized_pnl=row.unrealized_pnl,
        unrealized_pnl_pct=row.unrealized_pnl_pct,
        realized_pnl=row.realized_pnl,
        hold_to_resolution=row.hold_to_resolution,
        opened_at=row.opened_at,
    )


async def _list_positions(
    session: AsyncSession, *, venue: VenueId | None = None, market_id: str | None = None
) -> list[PositionOut]:
    """Read open `Position` rows for the active mode, optionally filtered.

    Args:
        session: Session to read from.
        venue: Restrict to one venue, if given.
        market_id: Restrict to one venue-native market id, if given.

    Returns:
        list[PositionOut]: Matching open positions, most recently opened
            first.
    """
    query = (
        select(PositionRow, MarketRow.condition_id)
        .join(MarketRow, MarketRow.id == PositionRow.market_id)
        .where(
            PositionRow.mode == settings.trading_mode,
            PositionRow.closed_at.is_(None),
        )
        .order_by(PositionRow.opened_at.desc())
    )
    if venue is not None:
        query = query.where(PositionRow.venue == venue)
    if market_id is not None:
        query = query.where(MarketRow.condition_id == market_id)
    rows = (await session.execute(query)).all()
    return [_position_out(position, condition_id) for position, condition_id in rows]


@router.post("/orders")
async def place_order(order: OrderRequest, order_router: RouterDep) -> OrderResponse:
    """Place a new order.

    Builds a one-leg, `best_effort` `Intent` from the validated request
    (field-by-field — see this module's docstring) and submits it
    through `OrderRouter.submit()`.

    Args:
        order: Validated order request.
        order_router: The app-scoped `OrderRouter` singleton.

    Returns:
        OrderResponse: The routed intent's outcome.
    """
    leg = Leg(
        market_id=order.market_id,
        outcome=order.outcome,
        side=order.side,
        limit_price=order.price,
        size_contracts=order.size,
        venue=order.venue,
    )
    intent = Intent(
        kind="single",
        legs=[leg],
        hold_to_resolution=False,
        atomicity="best_effort",
        confidence=1.0,
    )
    routed = await order_router.submit(intent, strategy="api")
    if routed.legs:
        leg_result = routed.legs[0]
        return OrderResponse(
            intent_id=routed.intent_id,
            status=routed.status,
            client_order_id=leg_result.client_order_id,
            id=leg_result.order_row_id,
            leg_status=leg_result.status,
            filled_size=leg_result.filled_size,
            avg_price=leg_result.avg_price,
            fee=leg_result.fee,
            reason=routed.reason,
        )
    return OrderResponse(
        intent_id=routed.intent_id,
        status=routed.status,
        reason=routed.reason,
    )


@router.delete("/orders/{order_id}")
async def cancel_order(order_id: int, order_router: RouterDep) -> CancelResponse:
    """Cancel an existing order.

    Routes through `OrderRouter.cancel()` — see that method's docstring
    for what happens when the order has already filled, expired, or is
    still `PENDING` with no venue acknowledgement yet (both are reported
    as a no-op, never as a false "cancelled").

    Args:
        order_id: Primary key of the persisted `orders` row to cancel.
        order_router: The app-scoped `OrderRouter` singleton.

    Returns:
        CancelResponse: What happened.

    Raises:
        HTTPException: 404 if no order with `order_id` exists in the
            active trading mode.
    """
    try:
        result = await order_router.cancel(order_id)
    except UnknownOrder as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return CancelResponse(
        id=result.order_id,
        status=result.status.value,
        cancelled=result.cancelled,
        reason=result.reason,
    )


@router.get("/orders")
async def list_orders(
    session: AsyncSessionDep,
    status: str | None = Query(None, description="Filter by order status, e.g. OPEN/FILLED."),
) -> OrderListResponse:
    """List orders in the active trading mode.

    Args:
        session: Database session.
        status: Optional `OrderStatus` name to filter by (case-insensitive).

    Returns:
        OrderListResponse: Matching orders, most recently created first.

    Raises:
        HTTPException: 422 if `status` is not a recognized `OrderStatus`.
    """
    query = (
        select(OrderRow, MarketRow.condition_id)
        .join(MarketRow, MarketRow.id == OrderRow.market_id)
        .where(OrderRow.mode == settings.trading_mode)
        .order_by(OrderRow.created_at.desc())
    )
    if status is not None:
        try:
            status_enum = OrderStatus(status.upper())
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail=f"unknown order status {status!r}"
            ) from exc
        query = query.where(OrderRow.status == status_enum)
    rows = (await session.execute(query)).all()
    return OrderListResponse(orders=[_order_out(order, condition_id) for order, condition_id in rows])


@router.get("/positions")
async def list_positions(session: AsyncSessionDep) -> PositionListResponse:
    """List all open positions in the active trading mode.

    Args:
        session: Database session.

    Returns:
        PositionListResponse: Open positions, most recently opened first.
    """
    return PositionListResponse(positions=await _list_positions(session))


@router.get("/positions/{venue}/{market_id}")
async def get_position(
    venue: VenueId, market_id: str, session: AsyncSessionDep
) -> PositionListResponse:
    """Get the open position(s) for one venue-native market.

    Returns a LIST rather than a single object: a multi-outcome bundle
    market (PLAN.md D7) can have more than one open `Position` row for
    the same `(venue, market_id)` — one per outcome held.

    Args:
        venue: `"polymarket"` or `"kalshi"`.
        market_id: Venue-native market identifier.
        session: Database session.

    Returns:
        PositionListResponse: Open positions for this market, if any.
    """
    positions = await _list_positions(session, venue=venue, market_id=market_id)
    return PositionListResponse(positions=positions)


@router.get("/mode")
async def get_trading_mode() -> TradingModeResponse:
    """Report the active trading mode and whether the kill switch is engaged.

    `kill_switch` reports whether the kill-switch FILE currently exists
    (`Settings.kill_switch_path`) — independent of `mode` — so an
    operator can see the switch's own state rather than inferring it
    from whether live trading happens to be allowed right now.

    It is read through `app.execution.fences.kill_switch_engaged()`, the
    same function `assert_placement_allowed()` uses, so this endpoint and
    the halt it reports can never disagree. `true` here now means order
    placement really is halted, in paper and in live alike: before T14's
    remediation this field could report `true` while a cached
    `OrderRouter` went on placing orders, because the only check ran once
    per process inside a live adapter's constructor.

    Returns:
        TradingModeResponse: `{"mode": ..., "kill_switch": ...}`.
    """
    return TradingModeResponse(
        mode=settings.trading_mode,
        kill_switch=kill_switch_engaged(settings),
    )
