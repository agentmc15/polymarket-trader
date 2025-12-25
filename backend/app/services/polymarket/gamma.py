"""Polymarket Gamma API client for market metadata."""
from typing import Any

import httpx

from app.config import settings


class GammaApiClient:
    """Client for Polymarket Gamma API (market metadata)."""

    def __init__(self) -> None:
        """Initialize the Gamma API client."""
        self._client: httpx.AsyncClient | None = None

    async def initialize(self) -> None:
        """Initialize the HTTP client."""
        self._client = httpx.AsyncClient(
            base_url=settings.gamma_api_url,
            timeout=30.0,
        )

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client:
            await self._client.aclose()

    @property
    def client(self) -> httpx.AsyncClient:
        """Get the HTTP client."""
        if not self._client:
            raise RuntimeError("Client not initialized. Call initialize() first.")
        return self._client

    async def get_markets(
        self,
        limit: int = 100,
        offset: int = 0,
        active: bool = True,
        closed: bool = False,
    ) -> list[dict[str, Any]]:
        """Get markets from Gamma API.

        Args:
            limit: Maximum number of markets to return.
            offset: Pagination offset.
            active: Include active markets.
            closed: Include closed markets.

        Returns:
            list: Market data.
        """
        params = {
            "limit": limit,
            "offset": offset,
            "active": str(active).lower(),
            "closed": str(closed).lower(),
        }
        response = await self.client.get("/markets", params=params)
        response.raise_for_status()
        return response.json()

    async def get_market(self, condition_id: str) -> dict[str, Any]:
        """Get a specific market.

        Args:
            condition_id: Market condition ID.

        Returns:
            dict: Market data.
        """
        response = await self.client.get(f"/markets/{condition_id}")
        response.raise_for_status()
        return response.json()

    async def get_events(
        self,
        limit: int = 100,
        offset: int = 0,
        active: bool = True,
    ) -> list[dict[str, Any]]:
        """Get events (market groups).

        Args:
            limit: Maximum number of events to return.
            offset: Pagination offset.
            active: Include active events only.

        Returns:
            list: Event data.
        """
        params = {
            "limit": limit,
            "offset": offset,
            "active": str(active).lower(),
        }
        response = await self.client.get("/events", params=params)
        response.raise_for_status()
        return response.json()

    async def get_event(self, event_id: str) -> dict[str, Any]:
        """Get a specific event.

        Args:
            event_id: Event ID.

        Returns:
            dict: Event data with associated markets.
        """
        response = await self.client.get(f"/events/{event_id}")
        response.raise_for_status()
        return response.json()

    async def search_markets(
        self,
        query: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Search markets by query string.

        Args:
            query: Search query.
            limit: Maximum results.

        Returns:
            list: Matching markets.
        """
        params = {"query": query, "limit": limit}
        response = await self.client.get("/markets/search", params=params)
        response.raise_for_status()
        return response.json()


# Global client instance
gamma_client = GammaApiClient()


async def get_gamma_client() -> GammaApiClient:
    """Get the Gamma API client dependency.

    Returns:
        GammaApiClient: Initialized Gamma client.
    """
    if not gamma_client._client:
        await gamma_client.initialize()
    return gamma_client
