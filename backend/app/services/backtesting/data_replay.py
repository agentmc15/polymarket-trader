"""Data replay service for backtesting.

Streams historical market data from the database as `MarketSnapshot`
objects — interleaved with `ResolutionEvent`s (T09) — in chronological
order for backtesting.

Look-ahead hygiene (PLAN.md D6, GUARDRAILS.md §1.7)
---------------------------------------------------
A `MarketSnapshot` carries ONLY what was knowable at its `timestamp`.
The `Market` row a snapshot is enriched from also holds fields that
encode the FUTURE relative to that timestamp — `is_resolved`,
`resolution_outcome`, `resolved_at`, and the CURRENT `outcome_prices` —
and none of them may ever reach a snapshot. A strategy that can read
`is_resolved` off the snapshot it is deciding from is reading the answer
sheet, and every number the backtest produces after that is fiction.

The rule is ENFORCED, not merely documented: `_get_market_metadata`
raises if its result ever contains one of `FUTURE_ENCODING_MARKET_FIELDS`,
so a future edit that adds one fails loudly instead of quietly
manufacturing edge. Resolution reaches the engine the only legitimate
way — as a `ResolutionEvent` emitted AT `resolved_at`, after every
snapshot that predates it.

(The fields that ARE attached — `token_id`, `question`, `category`,
`end_date`, `resolution_rules` — are all knowable when the market opens.
`end_date` is the scheduled close time, not the settlement outcome.)
"""
import heapq
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.strategies.base import MarketSnapshot, outcome_key
from app.utils.time import ensure_aware
from app.venues.types import BookLevel, OrderBook, VenueId

logger = logging.getLogger(__name__)

#: `Market` columns that must NEVER be copied onto a `MarketSnapshot`,
#: because each one states something that was not known at the
#: snapshot's timestamp:
#:
#: - `is_resolved` / `resolution_outcome` / `resolved_at`: the answer.
#: - `outcome_prices`: the market's CURRENT prices — the price as of when
#:   the replay was set up, not as of the snapshot. A strategy reading it
#:   would be looking at the end of the series.
#:
#: Enforced in `_get_market_metadata`.
FUTURE_ENCODING_MARKET_FIELDS: frozenset[str] = frozenset(
    {"is_resolved", "resolution_outcome", "resolved_at", "outcome_prices"}
)


@dataclass(frozen=True)
class ResolutionEvent:
    """A market settling, delivered in the replay stream at `resolved_at`.

    This is the ONLY channel through which resolution reaches the engine
    (PLAN.md D6). Emitting it as a timestamped stream item — rather than
    attaching `is_resolved`/`resolution_outcome` to snapshots — is what
    keeps the outcome unknowable to a strategy until the instant it
    actually became known.

    Attributes:
        market_id: Venue-native market identifier.
        venue: `"polymarket"` or `"kalshi"`. Together with `market_id`
            this keys the engine's positions
            (`f"{venue}:{market_id}:{outcome}"`).
        winning_outcome: The outcome that pays $1.00 per contract; every
            other outcome on the market pays $0.00. Compared
            case-insensitively against `Position.outcome`, since the two
            venues disagree on casing (`"Yes"` vs `"yes"`).
        resolved_at: Aware UTC time the market resolved. The event is
            emitted at this point in the stream, strictly after every
            snapshot that predates it.
    """

    market_id: str
    venue: VenueId
    winning_outcome: str
    resolved_at: datetime

    def __post_init__(self) -> None:
        """Validate `resolved_at` is aware UTC and `winning_outcome` is set.

        Raises:
            TypeError: If `resolved_at` is not a `datetime`.
            ValueError: If `resolved_at` is naive, or `winning_outcome`
                is empty — settling to "nothing" would silently zero
                every position on the market.
        """
        ensure_aware(self.resolved_at)
        if not self.winning_outcome:
            raise ValueError("winning_outcome must be non-empty")


#: One item of a replay stream. The engine dispatches on the type.
ReplayItem = MarketSnapshot | ResolutionEvent


def _as_utc(value: datetime) -> datetime:
    """Interpret a database timestamp as aware UTC.

    `DateTime(timezone=True)` columns come back AWARE from Postgres but
    NAIVE from SQLite, which stores no offset — so the same replay code
    that works in production raises `TypeError: can't compare
    offset-naive and offset-aware datetimes` the moment it runs against
    the test database. This is the boundary where that is settled, and
    only here.

    Reading a naive value as UTC is not a guess: every datetime this
    codebase writes is UTC (GUARDRAILS.md §4), and the SQLite bind
    processor discards the offset on the way IN, so the stored wall clock
    already IS UTC. The alternative — letting the naive value through —
    means the comparison either raises or, worse, silently succeeds
    against another naive value that came from somewhere else.

    Args:
        value: A datetime loaded from a `DateTime(timezone=True)` column.

    Returns:
        datetime: The same instant, guaranteed aware.

    Raises:
        TypeError: If `value` is not a `datetime`.
    """
    if not isinstance(value, datetime):
        raise TypeError(f"expected datetime, got {type(value).__name__}")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _merge_key(item: ReplayItem) -> tuple[datetime, int]:
    """Return the sort key that interleaves events and snapshots.

    The second element breaks ties so a `ResolutionEvent` sorts BEFORE a
    `MarketSnapshot` bearing the identical timestamp. That ordering is
    deliberate: once a market has resolved, a quote on it is not
    tradeable information, so settling first means no strategy can act on
    a post-resolution price at the resolution instant.

    Args:
        item: A snapshot or a resolution event.

    Returns:
        tuple[datetime, int]: `(timestamp, 0)` for an event,
            `(timestamp, 1)` for a snapshot.
    """
    if isinstance(item, ResolutionEvent):
        return (item.resolved_at, 0)
    return (item.timestamp, 1)


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

        # Pending resolution events, as a heap of
        # `(resolved_at, market_id, event)` — small (one entry per
        # resolved market in the window), and drained as the snapshot
        # clock passes each `resolved_at`.
        self._resolution_heap: list[tuple[datetime, str, ResolutionEvent]] = []

        # Cache for market metadata
        self._market_metadata: dict[str, dict[str, Any]] = {}

    async def _load_resolution_events(self) -> list[ResolutionEvent]:
        """Build the heap of `ResolutionEvent`s due inside the window.

        A market qualifies when it `is_resolved`, carries a
        `resolved_at`, and that timestamp is at or before `end_date`. A
        market resolved BEFORE `start_date` still qualifies and its event
        is emitted first, ahead of every snapshot — because any quote
        recorded for it inside the window is post-resolution noise and
        must not be traded on.

        A row that claims `is_resolved` but has no `resolved_at` or no
        `resolution_outcome` is SKIPPED with a warning rather than
        guessed at: settling to an invented outcome would fabricate the
        entire P&L of every position on that market.

        Returns:
            list[ResolutionEvent]: A heap (via `heapq.heapify`) ordered by
                `(resolved_at, market_id)`.
        """
        from app.models.market import Market

        market_ids = self.market_ids or await self.get_available_markets()
        if not market_ids:
            return []

        query = select(Market).where(
            and_(
                Market.condition_id.in_(market_ids),
                Market.is_resolved.is_(True),
                Market.resolved_at.isnot(None),
                Market.resolved_at <= self.end_date,
            )
        )
        result = await self.session.execute(query)

        events: list[ResolutionEvent] = []
        for market in result.scalars().all():
            # The query already filters `resolved_at IS NOT NULL`; the
            # local check is what lets the type checker (and a future
            # caller that reuses this loop) see that.
            if market.resolved_at is None:
                continue
            if not market.resolution_outcome:
                logger.warning(
                    "market %r is flagged resolved but names no "
                    "resolution_outcome; skipping settlement rather than "
                    "guessing a winner",
                    market.condition_id,
                    extra={"market_id": market.condition_id},
                )
                continue
            events.append(
                ResolutionEvent(
                    market_id=market.condition_id,
                    venue="polymarket",
                    winning_outcome=market.resolution_outcome,
                    resolved_at=_as_utc(market.resolved_at),
                )
            )

        heap = [(e.resolved_at, e.market_id, e) for e in events]
        heapq.heapify(heap)
        self._resolution_heap = heap
        return events

    def _drain_events_until(
        self, cutoff: datetime
    ) -> list[ResolutionEvent]:
        """Pop every pending resolution event due at or before `cutoff`.

        Args:
            cutoff: Replay time reached so far (aware UTC).

        Returns:
            list[ResolutionEvent]: Due events, earliest first.
        """
        due: list[ResolutionEvent] = []
        while self._resolution_heap and self._resolution_heap[0][0] <= cutoff:
            due.append(heapq.heappop(self._resolution_heap)[2])
        return due

    async def __aiter__(self) -> AsyncIterator[ReplayItem]:
        """Yield `MarketSnapshot`s and `ResolutionEvent`s in time order.

        Resolution events are held in a small heap and released as the
        snapshot clock passes each `resolved_at`, so an event is always
        emitted AFTER every snapshot that predates it and BEFORE every
        snapshot that follows it. Anything still pending when the
        snapshot stream is exhausted is flushed at the end, so a market
        that resolves after its last recorded quote still settles.

        Yields:
            ReplayItem: A `MarketSnapshot` or a `ResolutionEvent`.
        """
        # Import here to avoid circular imports
        from app.models.price_history import PriceHistory

        self._current_offset = 0
        await self._load_resolution_events()

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
                row_ts = _as_utc(row.timestamp)
                # The replay clock advances with the ROW, not with the
                # snapshot that survives the time_step filter: a skipped
                # row still moves time forward, and a resolution that
                # happened before it must not be deferred past it.
                for event in self._drain_events_until(row_ts):
                    yield event

                # Apply time step filter
                if (
                    self.time_step
                    and self._last_timestamp
                    and row_ts - self._last_timestamp < self.time_step
                ):
                    continue

                # Convert to MarketSnapshot
                snapshot = await self._row_to_snapshot(row)
                self._last_timestamp = row_ts
                self._total_snapshots += 1

                yield snapshot

            self._current_offset += len(rows)

            # Check if we've reached the end
            if len(rows) < self.batch_size:
                break

        # Markets that resolved after their last recorded quote still
        # settle: leaving them open would silently convert a resolved
        # position into an "unrealized at end" mark, which is a different
        # (and flattering) number.
        for event in self._drain_events_until(self.end_date):
            yield event

    async def _row_to_snapshot(self, row: Any) -> MarketSnapshot:
        """Convert a database row to MarketSnapshot.

        Args:
            row: PriceHistory row.

        Returns:
            MarketSnapshot object.
        """
        # Get market metadata if available
        metadata = await self._get_market_metadata(row.market_id)
        # SQLite hands back naive datetimes for `DateTime(timezone=True)`
        # columns; `MarketSnapshot` rejects a naive timestamp outright
        # (PLAN.md R9), so the coercion happens here at the boundary.
        row_ts = _as_utc(row.timestamp)

        # Build orderbook if requested
        orderbook: dict[str, Any] = {}
        if self.include_orderbook:
            orderbook = await self._get_orderbook(row.market_id, row_ts)

        # Get recent trades if requested
        recent_trades: list[dict[str, Any]] = []
        if self.include_trades:
            recent_trades = await self._get_recent_trades(
                row.market_id, row_ts
            )

        # Calculate spread if not stored
        spread = row.spread
        if spread is None and row.yes_bid and row.yes_ask:
            spread = row.yes_ask - row.yes_bid

        # T21 (PLAN.md D6/D10): attach a RECORDED book when one exists
        # for this row's own (venue, market, outcome) within
        # `book_match_window_s` before `row_ts`. `PriceHistory` is
        # Polymarket-only and inherently binary (yes_price/no_price
        # columns, no per-outcome rows), so "this row's own outcome" is
        # always `"YES"` here — the same "primary outcome" convention
        # `app.services.scanner._snapshot_from_market` documents for the
        # identical reason (`MarketSnapshot.book` is a SINGLE field, one
        # outcome at a time). `_get_recorded_book` itself makes no
        # YES/NO assumption (it matches whatever `outcome` string it is
        # given, verbatim), so a caller that DOES know a market's
        # non-binary outcome label can reuse it unchanged (T21
        # carry-forward 1) — the binary default lives here, at this one
        # call site, not inside the lookup. When no recorded book
        # qualifies, `Backtester._book_for` synthesizes one from this
        # snapshot's top-of-book quotes instead (T07/PLAN.md D6).
        book = await self._get_recorded_book(
            venue="polymarket", market_id=row.market_id, outcome="YES", ts=row_ts
        )

        return MarketSnapshot(
            market_id=row.market_id,
            token_id=metadata.get("token_id", row.market_id),
            timestamp=row_ts,
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
            end_date=(
                _as_utc(metadata["end_date"])
                if metadata.get("end_date") is not None
                else None
            ),
            resolution_rules=metadata.get("resolution_rules"),
            book=book,
        )

    async def _get_recorded_book(
        self,
        venue: VenueId,
        market_id: str,
        outcome: str,
        ts: datetime,
    ) -> OrderBook | None:
        """Return the most recent recorded `BookSnapshot` for `(venue, market_id, outcome)`.

        A row qualifies when its own `ts` is AT OR BEFORE `ts` (never
        after — attaching a book observed in the future relative to the
        price row it is enriching would be look-ahead, PLAN.md D6) and
        within `settings.book_match_window_s` seconds before it. Among
        qualifying rows, the MOST RECENT one is used — the freshest
        depth still inside the window is a better stand-in for "the book
        at this instant" than an older one.

        `outcome` is matched EXACTLY, with no YES/NO fallback (T21
        carry-forward 1): this function's contract is "find the recorded
        book for the outcome you asked for", full stop, so it works
        identically whether the caller passes `"YES"` or an arbitrary
        multi-outcome bundle label like `"Trump"`.

        What it matches exactly is the outcome's IDENTITY, not the
        caller's spelling: `app.services.data_collector.DataCollector.
        _upsert_book_snapshot` persists every row under
        `app.strategies.base.outcome_key()`, and this canonicalizes the
        argument the same way before querying (T21d defect 6). Without
        that, a caller asking for `"Trump"` missed a row written as
        `"TRUMP"` and got no depth at all — `Backtester._book_for`
        cannot synthesize a non-binary book, so the leg simply never
        filled. The two sides now agree on spelling regardless of which
        venue's casing convention produced the label.

        Args:
            venue: Venue of the book to look up.
            market_id: Venue-native market identifier.
            outcome: Outcome label to match, in any spelling —
                canonicalized to its identity here.
            ts: The price row's own timestamp — the upper bound of the
                match window.

        Returns:
            OrderBook | None: The recorded book (tagged
                `depth_source="recorded"` — see `BookSnapshot.
                depth_source`), or `None` if no row qualifies.
        """
        from app.models.book_snapshot import BookSnapshot

        window = timedelta(seconds=settings.book_match_window_s)
        query = (
            select(BookSnapshot)
            .where(
                and_(
                    BookSnapshot.venue == venue,
                    BookSnapshot.market_id == market_id,
                    BookSnapshot.outcome == outcome_key(outcome),
                    BookSnapshot.ts <= ts,
                    BookSnapshot.ts >= ts - window,
                )
            )
            .order_by(BookSnapshot.ts.desc())
            .limit(1)
        )
        result = await self.session.execute(query)
        row = result.scalars().first()
        if row is None:
            return None

        return OrderBook(
            venue=cast(VenueId, row.venue),
            market_id=row.market_id,
            outcome=row.outcome,
            bids=tuple(
                BookLevel(price=level["price"], size=level["size"])
                for level in row.bids
            ),
            asks=tuple(
                BookLevel(price=level["price"], size=level["size"])
                for level in row.asks
            ),
            ts=_as_utc(row.ts),
            metadata={"depth_source": row.depth_source},
        )

    async def _get_market_metadata(self, market_id: str) -> dict[str, Any]:
        """Get cached market metadata, with the look-ahead guard applied.

        ONLY fields knowable when the market opened are copied:
        `token_id`, `question`, `category`, `end_date` (the scheduled
        close time) and `resolution_rules` (the published criteria — the
        rules, never the answer).

        The returned dict is checked against
        `FUTURE_ENCODING_MARKET_FIELDS` before it is cached. That check
        cannot fail for the code as written; it exists so that the day
        someone adds `"is_resolved": market.is_resolved` here — the
        single cheapest way to destroy every number this engine
        produces — the run stops instead of quietly reporting a strategy
        that could see the answer sheet (GUARDRAILS.md §1.7).

        Args:
            market_id: Market condition ID.

        Returns:
            dict[str, Any]: Market metadata, guaranteed free of any
                future-encoding field.

        Raises:
            ValueError: If the assembled metadata contains a field from
                `FUTURE_ENCODING_MARKET_FIELDS`.
        """
        if market_id in self._market_metadata:
            return self._market_metadata[market_id]

        metadata: dict[str, Any] = {}
        # Try to fetch from Market table
        try:
            from app.models.market import Market

            query = select(Market).where(Market.condition_id == market_id)
            result = await self.session.execute(query)
            market = result.scalar_one_or_none()

            if market:
                metadata = {
                    "token_id": market.token_ids.get("yes", market_id) if market.token_ids else market_id,
                    "question": market.question,
                    "category": market.category,
                    "end_date": market.end_date,
                    "resolution_rules": market.extra_data.get("resolution_rules") if market.extra_data else None,
                }

        except Exception as e:
            logger.warning(f"Failed to fetch market metadata for {market_id}: {e}")
            metadata = {}

        leaked = FUTURE_ENCODING_MARKET_FIELDS & metadata.keys()
        if leaked:
            raise ValueError(
                f"market metadata for {market_id!r} carries future-encoding "
                f"field(s) {sorted(leaked)}; a snapshot must contain only what "
                "was knowable at its timestamp (PLAN.md D6). Resolution "
                "reaches the engine as a ResolutionEvent, never as a snapshot "
                "attribute."
            )

        self._market_metadata[market_id] = metadata
        return metadata

    async def _get_orderbook(
        self,
        market_id: str,  # noqa: ARG002 - placeholder signature, see docstring
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
        from sqlalchemy import func

        from app.models.price_history import PriceHistory

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
        from sqlalchemy import distinct

        from app.models.price_history import PriceHistory

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
        from sqlalchemy import func

        from app.models.price_history import PriceHistory

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

    Replays pre-loaded `MarketSnapshot` objects — and, optionally,
    `ResolutionEvent`s interleaved by time — without database access.
    """

    def __init__(
        self,
        snapshots: list[MarketSnapshot],
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        resolutions: list[ResolutionEvent] | None = None,
    ) -> None:
        """Initialize with snapshot list.

        Args:
            snapshots: List of MarketSnapshot objects.
            start_date: Optional start filter.
            end_date: Optional end filter.
            resolutions: Optional `ResolutionEvent`s, interleaved with
                the snapshots by timestamp exactly as `DataReplayer`
                does. An event sharing a timestamp with a snapshot is
                emitted FIRST (see `_merge_key`).
        """
        self.snapshots = sorted(snapshots, key=lambda s: s.timestamp)
        self.resolutions = sorted(
            resolutions or [], key=lambda e: (e.resolved_at, e.market_id)
        )
        self.start_date = start_date
        self.end_date = end_date
        self._index = 0

    async def __aiter__(self) -> AsyncIterator[ReplayItem]:
        """Yield snapshots and resolution events in merged time order.

        `_index` (and therefore `progress`) counts SNAPSHOTS only, so a
        stream carrying resolution events does not report progress above
        1.0.

        Yields:
            ReplayItem: A `MarketSnapshot` or a `ResolutionEvent`.
        """
        merged: list[ReplayItem] = sorted(
            [*self.snapshots, *self.resolutions], key=_merge_key
        )
        for item in merged:
            timestamp = _merge_key(item)[0]
            # Apply date filters
            if self.start_date and timestamp < self.start_date:
                continue
            if self.end_date and timestamp > self.end_date:
                continue

            if isinstance(item, MarketSnapshot):
                self._index += 1
            yield item

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
        start_date: Start timestamp. Must be aware UTC (PLAN.md R9).
        end_date: End timestamp. Must be aware UTC (PLAN.md R9).
        interval_minutes: Minutes between snapshots.
        initial_price: Starting YES price.
        volatility: Price volatility per step.

    Returns:
        List of MarketSnapshot objects, each with an aware UTC `timestamp`.

    Raises:
        TypeError: If `start_date`/`end_date` is not a `datetime`.
        ValueError: If `start_date`/`end_date` is naive (`tzinfo is None`).
    """
    import random

    ensure_aware(start_date)
    ensure_aware(end_date)

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
