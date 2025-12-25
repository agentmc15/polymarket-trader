"""Cross-platform arbitrage strategy.

Exploits price differences for the same event across
different prediction market platforms (Polymarket, Kalshi, etc).
"""
from datetime import datetime
from typing import Any

from app.strategies.base import BaseStrategy, MarketSnapshot, Signal, SignalType


DEFAULT_CONFIG: dict[str, Any] = {
    # Minimum profit margin after fees
    "min_profit_margin": 0.03,
    # Polymarket fee rate
    "polymarket_fee_rate": 0.0,
    # Kalshi fee rate (typically 7% on profits)
    "kalshi_fee_rate": 0.07,
    # Other platform fees
    "other_platform_fees": {
        "predictit": 0.10,
        "betfair": 0.05,
    },
    # Maximum position as fraction of portfolio
    "max_position_pct": 0.15,
    # Minimum position size
    "min_position_size": 50.0,
    # Maximum position size
    "max_position_size": 1000.0,
    # Maximum execution delay tolerance (seconds)
    "max_execution_delay": 30,
    # Slippage buffer
    "slippage_buffer": 0.01,
    # Platforms to compare
    "platforms": ["polymarket", "kalshi"],
}


class CrossPlatformArbitrageStrategy(BaseStrategy):
    """Arbitrage strategy across prediction market platforms.

    Identifies the same event listed on multiple platforms and
    trades when prices diverge enough to profit after fees.

    Example:
        Polymarket: "Biden wins 2024" = 0.45
        Kalshi: "Biden wins 2024" = 0.50
        After fees, buy Polymarket, sell Kalshi for 5% edge
    """

    name = "cross_platform_arbitrage"
    description = "Arbitrage Polymarket vs other platforms"
    version = "1.0.0"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """Initialize with merged config."""
        merged_config = {**DEFAULT_CONFIG, **(config or {})}
        super().__init__(merged_config)
        # Cross-platform prices: {normalized_event: {platform: (price, timestamp, market_id)}}
        self._cross_platform_prices: dict[str, dict[str, tuple[float, datetime, str]]] = {}
        self._arbitrage_opportunities = 0

    def on_market_data(self, snapshot: MarketSnapshot) -> Signal | None:
        """Check for cross-platform arbitrage opportunities.

        Note: This strategy requires external data feed for other platforms.
        The snapshot.metadata should contain cross_platform_prices.

        Args:
            snapshot: Current market state with cross-platform data.

        Returns:
            Signal if arbitrage opportunity exists, None otherwise.
        """
        # Get cross-platform prices from metadata
        cross_prices = snapshot.metadata.get("cross_platform_prices", {})

        if not cross_prices:
            # If no external data, try to match with cached data
            normalized_event = self._normalize_event(snapshot.question)
            if normalized_event:
                self._update_cache(
                    normalized_event,
                    "polymarket",
                    snapshot.yes_price,
                    snapshot.timestamp,
                    snapshot.market_id,
                )
                cross_prices = self._cross_platform_prices.get(normalized_event, {})

        if len(cross_prices) < 2:
            return None

        # Find arbitrage opportunity
        opportunity = self._find_arbitrage(cross_prices, snapshot)

        if not opportunity:
            return None

        self._arbitrage_opportunities += 1

        return Signal(
            type=opportunity["signal_type"],
            market_id=snapshot.market_id,
            token_id=snapshot.token_id,
            outcome=opportunity["outcome"],
            price=opportunity["price"],
            size=0.0,
            confidence=opportunity["confidence"],
            timestamp=snapshot.timestamp,
            metadata={
                "strategy": self.name,
                "is_cross_platform_arb": True,
                "buy_platform": opportunity["buy_platform"],
                "sell_platform": opportunity["sell_platform"],
                "buy_price": opportunity["buy_price"],
                "sell_price": opportunity["sell_price"],
                "gross_profit": opportunity["gross_profit"],
                "net_profit": opportunity["net_profit"],
            },
        )

    def _normalize_event(self, question: str) -> str | None:
        """Normalize event question for cross-platform matching."""
        if not question:
            return None

        # Simple normalization - production would use better NLP
        normalized = question.lower().strip()

        # Remove common variations
        replacements = [
            ("will ", ""),
            ("won't ", "not "),
            ("  ", " "),
        ]

        for old, new in replacements:
            normalized = normalized.replace(old, new)

        return normalized if len(normalized) > 10 else None

    def _update_cache(
        self,
        event: str,
        platform: str,
        price: float,
        timestamp: datetime,
        market_id: str,
    ) -> None:
        """Update cross-platform price cache."""
        if event not in self._cross_platform_prices:
            self._cross_platform_prices[event] = {}

        self._cross_platform_prices[event][platform] = (price, timestamp, market_id)

    def _find_arbitrage(
        self,
        cross_prices: dict[str, Any],
        snapshot: MarketSnapshot,
    ) -> dict[str, Any] | None:
        """Find arbitrage opportunity across platforms."""
        platforms = list(cross_prices.keys())

        best_opportunity = None
        best_profit = 0

        for i, buy_platform in enumerate(platforms):
            for sell_platform in platforms[i + 1:]:
                # Get prices
                buy_data = cross_prices[buy_platform]
                sell_data = cross_prices[sell_platform]

                if isinstance(buy_data, tuple):
                    buy_price, buy_time, _ = buy_data
                else:
                    buy_price = buy_data.get("price", buy_data)
                    buy_time = datetime.utcnow()

                if isinstance(sell_data, tuple):
                    sell_price, sell_time, _ = sell_data
                else:
                    sell_price = sell_data.get("price", sell_data)
                    sell_time = datetime.utcnow()

                # Check if prices are stale
                max_delay = self.config["max_execution_delay"]
                if (snapshot.timestamp - buy_time).total_seconds() > max_delay:
                    continue
                if (snapshot.timestamp - sell_time).total_seconds() > max_delay:
                    continue

                # Calculate arbitrage both ways
                for direction in ["normal", "reverse"]:
                    if direction == "reverse":
                        buy_price, sell_price = sell_price, buy_price
                        buy_platform, sell_platform = sell_platform, buy_platform

                    # Gross profit (buy low, sell high)
                    gross_profit = sell_price - buy_price

                    if gross_profit <= 0:
                        continue

                    # Calculate fees
                    buy_fee = self._get_platform_fee(buy_platform)
                    sell_fee = self._get_platform_fee(sell_platform)

                    # Net profit after fees and slippage
                    slippage = self.config["slippage_buffer"]
                    net_profit = gross_profit - buy_fee - sell_fee - slippage

                    if net_profit < self.config["min_profit_margin"]:
                        continue

                    if net_profit > best_profit:
                        best_profit = net_profit
                        best_opportunity = {
                            "buy_platform": buy_platform,
                            "sell_platform": sell_platform,
                            "buy_price": buy_price,
                            "sell_price": sell_price,
                            "gross_profit": gross_profit,
                            "net_profit": net_profit,
                            "signal_type": SignalType.BUY if buy_platform == "polymarket" else SignalType.SELL,
                            "outcome": "YES" if buy_platform == "polymarket" else "NO",
                            "price": snapshot.yes_price,
                            "confidence": min(net_profit / 0.10, 0.95),
                        }

        return best_opportunity

    def _get_platform_fee(self, platform: str) -> float:
        """Get fee rate for a platform."""
        if platform == "polymarket":
            return self.config["polymarket_fee_rate"]
        elif platform == "kalshi":
            return self.config["kalshi_fee_rate"]
        else:
            return self.config["other_platform_fees"].get(platform, 0.05)

    def calculate_position_size(
        self,
        signal: Signal,
        portfolio_value: float,
        positions: dict[str, Any],
    ) -> float:
        """Calculate position size for cross-platform arbitrage."""
        max_by_pct = portfolio_value * self.config["max_position_pct"]
        position_size = min(max_by_pct, self.config["max_position_size"])
        position_size = max(position_size, self.config["min_position_size"])

        # Scale by confidence
        position_size *= signal.confidence

        # Cross-platform arb is higher confidence, allow larger positions
        position_size = min(position_size, portfolio_value * 0.3)

        return position_size

    def reset(self) -> None:
        """Reset strategy state."""
        super().reset()
        self._cross_platform_prices.clear()
        self._arbitrage_opportunities = 0

    def get_stats(self) -> dict[str, Any]:
        """Get strategy statistics."""
        stats = super().get_stats()
        stats.update({
            "arbitrage_opportunities": self._arbitrage_opportunities,
            "tracked_events": len(self._cross_platform_prices),
        })
        return stats
