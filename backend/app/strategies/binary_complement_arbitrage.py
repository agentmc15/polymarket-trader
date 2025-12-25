"""Binary complement arbitrage strategy.

Exploits mispricing when YES + NO prices sum to less than 1.0,
guaranteeing profit by buying both outcomes.
"""
from datetime import datetime
from typing import Any

from app.strategies.base import BaseStrategy, MarketSnapshot, Signal, SignalType


DEFAULT_CONFIG: dict[str, Any] = {
    # Minimum profit margin after fees to trigger trade
    "min_profit_margin": 0.02,
    # Trading fee rate (Polymarket is typically 0%)
    "fee_rate": 0.0,
    # Maximum position size as fraction of portfolio
    "max_position_pct": 0.10,
    # Minimum position size in dollars
    "min_position_size": 10.0,
    # Maximum position size in dollars
    "max_position_size": 1000.0,
    # Minimum liquidity required (volume_24h)
    "min_liquidity": 1000.0,
    # Use mid prices vs best bid/ask
    "use_mid_prices": False,
}


class BinaryComplementArbitrageStrategy(BaseStrategy):
    """Arbitrage strategy exploiting YES + NO < 1.0 mispricings.

    In a binary market, YES + NO should always equal 1.0. When the sum
    is less than 1.0 (minus fees), buying both outcomes guarantees profit
    since one will resolve to 1.0.

    Example:
        YES = 0.45, NO = 0.48 -> Sum = 0.93
        Cost to buy both = 0.93
        Guaranteed payout = 1.00
        Profit = 0.07 (7.5% return)
    """

    name = "binary_complement_arbitrage"
    description = "Arbitrage when YES + NO < 1.0"
    version = "1.0.0"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """Initialize with merged config."""
        merged_config = {**DEFAULT_CONFIG, **(config or {})}
        super().__init__(merged_config)
        self._opportunities_found = 0
        self._total_theoretical_profit = 0.0

    def on_market_data(self, snapshot: MarketSnapshot) -> Signal | None:
        """Check for arbitrage opportunity in binary market.

        Args:
            snapshot: Current market state.

        Returns:
            Signal to buy YES if arbitrage exists, None otherwise.
            (Execution should buy both YES and NO proportionally)
        """
        # Get prices based on config
        if self.config["use_mid_prices"]:
            yes_price = snapshot.mid_price
            no_price = (
                (snapshot.no_bid + snapshot.no_ask) / 2
                if snapshot.no_bid and snapshot.no_ask
                else snapshot.no_price
            )
        else:
            # Use ask prices (what we'd pay to buy)
            yes_price = snapshot.yes_ask if snapshot.yes_ask else snapshot.yes_price
            no_price = snapshot.no_ask if snapshot.no_ask else snapshot.no_price

        # Check liquidity requirement
        if snapshot.volume_24h < self.config["min_liquidity"]:
            return None

        # Calculate total cost and potential profit
        total_cost = yes_price + no_price
        fee_rate = self.config["fee_rate"]
        total_cost_with_fees = total_cost * (1 + fee_rate)

        # Profit margin (we get 1.0 back guaranteed)
        profit_margin = 1.0 - total_cost_with_fees

        if profit_margin < self.config["min_profit_margin"]:
            return None

        # Found arbitrage opportunity
        self._opportunities_found += 1
        self._total_theoretical_profit += profit_margin

        # Signal to buy YES (the strategy executor should handle buying both)
        return Signal(
            type=SignalType.BUY,
            market_id=snapshot.market_id,
            token_id=snapshot.token_id,
            outcome="YES",
            price=yes_price,
            size=0.0,  # Will be calculated by calculate_position_size
            confidence=min(profit_margin / 0.10, 1.0),  # Scale confidence by margin
            timestamp=snapshot.timestamp,
            metadata={
                "strategy": self.name,
                "yes_price": yes_price,
                "no_price": no_price,
                "total_cost": total_cost,
                "profit_margin": profit_margin,
                "is_arbitrage": True,
            },
        )

    def calculate_position_size(
        self,
        signal: Signal,
        portfolio_value: float,
        positions: dict[str, Any],
    ) -> float:
        """Calculate position size for arbitrage trade.

        Args:
            signal: The arbitrage signal.
            portfolio_value: Current portfolio value.
            positions: Current positions.

        Returns:
            Position size in dollars (for buying both YES and NO).
        """
        # Calculate max position based on portfolio percentage
        max_by_pct = portfolio_value * self.config["max_position_pct"]

        # Apply absolute limits
        position_size = min(
            max_by_pct,
            self.config["max_position_size"],
        )
        position_size = max(position_size, self.config["min_position_size"])

        # Scale by confidence (higher margin = larger position)
        position_size *= signal.confidence

        # Ensure we don't exceed available capital
        position_size = min(position_size, portfolio_value * 0.5)

        return position_size

    def reset(self) -> None:
        """Reset strategy state."""
        super().reset()
        self._opportunities_found = 0
        self._total_theoretical_profit = 0.0

    def get_stats(self) -> dict[str, Any]:
        """Get strategy statistics."""
        stats = super().get_stats()
        stats.update({
            "opportunities_found": self._opportunities_found,
            "total_theoretical_profit": self._total_theoretical_profit,
        })
        return stats
