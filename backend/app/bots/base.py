"""Base bot class."""
import asyncio
import contextlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from app.strategies.base import BaseStrategy, Signal, SignalType


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
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

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

            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.config.trading_interval,
                )

    @abstractmethod
    async def _tick(self) -> None:
        """Execute one trading cycle."""
        pass

    async def execute_signal(self, signal: Signal) -> bool:
        """Execute a trading signal by routing it through `OrderRouter`.

        Normalizes the one-leg `signal` into an `Intent`
        (`Signal.to_intent()`, PLAN.md D7 back-compat) and submits it
        through the SAME app-scoped `OrderRouter` the API layer uses
        (`app.api.deps.get_router`) — the only path from an intent to a
        venue order (PLAN.md D4). A bot never calls
        `adapter.place_order`/`cancel_order` directly (GUARDRAILS.md
        §1.1); `tests/test_fences.py` confines both to `app/execution/`
        and `app/venues/`, and `app/bots/` is neither.

        The `get_router` import below is deliberately LOCAL to this
        method, not at module scope: `app.api.deps` sits behind
        `app.api`'s package `__init__`, which imports the entire FastAPI
        route tree (`app/api/routes/*.py`) as a side effect of being
        imported at all. That is the same reason
        `app/venues/registry.py` defers ITS adapter imports to
        first-call time rather than module scope — a bot module that
        only ever computes signals (under test, or inside a Celery
        worker that never serves HTTP) should not have to pay for that
        import merely by being imported.

        Args:
            signal: Signal to execute. A `HOLD` signal is not an order
                (`Signal.to_intent()` has no representation for one) and
                is reported as unsuccessful without reaching the router.

        Returns:
            bool: `True` if the intent was not rejected before
                placement (`RoutedIntent.status` is `"pending"` or
                `"executed"`) — i.e. at least one attempt reached the
                venue. `False` if a pre-flight check (risk limits,
                capital) rejected it, if everything placed and nothing
                filled (`"expired"`), or if `signal` was `HOLD`.
        """
        if signal.type == SignalType.HOLD:
            return False

        from app.api.deps import get_router

        intent = signal.to_intent()
        order_router = await get_router()
        routed = await order_router.submit(intent, strategy=self.config.strategy.name)
        return routed.status in ("pending", "executed")

    def can_trade(self) -> bool:
        """Check if bot can execute trades.

        Returns:
            bool: True if trading is allowed.
        """
        if not self.config.enabled:
            return False
        if self.state.trades_today >= self.config.max_daily_trades:
            return False
        return abs(self.state.pnl_today) < self.config.max_daily_loss

    def reset_daily_stats(self) -> None:
        """Reset daily statistics."""
        self.state.trades_today = 0
        self.state.pnl_today = 0.0
