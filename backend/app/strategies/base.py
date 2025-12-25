"""Base strategy interface."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Signal:
    """Trading signal from a strategy."""

    market_id: str
    token_id: str
    side: str  # BUY or SELL
    strength: float  # 0.0 to 1.0
    price: float | None = None
    size: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class StrategyContext:
    """Context passed to strategy for decision making."""

    current_time: datetime
    positions: dict[str, Any]
    capital: float
    market_data: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)


class BaseStrategy(ABC):
    """Abstract base class for all trading strategies."""

    name: str = "base"
    description: str = "Base strategy interface"

    def __init__(self, parameters: dict[str, Any] | None = None) -> None:
        """Initialize the strategy.

        Args:
            parameters: Strategy-specific parameters.
        """
        self.parameters = parameters or {}
        self._is_initialized = False

    @property
    def is_initialized(self) -> bool:
        """Check if strategy is initialized."""
        return self._is_initialized

    async def initialize(self) -> None:
        """Initialize the strategy (load models, historical data, etc.)."""
        self._is_initialized = True

    async def cleanup(self) -> None:
        """Clean up resources."""
        self._is_initialized = False

    @abstractmethod
    async def generate_signals(self, context: StrategyContext) -> list[Signal]:
        """Generate trading signals based on current context.

        Args:
            context: Current market context and portfolio state.

        Returns:
            list: Trading signals to execute.
        """
        pass

    @abstractmethod
    async def should_exit(
        self,
        position: dict[str, Any],
        context: StrategyContext,
    ) -> Signal | None:
        """Determine if a position should be exited.

        Args:
            position: Current position.
            context: Current market context.

        Returns:
            Signal: Exit signal if position should be closed, None otherwise.
        """
        pass

    def validate_parameters(self) -> bool:
        """Validate strategy parameters.

        Returns:
            bool: True if parameters are valid.
        """
        return True

    def get_default_parameters(self) -> dict[str, Any]:
        """Get default parameter values.

        Returns:
            dict: Default parameters.
        """
        return {}

    def update_parameters(self, parameters: dict[str, Any]) -> None:
        """Update strategy parameters.

        Args:
            parameters: New parameters to apply.
        """
        self.parameters.update(parameters)

    def __repr__(self) -> str:
        """String representation."""
        return f"{self.__class__.__name__}(name={self.name}, params={self.parameters})"
