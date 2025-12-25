"""Trading strategies for Polymarket.

This module provides a collection of trading strategies for backtesting
and live trading on Polymarket prediction markets.
"""
from typing import Any

from app.strategies.base import BaseStrategy, MarketSnapshot, Signal, SignalType

# Import all strategy implementations
from app.strategies.binary_complement_arbitrage import (
    BinaryComplementArbitrageStrategy,
    DEFAULT_CONFIG as BINARY_ARB_CONFIG,
)
from app.strategies.multi_outcome_bundle_arbitrage import (
    MultiOutcomeBundleArbitrageStrategy,
    DEFAULT_CONFIG as MULTI_OUTCOME_CONFIG,
)
from app.strategies.catalyst_momentum import (
    CatalystMomentumStrategy,
    DEFAULT_CONFIG as CATALYST_CONFIG,
)
from app.strategies.settlement_edge import (
    SettlementEdgeStrategy,
    DEFAULT_CONFIG as SETTLEMENT_CONFIG,
)
from app.strategies.term_structure_spreads import (
    TermStructureSpreadsStrategy,
    DEFAULT_CONFIG as TERM_STRUCTURE_CONFIG,
)
from app.strategies.correlation_hedging import (
    CorrelationHedgingStrategy,
    DEFAULT_CONFIG as CORRELATION_CONFIG,
)
from app.strategies.cross_platform_arbitrage import (
    CrossPlatformArbitrageStrategy,
    DEFAULT_CONFIG as CROSS_PLATFORM_CONFIG,
)
from app.strategies.favorite_compounder import (
    FavoriteCompounderStrategy,
    DEFAULT_CONFIG as FAVORITE_CONFIG,
)
from app.strategies.no_bias_exploit import (
    NoBiasExploitStrategy,
    DEFAULT_CONFIG as NO_BIAS_CONFIG,
)
from app.strategies.whale_copy_trading import (
    WhaleCopyTradingStrategy,
    DEFAULT_CONFIG as WHALE_COPY_CONFIG,
)


# Strategy registry mapping names to classes
STRATEGIES: dict[str, type[BaseStrategy]] = {
    "binary_complement_arbitrage": BinaryComplementArbitrageStrategy,
    "multi_outcome_bundle_arbitrage": MultiOutcomeBundleArbitrageStrategy,
    "catalyst_momentum": CatalystMomentumStrategy,
    "settlement_edge": SettlementEdgeStrategy,
    "term_structure_spreads": TermStructureSpreadsStrategy,
    "correlation_hedging": CorrelationHedgingStrategy,
    "cross_platform_arbitrage": CrossPlatformArbitrageStrategy,
    "favorite_compounder": FavoriteCompounderStrategy,
    "no_bias_exploit": NoBiasExploitStrategy,
    "whale_copy_trading": WhaleCopyTradingStrategy,
}

# Default configurations for each strategy
DEFAULT_CONFIGS: dict[str, dict[str, Any]] = {
    "binary_complement_arbitrage": BINARY_ARB_CONFIG,
    "multi_outcome_bundle_arbitrage": MULTI_OUTCOME_CONFIG,
    "catalyst_momentum": CATALYST_CONFIG,
    "settlement_edge": SETTLEMENT_CONFIG,
    "term_structure_spreads": TERM_STRUCTURE_CONFIG,
    "correlation_hedging": CORRELATION_CONFIG,
    "cross_platform_arbitrage": CROSS_PLATFORM_CONFIG,
    "favorite_compounder": FAVORITE_CONFIG,
    "no_bias_exploit": NO_BIAS_CONFIG,
    "whale_copy_trading": WHALE_COPY_CONFIG,
}

# Strategy categories for organization
STRATEGY_CATEGORIES: dict[str, list[str]] = {
    "arbitrage": [
        "binary_complement_arbitrage",
        "multi_outcome_bundle_arbitrage",
        "cross_platform_arbitrage",
    ],
    "momentum": [
        "catalyst_momentum",
    ],
    "mean_reversion": [
        "correlation_hedging",
        "term_structure_spreads",
    ],
    "edge": [
        "settlement_edge",
        "no_bias_exploit",
    ],
    "systematic": [
        "favorite_compounder",
    ],
    "copy_trading": [
        "whale_copy_trading",
    ],
}


def get_strategy(
    name: str,
    config: dict[str, Any] | None = None,
) -> BaseStrategy:
    """Factory function to create a strategy by name.

    Args:
        name: Strategy name (must be in STRATEGIES registry).
        config: Optional configuration to override defaults.

    Returns:
        Instantiated strategy object.

    Raises:
        ValueError: If strategy name is not found in registry.

    Example:
        >>> strategy = get_strategy("binary_complement_arbitrage")
        >>> strategy = get_strategy("catalyst_momentum", {"min_price_change": 0.08})
    """
    if name not in STRATEGIES:
        available = ", ".join(sorted(STRATEGIES.keys()))
        raise ValueError(
            f"Unknown strategy: '{name}'. Available strategies: {available}"
        )

    strategy_class = STRATEGIES[name]
    return strategy_class(config)


def get_default_config(name: str) -> dict[str, Any]:
    """Get the default configuration for a strategy.

    Args:
        name: Strategy name.

    Returns:
        Default configuration dictionary.

    Raises:
        ValueError: If strategy name is not found.
    """
    if name not in DEFAULT_CONFIGS:
        available = ", ".join(sorted(DEFAULT_CONFIGS.keys()))
        raise ValueError(
            f"Unknown strategy: '{name}'. Available strategies: {available}"
        )

    return DEFAULT_CONFIGS[name].copy()


def list_strategies() -> list[dict[str, Any]]:
    """List all available strategies with metadata.

    Returns:
        List of strategy info dictionaries.
    """
    strategies = []

    for name, strategy_class in STRATEGIES.items():
        # Find category
        category = "other"
        for cat, strats in STRATEGY_CATEGORIES.items():
            if name in strats:
                category = cat
                break

        strategies.append({
            "name": name,
            "class": strategy_class.__name__,
            "description": strategy_class.description,
            "version": strategy_class.version,
            "category": category,
        })

    return strategies


def get_strategies_by_category(category: str) -> list[str]:
    """Get strategy names in a category.

    Args:
        category: Category name.

    Returns:
        List of strategy names.
    """
    return STRATEGY_CATEGORIES.get(category, [])


__all__ = [
    # Base classes
    "BaseStrategy",
    "MarketSnapshot",
    "Signal",
    "SignalType",
    # Strategy classes
    "BinaryComplementArbitrageStrategy",
    "MultiOutcomeBundleArbitrageStrategy",
    "CatalystMomentumStrategy",
    "SettlementEdgeStrategy",
    "TermStructureSpreadsStrategy",
    "CorrelationHedgingStrategy",
    "CrossPlatformArbitrageStrategy",
    "FavoriteCompounderStrategy",
    "NoBiasExploitStrategy",
    "WhaleCopyTradingStrategy",
    # Registry and factory
    "STRATEGIES",
    "DEFAULT_CONFIGS",
    "STRATEGY_CATEGORIES",
    "get_strategy",
    "get_default_config",
    "list_strategies",
    "get_strategies_by_category",
]
