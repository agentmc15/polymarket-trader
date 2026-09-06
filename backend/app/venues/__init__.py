"""The venue-adapter seam (PLAN.md D3): normalized types, the `VenueAdapter`
Protocol, and the adapter registry.

Venues are data, strategies are venue-agnostic: strategy and execution
code imports from `app.venues` (or its submodules), never from
`app.venues.polymarket`/`app.venues.kalshi` directly.
"""
from app.venues.base import (
    BaseAdapter,
    FeeModel,
    MarketDataAdapter,
    ReconcileAdapter,
    VenueAdapter,
    VenueAuthError,
    VenueError,
    VenuePayloadError,
    VenueRateLimited,
)
from app.venues.registry import TradingMode, get_adapter, get_read_adapter
from app.venues.types import (
    DEPTH_SOURCE_KEY,
    Balance,
    BookLevel,
    DepthSource,
    FeeSchedule,
    Fill,
    Liquidity,
    MarketStatus,
    OrderAck,
    OrderBook,
    OrderRequest,
    OrderSide,
    Position,
    TimeInForce,
    VenueId,
    VenueMarket,
    WalkSide,
)

__all__ = [
    "VenueId",
    "MarketStatus",
    "OrderSide",
    "TimeInForce",
    "Liquidity",
    "WalkSide",
    "DepthSource",
    "DEPTH_SOURCE_KEY",
    "FeeSchedule",
    "BookLevel",
    "OrderBook",
    "VenueMarket",
    "OrderRequest",
    "OrderAck",
    "Fill",
    "Balance",
    "Position",
    "FeeModel",
    "MarketDataAdapter",
    "ReconcileAdapter",
    "VenueAdapter",
    "BaseAdapter",
    "VenueError",
    "VenuePayloadError",
    "VenueAuthError",
    "VenueRateLimited",
    "TradingMode",
    "get_adapter",
    "get_read_adapter",
]
