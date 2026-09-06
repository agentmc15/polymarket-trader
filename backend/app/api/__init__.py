"""API router configuration."""
from fastapi import APIRouter

from app.api.routes import arbitrage, backtesting, bots, links, markets, trading

api_router = APIRouter()

# Include all route modules
api_router.include_router(markets.router, prefix="/markets", tags=["markets"])
api_router.include_router(trading.router, prefix="/trading", tags=["trading"])
api_router.include_router(arbitrage.router, prefix="/arbitrage", tags=["arbitrage"])
api_router.include_router(backtesting.router, prefix="/backtests", tags=["backtesting"])
api_router.include_router(bots.router, prefix="/bots", tags=["bots"])
# Cross-venue event equivalence review (PLAN.md D9): the matcher proposes,
# a human approves, and only these routes may advance a link's lifecycle.
api_router.include_router(links.router, prefix="/links", tags=["links"])
