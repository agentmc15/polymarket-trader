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
import math
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
from app.models.selection_membership import SelectionMembership
from app.models.trade_history import TradeHistory, TradeOutcome, TradeSide
from app.strategies.base import outcome_key
from app.utils.time import utcnow
from app.venues.base import MarketDataAdapter, VenueError
from app.venues.types import (
    OrderBook,
    VenueId,
    VenueMarket,
    quotable_spread,
    venue_volume,
)

logger = logging.getLogger(__name__)


#: Per-CANDIDATE faults `collect_books` isolates to ONE market or ONE
#: book, rather than the whole venue (mm-proveout T16, Phase 2 review
#: part b). Mirrors `app.services.scanner.VENUE_READ_FAULTS`
#: (`VenueError`, `httpx.HTTPError`) exactly, kept as this module's own
#: copy rather than a cross-module import -- the same reason
#: `_book_collection_volume` below is a local copy of `venue_volume`
#: rather than reusing a helper defined elsewhere. `httpx.HTTPStatusError`
#: (a subclass of `httpx.HTTPError`) is the fault this tuple was widened
#: FOR: `app.venues.kalshi.adapter.raise_for_venue_error` maps 429 and
#: 401/403 to `VenueError` subclasses and lets every OTHER status
#: (a 404 included) fall through to `httpx.Response.raise_for_status()`
#: -- deliberately, per that function's own docstring, and the mapping is
#: explicitly NOT this task's to change. Before this tuple existed, the
#: per-candidate catches below were `except VenueError` only, so a 404 on
#: `GET /markets/{ticker}` -- 3 of 5 real tickers sampled from Kalshi's
#: own open listing returned exactly that, an ORDINARY case, not a
#: hypothetical -- escaped straight through to the venue-level
#: `except Exception` several lines down and took the ENTIRE venue's tick
#: with it: every other candidate in `candidates_per_venue[venue]`, and every already-
#: selected market's book, lost along with the one dead ticker. See
#: `collect_books`'s own docstring for the "PER-CANDIDATE ISOLATION"
#: section this fixes.
_MARKET_FETCH_FAULTS: tuple[type[Exception], ...] = (VenueError, httpx.HTTPError)


# API Base URLs
GAMMA_API_BASE = "https://gamma-api.polymarket.com"
CLOB_API_BASE = "https://clob.polymarket.com"
DATA_API_BASE = "https://data-api.polymarket.com"


def _book_collection_volume(market: VenueMarket) -> float:
    """Return the volume this venue publishes, for top-N ranking.

    Delegates to `app.venues.types.venue_volume`. This was a local copy
    reading `raw["volume"]`, a key Kalshi's `/events` payload does not
    send — so every one of 96,478 open Kalshi markets ranked 0.0 and the
    "top N by volume" was a tie across the whole venue. See
    `venue_volume` for the field list and the reasoning.

    Args:
        market: The market to rank.

    Returns:
        float: Volume, `>= 0.0`.
    """
    return venue_volume(market)


#: Per-venue LISTING payload key carrying the LIFETIME (not 24-hour)
#: volume counter, for `BookSnapshot.volume_lifetime` (mm-proveout T15,
#: migration `008`). Deliberately a SEPARATE, narrower table from
#: `app.venues.types._VOLUME_KEYS`: that tuple tries the 24-hour fields
#: FIRST (correct for `venue_volume`'s own job, ranking), so reusing it
#: here would silently prefer the same field this column exists to stop
#: using for a between-snapshot delta. Measured live 2026-09-07: Kalshi's
#: `volume_24h_fp` did not move for a single one of 6,058 actively-traded
#: markets over 245 seconds; lifetime `volume_fp` moved for 25 of them.
_LIFETIME_VOLUME_KEY: dict[VenueId, str] = {
    "kalshi": "volume_fp",
    "polymarket": "volumeNum",
}


def _raw_lifetime_volume(market: VenueMarket) -> float | None:
    """Parse this venue's LIFETIME volume counter from the listing payload.

    Never `venue_volume(market)` -- see `_LIFETIME_VOLUME_KEY`'s comment
    for why that helper's 24-hour-first ordering is wrong for this
    column. Returns `None` (not `0.0`, not a negative) for a missing,
    non-numeric, non-finite, or negative reading: `passive_fill.py:97`
    raises `ValueError` on a negative volume and `:142` treats `0.0` as
    "definitely nothing traded", so a value this function cannot vouch
    for must be "unknown", never either of those.

    This is deliberately NOT `max(parsed, 0.0)` the way `venue_volume`
    clamps a negative for ranking purposes: a negative LIFETIME counter
    reading is not a real observation to clamp, it is a payload this
    function cannot honor at all.

    Args:
        market: The market whose listing payload to read.

    Returns:
        float | None: The parsed lifetime volume, `>= 0.0`, or `None`.
    """
    key = _LIFETIME_VOLUME_KEY.get(market.venue)
    if key is None or key not in market.raw:
        return None
    value = market.raw[key]
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed < 0.0:
        return None
    return parsed


def _monotonic_lifetime_volume(
    previous: float | None, candidate: float | None
) -> float | None:
    """Apply migration `008`'s monotonicity guard to a lifetime volume read.

    A lifetime counter should only ever climb, but Polymarket's `volumeNum`
    was measured DECREASING for 29 of 255 markets over ~28 minutes -- the
    counter itself gets restated. A decrease does not mean "volume went
    backwards" (impossible); it means the PREVIOUS or CURRENT reading (we
    cannot tell which) no longer describes anything real, so the honest
    answer is `None` -- unknown -- never the smaller value and never
    `0.0` (`passive_fill.py` treats `0.0` as a hard "no trading" gate) or
    a negative (`TradeRange.__post_init__` raises on one).

    Compares only against the IMMEDIATELY PRECEDING known value, not the
    deepest historical one: once a restatement is detected, the next
    reading has no reliable baseline to be judged against either, so
    treating `previous=None` (nothing to compare) as "accept the
    candidate outright" is the same rule applied uniformly rather than a
    special case -- one restatement resets the baseline instead of
    permanently vetoing every later poll.

    Args:
        previous: The last known `volume_lifetime` for this
            `(venue, market_id, outcome)`, or `None` if there is none
            (a brand-new series, or the last known value was itself
            already `None`).
        candidate: This poll's `_raw_lifetime_volume(market)` reading.

    Returns:
        float | None: `candidate` if it is present and `>= previous` (or
            `previous` is `None`); `None` if `candidate` is `None` or a
            decrease from `previous`.
    """
    if candidate is None:
        return None
    if previous is None or candidate >= previous:
        return candidate
    return None


def select_quotable_markets(
    markets: Sequence[VenueMarket],
) -> tuple[list[VenueMarket], list[VenueMarket], list[VenueMarket]]:
    """Partition listing candidates by quotability (mm-proveout T7, PLAN.md D1).

    `collect_books` used to rank every candidate by volume alone and
    keep the top `settings.book_collection_top_n`. Measured live, that
    selected 2 Kalshi and 0 Polymarket markets (out of the top 50) with
    spread `>= 0.10` -- volume concentrates on the TIGHTEST books, which
    is exactly what `MarketMaker`'s current `min_spread=0.25` (changed
    2026-09-08 from 0.10; NOT yet certified by the kit's two-split rule
    -- see `app.strategies.market_making`'s module docstring) also
    refuses to quote inside of. Selection is now three sets, computed in
    one pass:

      1. `two_sided` -- every candidate `app.venues.types.quotable_spread`
         did not reject (both sides present, parseable, `0 < bid < ask <
         1`), read from the LISTING payload the caller already fetched
         via `get_market` -- never an extra `get_book` call.
      2. `quotable` -- the subset of `two_sided` whose spread clears
         `settings.book_collection_min_spread` AND whose
         `venue_volume` clears `settings.book_collection_min_volume` (a
         floor against a market that is two-sided only because nobody
         has traded it).
      3. `selected` -- `quotable`, ranked by volume descending and
         capped at `settings.book_collection_top_n`.

    A single, shared function rather than one implementation inside
    `collect_books` and a second inside a probe script: `venue_volume`
    replaced exactly this kind of drifted duplicate
    (`test_volume_ranking.py`), and the four counts a caller logs/prints
    ("listed, two-sided, quotable, selected") must always describe the
    identical partition.

    Args:
        markets: Candidate markets already fetched from a venue's
            listing endpoint (i.e. already paid for). Never re-fetched
            or re-ordered as a side effect -- `two_sided`/`quotable`
            preserve `markets`' original order.

    Returns:
        tuple[list[VenueMarket], list[VenueMarket], list[VenueMarket]]:
            `(two_sided, quotable, selected)`, as described above.
    """
    two_sided: list[VenueMarket] = []
    quotable: list[VenueMarket] = []
    for market in markets:
        spread = quotable_spread(market)
        if spread is None:
            continue
        two_sided.append(market)
        if (
            spread >= settings.book_collection_min_spread
            and _book_collection_volume(market) >= settings.book_collection_min_volume
        ):
            quotable.append(market)

    selected = sorted(quotable, key=_book_collection_volume, reverse=True)[
        : settings.book_collection_top_n
    ]
    return two_sided, quotable, selected


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
        candidates_per_venue: Mapping[VenueId, Sequence[str | VenueMarket]],
        session: AsyncSession,
    ) -> int:
        """Record order-book depth for the markets the policy would quote, per venue.

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

        For each venue in `adapters`: resolves every entry in
        `candidates_per_venue[venue]` to a `VenueMarket` — an entry that
        IS ALREADY a `VenueMarket` is used AS-IS, with NO `get_market`
        call at all; a bare `str` (a market id) is resolved with one
        `adapter.get_market(market_id)` call, kept for backward
        compatibility with callers that only have ids on hand
        (`app.scripts.collect_prices.collect_books_once`, and this
        module's own pre-T16 tests). mm-proveout T16 (Phase 2 review,
        part a): `app.tasks.collection.run_collect_books` (the
        60-second beat, the ONLY production caller) already paid for a
        full `adapter.list_markets(status="open")` walk to learn what to
        pass here — Kalshi's `/events?status=open&with_nested_markets=
        true` returns every `VenueMarket` this method needs directly,
        measured live 2026-09-07 at 99,588 markets in 10.46s — and used
        to then throw those objects away and re-fetch each one with
        `get_market`, measured live at 0.096s/call, ~2.66 HOURS for that
        same candidate count against a 60-second beat. Every one of
        those per-market re-fetches was redundant: `list_markets` and
        `get_market` both funnel through the SAME `_build_market` on the
        SAME field set (verified live: a nested-listing market payload
        and a `GET /markets/{ticker}` payload for the same ticker were
        checked key-for-key and were IDENTICAL, including the
        `fee_waiver_expiration_time`/`price_level_structure`/
        `minimum_order_size` fields `_fee_schedule`/`tick_size`/
        `min_size` read — neither payload carried the first or the last
        of those, so both paths compute the identical `FeeSchedule`/
        `min_size` for Kalshi). The beat now passes `VenueMarket`
        objects straight through and pays for the listing walk only.

        Then selects which resolved markets actually get a book fetch
        via `select_quotable_markets` (mm-proveout T7, PLAN.md D1):
        two-sided (spread computable from the LISTING payload, never an
        extra request) with `spread >= settings.book_collection_min_spread`
        and `venue_volume >= settings.book_collection_min_volume`, ranked
        by volume within THAT set and capped at
        `settings.book_collection_top_n`. This replaces ranking every
        candidate by volume alone, which measured live selected 2 Kalshi
        and 0 Polymarket markets (of the top 50) with spread `>= 0.10` —
        volume alone finds the tightest books, which is exactly what
        `MarketMaker`'s current `min_spread=0.25` (changed 2026-09-08
        from 0.10; NOT yet certified by the kit's two-split rule — see
        `app.strategies.market_making`'s module docstring) also refuses
        to quote.
        Logs `listed`/`two_sided`/`quotable`/`selected` counts per venue
        so a silent collapse (a venue renaming a field, a threshold set
        too tight) is visible without reading a `BookSnapshot` row.
        Records `selected` into `SelectionMembership`
        (`_record_selection_membership`, mm-proveout T16 part c) BEFORE
        the book-fetch loop below, so a market's SELECTION decision is
        persisted whether or not its subsequent `get_book` call
        succeeds — see that method's docstring. For EACH selected
        market's `outcomes` — every outcome the venue payload carries,
        not only `"YES"`/`"NO"` (T21 carry-forward 1: a multi-outcome
        bundle market needs a book per named outcome, e.g. a candidate
        in an election market) — fetches
        `adapter.get_book(market_id, outcome)` and persists one
        `BookSnapshot` row.

        A single candidate's `get_market` resolution failing, or a
        single outcome's `get_book` failing, with a fault in
        `_MARKET_FETCH_FAULTS` (`VenueError` OR `httpx.HTTPError` — a
        404, a rate limit, a timeout, a malformed payload), is logged
        and skipped; it never aborts the whole call the way one bad
        market must not blank out an entire venue's collection.

        PER-CANDIDATE ISOLATION MUST COVER `httpx.HTTPError`, NOT ONLY
        `VenueError` (mm-proveout T16, Phase 2 review part b, confirmed
        live 2026-09-07: 3 of 5 real tickers sampled from Kalshi's own
        open listing returned 404 on `GET /markets/{ticker}` — an
        ORDINARY case at this venue's scale, not a hypothetical, and
        over a long pass at least one is certain every tick).
        `app.venues.kalshi.adapter.raise_for_venue_error` maps 429 and
        401/403 to `VenueError` subclasses and lets every OTHER status —
        a 404 included — fall through to `httpx.Response.
        raise_for_status()`, which raises `httpx.HTTPStatusError`; that
        mapping is DELIBERATE per that function's own docstring and is
        explicitly not this task's to change. Before this fix, the two
        per-candidate catches below (`get_market`'s id-resolution path
        and `get_book`) were `except VenueError` only, so that ordinary
        404 was NOT a per-candidate fault at all — it escaped straight
        through to the venue-level `except Exception` several lines
        down and cost the ENTIRE venue's tick: every other candidate in
        `candidates_per_venue[venue]`, and every already-selected
        market's book, lost along with the one delisted ticker. Both
        catches now use `_MARKET_FETCH_FAULTS` (`VenueError`,
        `httpx.HTTPError`) instead.

        PER-VENUE ISOLATION, ESTABLISHED HERE, NOT MERELY ASSUMED
        (mm-proveout T9 red-team, confirmed live): before that fix, the
        per-candidate guarantee above held only for `VenueError` raised
        by `get_market`/`get_book` — anything else (an unwrapped payload
        bug surfacing as `KeyError`/`AttributeError`, an `IntegrityError`
        out of `_upsert_book_snapshot`, `select_quotable_markets` itself
        raising) propagated straight out of this method. Because the
        `for venue, candidates in candidates_per_venue.items()` loop had
        no boundary around each iteration, that exception unwound the
        WHOLE call: a healthy SECOND venue was never even attempted, and
        — because the old version committed once at the very end — the
        FIRST venue's already-`session.add()`-ed rows, from before the
        exception, were never persisted either. One venue's bug meant
        zero data from BOTH venues, and because this now runs
        unattended on a `book_collection_interval_s` beat
        (`app.tasks.collection.collect_books`) rather than a
        supervised, one-shot CLI run, that failure mode would repeat
        silently every tick indefinitely, with `app.scripts.
        collection_health` (T10) the only thing eventually noticing an
        empty table.

        The fix is TWO PARTS, chosen together ("per-venue try/except at
        the right breadth, per-venue commit" — both, not either):

          1. Each venue's entire body (candidate resolution through book
             writes) runs inside its own `try/except Exception`. This
             deliberately catches BARE `Exception`, at VENUE granularity
             — the same shape and the same justification
             `app.tasks.execution.reconcile_venues` already uses
             (`# noqa: BLE001 - one venue must not stop the rest`):
             Polymarket and Kalshi are different adapter classes reading
             different payload shapes through different HTTP APIs, so a
             bug specific to one venue's data has no structural reason
             to also be a bug in the other's. This is DELIBERATELY
             narrower than it looks: it is NOT the same choice
             `app.services.scanner.scan` makes for a single BOOK inside
             one venue's OWN batch — that code catches only
             `VENUE_READ_FAULTS` (never bare `Exception`) at that finer
             grain precisely because every book in a pass shares the
             SAME parsing/scoring code path, so swallowing an
             `AttributeError` there would misreport "one flaky book"
             when the truth is a systemic bug quietly eating every
             book in the pass (see
             `tests/services/test_scanner_fault_isolation.py`'s module
             docstring). Here the finer-grained catches
             (`get_market`/`get_book`, above) are now exactly as wide as
             `VENUE_READ_FAULTS` (this module's own `_MARKET_FETCH_FAULTS`
             copy of it — see that constant's docstring for why a local
             copy rather than an import), so a genuine, systemic bug
             within ONE venue's own batch still raises loudly (visible
             via `logger.exception` below and via `app.scripts.
             collection_health` reporting that venue's snapshot count as
             zero for the tick), it just no longer takes the OTHER
             venue's collection down with it.
          2. `session.commit()` moved from once-at-the-end-of-the-call to
             once per successful venue, immediately after that venue's
             book writes finish (inside the same `try`). A venue that
             raises now rolls back only its own uncommitted work
             (`session.rollback()` in the `except`, safe to call even
             when there is nothing pending) — any EARLIER venue in this
             same call already committed and is unaffected regardless of
             which venue in `candidates_per_venue` failed or in what
             order the mapping iterates.

        `written` only counts a venue's snapshots once that venue's
        `session.commit()` has actually succeeded — a venue whose commit
        itself raises is treated as a full failure of that venue (rolled
        back, logged, not counted), never a partial credit for rows that
        turned out not to be durable.

        Idempotent on ROW COUNT: re-running this over a book that has not
        moved writes no duplicate row. `_upsert_book_snapshot` does this
        by CHECK-THEN-UPSERT (query the natural key `(venue, market_id,
        outcome, ts)`; on a hit, refresh `volume`/`taker_fee_rate`/
        `maker_rebate_rate` in place and return without adding a row; on
        a miss, insert) rather than a database-native `ON CONFLICT`
        clause — SQLAlchemy's `postgresql.insert(...).on_conflict_do_update`
        (already used by `_upsert_market` above) has no equivalent that
        compiles on SQLite, and GUARDRAILS.md/T21 require this method to
        pass on SQLite too. This is idempotent in row count, not
        necessarily in row CONTENT: a second collection run reading the
        SAME book (e.g. `tests.venues.fixture_adapter.FixtureAdapter`,
        whose `get_book` is deliberately static) reports the identical
        `(venue, market_id, outcome, ts)`, so no new row is added — but
        if that market's `venue_volume`/`fee` changed between the two
        runs (a quiet Polymarket book can report the same `ts` for many
        polls while its `feeSchedule` moves underneath — see
        `_upsert_book_snapshot`'s docstring), the existing row's
        `volume`/`taker_fee_rate`/`maker_rebate_rate` are updated to the
        latest values rather than frozen at whatever they were on first
        sight.

        Commits once PER VENUE, immediately after that venue's book
        writes finish (see "PER-VENUE ISOLATION" above) — not once at the
        end of the whole call. This still matches the intent
        `collect_price_snapshot`/`collect_all_prices` above have (unlike
        `app.services.scanner.scan`'s "caller commits" convention): the
        rows this call wrote are durable even when the caller opens a
        fresh session on its next invocation, as
        `app.scripts.collect_prices`'s loop does — it is just durable
        per venue rather than only all-or-nothing for the whole call.

        Args:
            adapters: One READ-ONLY `MarketDataAdapter` per venue —
                exactly `app.services.scanner.scan`'s own `adapters`
                parameter, obtained from
                `app.venues.registry.get_read_adapter`/
                `app.api.deps.get_market_data_adapters`, never
                `get_adapter`, so this can never acquire an
                order-placing adapter (GUARDRAILS.md §1.1).
            candidates_per_venue: Candidates to consider, per venue —
                each entry either a bare market id (`str`, resolved with
                one `get_market` call) or an already-fetched
                `VenueMarket` (used directly, no `get_market` call —
                what `app.tasks.collection.run_collect_books`, the
                beat, passes). A venue absent from `adapters` is skipped
                with a warning rather than raising.
            session: Session to persist `BookSnapshot` rows on. Commits
                (and, on a venue's failure, rolls back) once per venue —
                it is a parameter, not `self.session`, so a caller
                already holding its own session (e.g. one shared with a
                price collection pass in the same loop iteration) can
                reuse it.

        Returns:
            int: Number of NEW `BookSnapshot` rows written across every
                venue that committed successfully (duplicates skipped by
                the idempotency check are not counted; a venue whose
                processing raised — see "PER-VENUE ISOLATION" above — is
                not counted for that tick, whatever it had written before
                the exception).
        """
        written = 0
        for venue, candidates in candidates_per_venue.items():
            adapter = adapters.get(venue)
            if adapter is None:
                logger.warning(
                    f"collect_books: no adapter registered for venue "
                    f"{venue!r}; skipping {len(candidates)} candidate market(s)",
                    extra={"venue": venue},
                )
                continue

            try:
                markets: list[VenueMarket] = []
                for candidate in candidates:
                    if isinstance(candidate, VenueMarket):
                        # Already fetched by the caller's listing walk
                        # (the beat) — NO `get_market` call. See the
                        # docstring's "T16 part a" section.
                        markets.append(candidate)
                        continue
                    try:
                        markets.append(await adapter.get_market(candidate))
                    except _MARKET_FETCH_FAULTS as exc:
                        logger.warning(
                            f"collect_books: get_market failed for {candidate}: {exc}",
                            extra={"venue": venue, "market_id": candidate},
                        )

                two_sided, quotable, selected = select_quotable_markets(markets)
                logger.info(
                    "collect_books: venue=%s listed=%d two_sided=%d quotable=%d "
                    "selected=%d",
                    venue,
                    len(markets),
                    len(two_sided),
                    len(quotable),
                    len(selected),
                    extra={
                        "venue": venue,
                        "listed": len(markets),
                        "two_sided": len(two_sided),
                        "quotable": len(quotable),
                        "selected": len(selected),
                    },
                )
                await self._record_selection_membership(session, venue, selected)

                venue_written = 0
                for market in selected:
                    for outcome in market.outcomes:
                        try:
                            book = await adapter.get_book(market.market_id, outcome)
                        except _MARKET_FETCH_FAULTS as exc:
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
                            venue_written += 1

                await session.commit()
            except Exception as exc:  # noqa: BLE001 - one venue's bug must not blank the other (T9)
                await session.rollback()
                logger.exception(
                    "collect_books: venue=%s failed with an unhandled "
                    "exception (%s); this venue's uncommitted rows for this "
                    "tick are rolled back, other venues are unaffected",
                    venue,
                    type(exc).__name__,
                    extra={"venue": venue, "error_type": type(exc).__name__},
                )
                continue

            written += venue_written

        logger.info(f"Collected {written} new book snapshot(s)")
        return written

    async def _record_selection_membership(
        self,
        session: AsyncSession,
        venue: VenueId,
        selected: Sequence[VenueMarket],
    ) -> None:
        """Upsert this tick's `selected` set into `SelectionMembership`.

        mm-proveout T16 part c. See `app.models.selection_membership`'s
        module docstring for the full rationale and the design's
        deliberate limits; this is the one writer. Called from
        `collect_books` BEFORE the per-market `get_book` loop, so a row
        here reflects the SELECTION DECISION `select_quotable_markets`
        made this tick regardless of whether the subsequent book fetch
        for that market succeeds — a market that was selected but whose
        `get_book` call then failed is still `selected=True` here (the
        collector CHOSE to quote it; the fetch fault is a separate,
        already-isolated failure, not a selection failure).

        ONE bulk `SELECT` for every existing row on this venue, then
        in-memory upserts — not one query per market — because the
        table this reads is bounded by DISTINCT markets ever selected on
        the venue (its whole point, see the module docstring), not by
        elapsed ticks, so fetching all of it is cheap. A market entry
        gets `selected=True`/`last_selected_at=now` whether it is new or
        already tracked; any TRACKED market that was `selected=True`
        before this call and is NOT in `selected` now transitions to
        `selected=False`/`last_deselected_at=now`. A market this venue
        has never selected, and does not select now, is never written at
        all — this table only ever grows to the size of "markets that
        were selected at least once", never the full venue listing.

        Args:
            session: Session to read/write on. Not committed here — the
                caller (`collect_books`) commits once per venue alongside
                the `BookSnapshot` writes, so a rollback on that venue's
                failure undoes membership updates and book writes
                together.
            venue: The venue this tick's `selected` set belongs to.
            selected: This tick's `select_quotable_markets` `selected`
                output — the markets `collect_books` is about to attempt
                a book fetch for.
        """
        now = utcnow()
        selected_ids = {market.market_id for market in selected}
        existing_rows = (
            await session.execute(
                select(SelectionMembership).where(
                    SelectionMembership.venue == venue
                )
            )
        ).scalars().all()
        existing_by_id = {row.market_id: row for row in existing_rows}

        for market_id in selected_ids:
            row = existing_by_id.get(market_id)
            if row is None:
                session.add(
                    SelectionMembership(
                        venue=venue,
                        market_id=market_id,
                        selected=True,
                        last_selected_at=now,
                        last_deselected_at=None,
                    )
                )
            else:
                row.selected = True
                row.last_selected_at = now

        for market_id, row in existing_by_id.items():
            if row.selected and market_id not in selected_ids:
                row.selected = False
                row.last_deselected_at = now

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

        Also writes `volume`/`taker_fee_rate`/`maker_rebate_rate`
        (mm-proveout T8, PLAN.md D9, migration `008`): `volume` is
        `venue_volume(market)` on the LISTING payload the caller already
        fetched (the same helper `select_quotable_markets` ranks by —
        never a second, independently-derived reading of `raw`), and the
        two fee fields are `market.fee.taker_rate`/
        `market.fee.maker_rebate_rate` — `app.venues.types.FeeSchedule`,
        GUARDRAILS.md §1.5's one sanctioned source of a fee, never a
        literal. See `BookSnapshot`'s module docstring for why these
        travel with the row instead of being re-looked-up later.

        WHAT "THE FEE IN FORCE" MEANS ON A HIT (T8 red-team retry): a
        same-`ts` poll is NOT treated as "nothing changed" for
        `volume`/`taker_fee_rate`/`maker_rebate_rate` — on a hit, this
        method REFRESHES those three columns to `market`'s current
        values rather than leaving the row untouched. This matters
        because `book.ts` and "the market has not changed" are not the
        same fact on Polymarket: `PolymarketAdapter._parse_book` sets
        `OrderBook.ts` from the CLOB payload's own `timestamp` — the
        instant the BOOK last moved — not poll time, so a quiet
        Polymarket market can report the IDENTICAL `ts` across many
        60-second `collect_books` polls while its `feeSchedule` (and
        cumulative volume) changes underneath. `KalshiAdapter.get_book`
        stamps `ts=utcnow()` instead, so a Kalshi poll essentially never
        collides with a prior `ts` and this branch is a Polymarket
        concern in practice. Treating an unchanged `ts` as "no update
        needed" would let the FIRST fee this row ever saw survive
        forever on exactly the quiet-book case migration `008`'s
        docstring names as the reason these columns exist — measured:
        two polls of an unmoved book, fee 0.07 -> 0.02 and volume
        500.0 -> 9999.0 between them, wrote the row once and then
        silently dropped every later poll, leaving `taker_fee_rate=0.07`
        stale forever. "The fee in force" therefore means the fee as of
        the MOST RECENT poll that observed this book, not the fee at
        first sight — a reader (`mm_replay_snapshots`, T11) computing
        P&L from a row's `taker_fee_rate` gets the latest known rate for
        that quote, never a frozen one. `bids`/`asks`/`tick_size`/
        `min_size` are NOT refreshed on a hit: those are properties of
        the book itself, and an unchanged `ts` on Polymarket means the
        book has genuinely not moved (if it had, `ts` would differ and
        this would be a new row, not a hit) — there is no new depth to
        write.

        Also writes `volume_lifetime`/`observed_at`/`fee_source`/
        `maker_fee_rate` (mm-proveout T15, migration `008`):

        - `volume_lifetime` is `_raw_lifetime_volume(market)` (Kalshi
          `volume_fp`, Polymarket `volumeNum` — never `venue_volume`,
          which prefers the 24-hour fields this column exists to stop
          using), passed through `_monotonic_lifetime_volume` against
          the last known reading for this `(venue, market_id, outcome)`:
          on a HIT that "last known reading" is the row's OWN
          currently-stored `volume_lifetime` (about to be overwritten);
          on a MISS it is the most recent PRIOR row's `volume_lifetime`
          for this same key, fetched via `_last_known_lifetime_volume`
          — necessary because Kalshi's `ts=utcnow()` means Kalshi
          practically always takes the miss/insert branch (see the
          previous paragraph), so a guard that only compared within a
          single row's refresh would never fire for Kalshi at all, the
          venue the 24-hour-vs-lifetime finding was measured on.
        - `observed_at` is `utcnow()`, computed ONCE at the top of this
          call and written to BOTH branches UNCONDITIONALLY — not
          gated behind the `if row.volume != ... or ...` check below.
          Its whole job is "when did we last confirm this row is still
          current", which a content-gated write cannot answer for a
          market that is genuinely unchanged (same volume, same fee)
          for many consecutive polls.
        - `fee_source`/`maker_fee_rate` are `market.fee.source`/
          `market.fee.maker_rate` — the same `FeeSchedule` `taker_rate`/
          `maker_rebate_rate` above already read from, refreshed the
          same way on a hit.

        See `BookSnapshot`'s module docstring for the full rationale
        (irreversibility, the measured decrease/staleness numbers, and
        why each is `None` rather than `0.0` or negative).

        Args:
            session: Session to check against, and either update (on a
                hit) or add to (on a miss).
            book: The `OrderBook` just read from the venue.
            market: The book's parent market, for `tick_size`/`min_size`
                and, now, `fee`/volume.

        Returns:
            bool: `True` if a new row was added, `False` if the natural
                key `(venue, market_id, outcome, ts)` already existed
                (whether or not its volume/fee columns were refreshed).
        """
        observed_at = utcnow()
        outcome = outcome_key(book.outcome)
        existing = await session.execute(
            select(BookSnapshot).where(
                BookSnapshot.venue == book.venue,
                BookSnapshot.market_id == book.market_id,
                BookSnapshot.outcome == outcome,
                BookSnapshot.ts == book.ts,
            )
        )
        row = existing.scalar_one_or_none()
        if row is not None:
            current_volume = venue_volume(market)
            current_taker_fee_rate = market.fee.taker_rate
            current_maker_rebate_rate = market.fee.maker_rebate_rate
            current_maker_fee_rate = market.fee.maker_rate
            current_fee_source = market.fee.source
            current_volume_lifetime = _monotonic_lifetime_volume(
                previous=row.volume_lifetime,
                candidate=_raw_lifetime_volume(market),
            )
            if (
                row.volume != current_volume
                or row.taker_fee_rate != current_taker_fee_rate
                or row.maker_rebate_rate != current_maker_rebate_rate
                or row.maker_fee_rate != current_maker_fee_rate
                or row.fee_source != current_fee_source
                or row.volume_lifetime != current_volume_lifetime
            ):
                row.volume = current_volume
                row.taker_fee_rate = current_taker_fee_rate
                row.maker_rebate_rate = current_maker_rebate_rate
                row.maker_fee_rate = current_maker_fee_rate
                row.fee_source = current_fee_source
                row.volume_lifetime = current_volume_lifetime
            # Unconditional, unlike the block above: this poll DID just
            # confirm the row is current, whether or not anything else
            # about it changed (module docstring's `observed_at` section).
            row.observed_at = observed_at
            return False

        previous_volume_lifetime = await self._last_known_lifetime_volume(
            session, venue=book.venue, market_id=book.market_id, outcome=outcome
        )
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
                volume=venue_volume(market),
                taker_fee_rate=market.fee.taker_rate,
                maker_rebate_rate=market.fee.maker_rebate_rate,
                volume_lifetime=_monotonic_lifetime_volume(
                    previous=previous_volume_lifetime,
                    candidate=_raw_lifetime_volume(market),
                ),
                observed_at=observed_at,
                fee_source=market.fee.source,
                maker_fee_rate=market.fee.maker_rate,
            )
        )
        return True

    async def _last_known_lifetime_volume(
        self,
        session: AsyncSession,
        *,
        venue: VenueId,
        market_id: str,
        outcome: str,
    ) -> float | None:
        """Return the most recent prior `volume_lifetime` for this key.

        The monotonicity guard (`_monotonic_lifetime_volume`) needs "the
        last known reading" to compare a new one against. On a HIT,
        `_upsert_book_snapshot` already has that in hand (the row being
        refreshed). On a MISS — a brand-new `ts`, which is the ONLY path
        Kalshi practically ever takes (`ts=utcnow()` on every poll) —
        there is no in-hand row, so this queries the most recently
        observed row for the same `(venue, market_id, outcome)`, ordered
        by `ts` descending, and returns its `volume_lifetime` (which may
        itself be `None`, e.g. a prior restatement or a pre-`008` row).

        One extra `SELECT` per newly-inserted row, in addition to the
        existence check `_upsert_book_snapshot` already performs — the
        same trade-off that check-then-insert already makes (correctness
        that works identically on SQLite and Postgres) over a
        database-native `ON CONFLICT`/upsert that would not.

        Args:
            session: Session to query.
            venue: The book's venue.
            market_id: The book's venue-native market id.
            outcome: The book's ALREADY-canonicalized outcome (see
                `_upsert_book_snapshot`'s docstring on `outcome_key`).

        Returns:
            float | None: The most recent prior row's `volume_lifetime`,
                or `None` if there is no prior row for this key or its
                `volume_lifetime` is itself `None`.
        """
        result = await session.execute(
            select(BookSnapshot.volume_lifetime)
            .where(
                BookSnapshot.venue == venue,
                BookSnapshot.market_id == market_id,
                BookSnapshot.outcome == outcome,
            )
            .order_by(BookSnapshot.ts.desc(), BookSnapshot.id.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

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
