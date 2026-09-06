"""SQLAlchemy models."""
from app.models.backtest import Backtest, BacktestTrade
from app.models.backtest_run import BacktestRun, BacktestRunStatus
from app.models.base import Base
from app.models.book_snapshot import BookSnapshot, BookSnapshotDepthSource
from app.models.event_link import EventLink, EventLinkStatus
from app.models.intent import IntentRecord
from app.models.market import Market, MarketPrice
from app.models.position import Position
from app.models.price_history import PriceHistory
from app.models.strategy import Strategy
from app.models.trade import Order, Trade
from app.models.trade_history import TradeHistory, TradeOutcome, TradeSide

__all__ = [
    "Base",
    "Market",
    "MarketPrice",
    "Trade",
    "Order",
    "Position",
    "Strategy",
    "Backtest",
    "BacktestTrade",
    "IntentRecord",
    # Cross-venue event equivalence (PLAN.md D9)
    "EventLink",
    "EventLinkStatus",
    # Recorded order-book depth (PLAN.md D6/D10, T21)
    "BookSnapshot",
    "BookSnapshotDepthSource",
    # Backtesting models
    "PriceHistory",
    "TradeHistory",
    "TradeSide",
    "TradeOutcome",
    "BacktestRun",
    "BacktestRunStatus",
]
