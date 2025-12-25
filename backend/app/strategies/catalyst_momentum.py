"""Catalyst momentum trading strategy.

Trades based on news events and information catalysts that
should move market prices.
"""
from datetime import datetime, timedelta
from typing import Any

from app.strategies.base import BaseStrategy, MarketSnapshot, Signal, SignalType


DEFAULT_CONFIG: dict[str, Any] = {
    # Minimum price change to trigger momentum signal
    "min_price_change": 0.05,
    # Time window to measure momentum (minutes)
    "momentum_window_minutes": 30,
    # Minimum volume surge multiplier
    "min_volume_surge": 2.0,
    # Maximum position as fraction of portfolio
    "max_position_pct": 0.15,
    # Minimum position size
    "min_position_size": 25.0,
    # Maximum position size
    "max_position_size": 500.0,
    # Stop loss percentage
    "stop_loss_pct": 0.10,
    # Take profit percentage
    "take_profit_pct": 0.20,
    # Avoid trading near resolution
    "min_hours_to_resolution": 24,
    # Minimum confidence to trade
    "min_confidence": 0.6,
    # Decay factor for older price data
    "price_decay_factor": 0.9,
}


class CatalystMomentumStrategy(BaseStrategy):
    """Momentum strategy triggered by news catalysts.

    Identifies sudden price movements accompanied by volume surges,
    indicating new information entering the market. Trades in the
    direction of momentum with stop-loss protection.

    Key indicators:
    - Rapid price change within time window
    - Volume surge above normal levels
    - Spread tightening (increased certainty)
    """

    name = "catalyst_momentum"
    description = "Trade news-driven momentum"
    version = "1.0.0"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """Initialize with merged config."""
        merged_config = {**DEFAULT_CONFIG, **(config or {})}
        super().__init__(merged_config)
        # Price history per market: {market_id: [(timestamp, price, volume)]}
        self._price_history: dict[str, list[tuple[datetime, float, float]]] = {}
        self._signals_generated = 0

    def on_market_data(self, snapshot: MarketSnapshot) -> Signal | None:
        """Detect momentum catalysts and generate signals.

        Args:
            snapshot: Current market state.

        Returns:
            Signal if momentum detected, None otherwise.
        """
        market_id = snapshot.market_id

        # Check time to resolution
        if snapshot.end_date:
            hours_to_end = (snapshot.end_date - snapshot.timestamp).total_seconds() / 3600
            if hours_to_end < self.config["min_hours_to_resolution"]:
                return None

        # Update price history
        if market_id not in self._price_history:
            self._price_history[market_id] = []

        self._price_history[market_id].append(
            (snapshot.timestamp, snapshot.yes_price, snapshot.volume)
        )

        # Clean old entries
        window = timedelta(minutes=self.config["momentum_window_minutes"] * 2)
        cutoff = snapshot.timestamp - window
        self._price_history[market_id] = [
            entry for entry in self._price_history[market_id]
            if entry[0] > cutoff
        ]

        # Need enough history
        history = self._price_history[market_id]
        if len(history) < 3:
            return None

        # Calculate momentum
        momentum_data = self._calculate_momentum(history, snapshot)
        if not momentum_data:
            return None

        price_change = momentum_data["price_change"]
        volume_surge = momentum_data["volume_surge"]
        momentum_direction = momentum_data["direction"]

        # Check thresholds
        if abs(price_change) < self.config["min_price_change"]:
            return None

        if volume_surge < self.config["min_volume_surge"]:
            return None

        # Calculate confidence based on momentum strength
        confidence = self._calculate_confidence(price_change, volume_surge, snapshot)

        if confidence < self.config["min_confidence"]:
            return None

        # Determine signal type
        signal_type = SignalType.BUY if momentum_direction > 0 else SignalType.SELL

        # Calculate stop loss and take profit
        current_price = snapshot.yes_price
        if signal_type == SignalType.BUY:
            stop_loss = current_price * (1 - self.config["stop_loss_pct"])
            take_profit = current_price * (1 + self.config["take_profit_pct"])
        else:
            stop_loss = current_price * (1 + self.config["stop_loss_pct"])
            take_profit = current_price * (1 - self.config["take_profit_pct"])

        self._signals_generated += 1

        return Signal(
            type=signal_type,
            market_id=market_id,
            token_id=snapshot.token_id,
            outcome="YES" if signal_type == SignalType.BUY else "NO",
            price=current_price,
            size=0.0,
            confidence=confidence,
            timestamp=snapshot.timestamp,
            stop_loss=stop_loss,
            take_profit=take_profit,
            metadata={
                "strategy": self.name,
                "price_change": price_change,
                "volume_surge": volume_surge,
                "momentum_direction": momentum_direction,
            },
        )

    def _calculate_momentum(
        self,
        history: list[tuple[datetime, float, float]],
        snapshot: MarketSnapshot,
    ) -> dict[str, Any] | None:
        """Calculate momentum indicators from price history."""
        window_minutes = self.config["momentum_window_minutes"]
        window_start = snapshot.timestamp - timedelta(minutes=window_minutes)

        # Get prices in window
        window_prices = [
            (ts, price, vol) for ts, price, vol in history
            if ts >= window_start
        ]

        if len(window_prices) < 2:
            return None

        # Price change from start to end of window
        start_price = window_prices[0][1]
        end_price = window_prices[-1][1]
        price_change = end_price - start_price

        # Volume in window vs historical average
        window_volume = sum(vol for _, _, vol in window_prices)
        historical_volume = sum(vol for _, _, vol in history) / max(len(history), 1)

        volume_surge = window_volume / max(historical_volume, 0.001)

        return {
            "price_change": price_change,
            "volume_surge": volume_surge,
            "direction": 1 if price_change > 0 else -1,
            "start_price": start_price,
            "end_price": end_price,
        }

    def _calculate_confidence(
        self,
        price_change: float,
        volume_surge: float,
        snapshot: MarketSnapshot,
    ) -> float:
        """Calculate trade confidence from momentum indicators."""
        # Base confidence from price change magnitude
        price_confidence = min(abs(price_change) / 0.15, 1.0)

        # Volume confirmation
        volume_confidence = min((volume_surge - 1) / 3, 1.0)

        # Spread factor (tighter = more confident)
        spread_confidence = 1.0
        if snapshot.spread:
            spread_confidence = max(0, 1 - snapshot.spread / 0.10)

        # Weighted average
        confidence = (
            price_confidence * 0.4 +
            volume_confidence * 0.4 +
            spread_confidence * 0.2
        )

        return min(max(confidence, 0), 1.0)

    def calculate_position_size(
        self,
        signal: Signal,
        portfolio_value: float,
        positions: dict[str, Any],
    ) -> float:
        """Calculate position size based on momentum strength."""
        max_by_pct = portfolio_value * self.config["max_position_pct"]
        position_size = min(max_by_pct, self.config["max_position_size"])
        position_size = max(position_size, self.config["min_position_size"])

        # Scale by confidence
        position_size *= signal.confidence

        # Reduce size if we already have positions in this market
        market_exposure = sum(
            pos.get("size", 0) for pos in positions.values()
            if pos.get("market_id") == signal.market_id
        )
        if market_exposure > 0:
            position_size *= 0.5

        return position_size

    def reset(self) -> None:
        """Reset strategy state."""
        super().reset()
        self._price_history.clear()
        self._signals_generated = 0

    def get_stats(self) -> dict[str, Any]:
        """Get strategy statistics."""
        stats = super().get_stats()
        stats.update({
            "signals_generated": self._signals_generated,
            "markets_tracked": len(self._price_history),
        })
        return stats
