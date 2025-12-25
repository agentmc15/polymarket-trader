"""API router configuration."""
from fastapi import APIRouter

from app.api.routes import arbitrage, backtesting, bots, markets, traders, trading

api_router = APIRouter()

# Include all route modules
api_router.include_router(markets.router, prefix="/markets", tags=["markets"])
api_router.include_router(trading.router, prefix="/trading", tags=["trading"])
api_router.include_router(arbitrage.router, prefix="/arbitrage", tags=["arbitrage"])
api_router.include_router(traders.router, prefix="/traders", tags=["traders"])
api_router.include_router(backtesting.router, prefix="/backtests", tags=["backtesting"])
api_router.include_router(bots.router, prefix="/bots", tags=["bots"])
