"""Bot manager for running multiple bots."""
import asyncio
from typing import Any

from app.bots.base import BaseBot, BotStatus


class BotManager:
    """Manager for running and monitoring trading bots."""

    def __init__(self) -> None:
        """Initialize the bot manager."""
        self._bots: dict[str, BaseBot] = {}
        self._lock = asyncio.Lock()

    @property
    def bots(self) -> dict[str, BaseBot]:
        """Get all registered bots."""
        return self._bots.copy()

    async def register_bot(self, bot_id: str, bot: BaseBot) -> None:
        """Register a bot with the manager.

        Args:
            bot_id: Unique bot identifier.
            bot: Bot instance.
        """
        async with self._lock:
            if bot_id in self._bots:
                raise ValueError(f"Bot {bot_id} already registered")
            self._bots[bot_id] = bot

    async def unregister_bot(self, bot_id: str) -> None:
        """Unregister a bot from the manager.

        Args:
            bot_id: Bot identifier to remove.
        """
        async with self._lock:
            if bot_id not in self._bots:
                raise ValueError(f"Bot {bot_id} not found")
            bot = self._bots[bot_id]
            if bot.is_running:
                await bot.stop()
            del self._bots[bot_id]

    async def start_bot(self, bot_id: str) -> None:
        """Start a specific bot.

        Args:
            bot_id: Bot identifier.
        """
        bot = self._bots.get(bot_id)
        if not bot:
            raise ValueError(f"Bot {bot_id} not found")
        await bot.start()

    async def stop_bot(self, bot_id: str) -> None:
        """Stop a specific bot.

        Args:
            bot_id: Bot identifier.
        """
        bot = self._bots.get(bot_id)
        if not bot:
            raise ValueError(f"Bot {bot_id} not found")
        await bot.stop()

    async def start_all(self) -> dict[str, bool]:
        """Start all registered bots.

        Returns:
            dict: Bot ID to success status mapping.
        """
        results = {}
        for bot_id, bot in self._bots.items():
            try:
                await bot.start()
                results[bot_id] = True
            except Exception as e:
                results[bot_id] = False
                print(f"Failed to start bot {bot_id}: {e}")
        return results

    async def stop_all(self) -> dict[str, bool]:
        """Stop all running bots.

        Returns:
            dict: Bot ID to success status mapping.
        """
        results = {}
        for bot_id, bot in self._bots.items():
            try:
                await bot.stop()
                results[bot_id] = True
            except Exception as e:
                results[bot_id] = False
                print(f"Failed to stop bot {bot_id}: {e}")
        return results

    def get_bot(self, bot_id: str) -> BaseBot | None:
        """Get a bot by ID.

        Args:
            bot_id: Bot identifier.

        Returns:
            BaseBot: Bot instance or None if not found.
        """
        return self._bots.get(bot_id)

    def get_status(self, bot_id: str) -> dict[str, Any]:
        """Get status for a specific bot.

        Args:
            bot_id: Bot identifier.

        Returns:
            dict: Bot status information.
        """
        bot = self._bots.get(bot_id)
        if not bot:
            return {"error": "Bot not found"}

        return {
            "bot_id": bot_id,
            "name": bot.name,
            "status": bot.status.value,
            "trades_today": bot.state.trades_today,
            "pnl_today": bot.state.pnl_today,
            "last_trade_at": bot.state.last_trade_at,
            "last_error": bot.state.last_error,
            "started_at": bot.state.started_at,
            "stopped_at": bot.state.stopped_at,
        }

    def get_all_status(self) -> list[dict[str, Any]]:
        """Get status for all bots.

        Returns:
            list: Status information for all bots.
        """
        return [self.get_status(bot_id) for bot_id in self._bots]

    def get_running_bots(self) -> list[str]:
        """Get IDs of all running bots.

        Returns:
            list: Bot IDs that are currently running.
        """
        return [
            bot_id
            for bot_id, bot in self._bots.items()
            if bot.status == BotStatus.RUNNING
        ]


# Global bot manager instance
bot_manager = BotManager()
