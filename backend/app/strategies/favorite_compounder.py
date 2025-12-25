"""Favorite compounder strategy.

Systematically trades high-probability outcomes to compound
small gains with high win rate.
"""
from datetime import datetime, timedelta
from typing import Any

from app.strategies.base import BaseStrategy, MarketSnapshot, Signal, SignalType


DEFAULT_CONFIG: dict[str, Any] = {
    # Minimum probability to consider a favorite
    "min_probability": 0.85,
    # Maximum probability (avoid certainties with no upside)
    "max_probability": 0.97,
    # Minimum edge over implied probability
    "min_edge": 0.02,
    # Maximum position as fraction of portfolio
    "max_position_pct": 0.20,
    # Minimum position size
    "min_position_size": 50.0,
    # Maximum position size
    "max_position_size": 500.0,
    # Minimum time to resolution (hours)
    "min_hours_to_resolution": 12,
    # Maximum time to resolution (hours)
    "max_hours_to_resolution": 168,  # 1 week
    # Minimum liquidity (24h volume)
    "min_liquidity": 5000.0,
    # Reinvestment rate (compound winnings)
    "reinvestment_rate": 0.80,
    # Maximum concurrent positions
    "max_positions": 10,
    # Categories to prefer (empty = all)
    "preferred_categories": [],
}


class FavoriteCompounderStrategy(BaseStrategy):
    """Strategy for compounding gains on high-probability favorites.

    Targets markets where one outcome is heavily favored but still
    offers small edge. High win rate compounds small gains into
    significant returns over time.

    Example:
        Market probability: 0.92
        Our estimate: 0.95
        Expected value: 0.95 * 1.087 = 1.033 (3.3% edge)
        Win rate: ~92%
        Kelly fraction suggests moderate sizing

    Risk: Occasional large losses when favorites lose.
    """

    name = "favorite_compounder"
    description = "Compound gains on high-probability favorites"
    version = "1.0.0"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """Initialize with merged config."""
        merged_config = {**DEFAULT_CONFIG, **(config or {})}
        super().__init__(merged_config)
        self._favorites_traded = 0
        self._favorites_won = 0
        self._current_positions: set[str] = set()

    def on_market_data(self, snapshot: MarketSnapshot) -> Signal | None:
        """Identify and trade high-probability favorites.

        Args:
            snapshot: Current market state.

        Returns:
            Signal if favorable favorite found, None otherwise.
        """
        # Check position limits
        if len(self._current_positions) >= self.config["max_positions"]:
            return None

        # Skip if already in this market
        if snapshot.market_id in self._current_positions:
            return None

        # Check category preference
        if self.config["preferred_categories"]:
            if snapshot.category not in self.config["preferred_categories"]:
                return None

        # Check liquidity
        if snapshot.volume_24h < self.config["min_liquidity"]:
            return None

        # Check time to resolution
        if snapshot.end_date:
            hours_to_end = (snapshot.end_date - snapshot.timestamp).total_seconds() / 3600

            if hours_to_end < self.config["min_hours_to_resolution"]:
                return None
            if hours_to_end > self.config["max_hours_to_resolution"]:
                return None

        # Find the favorite (higher probability outcome)
        yes_prob = snapshot.yes_price
        no_prob = snapshot.no_price

        if yes_prob >= no_prob:
            favorite_outcome = "YES"
            favorite_price = yes_prob
            underdog_price = no_prob
        else:
            favorite_outcome = "NO"
            favorite_price = no_prob
            underdog_price = yes_prob

        # Check probability thresholds
        if favorite_price < self.config["min_probability"]:
            return None
        if favorite_price > self.config["max_probability"]:
            return None

        # Estimate true probability (simple model)
        estimated_prob = self._estimate_true_probability(
            favorite_price,
            snapshot,
        )

        # Calculate edge
        edge = estimated_prob - favorite_price

        if edge < self.config["min_edge"]:
            return None

        # Calculate expected value
        # EV = (prob * payout) - (1-prob) * stake
        # For favorites, payout = 1/price - 1
        payout_ratio = 1.0 / favorite_price - 1.0
        expected_value = (estimated_prob * payout_ratio) - (1 - estimated_prob)

        if expected_value < self.config["min_edge"]:
            return None

        # Calculate confidence
        confidence = self._calculate_confidence(
            favorite_price,
            estimated_prob,
            edge,
            snapshot,
        )

        self._favorites_traded += 1

        return Signal(
            type=SignalType.BUY,
            market_id=snapshot.market_id,
            token_id=snapshot.token_id,
            outcome=favorite_outcome,
            price=favorite_price,
            size=0.0,
            confidence=confidence,
            timestamp=snapshot.timestamp,
            metadata={
                "strategy": self.name,
                "is_favorite": True,
                "market_probability": favorite_price,
                "estimated_probability": estimated_prob,
                "edge": edge,
                "expected_value": expected_value,
                "payout_ratio": payout_ratio,
            },
        )

    def _estimate_true_probability(
        self,
        market_price: float,
        snapshot: MarketSnapshot,
    ) -> float:
        """Estimate true probability from market data.

        Simple model: favorites tend to be slightly underpriced
        due to longshot bias (people prefer betting underdogs).
        """
        # Base adjustment for favorite-longshot bias
        # Higher favorites are often more underpriced
        bias_adjustment = (market_price - 0.5) * 0.05

        # Volume confidence adjustment
        volume_factor = 1.0
        if snapshot.volume_24h > 50000:
            volume_factor = 1.01  # High volume = more efficient
        elif snapshot.volume_24h < 10000:
            volume_factor = 1.03  # Low volume = more mispricing

        # Spread adjustment (tighter spread = more accurate)
        spread_factor = 1.0
        if snapshot.spread and snapshot.spread < 0.02:
            spread_factor = 1.0  # Tight spread, trust market
        elif snapshot.spread and snapshot.spread > 0.05:
            spread_factor = 1.02  # Wide spread, possible edge

        estimated = market_price + bias_adjustment
        estimated *= (volume_factor * spread_factor) ** 0.5

        # Clamp to valid probability
        return min(max(estimated, 0.01), 0.99)

    def _calculate_confidence(
        self,
        market_price: float,
        estimated_prob: float,
        edge: float,
        snapshot: MarketSnapshot,
    ) -> float:
        """Calculate trading confidence."""
        # Base confidence from edge size
        edge_confidence = min(edge / 0.08, 1.0)

        # Probability confidence (higher prob = more confident)
        prob_confidence = market_price

        # Time confidence (closer to resolution = more confident)
        time_confidence = 0.5
        if snapshot.end_date:
            hours = (snapshot.end_date - snapshot.timestamp).total_seconds() / 3600
            if hours < 24:
                time_confidence = 0.8
            elif hours < 72:
                time_confidence = 0.6

        confidence = (
            edge_confidence * 0.3 +
            prob_confidence * 0.4 +
            time_confidence * 0.3
        )

        return min(max(confidence, 0.3), 0.9)

    def calculate_position_size(
        self,
        signal: Signal,
        portfolio_value: float,
        positions: dict[str, Any],
    ) -> float:
        """Calculate position size using Kelly criterion variant."""
        # Get edge and probability from signal
        edge = signal.metadata.get("edge", 0.02)
        prob = signal.metadata.get("estimated_probability", 0.90)
        payout = signal.metadata.get("payout_ratio", 0.1)

        # Simplified Kelly fraction
        # f = (p * b - q) / b where p=prob, q=1-p, b=payout
        q = 1 - prob
        kelly_fraction = (prob * payout - q) / payout if payout > 0 else 0

        # Use fractional Kelly (safer)
        kelly_fraction *= 0.25  # Quarter Kelly

        # Apply position limits
        max_by_pct = portfolio_value * self.config["max_position_pct"]
        kelly_size = portfolio_value * kelly_fraction

        position_size = min(kelly_size, max_by_pct, self.config["max_position_size"])
        position_size = max(position_size, self.config["min_position_size"])

        # Scale by confidence
        position_size *= signal.confidence

        return position_size

    def on_trade_executed(self, trade: dict[str, Any]) -> None:
        """Track position for favorites."""
        super().on_trade_executed(trade)
        self._current_positions.add(trade.get("market_id", ""))

    def on_position_closed(self, position: dict[str, Any], pnl: float) -> None:
        """Track favorite performance."""
        super().on_position_closed(position, pnl)

        market_id = position.get("market_id", "")
        self._current_positions.discard(market_id)

        if pnl > 0:
            self._favorites_won += 1

    def reset(self) -> None:
        """Reset strategy state."""
        super().reset()
        self._favorites_traded = 0
        self._favorites_won = 0
        self._current_positions.clear()

    def get_stats(self) -> dict[str, Any]:
        """Get strategy statistics."""
        stats = super().get_stats()
        win_rate = (
            self._favorites_won / self._favorites_traded
            if self._favorites_traded > 0
            else 0
        )
        stats.update({
            "favorites_traded": self._favorites_traded,
            "favorites_won": self._favorites_won,
            "actual_win_rate": win_rate,
            "active_positions": len(self._current_positions),
        })
        return stats
