"""Polymarket CLOB API client wrapper."""
from typing import Any

import httpx
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds

from app.config import settings


class ClobClientWrapper:
    """Wrapper for Polymarket CLOB client with async support."""

    def __init__(self) -> None:
        """Initialize the CLOB client wrapper."""
        self._client: ClobClient | None = None
        self._http_client: httpx.AsyncClient | None = None

    async def initialize(self) -> None:
        """Initialize the CLOB client with credentials."""
        private_key = settings.polymarket_private_key.get_secret_value()
        if not private_key:
            raise ValueError("POLYMARKET_PRIVATE_KEY is required")

        self._client = ClobClient(
            host=settings.clob_api_url,
            key=private_key,
            chain_id=settings.chain_id,
            signature_type=1,  # For email/Magic wallet
            funder=settings.polymarket_funder_address or None,
        )

        # Set API credentials if available
        api_key = settings.polymarket_api_key.get_secret_value()
        api_secret = settings.polymarket_api_secret.get_secret_value()
        api_passphrase = settings.polymarket_api_passphrase.get_secret_value()

        if api_key and api_secret and api_passphrase:
            self._client.set_api_creds(
                ApiCreds(
                    api_key=api_key,
                    api_secret=api_secret,
                    api_passphrase=api_passphrase,
                )
            )
        else:
            # Derive API credentials
            self._client.set_api_creds(self._client.create_or_derive_api_creds())

        self._http_client = httpx.AsyncClient(
            base_url=settings.clob_api_url,
            timeout=30.0,
        )

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._http_client:
            await self._http_client.aclose()

    @property
    def client(self) -> ClobClient:
        """Get the underlying CLOB client."""
        if not self._client:
            raise RuntimeError("Client not initialized. Call initialize() first.")
        return self._client

    async def get_markets(self, next_cursor: str = "") -> dict[str, Any]:
        """Get all markets.

        Args:
            next_cursor: Pagination cursor.

        Returns:
            dict: Markets response with data and next_cursor.
        """
        return self.client.get_markets(next_cursor=next_cursor)

    async def get_market(self, condition_id: str) -> dict[str, Any]:
        """Get a specific market.

        Args:
            condition_id: Market condition ID.

        Returns:
            dict: Market data.
        """
        return self.client.get_market(condition_id=condition_id)

    async def get_orderbook(self, token_id: str) -> dict[str, Any]:
        """Get orderbook for a token.

        Args:
            token_id: Token ID.

        Returns:
            dict: Orderbook with bids and asks.
        """
        return self.client.get_order_book(token_id=token_id)

    async def get_price(self, token_id: str, side: str) -> float:
        """Get current price for a token.

        Args:
            token_id: Token ID.
            side: BUY or SELL.

        Returns:
            float: Current price.
        """
        return self.client.get_price(token_id=token_id, side=side)

    async def create_order(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str,
        order_type: str = "GTC",
    ) -> dict[str, Any]:
        """Create and sign an order.

        Args:
            token_id: Token ID.
            price: Order price.
            size: Order size.
            side: BUY or SELL.
            order_type: Order type (GTC, GTD, FOK).

        Returns:
            dict: Signed order ready for submission.
        """
        order = self.client.create_order(
            token_id=token_id,
            price=price,
            size=size,
            side=side,
        )
        return order

    async def post_order(self, order: dict[str, Any]) -> dict[str, Any]:
        """Submit a signed order.

        Args:
            order: Signed order from create_order.

        Returns:
            dict: Order response with order_id.
        """
        return self.client.post_order(order)

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        """Cancel an order.

        Args:
            order_id: Order ID to cancel.

        Returns:
            dict: Cancellation response.
        """
        return self.client.cancel(order_id=order_id)

    async def get_orders(self, market: str | None = None) -> list[dict[str, Any]]:
        """Get open orders.

        Args:
            market: Optional market filter.

        Returns:
            list: Open orders.
        """
        return self.client.get_orders(market=market)

    async def get_trades(
        self,
        market: str | None = None,
        maker_address: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get trade history.

        Args:
            market: Optional market filter.
            maker_address: Optional maker address filter.

        Returns:
            list: Trade history.
        """
        return self.client.get_trades(market=market, maker_address=maker_address)


# Global client instance
clob_client = ClobClientWrapper()


async def get_clob_client() -> ClobClientWrapper:
    """Get the CLOB client dependency.

    Returns:
        ClobClientWrapper: Initialized CLOB client.
    """
    if not clob_client._client:
        await clob_client.initialize()
    return clob_client
