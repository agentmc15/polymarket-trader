"""Recorded order-book depth for backtesting and paper trading (T21, PLAN.md D6/D10).

`PriceHistory` stores only top-of-book (`yes_bid`/`yes_ask`/`no_bid`/
`no_ask`), so until this table accumulates rows a backtest has no real
depth to walk and `app.execution.fill_engine.synthesize_book` FABRICATES
a one-level-per-side book from `liquidity_fraction * volume_24h` instead
(PLAN.md D6). `BookSnapshot` is the other half of that promise: a REAL,
observed order book — every level a venue actually quoted, for one
`(venue, market_id, outcome)` at one instant — persisted so
`app.services.backtesting.data_replay.DataReplayer` can attach it to a
`MarketSnapshot` and `app.services.backtesting.engine.Backtester._book_for`
can walk real levels instead of an invented one, labeling the result
`depth_source="recorded"` (GUARDRAILS.md §1.7) wherever it does.

WHY `outcome` IS NOT CONSTRAINED TO `"YES"`/`"NO"` (T21 carry-forward 1,
recorded in NOTES.md): `app.strategies.multi_outcome_bundle_arbitrage`
needs a book for EVERY outcome of a >=3-outcome market (e.g. a named
candidate in an election market), not just a binary complement.
`app.services.data_collector.DataCollector.collect_books` therefore
iterates `VenueMarket.outcomes` in full — whatever labels the venue
payload actually carries — and writes one row per outcome, so this table
is the first place in the persisted schema where an arbitrary,
non-binary outcome label gets first-class depth. `outcome` stores that
label's canonical IDENTITY — `app.strategies.base.outcome_key()`:
`"YES"`/`"NO"` for the binary pair, and every other label stripped of
surrounding whitespace and case-folded — the same identity
`Position.position_id` is built from. A lookup by
`(venue, market_id, outcome)` therefore cannot miss a row purely because
of a casing or whitespace mismatch between two venues' payloads, and the
UNIQUE constraint below dedupes what it claims to dedupe.

T21d defect 6 (NOTES.md) is why that sentence is now specific about
WHICH canonicalization. This column previously stored
`normalize_outcome()`'s output, which canonicalizes `"YES"`/`"NO"` and
passes every other label through verbatim — so the guarantee held for
exactly the binary case that never needed it. Four `collect_books` runs
over one book at one `ts`, spelled `"Trump"`/`"TRUMP"`/`"trump"`/
`"Trump "`, wrote FOUR rows, and `DataReplayer._get_recorded_book` —
which matches `outcome` EXACTLY — found none of them for a caller
asking under a fifth spelling. Migration `007` canonicalizes the rows
written under the old rule.

The stored label is an identity, not a display string: a caller that
wants to SHOW a venue's own capitalization of an outcome must read it
from the venue payload (`VenueMarket.outcomes`), not from here. That
trade is deliberate — this column's whole job is to be found again.

WHY `depth_source` EXISTS ON A TABLE THAT ONLY EVER STORES OBSERVED
DEPTH: every row `collect_books` writes comes from a real
`VenueAdapter.get_book()` response, so today this column is written as
`"recorded"` on every row and nothing else. It is still a first-class
column, not a hardcoded assumption at read time, for two reasons: (1)
`app.venues.types.OrderBook`/`DepthSource` is the canonical vocabulary
this whole codebase uses to describe book provenance, and reusing it
here means `data_replay.py`'s conversion of a row back into an
`OrderBook` reads this column instead of hardcoding the literal
`"recorded"` a second place; (2) if a future collection path ever
records a PARTIALLY synthetic book (e.g. an adapter that pads a thin,
observed book with a modeled tail), the schema does not need a migration
to say so — it already has the column to be honest about it.

UNIQUE on `(venue, market_id, outcome, ts)`: a re-run of `collect_books`
over data that has not moved (the same venue quote, observed again at
the same reported timestamp — `tests.venues.fixture_adapter.FixtureAdapter`
`get_book` is deliberately STATIC, returning the identical book, and
therefore the identical `ts`, on every call) must not accumulate
duplicate rows. `DataCollector.collect_books` enforces this with a
check-then-insert (query the natural key, skip if present) rather than a
database-native `ON CONFLICT` clause, so the same code path is correct
on SQLite (tests) and Postgres (production) without a dialect branch —
see that method's docstring for why.
"""
from datetime import datetime
from typing import Literal

from sqlalchemy import CheckConstraint, DateTime, Float, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, JSONList

#: Mirrors `app.venues.types.DepthSource` exactly. Not imported — see
#: `app.models.trade._VENUE_VALUES` for why `app.models` does not import
#: from sibling packages at runtime; keep these in sync by hand.
BookSnapshotDepthSource = Literal["recorded", "synthetic"]

#: Mirrors `app.venues.types.VenueId` exactly (see the note above).
_VENUE_VALUES = ("polymarket", "kalshi")
_DEPTH_SOURCE_VALUES = ("recorded", "synthetic")


class BookSnapshot(Base):
    """One observed order book for `(venue, market_id, outcome)` at `ts`.

    Attributes:
        id: Surrogate primary key, autoincrement.
        venue: `"polymarket"` or `"kalshi"`.
        market_id: Venue-native market identifier (Polymarket
            `condition_id`; Kalshi `ticker`).
        outcome: Outcome IDENTITY, canonicalized via
            `app.strategies.base.outcome_key()` before it ever reaches
            this column — `"YES"`/`"NO"` for a binary market, or a
            bundle's named outcome stripped and case-folded (e.g.
            `"Trump"` and `"TRUMP"` both store `"trump"`). See the
            module docstring for why this is not constrained to a binary
            pair, and why it is an identity rather than the venue's own
            spelling.
        ts: Aware UTC time this book was observed (the venue's own
            `OrderBook.ts`, not collection wall-clock time — the two can
            differ under retry/backoff, and only the venue's own
            timestamp is meaningful for `data_replay.py`'s "within
            `book_match_window_s` before the price row's `ts`" matching).
        bids: JSON list of `{"price": float, "size": float}`, best price
            first — mirrors `app.venues.types.BookLevel` field-by-field
            so the round trip through `OrderBook` is lossless.
        asks: JSON list of `{"price": float, "size": float}`, best price
            first.
        tick_size: Minimum price increment on this market, a probability
            in `(0.0, 1.0]` — copied from `VenueMarket.tick_size` at
            collection time so a replayed fill can still validate against
            it (`SimulatedFillEngine.fill`'s `market` parameter).
        min_size: Minimum order size in contracts, `>= 0` — copied from
            `VenueMarket.min_size`.
        depth_source: See `BookSnapshotDepthSource` and the module
            docstring's "WHY `depth_source` EXISTS" section. Every row
            `collect_books` writes sets this to `"recorded"`.
    """

    __tablename__ = "book_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    venue: Mapped[str] = mapped_column(String(16), nullable=False)
    market_id: Mapped[str] = mapped_column(String(128), nullable=False)
    outcome: Mapped[str] = mapped_column(String(50), nullable=False)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    bids: Mapped[list] = mapped_column(JSONList, default=list, nullable=False)
    asks: Mapped[list] = mapped_column(JSONList, default=list, nullable=False)
    tick_size: Mapped[float] = mapped_column(Float, nullable=False)
    min_size: Mapped[float] = mapped_column(Float, nullable=False)
    depth_source: Mapped[str] = mapped_column(
        String(16),
        default="recorded",
        server_default="recorded",
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "venue",
            "market_id",
            "outcome",
            "ts",
            name="uq_book_snapshots_venue_market_outcome_ts",
        ),
        Index(
            "ix_book_snapshots_venue_market_outcome",
            "venue",
            "market_id",
            "outcome",
        ),
        CheckConstraint(
            f"venue IN ({', '.join(repr(v) for v in _VENUE_VALUES)})",
            name="ck_book_snapshots_venue_valid",
        ),
        CheckConstraint(
            f"depth_source IN ({', '.join(repr(v) for v in _DEPTH_SOURCE_VALUES)})",
            name="ck_book_snapshots_depth_source_valid",
        ),
        CheckConstraint(
            "tick_size > 0.0 AND tick_size <= 1.0",
            name="ck_book_snapshots_tick_size_range",
        ),
        CheckConstraint(
            "min_size >= 0.0",
            name="ck_book_snapshots_min_size_nonnegative",
        ),
    )
