"""Correlation hedging strategy.

Trades pairs of correlated markets to profit from
temporary divergences while hedging systematic risk.
"""
from datetime import datetime, timedelta
from typing import Any

from app.strategies.base import BaseStrategy, MarketSnapshot, Signal, SignalType


DEFAULT_CONFIG: dict[str, Any] = {
    # Minimum correlation to consider markets related
    "min_correlation": 0.70,
    # Minimum divergence to trade
    "min_divergence": 0.08,
    # Maximum divergence (avoid structural breaks)
    "max_divergence": 0.25,
    # Lookback period for correlation calculation (in data points)
    "correlation_lookback": 50,
    # Maximum position as fraction of portfolio
    "max_position_pct": 0.10,
    # Minimum position size per leg
    "min_position_size": 25.0,
    # Maximum position size per leg
    "max_position_size": 350.0,
    # Mean reversion expected timeframe (hours)
    "expected_reversion_hours": 48,
    # Stop loss on spread widening
    "max_spread_widening": 0.10,
    # Categories to look for correlations
    "correlation_categories": [
        "politics",
        "crypto",
        "sports",
        "economics",
    ],
}


class CorrelationHedgingStrategy(BaseStrategy):
    """Pairs trading strategy for correlated prediction markets.

    Identifies markets that should move together and trades
    divergences, expecting mean reversion.

    Example:
        "Will Candidate A win primary?" = 0.65
        "Will Candidate A win nomination?" = 0.55
        These should be correlated (can't win nomination without primary)
        Divergence suggests buy nomination, sell primary
    """

    name = "correlation_hedging"
    description = "Trade correlated market pairs"
    version = "1.0.0"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """Initialize with merged config."""
        merged_config = {**DEFAULT_CONFIG, **(config or {})}
        super().__init__(merged_config)
        # Price history: {market_id: [(timestamp, price)]}
        self._price_history: dict[str, list[tuple[datetime, float]]] = {}
        # Correlation cache: {(market_a, market_b): correlation}
        self._correlations: dict[tuple[str, str], float] = {}
        # Market metadata for matching
        self._market_metadata: dict[str, dict] = {}
        self._pair_trades = 0

    def on_market_data(self, snapshot: MarketSnapshot) -> Signal | None:
        """Analyze correlations and find divergence opportunities.

        Args:
            snapshot: Current market state.

        Returns:
            Signal if divergence opportunity found, None otherwise.
        """
        market_id = snapshot.market_id

        # Update price history
        if market_id not in self._price_history:
            self._price_history[market_id] = []

        self._price_history[market_id].append(
            (snapshot.timestamp, snapshot.yes_price)
        )

        # Limit history size
        max_history = self.config["correlation_lookback"] * 2
        if len(self._price_history[market_id]) > max_history:
            self._price_history[market_id] = self._price_history[market_id][-max_history:]

        # Update market metadata
        self._market_metadata[market_id] = {
            "category": snapshot.category,
            "question": snapshot.question,
            "current_price": snapshot.yes_price,
            "token_id": snapshot.token_id,
        }

        # Find correlated markets
        correlated_markets = self._find_correlated_markets(market_id)

        if not correlated_markets:
            return None

        # Check for divergence opportunities
        signal = self._find_divergence(market_id, correlated_markets, snapshot)

        return signal

    def _find_correlated_markets(self, market_id: str) -> list[tuple[str, float]]:
        """Find markets correlated with the given market."""
        if market_id not in self._price_history:
            return []

        current_metadata = self._market_metadata.get(market_id, {})
        current_category = current_metadata.get("category")

        correlated = []

        for other_id, other_history in self._price_history.items():
            if other_id == market_id:
                continue

            other_metadata = self._market_metadata.get(other_id, {})

            # Only compare within same category for efficiency
            if other_metadata.get("category") != current_category:
                continue

            # Check cache first
            cache_key = tuple(sorted([market_id, other_id]))
            if cache_key in self._correlations:
                correlation = self._correlations[cache_key]
            else:
                correlation = self._calculate_correlation(market_id, other_id)
                self._correlations[cache_key] = correlation

            if correlation and correlation >= self.config["min_correlation"]:
                correlated.append((other_id, correlation))

        # Sort by correlation strength
        correlated.sort(key=lambda x: x[1], reverse=True)

        return correlated[:5]  # Top 5 correlated markets

    def _calculate_correlation(self, market_a: str, market_b: str) -> float | None:
        """Calculate price correlation between two markets."""
        history_a = self._price_history.get(market_a, [])
        history_b = self._price_history.get(market_b, [])

        lookback = self.config["correlation_lookback"]

        if len(history_a) < lookback or len(history_b) < lookback:
            return None

        # Get recent prices
        prices_a = [p for _, p in history_a[-lookback:]]
        prices_b = [p for _, p in history_b[-lookback:]]

        # Simple correlation calculation
        mean_a = sum(prices_a) / len(prices_a)
        mean_b = sum(prices_b) / len(prices_b)

        numerator = sum(
            (a - mean_a) * (b - mean_b)
            for a, b in zip(prices_a, prices_b)
        )

        var_a = sum((a - mean_a) ** 2 for a in prices_a)
        var_b = sum((b - mean_b) ** 2 for b in prices_b)

        if var_a == 0 or var_b == 0:
            return None

        correlation = numerator / ((var_a * var_b) ** 0.5)

        return correlation

    def _find_divergence(
        self,
        market_id: str,
        correlated_markets: list[tuple[str, float]],
        snapshot: MarketSnapshot,
    ) -> Signal | None:
        """Find tradeable divergences with correlated markets."""
        current_price = snapshot.yes_price

        for other_id, correlation in correlated_markets:
            other_metadata = self._market_metadata.get(other_id, {})
            other_price = other_metadata.get("current_price")

            if other_price is None:
                continue

            # Calculate divergence
            # For highly correlated markets, prices should be similar
            divergence = current_price - other_price

            if abs(divergence) < self.config["min_divergence"]:
                continue
            if abs(divergence) > self.config["max_divergence"]:
                continue

            # Determine trade direction (mean reversion)
            if divergence > 0:
                # Current market overpriced relative to correlated
                signal_type = SignalType.SELL
                outcome = "NO"
            else:
                # Current market underpriced
                signal_type = SignalType.BUY
                outcome = "YES"

            confidence = correlation * min(abs(divergence) / 0.15, 1.0)
            confidence = min(confidence, 0.85)

            self._pair_trades += 1

            return Signal(
                type=signal_type,
                market_id=market_id,
                token_id=snapshot.token_id,
                outcome=outcome,
                price=current_price,
                size=0.0,
                confidence=confidence,
                timestamp=snapshot.timestamp,
                stop_loss=current_price + (self.config["max_spread_widening"] * (1 if signal_type == SignalType.SELL else -1)),
                metadata={
                    "strategy": self.name,
                    "is_pair_trade": True,
                    "correlated_market": other_id,
                    "correlation": correlation,
                    "divergence": divergence,
                    "other_price": other_price,
                },
            )

        return None

    def calculate_position_size(
        self,
        signal: Signal,
        portfolio_value: float,
        positions: dict[str, Any],
    ) -> float:
        """Calculate position size for correlation trade."""
        max_by_pct = portfolio_value * self.config["max_position_pct"]
        position_size = min(max_by_pct, self.config["max_position_size"])
        position_size = max(position_size, self.config["min_position_size"])

        # Scale by confidence (which includes correlation)
        position_size *= signal.confidence

        # Limit total pair exposure
        pair_positions = sum(
            1 for p in positions.values()
            if p.get("metadata", {}).get("is_pair_trade")
        )
        if pair_positions > 4:
            position_size *= 0.5

        return position_size

    def reset(self) -> None:
        """Reset strategy state."""
        super().reset()
        self._price_history.clear()
        self._correlations.clear()
        self._market_metadata.clear()
        self._pair_trades = 0

    def get_stats(self) -> dict[str, Any]:
        """Get strategy statistics."""
        stats = super().get_stats()
        stats.update({
            "pair_trades": self._pair_trades,
            "tracked_markets": len(self._price_history),
            "correlation_pairs": len(self._correlations),
        })
        return stats
