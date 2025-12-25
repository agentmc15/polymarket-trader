"""Trading-related API endpoints."""
from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.api.deps import AsyncSessionDep

router = APIRouter()


class OrderRequest(BaseModel):
    """Order request schema."""

    condition_id: str
    token_id: str
    side: Literal["BUY", "SELL"]
    size: float = Field(gt=0)
    price: float = Field(gt=0, le=1)
    order_type: Literal["GTC", "GTD", "FOK"] = "GTC"


class OrderResponse(BaseModel):
    """Order response schema."""

    order_id: str
    status: str
    message: str


@router.post("/orders")
async def place_order(
    order: OrderRequest,
    session: AsyncSessionDep,
) -> OrderResponse:
    """Place a new order.

    Args:
        order: Order details.
        session: Database session.

    Returns:
        OrderResponse: Order confirmation.
    """
    # TODO: Implement order placement
    return OrderResponse(
        order_id="placeholder",
        status="pending",
        message="Order placement not yet implemented",
    )


@router.delete("/orders/{order_id}")
async def cancel_order(
    order_id: str,
    session: AsyncSessionDep,
) -> dict:
    """Cancel an existing order.

    Args:
        order_id: Order ID to cancel.
        session: Database session.

    Returns:
        dict: Cancellation confirmation.
    """
    # TODO: Implement order cancellation
    return {"order_id": order_id, "status": "cancelled"}


@router.get("/orders")
async def list_orders(
    session: AsyncSessionDep,
    status: str | None = Query(None),
) -> dict:
    """List all orders.

    Args:
        session: Database session.
        status: Filter by order status.

    Returns:
        dict: List of orders.
    """
    # TODO: Implement order listing
    return {"orders": []}


@router.get("/positions")
async def list_positions(
    session: AsyncSessionDep,
) -> dict:
    """List all open positions.

    Args:
        session: Database session.

    Returns:
        dict: List of positions.
    """
    # TODO: Implement position listing
    return {"positions": []}


@router.get("/positions/{condition_id}")
async def get_position(
    condition_id: str,
    session: AsyncSessionDep,
) -> dict:
    """Get position for a specific market.

    Args:
        condition_id: Market condition ID.
        session: Database session.

    Returns:
        dict: Position details.
    """
    # TODO: Implement position details
    return {"condition_id": condition_id, "position": None}
