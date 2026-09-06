"""Data collection service for Polymarket market data.

Provides clients for fetching data from Polymarket's various APIs
and methods for syncing data to the database.

`collect_books` (T21, PLAN.md D6/D10) is the one method on this class
that talks to a venue through the `app.venues.base.MarketDataAdapter`
seam rather than through `GammaAPIClient`/`CLOBDataClient` — it is the
only method here that runs on BOTH venues, since `BookSnapshot` depth is
needed on both Polymarket and Kalshi for a recorded-book backtest.
"""
import asyncio
import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.book_snapshot import BookSnapshot
from app.models.market import Market
from app.models.price_history import PriceHistory
from app.models.trade_history import TradeHistory, TradeOutcome, TradeSide
from app.strategies.base import outcome_key
from app.venues.base import MarketDataAdapter, VenueError
from app.venues.types import OrderBook, VenueId, VenueMarket

logger = logging.getLogger(__name__)


# API Base URLs
GAMMA_API_BASE = "https://gamma-api.polymarket.com"
CLOB_API_BASE = "https://clob.polymarket.com"
DATA_API_BASE = "https://data-api.polymarket.com"


def _book_collection_volume(market: VenueMarket) -> float:
    """Return the best available 24h-volume proxy for `collect_books` ranking.

    Mirrors `app.services.scanner._volume` exactly: `VenueMarket` has no
    normalized `volume_24h` field, but both real venues' raw payloads
    carry a `"volume"` key (`tests/fixtures/polymarket/gamma_markets.json`,
    `tests/fixtures/kalshi/markets.json`), so this reads
    `VenueMarket.raw["volume"]` rather than inventing a second, competing
    ranking key for the same "top-N markets by volume" purpose.

    Args:
        market: The market to rank.

    Returns:
        float: `market.raw["volume"]` coerced to `float`; `0.0` if the
            key is absent or not numeric.
    """
    raw_volume = market.raw.get("volume")
    try:
        return float(raw_volume)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


class GammaAPIClient:
    """Client for Polymarket Gamma API (market metadata).

    The Gamma API provides market information, events, and metadata.
    It does not require authentication.

    Endpoints:
        GET /markets - List all markets
        GET /markets/{condition_id} - Get market details
        GET /events - List events
        GET /events/{event_id} - Get event details
    """

    def __init__(
        self,
        base_url: str = GAMMA_API_BASE,
        timeout: float = 30.0,
    ) -> None:
        """Initialize the Gamma API client.

        Args:
            base_url: API base URL.
            timeout: Request timeout in seconds.
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "PolymarketTrader/1.0",
                },
            )
        return self._client

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def get_markets(
        self,
        limit: int = 100,
        offset: int = 0,
        active: bool | None = None,
        closed: bool | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch markets from Gamma API.

        Args:
            limit: Maximum markets to return.
            offset: Pagination offset.
            active: Filter for active markets.
            closed: Filter for closed markets.

        Returns:
            List of market dictionaries.
        """
        client = await self._get_client()

        params: dict[str, Any] = {
            "limit": limit,
            "offset": offset,
        }

        if active is not None:
            params["active"] = str(active).lower()
        if closed is not None:
            params["closed"] = str(closed).lower()

        try:
            response = await client.get("/markets", params=params)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            logger.error(f"Failed to fetch markets: {e}")
            raise

    async def get_market(self, condition_id: str) -> dict[str, Any] | None:
        """Fetch a single market by condition ID.

        Args:
            condition_id: Market condition ID.

        Returns:
            Market data or None if not found.
        """
        client = await self._get_client()

        try:
            response = await client.get(f"/markets/{condition_id}")
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            logger.error(f"Failed to fetch market {condition_id}: {e}")
            raise

    async def get_events(
        self,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Fetch events from Gamma API.

        Args:
            limit: Maximum events to return.
            offset: Pagination offset.

        Returns:
            List of event dictionaries.
        """
        client = await self._get_client()

        try:
            response = await client.get(
                "/events",
                params={"limit": limit, "offset": offset},
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            logger.error(f"Failed to fetch events: {e}")
            raise

    async def get_all_markets(self, batch_size: int = 100) -> list[dict[str, Any]]:
        """Fetch all markets with pagination.

        Args:
            batch_size: Markets per request.

        Returns:
            List of all markets.
        """
        all_markets = []
        offset = 0

        while True:
            markets = await self.get_markets(limit=batch_size, offset=offset)
            if not markets:
                break

            all_markets.extend(markets)
            offset += len(markets)

            if len(markets) < batch_size:
                break

            # Rate limiting
            await asyncio.sleep(0.1)

        return all_markets


class CLOBDataClient:
    """Client for Polymarket CLOB API (orderbook and prices).

    The CLOB (Central Limit Order Book) API provides real-time
    pricing and orderbook data.

    Endpoints:
        GET /prices - Get current prices
        GET /book - Get orderbook
        GET /trades - Get recent trades
        GET /markets - Get market info
    """

    def __init__(
        self,
        base_url: str = CLOB_API_BASE,
        timeout: float = 30.0,
        api_key: str | None = None,
    ) -> None:
        """Initialize the CLOB API client.

        Args:
            base_url: API base URL.
            timeout: Request timeout in seconds.
            api_key: Optional API key for authenticated endpoints.
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.api_key = api_key
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._client is None or self._client.is_closed:
            headers = {
                "Accept": "application/json",
                "User-Agent": "PolymarketTrader/1.0",
            }
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"

            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                headers=headers,
            )
        return self._client

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def get_price(self, token_id: str) -> dict[str, Any] | None:
        """Get current price for a token.

        Args:
            token_id: Token ID (YES or NO token).

        Returns:
            Price data or None.
        """
        client = await self._get_client()

        try:
            response = await client.get("/price", params={"token_id": token_id})
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            logger.error(f"Failed to fetch price for {token_id}: {e}")
            return None

    async def get_prices(self, token_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Get prices for multiple tokens.

        Args:
            token_ids: List of token IDs.

        Returns:
            Dict mapping token_id to price data.
        """
        client = await self._get_client()

        try:
            response = await client.get(
                "/prices",
                params={"token_ids": ",".join(token_ids)},
            )
            response.raise_for_status()
            data = response.json()

            # Convert list to dict
            if isinstance(data, list):
                return {item.get("token_id", ""): item for item in data}
            return data
        except httpx.HTTPError as e:
            logger.error(f"Failed to fetch prices: {e}")
            return {}

    async def get_orderbook(
        self,
        token_id: str,
        depth: int = 10,
    ) -> dict[str, Any]:
        """Get orderbook for a token.

        Args:
            token_id: Token ID.
            depth: Number of levels to fetch.

        Returns:
            Orderbook with bids and asks.
        """
        client = await self._get_client()

        try:
            response = await client.get(
                "/book",
                params={"token_id": token_id, "depth": depth},
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            logger.error(f"Failed to fetch orderbook for {token_id}: {e}")
            return {"bids": [], "asks": []}

    async def get_midpoint(self, token_id: str) -> float | None:
        """Get midpoint price for a token.

        Args:
            token_id: Token ID.

        Returns:
            Midpoint price or None.
        """
        client = await self._get_client()

        try:
            response = await client.get(
                "/midpoint",
                params={"token_id": token_id},
            )
            response.raise_for_status()
            data = response.json()
            return float(data.get("mid", 0))
        except httpx.HTTPError as e:
            logger.debug(f"Failed to fetch midpoint for {token_id}: {e}")
            return None

    async def get_spread(self, token_id: str) -> dict[str, float]:
        """Get bid-ask spread for a token.

        Args:
            token_id: Token ID.

        Returns:
            Dict with bid, ask, and spread.
        """
        client = await self._get_client()

        try:
            response = await client.get(
                "/spread",
                params={"token_id": token_id},
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            logger.debug(f"Failed to fetch spread for {token_id}: {e}")
            return {"bid": 0, "ask": 0, "spread": 0}

    async def get_trades(
        self,
        token_id: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Get recent trades for a token.

        Args:
            token_id: Token ID.
            limit: Maximum trades to return.

        Returns:
            List of trade dictionaries.
        """
        client = await self._get_client()

        try:
            response = await client.get(
                "/trades",
                params={"token_id": token_id, "limit": limit},
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            logger.error(f"Failed to fetch trades for {token_id}: {e}")
            return []

    async def get_market_info(self, condition_id: str) -> dict[str, Any] | None:
        """Get market info from CLOB.

        Args:
            condition_id: Market condition ID.

        Returns:
            Market info or None.
        """
        client = await self._get_client()

        try:
            response = await client.get(f"/markets/{condition_id}")
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            logger.error(f"Failed to fetch market info for {condition_id}: {e}")
            return None


class PolymarketDataClient:
    """Client for Polymarket Data API (public trade tape).

    The Data API provides historical trade data for markets. Per-wallet
    endpoints (ranked wallet standings, individual wallet profile/trades/
    positions) supporting copy-trading were removed deliberately: see the
    `DataCollector` docstring below.

    Endpoints:
        GET /markets/{id}/trades - Market trade history
    """

    def __init__(
        self,
        base_url: str = DATA_API_BASE,
        timeout: float = 30.0,
    ) -> None:
        """Initialize the Data API client.

        Args:
            base_url: API base URL.
            timeout: Request timeout in seconds.
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "PolymarketTrader/1.0",
                },
            )
        return self._client

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def get_market_trades(
        self,
        market_id: str,
        limit: int = 100,
        offset: int = 0,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch market trade history.

        Args:
            market_id: Market condition ID.
            limit: Maximum trades to return.
            offset: Pagination offset.
            start_time: Filter trades after this time.
            end_time: Filter trades before this time.

        Returns:
            List of trade dictionaries.
        """
        client = await self._get_client()

        params: dict[str, Any] = {
            "limit": limit,
            "offset": offset,
        }

        if start_time:
            params["start_time"] = start_time.isoformat()
        if end_time:
            params["end_time"] = end_time.isoformat()

        try:
            response = await client.get(
                f"/markets/{market_id}/trades",
                params=params,
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            logger.error(f"Failed to fetch trades for market {market_id}: {e}")
            return []


class DataCollector:
    """Orchestrates data collection from all Polymarket APIs.

    Provides high-level methods for syncing market data and collecting
    price snapshots. Copy-trading support (mirroring a tracked wallet's
    trades, sourced from a ranked-standings feed of past performance) was
    removed deliberately: on prediction markets a single wallet's realized
    trades are too few, too correlated (same events, same news), and too
    survivorship-selected (a ranked-standings view shows winners after
    the fact) to distinguish skill from variance, and a copier also pays
    the latency and slippage the leader did not. `TradeHistory` (the
    public trade tape) is unaffected and remains in use by replay/backfill.

    Example:
        ```python
        async with get_session_context() as session:
            collector = DataCollector(session)
            await collector.sync_markets()
            await collector.collect_all_prices()
        ```
    """

    def __init__(
        self,
        session: AsyncSession,
        gamma_client: GammaAPIClient | None = None,
        clob_client: CLOBDataClient | None = None,
        data_client: PolymarketDataClient | None = None,
    ) -> None:
        """Initialize the data collector.

        Args:
            session: Database session.
            gamma_client: Optional Gamma API client.
            clob_client: Optional CLOB API client.
            data_client: Optional Data API client.
        """
        self.session = session
        self.gamma = gamma_client or GammaAPIClient()
        self.clob = clob_client or CLOBDataClient()
        self.data = data_client or PolymarketDataClient()

    async def close(self) -> None:
        """Close all API clients."""
        await self.gamma.close()
        await self.clob.close()
        await self.data.close()

    async def sync_markets(self, active_only: bool = True) -> int:
        """Sync all markets from Gamma API to database.

        Args:
            active_only: Only sync active markets.

        Returns:
            Number of markets synced.
        """
        logger.info("Starting market sync...")

        markets = await self.gamma.get_all_markets()

        if active_only:
            markets = [m for m in markets if m.get("active", False)]

        synced = 0

        for market_data in markets:
            try:
                await self._upsert_market(market_data)
                synced += 1
            except Exception as e:
                logger.error(f"Failed to sync market {market_data.get('condition_id')}: {e}")

        await self.session.commit()
        logger.info(f"Synced {synced} markets")

        return synced

    async def _upsert_market(self, data: dict[str, Any]) -> None:
        """Insert or update a market in the database.

        Args:
            data: Market data from API.
        """
        condition_id = data.get("condition_id") or data.get("conditionId")
        if not condition_id:
            return

        # Extract token IDs
        tokens = data.get("tokens", [])
        token_ids = {}
        outcomes = []

        for token in tokens:
            outcome = token.get("outcome", "").upper()
            token_id = token.get("token_id") or token.get("tokenId")
            if outcome and token_id:
                token_ids[outcome.lower()] = token_id
                outcomes.append(outcome)

        # Extract prices
        outcome_prices = {}
        for token in tokens:
            outcome = token.get("outcome", "").lower()
            price = token.get("price")
            if outcome and price is not None:
                outcome_prices[outcome] = float(price)

        # Build market record
        market_record = {
            "condition_id": condition_id,
            "question_id": data.get("question_id") or data.get("questionId"),
            "question": data.get("question", ""),
            "description": data.get("description"),
            "category": data.get("category"),
            "token_ids": token_ids,
            "outcomes": outcomes or ["YES", "NO"],
            "is_active": data.get("active", True),
            "is_resolved": data.get("closed", False) or data.get("resolved", False),
            "resolution_outcome": data.get("resolution") or data.get("resolutionOutcome"),
            "end_date": self._parse_datetime(data.get("end_date") or data.get("endDate")),
            "volume_24h": float(data.get("volume24hr", 0) or 0),
            "total_volume": float(data.get("volume", 0) or 0),
            "liquidity": float(data.get("liquidity", 0) or 0),
            "outcome_prices": outcome_prices,
            "source_url": data.get("url"),
            "icon_url": data.get("image") or data.get("icon"),
        }

        # Upsert using PostgreSQL INSERT ... ON CONFLICT
        stmt = insert(Market).values(**market_record)
        stmt = stmt.on_conflict_do_update(
            index_elements=["condition_id"],
            set_={
                "question": stmt.excluded.question,
                "description": stmt.excluded.description,
                "category": stmt.excluded.category,
                "token_ids": stmt.excluded.token_ids,
                "outcomes": stmt.excluded.outcomes,
                "is_active": stmt.excluded.is_active,
                "is_resolved": stmt.excluded.is_resolved,
                "resolution_outcome": stmt.excluded.resolution_outcome,
                "end_date": stmt.excluded.end_date,
                "volume_24h": stmt.excluded.volume_24h,
                "total_volume": stmt.excluded.total_volume,
                "liquidity": stmt.excluded.liquidity,
                "outcome_prices": stmt.excluded.outcome_prices,
                "updated_at": datetime.utcnow(),
            },
        )

        await self.session.execute(stmt)

    async def collect_price_snapshot(self, market_id: str) -> bool:
        """Collect and save current price snapshot for a market.

        Args:
            market_id: Market condition ID.

        Returns:
            True if snapshot saved successfully.
        """
        # Get market from database for token IDs
        query = select(Market).where(Market.condition_id == market_id)
        result = await self.session.execute(query)
        market = result.scalar_one_or_none()

        if not market or not market.token_ids:
            logger.warning(f"Market {market_id} not found or missing token IDs")
            return False

        # Get token IDs
        yes_token = market.token_ids.get("yes")
        no_token = market.token_ids.get("no")

        if not yes_token:
            return False

        # Fetch price data
        token_ids = [yes_token]
        if no_token:
            token_ids.append(no_token)

        prices = await self.clob.get_prices(token_ids)

        if not prices:
            return False

        # Get orderbook for spread
        orderbook = await self.clob.get_orderbook(yes_token, depth=5)

        # Extract data
        yes_data = prices.get(yes_token, {})
        no_data = prices.get(no_token, {}) if no_token else {}

        yes_price = float(yes_data.get("price", 0) or yes_data.get("mid", 0))
        no_price = float(no_data.get("price", 0) or no_data.get("mid", 0))

        if yes_price == 0 and no_price == 0:
            return False

        # Calculate from complement if one is missing
        if yes_price == 0:
            yes_price = 1 - no_price
        elif no_price == 0:
            no_price = 1 - yes_price

        # Extract bid/ask from orderbook
        bids = orderbook.get("bids", [])
        asks = orderbook.get("asks", [])

        yes_bid = float(bids[0].get("price", 0)) if bids else None
        yes_ask = float(asks[0].get("price", 0)) if asks else None

        spread = None
        if yes_bid and yes_ask:
            spread = yes_ask - yes_bid

        # Create price history record
        snapshot = PriceHistory(
            market_id=market_id,
            timestamp=datetime.utcnow(),
            yes_price=yes_price,
            no_price=no_price,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=1 - yes_ask if yes_ask else None,
            no_ask=1 - yes_bid if yes_bid else None,
            spread=spread,
            volume=float(yes_data.get("volume", 0) or 0),
            volume_24h=float(market.volume_24h or 0),
            open_interest=float(yes_data.get("openInterest", 0) or 0),
        )

        self.session.add(snapshot)
        await self.session.flush()

        return True

    async def collect_all_prices(
        self,
        batch_size: int = 20,
        delay_between_batches: float = 1.0,
    ) -> int:
        """Collect price snapshots for all active markets.

        Args:
            batch_size: Markets to process per batch.
            delay_between_batches: Seconds to wait between batches.

        Returns:
            Number of snapshots collected.
        """
        logger.info("Collecting prices for all active markets...")

        # Get all active markets
        query = select(Market).where(Market.is_active)
        result = await self.session.execute(query)
        markets = result.scalars().all()

        collected = 0
        market_ids = [m.condition_id for m in markets]

        for i in range(0, len(market_ids), batch_size):
            batch = market_ids[i:i + batch_size]

            # Collect prices concurrently
            tasks = [self.collect_price_snapshot(mid) for mid in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for mid, result in zip(batch, results, strict=True):
                if result is True:
                    collected += 1
                elif isinstance(result, Exception):
                    logger.error(f"Error collecting price for {mid}: {result}")

            await self.session.commit()

            if i + batch_size < len(market_ids):
                await asyncio.sleep(delay_between_batches)

        logger.info(f"Collected {collected} price snapshots")
        return collected

    async def collect_books(
        self,
        adapters: Mapping[VenueId, MarketDataAdapter],
        market_ids_per_venue: Mapping[VenueId, Sequence[str]],
        session: AsyncSession,
    ) -> int:
        """Record order-book depth for the top-N-by-volume markets, per venue.

        `PriceHistory`/`collect_price_snapshot` above record only
        top-of-book for Polymarket; this method is the other half of
        PLAN.md D6/D10 — it records the REAL, full order book so
        `app.services.backtesting.data_replay.DataReplayer` can attach a
        `depth_source="recorded"` book instead of always falling back to
        `app.execution.fill_engine.synthesize_book`'s fabricated one.
        Unlike every other method on this class it runs over BOTH
        venues, through the `app.venues.base.MarketDataAdapter` seam
        (never `GammaAPIClient`/`CLOBDataClient`), because Kalshi depth
        is exactly as necessary as Polymarket depth for a cross-venue
        backtest.

        For each venue in `adapters`: fetches `VenueMarket` for every id
        in `market_ids_per_venue[venue]` (candidates the CALLER selects —
        e.g. every currently-open market on that venue; this method does
        not enumerate a venue's market list itself), ranks the markets
        that resolved by the same volume proxy
        `app.services.scanner._volume` uses
        (`VenueMarket.raw["volume"]`, `0.0` if absent/non-numeric), and
        keeps the top `settings.book_collection_top_n`. For EACH of those
        markets' `outcomes` — every outcome the venue payload carries,
        not only `"YES"`/`"NO"` (T21 carry-forward 1: a multi-outcome
        bundle market needs a book per named outcome, e.g. a candidate in
        an election market) — fetches `adapter.get_book(market_id,
        outcome)` and persists one `BookSnapshot` row.

        A single market's `get_market` failing, or a single outcome's
        `get_book` failing (`VenueError` — a 404, a rate limit, a
        malformed payload), is logged and skipped; it never aborts the
        whole call the way one bad market must not blank out an entire
        venue's collection.

        Idempotent: re-running this over data that has not moved writes
        no duplicate row. `_upsert_book_snapshot` does this by
        CHECK-THEN-INSERT (query the natural key `(venue, market_id,
        outcome, ts)`, skip if a row is already there) rather than a
        database-native `ON CONFLICT` clause — SQLAlchemy's
        `postgresql.insert(...).on_conflict_do_update` (already used by
        `_upsert_market` above) has no equivalent that compiles on
        SQLite, and GUARDRAILS.md/T21 require this method to pass on
        SQLite too. A second collection run reading the SAME book (e.g.
        `tests.venues.fixture_adapter.FixtureAdapter`, whose `get_book`
        is deliberately static) reports the identical `(venue, market_id,
        outcome, ts)` and is therefore a genuine no-op, not a duplicate
        row.

        Commits once at the end of the call (matching
        `collect_price_snapshot`/`collect_all_prices` above, unlike
        `app.services.scanner.scan`'s "caller commits" convention) so the
        rows this call wrote are durable even when the caller opens a
        fresh session on its next invocation, as
        `app.scripts.collect_prices`'s loop does.

        Args:
            adapters: One READ-ONLY `MarketDataAdapter` per venue —
                exactly `app.services.scanner.scan`'s own `adapters`
                parameter, obtained from
                `app.venues.registry.get_read_adapter`/
                `app.api.deps.get_market_data_adapters`, never
                `get_adapter`, so this can never acquire an
                order-placing adapter (GUARDRAILS.md §1.1).
            market_ids_per_venue: Candidate market ids to consider, per
                venue. A venue absent from `adapters` is skipped with a
                warning rather than raising.
            session: Session to persist `BookSnapshot` rows on. This
                call commits it once at the end (see above) — it is a
                parameter, not `self.session`, so a caller already
                holding its own session (e.g. one shared with a price
                collection pass in the same loop iteration) can reuse
                it.

        Returns:
            int: Number of NEW `BookSnapshot` rows written (duplicates
                skipped by the idempotency check are not counted).
        """
        written = 0
        for venue, market_ids in market_ids_per_venue.items():
            adapter = adapters.get(venue)
            if adapter is None:
                logger.warning(
                    f"collect_books: no adapter registered for venue "
                    f"{venue!r}; skipping {len(market_ids)} candidate market(s)",
                    extra={"venue": venue},
                )
                continue

            markets: list[VenueMarket] = []
            for market_id in market_ids:
                try:
                    markets.append(await adapter.get_market(market_id))
                except VenueError as exc:
                    logger.warning(
                        f"collect_books: get_market failed for {market_id}: {exc}",
                        extra={"venue": venue, "market_id": market_id},
                    )

            top_markets = sorted(
                markets, key=_book_collection_volume, reverse=True
            )[: settings.book_collection_top_n]

            for market in top_markets:
                for outcome in market.outcomes:
                    try:
                        book = await adapter.get_book(market.market_id, outcome)
                    except VenueError as exc:
                        logger.debug(
                            f"collect_books: get_book failed for "
                            f"{market.market_id}/{outcome}: {exc}",
                            extra={
                                "venue": venue,
                                "market_id": market.market_id,
                                "outcome": outcome,
                            },
                        )
                        continue
                    if await self._upsert_book_snapshot(session, book, market):
                        written += 1

        await session.commit()
        logger.info(f"Collected {written} new book snapshot(s)")
        return written

    async def _upsert_book_snapshot(
        self,
        session: AsyncSession,
        book: OrderBook,
        market: VenueMarket,
    ) -> bool:
        """Insert one `BookSnapshot` row unless its natural key already exists.

        See `collect_books`'s docstring for why this is a check-then-
        insert rather than a database-native `ON CONFLICT` clause: the
        pattern must work identically on SQLite (tests) and Postgres
        (production), and SQLAlchemy's async ORM session has no single
        `ON CONFLICT` construct that compiles on both without a dialect
        branch. The `SELECT` executed here also triggers SQLAlchemy's
        autoflush of any row `session.add()`-ed earlier in this same
        call, so two outcomes of the same market (or a duplicate id
        appearing twice in one candidate list) cannot double-insert
        within a single `collect_books` call either.

        `book.outcome` is canonicalized through
        `app.strategies.base.outcome_key()` before either the existence
        check or the insert: a concrete adapter's `get_book` echoes back
        whatever casing the CALLER passed it (Polymarket's real payload
        spells outcomes `"Yes"`/`"No"`, not `"YES"`/`"NO"`) — so a
        `BookSnapshot` row keyed by the raw casing would never match the
        outcome `data_replay.py`'s lookup actually asks for.

        T21d defect 6: this used `normalize_outcome()`, which
        canonicalizes `"YES"`/`"NO"` and NOTHING ELSE, so the module
        docstring's guarantee that a lookup "can never miss a row purely
        because of a casing mismatch between two venues' payloads" held
        for exactly the binary case that did not need it. Four
        collections of the SAME book at the SAME `ts`, spelled
        `"Trump"`/`"TRUMP"`/`"trump"`/`"Trump "`, wrote FOUR rows past a
        unique constraint that was supposed to make that impossible, and
        a replay asking for any one spelling found depth for none of the
        others. `outcome_key()` strips and case-folds every label, so
        the check-then-insert below now actually dedupes what the
        constraint says it dedupes.

        Args:
            session: Session to check against and (on a miss) add to.
            book: The `OrderBook` just read from the venue.
            market: The book's parent market, for `tick_size`/`min_size`.

        Returns:
            bool: `True` if a new row was added, `False` if the natural
                key `(venue, market_id, outcome, ts)` already existed.
        """
        outcome = outcome_key(book.outcome)
        existing = await session.execute(
            select(BookSnapshot.id).where(
                BookSnapshot.venue == book.venue,
                BookSnapshot.market_id == book.market_id,
                BookSnapshot.outcome == outcome,
                BookSnapshot.ts == book.ts,
            )
        )
        if existing.scalar_one_or_none() is not None:
            return False

        session.add(
            BookSnapshot(
                venue=book.venue,
                market_id=book.market_id,
                outcome=outcome,
                ts=book.ts,
                bids=[
                    {"price": level.price, "size": level.size}
                    for level in book.bids
                ],
                asks=[
                    {"price": level.price, "size": level.size}
                    for level in book.asks
                ],
                tick_size=market.tick_size,
                min_size=market.min_size,
                depth_source="recorded",
            )
        )
        return True

    async def backfill_historical_data(
        self,
        start: datetime,
        end: datetime,
        market_ids: list[str] | None = None,
    ) -> int:
        """Backfill historical trade and price data.

        Args:
            start: Start of backfill period.
            end: End of backfill period.
            market_ids: Optional specific markets to backfill.

        Returns:
            Number of records backfilled.
        """
        logger.info(f"Backfilling data from {start} to {end}...")

        # Get markets to backfill
        if market_ids:
            query = select(Market).where(Market.condition_id.in_(market_ids))
        else:
            query = select(Market)

        result = await self.session.execute(query)
        markets = result.scalars().all()

        total_records = 0

        for market in markets:
            try:
                records = await self._backfill_market(market, start, end)
                total_records += records
            except Exception as e:
                logger.error(f"Failed to backfill market {market.condition_id}: {e}")

            # Rate limiting
            await asyncio.sleep(0.5)

        await self.session.commit()
        logger.info(f"Backfilled {total_records} records")

        return total_records

    async def _backfill_market(
        self,
        market: Market,
        start: datetime,
        end: datetime,
    ) -> int:
        """Backfill data for a single market.

        Args:
            market: Market model.
            start: Start time.
            end: End time.

        Returns:
            Number of records created.
        """
        records = 0
        offset = 0
        batch_size = 100

        while True:
            trades = await self.data.get_market_trades(
                market_id=market.condition_id,
                limit=batch_size,
                offset=offset,
                start_time=start,
                end_time=end,
            )

            if not trades:
                break

            for trade_data in trades:
                try:
                    await self._save_trade(market.condition_id, trade_data)
                    records += 1
                except Exception as e:
                    logger.debug(f"Failed to save trade: {e}")

            offset += len(trades)

            if len(trades) < batch_size:
                break

            await asyncio.sleep(0.1)

        return records

    async def _save_trade(
        self,
        market_id: str,
        data: dict[str, Any],
    ) -> None:
        """Save a trade record to the database.

        Args:
            market_id: Market condition ID.
            data: Trade data from API.
        """
        timestamp = self._parse_datetime(data.get("timestamp") or data.get("created_at"))
        if not timestamp:
            timestamp = datetime.utcnow()

        # Parse side
        side_str = (data.get("side") or "BUY").upper()
        side = TradeSide.BUY if side_str == "BUY" else TradeSide.SELL

        # Parse outcome
        outcome_str = (data.get("outcome") or data.get("asset") or "YES").upper()
        outcome = TradeOutcome.YES if outcome_str in ("YES", "Y") else TradeOutcome.NO

        trade = TradeHistory(
            market_id=market_id,
            timestamp=timestamp,
            side=side,
            outcome=outcome,
            price=float(data.get("price", 0)),
            size=float(data.get("size") or data.get("amount", 0)),
            maker_address=data.get("maker") or data.get("maker_address"),
            taker_address=data.get("taker") or data.get("taker_address"),
            tx_hash=data.get("tx_hash") or data.get("transactionHash"),
        )

        self.session.add(trade)

    def _parse_datetime(self, value: Any) -> datetime | None:
        """Parse datetime from various formats.

        Args:
            value: Datetime string or timestamp.

        Returns:
            Parsed datetime or None.
        """
        if value is None:
            return None

        if isinstance(value, datetime):
            return value

        if isinstance(value, (int, float)):
            # Unix timestamp
            if value > 1e12:  # Milliseconds
                value = value / 1000
            return datetime.fromtimestamp(value)

        if isinstance(value, str):
            # Try ISO format
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                pass

            # Try other formats
            formats = [
                "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d",
            ]
            for fmt in formats:
                try:
                    return datetime.strptime(value, fmt)
                except ValueError:
                    continue

        return None
