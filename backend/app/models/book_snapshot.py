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

`volume`/`taker_fee_rate`/`maker_rebate_rate` (mm-proveout T8, PLAN.md
D9, migration `008`) exist because a book alone cannot answer "was
anything happening here" or "what did crossing this cost, right then":

- `volume`: `app.venues.types.venue_volume(market)` on the LISTING
  payload at collection time. Kalshi's `PriceHistory` volume series is
  Polymarket-only (`DataCollector.collect_price_snapshot` never writes a
  Kalshi row), so the delta between two consecutive snapshots' `volume`
  is the ONLY activity signal a Kalshi `BookSnapshot` can carry — the
  same role `app.venues.kalshi.candles.Candle.volume` plays for the
  retrospective replay, needed here for the optimistic fill model
  (`app.scripts.mm_replay_snapshots`, T11).
- `taker_fee_rate`/`maker_rebate_rate`: `market.fee.taker_rate`/
  `market.fee.maker_rebate_rate` — `app.venues.types.FeeSchedule`,
  GUARDRAILS.md §1.5's one sanctioned source of a fee. Polymarket's
  per-market `feeSchedule` changes over time
  (`source="venue_schedule"`), so the fee IN FORCE has to travel with
  the row rather than being re-looked-up later against whatever the
  schedule has since become. `maker_rebate_rate` is data only:
  GUARDRAILS.md §2.3/D6 still forbid `FeeModel.fee()` from ever
  crediting it, and nothing here changes that.

"THE FEE IN FORCE" MEANS THE LATEST OBSERVED FEE FOR THIS ROW'S `ts`,
NOT THE FIRST ONE (T8 red-team retry): `ts` is the BOOK's own identity
(when it last moved), not a guarantee that the market's volume or fee
schedule was unchanged since the row was first written.
`DataCollector._upsert_book_snapshot` refreshes `volume`/
`taker_fee_rate`/`maker_rebate_rate` in place whenever a later
`collect_books` poll reports the SAME `(venue, market_id, outcome, ts)`
but a DIFFERENT `venue_volume`/`market.fee` — which happens on
Polymarket specifically because `OrderBook.ts` is the time the book
last moved, not poll time, so a quiet market can report the identical
`ts` for many polls while its `feeSchedule` changes underneath. A row's
`taker_fee_rate` therefore means "the taker rate as of the most recent
poll that observed this book", and a reader (`mm_replay_snapshots`,
T11) computing P&L from it is using the latest known rate for that
quote, never a value frozen at first sight. `bids`/`asks`/`tick_size`/
`min_size` are NOT refreshed this way: an unchanged `ts` means the book
itself has not moved (if it had, `ts` would differ and a new row would
be written), so there is never new depth to write in place.

All three are NULLABLE with no backfill (migration `008`): every row
collected before this migration has no venue call this migration could
make to reconstruct a historical volume or fee schedule, so `NULL` here
means "collected before T8", never `0.0` — a reader (`mm_replay_snapshots`)
must treat it as missing, not as a zero rate or zero volume.

`volume_lifetime`/`observed_at`/`fee_source`/`maker_fee_rate` (mm-proveout
T15, migration `008` -- same migration as the three columns above, because
it has not been applied to any database and this is the only window in
which adding a column is free) exist because the Phase 2 review found
three of the above four IRREVERSIBLE the moment collection starts -- all
three are missing columns, and a column cannot be added to data already
collected without one:

- `volume_lifetime`: `venue_volume(market)` is not a between-snapshot
  delta signal. It tries the 24-HOUR fields first (`volume_24h_fp`,
  `volume24hr`) because it is written for RANKING -- correct for that,
  wrong here. Measured live 2026-09-07: Kalshi's `volume_24h_fp` did not
  move for a single one of 6,058 actively-traded markets over 245
  seconds (it is a periodically recomputed aggregate, not a live
  counter), while lifetime `volume_fp` moved for 25 of the same 6,058.
  `passive_fill.py:142` returns no fills at all when `volume <= 0.0`, and
  `:97` raises on a negative -- gating BOTH fill models -- so a volume
  channel that never moves silently replays to zero fills forever. This
  column carries the LIFETIME counter instead (Kalshi `volume_fp`,
  Polymarket `volumeNum`), read by
  `app.services.data_collector._raw_lifetime_volume`.

  NOT SIMPLY THE LIFETIME KEY, UNGUARDED: Gamma's (Polymarket's) lifetime
  `volumeNum` was measured DECREASING for 29 of 255 markets over ~28
  minutes -- a lifetime counter can still be restated. A decrease is
  therefore stored as `None`, never the smaller value and never clamped
  to `0.0`: `0.0` would assert "nothing traded" (a claim `passive_fill.py`
  treats as a hard gate) and a negative would raise inside `TradeRange`.
  `None` is the only value that means "unknown, because the counter moved
  backwards." See `app.services.data_collector._monotonic_lifetime_volume`
  for the guard -- it compares only against the immediately preceding
  known value for this `(venue, market_id, outcome)`, not the deepest
  historical one, so one restatement resets the baseline rather than
  permanently vetoing every later, legitimately smaller-than-ancient
  reading.

- `observed_at`: the poll's own wall-clock time (`app.utils.time.utcnow()`),
  NOT `ts` -- `ts` is the book's own identity (when it last moved), and
  Polymarket's same-`ts` refresh path (see "THE FEE IN FORCE" above) means
  a row's `ts` can be far older than the moment it was last confirmed
  alive. Without this column, "the book has been quiet" and "the
  collector died" are permanently indistinguishable
  (`_GAP_SEMANTICS["polymarket"]` says so explicitly). Written on EVERY
  insert AND EVERY refresh -- not only when `volume`/fee actually changed
  -- because its job is to answer "when did we last confirm this row is
  still current", which a content-gated write cannot answer for a market
  that is genuinely unchanged for many polls in a row.

  THE SHIFT THIS COLUMN LETS T11 CORRECT FOR: a same-`ts` refresh
  overwrites `volume`/`taker_fee_rate`/`maker_rebate_rate`/
  `volume_lifetime` in place, so those columns on a refreshed row hold the
  values as of the LAST poll that saw this `ts` -- a moment close to the
  NEXT distinct snapshot's `ts`, not this one's. The volume channel is
  therefore shifted forward by roughly one collection dwell and partly
  measures activity AFTER this row's own mark (adjacent to a look-ahead,
  GUARDRAILS.md §4.3). A reader comparing `observed_at` to the next row's
  `ts` can detect and bound this shift; a reader with only `ts` cannot.

- `fee_source`: mirrors `market.fee.source` (`FeeSchedule.source`) -- the
  SAME field `taker_fee_rate`/`maker_rebate_rate` above already read from
  `market.fee`, just not previously carried. `_published_fee_schedule`
  (Polymarket) returns `None` for a payload it cannot honour and the
  caller falls back to `category_fee_schedule` -- a table its own
  docstring records as wrong on 82% of 1,918 live Polymarket markets.
  Preflight cannot catch that fallback: its check is
  `isinstance(market.fee.taker_rate, float)`, and a wrong default is a
  perfectly good float. Without `fee_source`, a silent fallback is
  undetectable both live and after the fact; with it, a reader (T12) can
  tell "the venue's own published rate" (`"venue_schedule"`) from a guess
  (`"category_table"`/`"settings_default"`) and label the rebate line's
  provenance accordingly.
- `maker_fee_rate`: `market.fee.maker_rate` -- the other rate on the same
  `FeeSchedule` `taker_fee_rate` already reads. D9 says "the fee in
  force"; for a PASSIVE quoter (this whole kit) the maker rate, not the
  taker rate, is the one actually paid on every resting fill, and only
  the taker rate was stored before this column existed. Kalshi's is
  `settings.kalshi_maker_fee_rate` (0.0175, confirmed against Kalshi's
  published schedule) as read through `market.fee` -- never a literal in
  `data_collector.py` (GUARDRAILS.md §1.5); Polymarket's is `0.0` for the
  same reason `taker_fee_rate` can be there -- makers pay nothing on that
  venue.

All four are NULLABLE with no backfill, same reasoning as the three
above: no venue call run offline can reconstruct a historical lifetime
counter, poll time, fee source or maker rate for a row already observed.
`NULL` here means "collected before T15" (or, for `volume_lifetime`
specifically, "the counter went backwards on this poll") -- never `0.0`,
never a negative.
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
        volume: The listing's `venue_volume` as of the MOST RECENT
            `collect_books` poll that observed this row's `ts`, or
            `None` for a row collected before migration `008`. Refreshed
            in place on a later poll of the same `ts` if it changed —
            see the module docstring's "THE FEE IN FORCE" section.
        taker_fee_rate: `market.fee.taker_rate` as of the most recent
            poll of this row's `ts`, or `None` for a row collected
            before migration `008`. Refreshed in place, same as
            `volume`.
        maker_rebate_rate: `market.fee.maker_rebate_rate` as of the most
            recent poll of this row's `ts`, or `None` for a row
            collected before migration `008`. Refreshed in place, same
            as `volume`. Data only — never credited by `FeeModel.fee()`
            (GUARDRAILS.md §2.3/D6).
        volume_lifetime: The venue's LIFETIME volume counter (Kalshi
            `volume_fp`, Polymarket `volumeNum`) as of the most recent
            poll, monotonicity-guarded — `None` if that counter decreased
            since the last known reading (a restatement, not a real
            decrease) as well as for a row collected before migration
            `008`. Never `0.0` for "no volume" and never negative. See
            the module docstring's `volume_lifetime` section and
            `app.services.data_collector._monotonic_lifetime_volume`.
        observed_at: The poll's own wall-clock time
            (`app.utils.time.utcnow()`), `None` only for a row collected
            before migration `008`. Written on every insert AND every
            refresh, regardless of whether any other column changed —
            see the module docstring's `observed_at` section for why a
            large `observed_at - ts` gap is meaningful, not an error.
        fee_source: `market.fee.source` as of the most recent poll of
            this row's `ts` (e.g. `"venue_schedule"`, `"category_table"`,
            `"settings"`, `"fee_waiver"`), or `None` for a row collected
            before migration `008`. Refreshed in place, same as `volume`.
        maker_fee_rate: `market.fee.maker_rate` as of the most recent
            poll of this row's `ts`, or `None` for a row collected before
            migration `008`. Refreshed in place, same as `volume`. The
            fee in force for a PASSIVE quoter (D9), as opposed to
            `taker_fee_rate`.
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
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    taker_fee_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    maker_rebate_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume_lifetime: Mapped[float | None] = mapped_column(Float, nullable=True)
    observed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    fee_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    maker_fee_rate: Mapped[float | None] = mapped_column(Float, nullable=True)

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
