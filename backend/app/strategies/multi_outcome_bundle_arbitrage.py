"""Multi-outcome bundle arbitrage strategy.

Exploits mispricing when the sum of all outcome prices in a
multi-outcome market is not equal to 1.0.
"""
from datetime import datetime
from typing import Any

from app.strategies.base import BaseStrategy, MarketSnapshot, Signal, SignalType


DEFAULT_CONFIG: dict[str, Any] = {
    # Minimum profit margin after fees
    "min_profit_margin": 0.03,
    # Fee rate per trade
    "fee_rate": 0.0,
    # Maximum position as fraction of portfolio
    "max_position_pct": 0.08,
    # Minimum position size
    "min_position_size": 10.0,
    # Maximum position size
    "max_position_size": 500.0,
    # Minimum number of outcomes to consider
    "min_outcomes": 3,
    # Maximum number of outcomes (complexity limit)
    "max_outcomes": 10,
    # Minimum liquidity per outcome
    "min_liquidity_per_outcome": 500.0,
}


class MultiOutcomeBundleArbitrageStrategy(BaseStrategy):
    """Arbitrage strategy for multi-outcome markets.

    In a market with N mutually exclusive outcomes, the sum of all
    outcome prices should equal 1.0. When sum < 1.0, buy all outcomes.
    When sum > 1.0, sell all outcomes (if possible).

    Example (3 outcomes):
        A = 0.30, B = 0.35, C = 0.30 -> Sum = 0.95
        Cost to buy all = 0.95
        Guaranteed payout = 1.00
        Profit = 0.05 (5.3% return)
    """

    name = "multi_outcome_bundle_arbitrage"
    description = "Arbitrage across multi-outcome markets"
    version = "1.0.0"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """Initialize with merged config."""
        merged_config = {**DEFAULT_CONFIG, **(config or {})}
        super().__init__(merged_config)
        self._bundle_opportunities = 0
        self._markets_analyzed: dict[str, dict] = {}

    def on_market_data(self, snapshot: MarketSnapshot) -> Signal | None:
        """Analyze multi-outcome market for arbitrage.

        This strategy requires the full orderbook with all outcomes.
        The snapshot.orderbook should contain prices for all outcomes.

        Args:
            snapshot: Current market state with orderbook data.

        Returns:
            Signal if bundle arbitrage exists, None otherwise.
        """
        orderbook = snapshot.orderbook
        if not orderbook:
            return None

        # Extract all outcome prices from orderbook
        outcomes = orderbook.get("outcomes", {})
        if not outcomes:
            # Fallback to binary market check
            outcomes = {
                "YES": snapshot.yes_ask or snapshot.yes_price,
                "NO": snapshot.no_ask or snapshot.no_price,
            }

        num_outcomes = len(outcomes)

        # Check outcome count limits
        if num_outcomes < self.config["min_outcomes"]:
            return None
        if num_outcomes > self.config["max_outcomes"]:
            return None

        # Calculate sum of best ask prices (cost to buy all)
        total_cost = 0.0
        outcome_prices: dict[str, float] = {}

        for outcome_name, outcome_data in outcomes.items():
            if isinstance(outcome_data, dict):
                price = outcome_data.get("ask", outcome_data.get("price", 0))
                liquidity = outcome_data.get("liquidity", 0)
            else:
                price = float(outcome_data)
                liquidity = self.config["min_liquidity_per_outcome"]

            # Check per-outcome liquidity
            if liquidity < self.config["min_liquidity_per_outcome"]:
                return None

            outcome_prices[outcome_name] = price
            total_cost += price

        # Apply fees
        fee_rate = self.config["fee_rate"]
        total_cost_with_fees = total_cost * (1 + fee_rate * num_outcomes)

        # Calculate profit margin
        profit_margin = 1.0 - total_cost_with_fees

        if profit_margin < self.config["min_profit_margin"]:
            return None

        # Found bundle arbitrage
        self._bundle_opportunities += 1

        # Store market analysis for reference
        self._markets_analyzed[snapshot.market_id] = {
            "outcomes": outcome_prices,
            "total_cost": total_cost,
            "profit_margin": profit_margin,
            "timestamp": snapshot.timestamp,
        }

        # Return signal for the first outcome (executor handles full bundle)
        first_outcome = list(outcome_prices.keys())[0]

        return Signal(
            type=SignalType.BUY,
            market_id=snapshot.market_id,
            token_id=snapshot.token_id,
            outcome=first_outcome,
            price=outcome_prices[first_outcome],
            size=0.0,
            confidence=min(profit_margin / 0.10, 1.0),
            timestamp=snapshot.timestamp,
            metadata={
                "strategy": self.name,
                "is_bundle_arb": True,
                "num_outcomes": num_outcomes,
                "outcome_prices": outcome_prices,
                "total_cost": total_cost,
                "profit_margin": profit_margin,
            },
        )

    def calculate_position_size(
        self,
        signal: Signal,
        portfolio_value: float,
        positions: dict[str, Any],
    ) -> float:
        """Calculate position size for bundle arbitrage.

        Position size is divided across all outcomes.

        Args:
            signal: The arbitrage signal.
            portfolio_value: Current portfolio value.
            positions: Current positions.

        Returns:
            Total position size for the bundle.
        """
        num_outcomes = signal.metadata.get("num_outcomes", 2)

        # Base position size
        max_by_pct = portfolio_value * self.config["max_position_pct"]
        position_size = min(max_by_pct, self.config["max_position_size"])
        position_size = max(position_size, self.config["min_position_size"])

        # Scale by confidence
        position_size *= signal.confidence

        # Ensure enough for all outcomes
        position_size = min(position_size, portfolio_value * 0.4)

        return position_size

    def reset(self) -> None:
        """Reset strategy state."""
        super().reset()
        self._bundle_opportunities = 0
        self._markets_analyzed.clear()

    def get_stats(self) -> dict[str, Any]:
        """Get strategy statistics."""
        stats = super().get_stats()
        stats.update({
            "bundle_opportunities": self._bundle_opportunities,
            "markets_analyzed": len(self._markets_analyzed),
        })
        return stats
