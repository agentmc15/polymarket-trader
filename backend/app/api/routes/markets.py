"""Market-related API endpoints."""
from fastapi import APIRouter, Query

from app.api.deps import AsyncSessionDep

router = APIRouter()


@router.get("")
async def list_markets(
    session: AsyncSessionDep,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    active: bool = Query(True),
) -> dict:
    """List all markets.

    Args:
        session: Database session.
        skip: Number of records to skip.
        limit: Maximum number of records to return.
        active: Filter for active markets only.

    Returns:
        dict: List of markets with pagination info.
    """
    # TODO: Implement market listing. T41: the envelope key below is
    # `data`, not `markets`, and that is deliberate, not a leftover —
    # see the reasoning below before changing it back.
    #
    # `frontend/src/types/index.ts::PaginatedResponse<T>` (`{data,
    # total, skip, limit}`) is the type `api.getMarkets()` declares and
    # `useMarkets` reads (`response.data || []`), and Markets is the
    # default, live tab in `App.tsx`. Every OTHER list envelope in this
    # backend uses a resource-named key instead (`BacktestListResponse`
    # -> `backtests`, `OpportunitiesResponse` -> `opportunities`,
    # `LinkListResponse` -> `links`, `OrderListResponse` -> `orders`,
    # `PositionListResponse` -> `positions`) — but every one of those
    # pairs with a BESPOKE frontend interface of the same name, mirrored
    # field-by-field (see the header comment in
    # `frontend/src/services/backtestApi.ts`, which spells out exactly
    # this class of drift: a friendlier rename between backend and
    # frontend field names is what left the Results tab permanently
    # inactive before it was fixed). `PaginatedResponse<T>` is different:
    # it is the one GENERIC envelope type in the frontend, and this
    # endpoint is its only real caller — nothing else in the frontend
    # uses it. That is strong evidence the generic `data` shape is the
    # deliberately-chosen contract for markets specifically, not an
    # accident to be overridden.
    #
    # Matching `data` here fixes the mismatch entirely within this
    # backend file: once real listing logic replaces the `[]` below, the
    # already-wired frontend renders it with no further change. Picking
    # `markets` instead would just move today's silent-empty risk to
    # tomorrow, since it would need a corresponding frontend edit (out
    # of scope for this task — see `backend/app/tasks/*` and this file
    # only) to avoid recreating the exact bug this fix exists to
    # prevent. `test_markets_route.py::test_list_markets_envelope_shape`
    # pins this shape.
    return {"data": [], "total": 0, "skip": skip, "limit": limit}


@router.get("/{condition_id}")
async def get_market(
    condition_id: str,
    session: AsyncSessionDep,
) -> dict:
    """Get market details.

    Args:
        condition_id: Market condition ID.
        session: Database session.

    Returns:
        dict: Market details.
    """
    # TODO: Implement market details
    return {"condition_id": condition_id, "data": None}


@router.get("/{condition_id}/orderbook")
async def get_orderbook(
    condition_id: str,
    session: AsyncSessionDep,
) -> dict:
    """Get market orderbook.

    Args:
        condition_id: Market condition ID.
        session: Database session.

    Returns:
        dict: Orderbook data with bids and asks.
    """
    # TODO: Implement orderbook
    return {"condition_id": condition_id, "bids": [], "asks": []}


@router.get("/{condition_id}/history")
async def get_price_history(
    condition_id: str,
    session: AsyncSessionDep,
    interval: str = Query("1h", pattern="^(1m|5m|15m|1h|4h|1d)$"),
) -> dict:
    """Get market price history.

    Args:
        condition_id: Market condition ID.
        session: Database session.
        interval: Time interval for candles.

    Returns:
        dict: Price history data.
    """
    # TODO: Implement price history
    return {"condition_id": condition_id, "interval": interval, "candles": []}
