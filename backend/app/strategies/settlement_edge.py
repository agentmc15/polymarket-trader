"""Settlement edge strategy.

Exploits edge cases and ambiguities in market resolution rules
to find mispriced outcomes.
"""
from datetime import datetime, timedelta
from typing import Any

from app.strategies.base import BaseStrategy, MarketSnapshot, Signal, SignalType


DEFAULT_CONFIG: dict[str, Any] = {
    # Minimum edge (difference from fair value)
    "min_edge": 0.08,
    # Keywords indicating resolution ambiguity
    "ambiguity_keywords": [
        "at least",
        "approximately",
        "around",
        "may",
        "could",
        "if",
        "unless",
        "depending",
        "discretion",
    ],
    # Keywords indicating clear resolution
    "clarity_keywords": [
        "exactly",
        "precisely",
        "official",
        "verified",
        "confirmed",
    ],
    # Maximum position as fraction of portfolio
    "max_position_pct": 0.10,
    # Minimum position size
    "min_position_size": 20.0,
    # Maximum position size
    "max_position_size": 300.0,
    # Minimum hours before resolution to trade
    "min_hours_to_resolution": 48,
    # Maximum hours before resolution
    "max_hours_to_resolution": 720,
    # Confidence boost for clear resolution rules
    "clarity_confidence_boost": 0.15,
}


class SettlementEdgeStrategy(BaseStrategy):
    """Strategy exploiting resolution rule edge cases.

    Analyzes market resolution rules to find:
    1. Ambiguous wording that may resolve unexpectedly
    2. Edge cases where market price doesn't reflect rules
    3. Technical definitions that retail traders misunderstand

    Example:
        Market: "Will X reach 100 by end of year?"
        Rule: "Based on closing price on Dec 31"
        Edge: Market trading at 0.80 but X is at 99.5 with high volatility
    """

    name = "settlement_edge"
    description = "Exploit resolution rule edge cases"
    version = "1.0.0"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """Initialize with merged config."""
        merged_config = {**DEFAULT_CONFIG, **(config or {})}
        super().__init__(merged_config)
        self._edges_found = 0
        self._analyzed_markets: set[str] = set()

    def on_market_data(self, snapshot: MarketSnapshot) -> Signal | None:
        """Analyze resolution rules for trading edge.

        Args:
            snapshot: Current market state including resolution_rules.

        Returns:
            Signal if edge found, None otherwise.
        """
        # Need resolution rules to analyze
        if not snapshot.resolution_rules:
            return None

        # Check time window relative to resolution
        if snapshot.end_date:
            hours_to_end = (snapshot.end_date - snapshot.timestamp).total_seconds() / 3600

            if hours_to_end < self.config["min_hours_to_resolution"]:
                return None
            if hours_to_end > self.config["max_hours_to_resolution"]:
                return None

        # Analyze resolution rules
        rule_analysis = self._analyze_rules(snapshot.resolution_rules)

        # Calculate fair value estimate based on rule analysis
        fair_value = self._estimate_fair_value(snapshot, rule_analysis)

        if fair_value is None:
            return None

        # Calculate edge
        current_price = snapshot.yes_price
        edge = fair_value - current_price

        if abs(edge) < self.config["min_edge"]:
            return None

        # Determine signal direction
        if edge > 0:
            signal_type = SignalType.BUY
            outcome = "YES"
        else:
            signal_type = SignalType.SELL
            outcome = "NO"

        # Calculate confidence
        confidence = self._calculate_confidence(edge, rule_analysis, snapshot)

        self._edges_found += 1
        self._analyzed_markets.add(snapshot.market_id)

        return Signal(
            type=signal_type,
            market_id=snapshot.market_id,
            token_id=snapshot.token_id,
            outcome=outcome,
            price=current_price,
            size=0.0,
            confidence=confidence,
            timestamp=snapshot.timestamp,
            metadata={
                "strategy": self.name,
                "fair_value": fair_value,
                "edge": edge,
                "rule_analysis": rule_analysis,
            },
        )

    def _analyze_rules(self, rules: str) -> dict[str, Any]:
        """Analyze resolution rules for ambiguity and edge cases."""
        rules_lower = rules.lower()

        # Count ambiguity indicators
        ambiguity_score = sum(
            1 for keyword in self.config["ambiguity_keywords"]
            if keyword in rules_lower
        )

        # Count clarity indicators
        clarity_score = sum(
            1 for keyword in self.config["clarity_keywords"]
            if keyword in rules_lower
        )

        # Check for specific edge case patterns
        has_time_condition = any(
            word in rules_lower
            for word in ["before", "after", "by", "until", "within"]
        )

        has_threshold = any(
            word in rules_lower
            for word in ["at least", "more than", "less than", "exceed", "reach"]
        )

        has_official_source = any(
            word in rules_lower
            for word in ["official", "government", "verified", "reuters", "associated press"]
        )

        return {
            "ambiguity_score": ambiguity_score,
            "clarity_score": clarity_score,
            "has_time_condition": has_time_condition,
            "has_threshold": has_threshold,
            "has_official_source": has_official_source,
            "net_clarity": clarity_score - ambiguity_score,
        }

    def _estimate_fair_value(
        self,
        snapshot: MarketSnapshot,
        rule_analysis: dict[str, Any],
    ) -> float | None:
        """Estimate fair value based on rule analysis.

        This is a simplified model - production would use more
        sophisticated NLP and event probability models.
        """
        current_price = snapshot.yes_price

        # If rules are very clear, trust current market price more
        if rule_analysis["net_clarity"] > 2:
            return None

        # If rules are ambiguous, look for potential mispricings
        if rule_analysis["ambiguity_score"] > 2:
            # High ambiguity often leads to retail mispricing
            # Slight mean reversion toward 0.5
            adjustment = (0.5 - current_price) * 0.15
            return current_price + adjustment

        # Check for threshold edge cases
        if rule_analysis["has_threshold"]:
            # Threshold markets often mispriced near the threshold
            if 0.4 < current_price < 0.6:
                # Near 50/50 with threshold - could go either way
                # Slight edge toward current direction
                direction = 1 if current_price > 0.5 else -1
                return current_price + (direction * 0.05)

        return None

    def _calculate_confidence(
        self,
        edge: float,
        rule_analysis: dict[str, Any],
        snapshot: MarketSnapshot,
    ) -> float:
        """Calculate trade confidence."""
        # Base confidence from edge size
        edge_confidence = min(abs(edge) / 0.20, 1.0)

        # Adjust for rule clarity
        clarity_adjustment = 0
        if rule_analysis["net_clarity"] > 0:
            clarity_adjustment = self.config["clarity_confidence_boost"]
        elif rule_analysis["net_clarity"] < 0:
            clarity_adjustment = -0.10

        # Volume confidence
        volume_confidence = 0.5
        if snapshot.volume_24h > 10000:
            volume_confidence = 0.7
        elif snapshot.volume_24h > 50000:
            volume_confidence = 0.85

        confidence = (
            edge_confidence * 0.5 +
            volume_confidence * 0.3 +
            0.2
        ) + clarity_adjustment

        return min(max(confidence, 0.1), 0.95)

    def calculate_position_size(
        self,
        signal: Signal,
        portfolio_value: float,
        positions: dict[str, Any],
    ) -> float:
        """Calculate position size for settlement edge trade."""
        max_by_pct = portfolio_value * self.config["max_position_pct"]
        position_size = min(max_by_pct, self.config["max_position_size"])
        position_size = max(position_size, self.config["min_position_size"])

        # Scale by confidence
        position_size *= signal.confidence

        # Reduce if already exposed to this market
        if signal.market_id in [p.get("market_id") for p in positions.values()]:
            position_size *= 0.5

        return position_size

    def reset(self) -> None:
        """Reset strategy state."""
        super().reset()
        self._edges_found = 0
        self._analyzed_markets.clear()

    def get_stats(self) -> dict[str, Any]:
        """Get strategy statistics."""
        stats = super().get_stats()
        stats.update({
            "edges_found": self._edges_found,
            "markets_analyzed": len(self._analyzed_markets),
        })
        return stats
