"""Base bot class."""
import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from app.strategies.base import BaseStrategy, Signal


class BotStatus(str, Enum):
    """Bot status enum."""

    CREATED = "created"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass
class BotConfig:
    """Bot configuration."""

    name: str
    strategy: BaseStrategy
    max_position_size: float = 1000.0
    max_daily_trades: int = 100
    max_daily_loss: float = 100.0
    trading_interval: float = 60.0  # seconds
    enabled: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class BotState:
    """Bot runtime state."""

    status: BotStatus = BotStatus.CREATED
    trades_today: int = 0
    pnl_today: float = 0.0
    last_trade_at: datetime | None = None
    last_error: str | None = None
    started_at: datetime | None = None
    stopped_at: datetime | None = None


class BaseBot(ABC):
    """Abstract base class for trading bots."""

    def __init__(self, config: BotConfig) -> None:
        """Initialize the bot.

        Args:
            config: Bot configuration.
        """
        self.config = config
        self.state = BotState()
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    @property
    def name(self) -> str:
        """Get bot name."""
        return self.config.name

    @property
    def status(self) -> BotStatus:
        """Get bot status."""
        return self.state.status

    @property
    def is_running(self) -> bool:
        """Check if bot is running."""
        return self.state.status == BotStatus.RUNNING

    async def start(self) -> None:
        """Start the bot."""
        if self.is_running:
            return

        self.state.status = BotStatus.STARTING
        self._stop_event.clear()

        try:
            await self.config.strategy.initialize()
            self.state.status = BotStatus.RUNNING
            self.state.started_at = datetime.utcnow()
            self._task = asyncio.create_task(self._run_loop())
        except Exception as e:
            self.state.status = BotStatus.ERROR
            self.state.last_error = str(e)
            raise

    async def stop(self) -> None:
        """Stop the bot."""
        if not self.is_running:
            return

        self.state.status = BotStatus.STOPPING
        self._stop_event.set()

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        await self.config.strategy.cleanup()
        self.state.status = BotStatus.STOPPED
        self.state.stopped_at = datetime.utcnow()

    async def _run_loop(self) -> None:
        """Main bot loop."""
        while not self._stop_event.is_set():
            try:
                await self._tick()
            except Exception as e:
                self.state.last_error = str(e)
                # Log error but continue running

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.config.trading_interval,
                )
            except asyncio.TimeoutError:
                pass

    @abstractmethod
    async def _tick(self) -> None:
        """Execute one trading cycle."""
        pass

    @abstractmethod
    async def execute_signal(self, signal: Signal) -> bool:
        """Execute a trading signal.

        Args:
            signal: Signal to execute.

        Returns:
            bool: True if execution was successful.
        """
        pass

    def can_trade(self) -> bool:
        """Check if bot can execute trades.

        Returns:
            bool: True if trading is allowed.
        """
        if not self.config.enabled:
            return False
        if self.state.trades_today >= self.config.max_daily_trades:
            return False
        if abs(self.state.pnl_today) >= self.config.max_daily_loss:
            return False
        return True

    def reset_daily_stats(self) -> None:
        """Reset daily statistics."""
        self.state.trades_today = 0
        self.state.pnl_today = 0.0
