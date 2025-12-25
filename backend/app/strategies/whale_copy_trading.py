"""Whale copy trading strategy.

Monitors and copies trades from successful whale wallets
with proven track records.
"""
from datetime import datetime, timedelta
from typing import Any

from app.strategies.base import BaseStrategy, MarketSnapshot, Signal, SignalType


DEFAULT_CONFIG: dict[str, Any] = {
    # Minimum wallet win rate to copy
    "min_win_rate": 0.55,
    # Minimum total PnL to consider wallet
    "min_total_pnl": 10000.0,
    # Minimum number of trades for confidence
    "min_trades": 20,
    # Maximum time delay to copy trade (seconds)
    "max_copy_delay": 300,
    # Copy multiplier (fraction of whale size)
    "copy_multiplier": 0.10,
    # Maximum position as fraction of portfolio
    "max_position_pct": 0.15,
    # Minimum position size
    "min_position_size": 20.0,
    # Maximum position size
    "max_position_size": 500.0,
    # Maximum slippage tolerance
    "max_slippage": 0.03,
    # Whale categories to prioritize
    "preferred_whale_types": [
        "consistent_winner",
        "high_volume",
        "insider_signal",
    ],
    # Maximum concurrent copy positions
    "max_copy_positions": 8,
    # Minimum confidence in whale signal
    "min_whale_confidence": 0.6,
}


class WhaleCopyTradingStrategy(BaseStrategy):
    """Strategy that copies trades from successful wallets.

    Monitors whale wallets with proven track records and
    copies their trades with configurable delay and sizing.

    Whale selection criteria:
    - Historical win rate
    - Total PnL
    - Trade frequency
    - Consistency across market types

    Risk: Whales may have different risk tolerance and capital.
    """

    name = "whale_copy_trading"
    description = "Copy trades from successful whale wallets"
    version = "1.0.0"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """Initialize with merged config."""
        merged_config = {**DEFAULT_CONFIG, **(config or {})}
        super().__init__(merged_config)
        # Tracked whales: {address: whale_stats}
        self._tracked_whales: dict[str, dict] = {}
        # Recent whale trades: {market_id: [(whale_address, side, price, timestamp)]}
        self._whale_trades: dict[str, list] = {}
        self._copied_trades = 0
        self._active_copies: set[str] = set()

    def on_market_data(self, snapshot: MarketSnapshot) -> Signal | None:
        """Check for whale trades to copy.

        The snapshot.metadata should contain whale_trades list
        from the data feed.

        Args:
            snapshot: Current market state with whale trade data.

        Returns:
            Signal if whale trade should be copied, None otherwise.
        """
        # Check position limits
        if len(self._active_copies) >= self.config["max_copy_positions"]:
            return None

        # Get whale trades from metadata
        whale_trades = snapshot.metadata.get("whale_trades", [])

        if not whale_trades:
            # Check internal cache for recent whale activity
            whale_trades = self._whale_trades.get(snapshot.market_id, [])

        if not whale_trades:
            return None

        # Find best whale trade to copy
        best_signal = None
        best_confidence = 0

        for trade in whale_trades:
            whale_address = trade.get("address", "")
            trade_side = trade.get("side", "")
            trade_price = trade.get("price", 0)
            trade_time = trade.get("timestamp", snapshot.timestamp)
            trade_size = trade.get("size", 0)

            # Check if whale is in our tracked list
            whale_stats = self._tracked_whales.get(whale_address)

            if not whale_stats:
                whale_stats = self._evaluate_whale(trade)
                if whale_stats:
                    self._tracked_whales[whale_address] = whale_stats
                else:
                    continue

            # Check whale qualifications
            if not self._whale_qualifies(whale_stats):
                continue

            # Check trade timing
            if isinstance(trade_time, str):
                trade_time = datetime.fromisoformat(trade_time.replace("Z", "+00:00"))

            delay = (snapshot.timestamp - trade_time).total_seconds()
            if delay > self.config["max_copy_delay"]:
                continue

            # Check slippage
            current_price = snapshot.yes_price if trade_side == "BUY" else snapshot.no_price
            slippage = abs(current_price - trade_price) / trade_price
            if slippage > self.config["max_slippage"]:
                continue

            # Calculate confidence
            confidence = self._calculate_whale_confidence(whale_stats, trade, snapshot)

            if confidence < self.config["min_whale_confidence"]:
                continue

            if confidence > best_confidence:
                best_confidence = confidence

                signal_type = SignalType.BUY if trade_side == "BUY" else SignalType.SELL
                outcome = "YES" if trade_side == "BUY" else "NO"

                best_signal = Signal(
                    type=signal_type,
                    market_id=snapshot.market_id,
                    token_id=snapshot.token_id,
                    outcome=outcome,
                    price=current_price,
                    size=trade_size * self.config["copy_multiplier"],
                    confidence=confidence,
                    timestamp=snapshot.timestamp,
                    metadata={
                        "strategy": self.name,
                        "is_copy_trade": True,
                        "whale_address": whale_address,
                        "whale_win_rate": whale_stats.get("win_rate", 0),
                        "whale_pnl": whale_stats.get("total_pnl", 0),
                        "original_price": trade_price,
                        "current_price": current_price,
                        "slippage": slippage,
                        "delay_seconds": delay,
                    },
                )

        if best_signal:
            self._copied_trades += 1

        return best_signal

    def _evaluate_whale(self, trade: dict) -> dict | None:
        """Evaluate a whale from trade data.

        In production, this would query on-chain data or a database
        of tracked whale performance.
        """
        # Check if trade metadata includes whale stats
        whale_stats = trade.get("whale_stats")
        if whale_stats:
            return whale_stats

        # Minimal evaluation from trade data
        address = trade.get("address", "")
        if not address:
            return None

        # Would typically fetch from database/API
        # Return None to skip unknown whales
        return None

    def _whale_qualifies(self, stats: dict) -> bool:
        """Check if whale meets minimum qualifications."""
        win_rate = stats.get("win_rate", 0)
        total_pnl = stats.get("total_pnl", 0)
        total_trades = stats.get("total_trades", 0)

        if win_rate < self.config["min_win_rate"]:
            return False
        if total_pnl < self.config["min_total_pnl"]:
            return False
        if total_trades < self.config["min_trades"]:
            return False

        return True

    def _calculate_whale_confidence(
        self,
        whale_stats: dict,
        trade: dict,
        snapshot: MarketSnapshot,
    ) -> float:
        """Calculate confidence in following this whale trade."""
        # Win rate contribution
        win_rate = whale_stats.get("win_rate", 0.5)
        win_rate_confidence = (win_rate - 0.5) * 2  # Scale 0.5-1.0 to 0-1

        # PnL contribution
        total_pnl = whale_stats.get("total_pnl", 0)
        pnl_confidence = min(total_pnl / 100000, 1.0)  # Scale to 100k

        # Recency contribution (more recent stats = more reliable)
        days_active = whale_stats.get("days_since_last_trade", 30)
        recency_confidence = max(0, 1 - days_active / 60)

        # Trade size contribution (larger = more conviction)
        trade_size = trade.get("size", 0)
        size_confidence = min(trade_size / 5000, 1.0)

        # Volume confirmation
        volume_confidence = 0.5
        if snapshot.volume_24h > 20000:
            volume_confidence = 0.7

        confidence = (
            win_rate_confidence * 0.35 +
            pnl_confidence * 0.25 +
            recency_confidence * 0.15 +
            size_confidence * 0.15 +
            volume_confidence * 0.10
        )

        return min(max(confidence, 0), 0.95)

    def calculate_position_size(
        self,
        signal: Signal,
        portfolio_value: float,
        positions: dict[str, Any],
    ) -> float:
        """Calculate position size for copy trade."""
        # Start with whale's scaled size
        base_size = signal.size

        # Apply portfolio limits
        max_by_pct = portfolio_value * self.config["max_position_pct"]
        position_size = min(base_size, max_by_pct, self.config["max_position_size"])
        position_size = max(position_size, self.config["min_position_size"])

        # Scale by confidence
        position_size *= signal.confidence

        return position_size

    def add_tracked_whale(
        self,
        address: str,
        win_rate: float,
        total_pnl: float,
        total_trades: int,
        **extra_stats: Any,
    ) -> None:
        """Add or update a tracked whale."""
        self._tracked_whales[address] = {
            "address": address,
            "win_rate": win_rate,
            "total_pnl": total_pnl,
            "total_trades": total_trades,
            "last_updated": datetime.utcnow(),
            **extra_stats,
        }

    def remove_tracked_whale(self, address: str) -> None:
        """Remove a whale from tracking."""
        self._tracked_whales.pop(address, None)

    def on_trade_executed(self, trade: dict[str, Any]) -> None:
        """Track active copy positions."""
        super().on_trade_executed(trade)
        if trade.get("metadata", {}).get("is_copy_trade"):
            self._active_copies.add(trade.get("market_id", ""))

    def on_position_closed(self, position: dict[str, Any], pnl: float) -> None:
        """Clean up closed copy positions."""
        super().on_position_closed(position, pnl)
        self._active_copies.discard(position.get("market_id", ""))

    def reset(self) -> None:
        """Reset strategy state."""
        super().reset()
        self._whale_trades.clear()
        self._copied_trades = 0
        self._active_copies.clear()
        # Keep tracked whales - they persist across sessions

    def get_stats(self) -> dict[str, Any]:
        """Get strategy statistics."""
        stats = super().get_stats()
        stats.update({
            "copied_trades": self._copied_trades,
            "tracked_whales": len(self._tracked_whales),
            "active_copies": len(self._active_copies),
            "whale_addresses": list(self._tracked_whales.keys())[:5],
        })
        return stats
