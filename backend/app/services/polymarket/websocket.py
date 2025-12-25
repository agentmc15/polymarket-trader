"""Polymarket WebSocket client for real-time data."""
import asyncio
import json
from collections.abc import Callable
from typing import Any

import websockets
from websockets.client import WebSocketClientProtocol

from app.config import settings


class PolymarketWebSocket:
    """WebSocket client for real-time Polymarket data."""

    WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws"

    def __init__(self) -> None:
        """Initialize the WebSocket client."""
        self._ws: WebSocketClientProtocol | None = None
        self._subscriptions: dict[str, set[str]] = {}
        self._callbacks: dict[str, list[Callable]] = {}
        self._running = False
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 60.0

    async def connect(self) -> None:
        """Connect to the WebSocket server."""
        self._running = True
        await self._connect_with_retry()

    async def _connect_with_retry(self) -> None:
        """Connect with automatic retry on failure."""
        while self._running:
            try:
                self._ws = await websockets.connect(
                    self.WS_URL,
                    ping_interval=30,
                    ping_timeout=10,
                )
                self._reconnect_delay = 1.0
                await self._resubscribe()
                await self._listen()
            except Exception as e:
                if self._running:
                    print(f"WebSocket connection error: {e}")
                    await asyncio.sleep(self._reconnect_delay)
                    self._reconnect_delay = min(
                        self._reconnect_delay * 2,
                        self._max_reconnect_delay,
                    )

    async def _listen(self) -> None:
        """Listen for incoming messages."""
        if not self._ws:
            return

        async for message in self._ws:
            try:
                data = json.loads(message)
                await self._handle_message(data)
            except json.JSONDecodeError:
                print(f"Invalid JSON message: {message}")
            except Exception as e:
                print(f"Error handling message: {e}")

    async def _handle_message(self, data: dict[str, Any]) -> None:
        """Handle incoming WebSocket message.

        Args:
            data: Parsed message data.
        """
        msg_type = data.get("type", "")
        channel = data.get("channel", "")

        key = f"{msg_type}:{channel}" if channel else msg_type
        callbacks = self._callbacks.get(key, [])

        for callback in callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(data)
                else:
                    callback(data)
            except Exception as e:
                print(f"Callback error: {e}")

    async def _resubscribe(self) -> None:
        """Resubscribe to all channels after reconnect."""
        for channel, assets in self._subscriptions.items():
            for asset in assets:
                await self._send_subscription(channel, asset, subscribe=True)

    async def _send_subscription(
        self,
        channel: str,
        asset_id: str,
        subscribe: bool = True,
    ) -> None:
        """Send subscription message.

        Args:
            channel: Channel name (e.g., "market", "user").
            asset_id: Asset ID to subscribe to.
            subscribe: True to subscribe, False to unsubscribe.
        """
        if not self._ws:
            return

        message = {
            "type": "subscribe" if subscribe else "unsubscribe",
            "channel": channel,
            "assets_ids": [asset_id],
        }
        await self._ws.send(json.dumps(message))

    async def subscribe_market(
        self,
        token_id: str,
        callback: Callable[[dict[str, Any]], None],
    ) -> None:
        """Subscribe to market updates.

        Args:
            token_id: Token ID to subscribe to.
            callback: Callback function for updates.
        """
        channel = "market"
        key = f"price_change:{channel}"

        if channel not in self._subscriptions:
            self._subscriptions[channel] = set()
        self._subscriptions[channel].add(token_id)

        if key not in self._callbacks:
            self._callbacks[key] = []
        self._callbacks[key].append(callback)

        if self._ws:
            await self._send_subscription(channel, token_id)

    async def subscribe_orderbook(
        self,
        token_id: str,
        callback: Callable[[dict[str, Any]], None],
    ) -> None:
        """Subscribe to orderbook updates.

        Args:
            token_id: Token ID to subscribe to.
            callback: Callback function for updates.
        """
        channel = "book"
        key = f"book:{channel}"

        if channel not in self._subscriptions:
            self._subscriptions[channel] = set()
        self._subscriptions[channel].add(token_id)

        if key not in self._callbacks:
            self._callbacks[key] = []
        self._callbacks[key].append(callback)

        if self._ws:
            await self._send_subscription(channel, token_id)

    async def unsubscribe(self, channel: str, token_id: str) -> None:
        """Unsubscribe from a channel.

        Args:
            channel: Channel name.
            token_id: Token ID to unsubscribe from.
        """
        if channel in self._subscriptions:
            self._subscriptions[channel].discard(token_id)

        if self._ws:
            await self._send_subscription(channel, token_id, subscribe=False)

    async def disconnect(self) -> None:
        """Disconnect from the WebSocket server."""
        self._running = False
        if self._ws:
            await self._ws.close()
            self._ws = None


# Global WebSocket instance
ws_client = PolymarketWebSocket()
