"""Term structure spreads strategy.

Trades spreads between related markets with different
resolution dates (e.g., monthly vs quarterly).
"""
from datetime import datetime, timedelta
from typing import Any

from app.strategies.base import BaseStrategy, MarketSnapshot, Signal, SignalType


DEFAULT_CONFIG: dict[str, Any] = {
    # Minimum spread to trade
    "min_spread": 0.05,
    # Maximum spread (avoid extreme dislocations)
    "max_spread": 0.30,
    # Expected term premium per month
    "expected_monthly_premium": 0.02,
    # Maximum position as fraction of portfolio
    "max_position_pct": 0.12,
    # Minimum position size
    "min_position_size": 25.0,
    # Maximum position size per leg
    "max_position_size": 400.0,
    # Minimum days between expirations
    "min_term_gap_days": 14,
    # Maximum days between expirations
    "max_term_gap_days": 180,
    # Stop loss on spread
    "spread_stop_loss": 0.08,
}


class TermStructureSpreadsStrategy(BaseStrategy):
    """Strategy trading term structure spreads.

    Identifies related markets with different expiration dates
    and trades when the spread deviates from expected term premium.

    Example:
        "Will BTC reach $100k by March?" = 0.60
        "Will BTC reach $100k by June?" = 0.70
        Spread = 0.10 for 3 months
        Expected = 0.06 (0.02 * 3 months)
        Edge = 0.04 -> Sell June, Buy March
    """

    name = "term_structure_spreads"
    description = "Trade date spread mispricings"
    version = "1.0.0"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """Initialize with merged config."""
        merged_config = {**DEFAULT_CONFIG, **(config or {})}
        super().__init__(merged_config)
        # Track related markets: {base_question: [(market_id, end_date, price)]}
        self._market_pairs: dict[str, list[tuple[str, datetime, float, str]]] = {}
        self._spread_trades = 0

    def on_market_data(self, snapshot: MarketSnapshot) -> Signal | None:
        """Analyze term structure for spread opportunities.

        Args:
            snapshot: Current market state.

        Returns:
            Signal if spread opportunity found, None otherwise.
        """
        if not snapshot.end_date:
            return None

        # Extract base question (remove date references for matching)
        base_question = self._normalize_question(snapshot.question)

        if not base_question:
            return None

        # Update market pair tracking
        market_entry = (
            snapshot.market_id,
            snapshot.end_date,
            snapshot.yes_price,
            snapshot.token_id,
        )

        if base_question not in self._market_pairs:
            self._market_pairs[base_question] = []

        # Update or add this market
        self._market_pairs[base_question] = [
            entry for entry in self._market_pairs[base_question]
            if entry[0] != snapshot.market_id
        ]
        self._market_pairs[base_question].append(market_entry)

        # Need at least 2 markets to form a spread
        markets = self._market_pairs[base_question]
        if len(markets) < 2:
            return None

        # Sort by expiration date
        markets.sort(key=lambda x: x[1])

        # Look for spread opportunities
        signal = self._find_spread_opportunity(markets, snapshot)

        return signal

    def _normalize_question(self, question: str) -> str | None:
        """Normalize question to find related markets."""
        if not question:
            return None

        # Remove common date patterns
        import re

        normalized = question.lower()

        # Remove specific dates
        date_patterns = [
            r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\s*\d{0,2},?\s*\d{4}\b",
            r"\b\d{1,2}/\d{1,2}/\d{2,4}\b",
            r"\bq[1-4]\s*\d{4}\b",
            r"\b(by|before|after|in)\s+(january|february|march|april|may|june|july|august|september|october|november|december)\b",
            r"\b\d{4}\b",
        ]

        for pattern in date_patterns:
            normalized = re.sub(pattern, "", normalized)

        # Clean up
        normalized = " ".join(normalized.split())

        if len(normalized) < 10:
            return None

        return normalized

    def _find_spread_opportunity(
        self,
        markets: list[tuple[str, datetime, float, str]],
        current_snapshot: MarketSnapshot,
    ) -> Signal | None:
        """Find mispriced spreads in related markets."""
        monthly_premium = self.config["expected_monthly_premium"]

        for i in range(len(markets) - 1):
            near_market = markets[i]
            far_market = markets[i + 1]

            near_id, near_date, near_price, near_token = near_market
            far_id, far_date, far_price, far_token = far_market

            # Check term gap
            days_between = (far_date - near_date).days

            if days_between < self.config["min_term_gap_days"]:
                continue
            if days_between > self.config["max_term_gap_days"]:
                continue

            # Calculate actual vs expected spread
            actual_spread = far_price - near_price
            months_between = days_between / 30
            expected_spread = monthly_premium * months_between

            spread_diff = actual_spread - expected_spread

            # Check if spread is tradeable
            if abs(spread_diff) < self.config["min_spread"]:
                continue
            if abs(actual_spread) > self.config["max_spread"]:
                continue

            # Determine trade direction
            if spread_diff > 0:
                # Far term overpriced relative to near
                # Sell far, buy near
                signal_type = SignalType.SELL
                target_market = far_id
                target_token = far_token
                target_price = far_price
                outcome = "NO"  # Selling YES is like buying NO
            else:
                # Far term underpriced
                # Buy far, sell near
                signal_type = SignalType.BUY
                target_market = far_id
                target_token = far_token
                target_price = far_price
                outcome = "YES"

            # Only signal if this is the current market
            if target_market != current_snapshot.market_id:
                continue

            confidence = min(abs(spread_diff) / 0.15, 0.9)
            self._spread_trades += 1

            return Signal(
                type=signal_type,
                market_id=target_market,
                token_id=target_token,
                outcome=outcome,
                price=target_price,
                size=0.0,
                confidence=confidence,
                timestamp=current_snapshot.timestamp,
                stop_loss=target_price + (self.config["spread_stop_loss"] * (-1 if signal_type == SignalType.BUY else 1)),
                metadata={
                    "strategy": self.name,
                    "is_spread_trade": True,
                    "near_market": near_id,
                    "far_market": far_id,
                    "actual_spread": actual_spread,
                    "expected_spread": expected_spread,
                    "spread_diff": spread_diff,
                    "days_between": days_between,
                },
            )

        return None

    def calculate_position_size(
        self,
        signal: Signal,
        portfolio_value: float,
        positions: dict[str, Any],
    ) -> float:
        """Calculate position size for spread trade."""
        max_by_pct = portfolio_value * self.config["max_position_pct"]
        position_size = min(max_by_pct, self.config["max_position_size"])
        position_size = max(position_size, self.config["min_position_size"])

        # Scale by confidence
        position_size *= signal.confidence

        # Check existing spread exposure
        spread_exposure = sum(
            1 for p in positions.values()
            if p.get("metadata", {}).get("is_spread_trade")
        )
        if spread_exposure > 3:
            position_size *= 0.5

        return position_size

    def reset(self) -> None:
        """Reset strategy state."""
        super().reset()
        self._market_pairs.clear()
        self._spread_trades = 0

    def get_stats(self) -> dict[str, Any]:
        """Get strategy statistics."""
        stats = super().get_stats()
        stats.update({
            "spread_trades": self._spread_trades,
            "tracked_pairs": len(self._market_pairs),
        })
        return stats
