"""Data replay service for backtesting.

Streams historical market data from the database as MarketSnapshot
objects in chronological order for backtesting.
"""
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any
import logging

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.strategies.base import MarketSnapshot


logger = logging.getLogger(__name__)


class DataReplayer:
    """Streams historical market data for backtesting.

    Efficiently queries the database and yields MarketSnapshot objects
    in chronological order, simulating real-time data flow.

    Example:
        ```python
        async with get_session_context() as session:
            replayer = DataReplayer(
                session=session,
                start_date=datetime(2024, 1, 1),
                end_date=datetime(2024, 6, 1),
            )
            async for snapshot in replayer:
                # Process snapshot
                signal = strategy.on_market_data(snapshot)
        ```
    """

    def __init__(
        self,
        session: AsyncSession,
        start_date: datetime,
        end_date: datetime,
        market_ids: list[str] | None = None,
        batch_size: int = 1000,
        include_trades: bool = False,
        include_orderbook: bool = False,
        time_step: timedelta | None = None,
    ) -> None:
        """Initialize the data replayer.

        Args:
            session: Async database session.
            start_date: Start of replay period.
            end_date: End of replay period.
            market_ids: Optional filter for specific markets.
            batch_size: Number of records to fetch per query.
            include_trades: Include recent trades in snapshots.
            include_orderbook: Include orderbook data in snapshots.
            time_step: Optional minimum time between snapshots.
        """
        self.session = session
        self.start_date = start_date
        self.end_date = end_date
        self.market_ids = market_ids
        self.batch_size = batch_size
        self.include_trades = include_trades
        self.include_orderbook = include_orderbook
        self.time_step = time_step

        # State
        self._current_offset = 0
        self._total_snapshots = 0
        self._last_timestamp: datetime | None = None

        # Cache for market metadata
        self._market_metadata: dict[str, dict[str, Any]] = {}

    async def __aiter__(self) -> AsyncIterator[MarketSnapshot]:
        """Async iterator yielding MarketSnapshot objects."""
        # Import here to avoid circular imports
        from app.models.price_history import PriceHistory

        self._current_offset = 0

        while True:
            # Build query
            query = (
                select(PriceHistory)
                .where(
                    and_(
                        PriceHistory.timestamp >= self.start_date,
                        PriceHistory.timestamp <= self.end_date,
                    )
                )
                .order_by(PriceHistory.timestamp)
                .offset(self._current_offset)
                .limit(self.batch_size)
            )

            # Add market filter if specified
            if self.market_ids:
                query = query.where(PriceHistory.market_id.in_(self.market_ids))

            # Execute query
            result = await self.session.execute(query)
            rows = result.scalars().all()

            if not rows:
                break

            for row in rows:
                # Apply time step filter
                if self.time_step and self._last_timestamp:
                    if row.timestamp - self._last_timestamp < self.time_step:
                        continue

                # Convert to MarketSnapshot
                snapshot = await self._row_to_snapshot(row)
                self._last_timestamp = row.timestamp
                self._total_snapshots += 1

                yield snapshot

            self._current_offset += len(rows)

            # Check if we've reached the end
            if len(rows) < self.batch_size:
                break

    async def _row_to_snapshot(self, row: Any) -> MarketSnapshot:
        """Convert a database row to MarketSnapshot.

        Args:
            row: PriceHistory row.

        Returns:
            MarketSnapshot object.
        """
        # Get market metadata if available
        metadata = await self._get_market_metadata(row.market_id)

        # Build orderbook if requested
        orderbook: dict[str, Any] = {}
        if self.include_orderbook:
            orderbook = await self._get_orderbook(row.market_id, row.timestamp)

        # Get recent trades if requested
        recent_trades: list[dict[str, Any]] = []
        if self.include_trades:
            recent_trades = await self._get_recent_trades(
                row.market_id, row.timestamp
            )

        # Calculate spread if not stored
        spread = row.spread
        if spread is None and row.yes_bid and row.yes_ask:
            spread = row.yes_ask - row.yes_bid

        return MarketSnapshot(
            market_id=row.market_id,
            token_id=metadata.get("token_id", row.market_id),
            timestamp=row.timestamp,
            yes_price=row.yes_price,
            no_price=row.no_price,
            yes_bid=row.yes_bid,
            yes_ask=row.yes_ask,
            no_bid=row.no_bid,
            no_ask=row.no_ask,
            spread=spread,
            volume=row.volume or 0.0,
            volume_24h=row.volume_24h or 0.0,
            open_interest=row.open_interest,
            orderbook=orderbook,
            recent_trades=recent_trades,
            question=metadata.get("question", ""),
            category=metadata.get("category"),
            end_date=metadata.get("end_date"),
            resolution_rules=metadata.get("resolution_rules"),
        )

    async def _get_market_metadata(self, market_id: str) -> dict[str, Any]:
        """Get cached market metadata.

        Args:
            market_id: Market condition ID.

        Returns:
            Market metadata dictionary.
        """
        if market_id in self._market_metadata:
            return self._market_metadata[market_id]

        # Try to fetch from Market table
        try:
            from app.models.market import Market

            query = select(Market).where(Market.condition_id == market_id)
            result = await self.session.execute(query)
            market = result.scalar_one_or_none()

            if market:
                self._market_metadata[market_id] = {
                    "token_id": market.token_ids.get("yes", market_id) if market.token_ids else market_id,
                    "question": market.question,
                    "category": market.category,
                    "end_date": market.end_date,
                    "resolution_rules": market.metadata.get("resolution_rules") if market.metadata else None,
                }
            else:
                self._market_metadata[market_id] = {}

        except Exception as e:
            logger.warning(f"Failed to fetch market metadata for {market_id}: {e}")
            self._market_metadata[market_id] = {}

        return self._market_metadata[market_id]

    async def _get_orderbook(
        self,
        market_id: str,
        timestamp: datetime,
    ) -> dict[str, Any]:
        """Get orderbook state at timestamp.

        This is a placeholder - production would query orderbook snapshots.

        Args:
            market_id: Market ID.
            timestamp: Point in time.

        Returns:
            Orderbook data.
        """
        # Placeholder - would need orderbook snapshot table
        return {
            "bids": [],
            "asks": [],
            "timestamp": timestamp.isoformat(),
        }

    async def _get_recent_trades(
        self,
        market_id: str,
        timestamp: datetime,
        lookback_minutes: int = 60,
    ) -> list[dict[str, Any]]:
        """Get recent trades before timestamp.

        Args:
            market_id: Market ID.
            timestamp: Current timestamp.
            lookback_minutes: How far back to look.

        Returns:
            List of recent trade dictionaries.
        """
        try:
            from app.models.trade_history import TradeHistory

            lookback_start = timestamp - timedelta(minutes=lookback_minutes)

            query = (
                select(TradeHistory)
                .where(
                    and_(
                        TradeHistory.market_id == market_id,
                        TradeHistory.timestamp >= lookback_start,
                        TradeHistory.timestamp <= timestamp,
                    )
                )
                .order_by(TradeHistory.timestamp.desc())
                .limit(50)
            )

            result = await self.session.execute(query)
            trades = result.scalars().all()

            return [
                {
                    "timestamp": t.timestamp.isoformat(),
                    "side": t.side.value if hasattr(t.side, 'value') else t.side,
                    "outcome": t.outcome.value if hasattr(t.outcome, 'value') else t.outcome,
                    "price": t.price,
                    "size": t.size,
                    "maker_address": t.maker_address,
                    "taker_address": t.taker_address,
                }
                for t in trades
            ]

        except Exception as e:
            logger.warning(f"Failed to fetch recent trades for {market_id}: {e}")
            return []

    async def get_total_count(self) -> int:
        """Get total number of snapshots in the replay period.

        Returns:
            Total snapshot count.
        """
        from app.models.price_history import PriceHistory
        from sqlalchemy import func

        query = select(func.count(PriceHistory.id)).where(
            and_(
                PriceHistory.timestamp >= self.start_date,
                PriceHistory.timestamp <= self.end_date,
            )
        )

        if self.market_ids:
            query = query.where(PriceHistory.market_id.in_(self.market_ids))

        result = await self.session.execute(query)
        return result.scalar() or 0

    async def get_available_markets(self) -> list[str]:
        """Get list of markets available in the replay period.

        Returns:
            List of market IDs.
        """
        from app.models.price_history import PriceHistory
        from sqlalchemy import distinct

        query = select(distinct(PriceHistory.market_id)).where(
            and_(
                PriceHistory.timestamp >= self.start_date,
                PriceHistory.timestamp <= self.end_date,
            )
        )

        result = await self.session.execute(query)
        return [row[0] for row in result.all()]

    async def get_date_range(self) -> tuple[datetime | None, datetime | None]:
        """Get actual date range of available data.

        Returns:
            Tuple of (min_date, max_date).
        """
        from app.models.price_history import PriceHistory
        from sqlalchemy import func

        query = select(
            func.min(PriceHistory.timestamp),
            func.max(PriceHistory.timestamp),
        )

        if self.market_ids:
            query = query.where(PriceHistory.market_id.in_(self.market_ids))

        result = await self.session.execute(query)
        row = result.one_or_none()

        if row:
            return row[0], row[1]
        return None, None

    @property
    def progress(self) -> float:
        """Get replay progress (0-1)."""
        if self._last_timestamp is None:
            return 0.0

        total_duration = (self.end_date - self.start_date).total_seconds()
        if total_duration <= 0:
            return 1.0

        elapsed = (self._last_timestamp - self.start_date).total_seconds()
        return min(max(elapsed / total_duration, 0.0), 1.0)

    @property
    def snapshots_processed(self) -> int:
        """Get number of snapshots processed so far."""
        return self._total_snapshots


class InMemoryDataReplayer:
    """In-memory data replayer for testing.

    Replays pre-loaded MarketSnapshot objects without database access.
    """

    def __init__(
        self,
        snapshots: list[MarketSnapshot],
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> None:
        """Initialize with snapshot list.

        Args:
            snapshots: List of MarketSnapshot objects.
            start_date: Optional start filter.
            end_date: Optional end filter.
        """
        self.snapshots = sorted(snapshots, key=lambda s: s.timestamp)
        self.start_date = start_date
        self.end_date = end_date
        self._index = 0

    async def __aiter__(self) -> AsyncIterator[MarketSnapshot]:
        """Async iterator yielding snapshots."""
        for snapshot in self.snapshots:
            # Apply date filters
            if self.start_date and snapshot.timestamp < self.start_date:
                continue
            if self.end_date and snapshot.timestamp > self.end_date:
                continue

            self._index += 1
            yield snapshot

    @property
    def progress(self) -> float:
        """Get replay progress."""
        if not self.snapshots:
            return 1.0
        return self._index / len(self.snapshots)


def create_sample_snapshots(
    market_id: str,
    start_date: datetime,
    end_date: datetime,
    interval_minutes: int = 5,
    initial_price: float = 0.5,
    volatility: float = 0.02,
) -> list[MarketSnapshot]:
    """Create sample market snapshots for testing.

    Generates synthetic price data with random walk.

    Args:
        market_id: Market identifier.
        start_date: Start timestamp.
        end_date: End timestamp.
        interval_minutes: Minutes between snapshots.
        initial_price: Starting YES price.
        volatility: Price volatility per step.

    Returns:
        List of MarketSnapshot objects.
    """
    import random

    snapshots = []
    current_time = start_date
    current_price = initial_price

    while current_time <= end_date:
        # Random walk price change
        change = random.gauss(0, volatility)
        current_price = max(0.01, min(0.99, current_price + change))

        # Generate spread
        spread = random.uniform(0.01, 0.03)

        snapshot = MarketSnapshot(
            market_id=market_id,
            token_id=f"{market_id}_yes",
            timestamp=current_time,
            yes_price=current_price,
            no_price=1 - current_price,
            yes_bid=current_price - spread / 2,
            yes_ask=current_price + spread / 2,
            no_bid=(1 - current_price) - spread / 2,
            no_ask=(1 - current_price) + spread / 2,
            spread=spread,
            volume=random.uniform(100, 10000),
            volume_24h=random.uniform(10000, 100000),
            question=f"Sample market {market_id}?",
            category="test",
        )
        snapshots.append(snapshot)

        current_time += timedelta(minutes=interval_minutes)

    return snapshots
