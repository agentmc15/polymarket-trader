"""Per-market record of book-collection selection state (mm-proveout T16).

THE GAP THIS TABLE CLOSES. `DataCollector.collect_books` writes a
`BookSnapshot` row only for a market `select_quotable_markets` (T7,
PLAN.md D1) put in its `selected` set THAT TICK. Everything else about a
market's absence from `BookSnapshot` at a given tick looked identical
before this table existed: a market that lost quotability (its spread
tightened, its volume dried up, it was pushed out by the
`book_collection_top_n` cap) and a market the collector genuinely FAILED
to reach (a 404, a rate limit, a transient bug) both produce exactly the
same observable fact -- no row. Measured on Kalshi over 5 minutes
(Phase 2 review, 2026-09-07): `quotable` held at 684 and `selected` held
at 500, but 20 markets (4.0% of `selected`) left the set and 20 entered,
and EVERY departure was a lost quotability, none were pushed out by the
cap. Over three weeks of a 60-second beat, any given market's
`BookSnapshot` series will be full of holes from exactly this churn, and
T11 (`app.scripts.mm_replay_snapshots`) cannot tell "we chose not to
quote this, right then" from "the collector broke" just by staring at the
gap.

WHAT THIS TABLE IS, AND WHY IT IS NOT ONE ROW PER (VENUE, MARKET, TICK).
The obvious design -- write a row every tick for every market
`collect_books` considers -- reproduces `BookSnapshot`'s own scale
(hundreds of markets x a 60-second beat x three weeks) for a fact that,
per the measurement above, changes for only ~4% of the selected set every
five minutes. This table instead keeps exactly ONE row per
`(venue, market_id)` -- the CURRENT selection state plus the timestamp of
the most recent transition in each direction -- upserted every tick the
same check-then-update way `DataCollector._upsert_book_snapshot` already
handles `BookSnapshot` (correct on SQLite and Postgres without a dialect
branch). Its size is bounded by the number of DISTINCT markets ever
observed as selected on a venue, not by how many ticks have run --
exactly the "keep it small" constraint this table was built under.

WHAT THIS DELIBERATELY DOES NOT CAPTURE (say so, per this kit's own
documentation convention). `last_selected_at`/`last_deselected_at` are
each the MOST RECENT transition of their kind, not a full history: a
market that entered, left, re-entered and left again within one gap in
its `BookSnapshot` series leaves only the LATEST "left" timestamp behind,
not the earlier one. For T11's actual use -- "is there a `left` transition
somewhere inside this specific gap's time range" -- that is sufficient
whenever the gap is the market's ONLY excursion out of the selected set
(the common case, per the measured 4%-per-5-minutes churn rate: a
departure is not usually followed by an immediate re-entry and a second
departure inside the same short gap). A market that flaps rapidly enough
to depart and return more than once inside a single `BookSnapshot` gap is
a real limitation of this design, not a case this migration claims to
solve; a future task that needs full transition history should replace
this with an append-only event log rather than stretch this table to do
both jobs.

`DataCollector.collect_books` is the only writer (called once per venue
per tick, right after `select_quotable_markets` computes `selected` --
BEFORE the per-market `get_book` calls, so a row here reflects the
SELECTION DECISION regardless of whether the subsequent book fetch
succeeded). T11 (or any later reader) is expected to treat a market
currently marked `selected=False`, or one whose `last_deselected_at`
falls inside a `BookSnapshot` gap, as "deliberately not collected" rather
than "collector fault" for that stretch.
"""
from datetime import datetime
from typing import Literal

from sqlalchemy import Boolean, CheckConstraint, DateTime, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

#: Mirrors `app.venues.types.VenueId` exactly. Not imported -- see
#: `app.models.book_snapshot`'s identical note for why `app.models` does
#: not import from sibling packages at runtime; keep these in sync by
#: hand.
SelectionVenue = Literal["polymarket", "kalshi"]
_VENUE_VALUES = ("polymarket", "kalshi")


class SelectionMembership(Base):
    """Current book-collection selection state for one `(venue, market_id)`.

    Attributes:
        id: Surrogate primary key, autoincrement.
        venue: `"polymarket"` or `"kalshi"`.
        market_id: Venue-native market identifier (Polymarket
            `condition_id`; Kalshi `ticker`).
        selected: Whether this market was in `select_quotable_markets`'
            `selected` set on the MOST RECENT tick that considered it.
            `True` the whole time a market stays selected across
            consecutive ticks -- this row is upserted, not re-inserted,
            so a long run of "still selected" ticks touches one row
            repeatedly rather than accumulating one row per tick.
        last_selected_at: Wall-clock time of the most recent tick on
            which this market was in the `selected` set. `None` only if
            this market has never once been selected (which cannot
            happen for a row that exists at all under normal operation,
            since a row is only ever created inside the "add to
            `selected`" branch — but is not constrained `NOT NULL` so a
            future writer that also tracks `quotable`-but-never-selected
            markets is not blocked by this schema).
        last_deselected_at: Wall-clock time of the most recent tick on
            which this market TRANSITIONED from selected to not-selected
            (i.e., it was `selected=True` on the previous tick that
            considered it, and is not in the CURRENT tick's `selected`
            set). `None` if this market has never left the selected set
            since its row was created. See the module docstring for why
            this is the LATEST transition only, not a full history.
    """

    __tablename__ = "selection_membership"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    venue: Mapped[str] = mapped_column(String(16), nullable=False)
    market_id: Mapped[str] = mapped_column(String(128), nullable=False)
    selected: Mapped[bool] = mapped_column(Boolean, nullable=False)
    last_selected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_deselected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint(
            "venue", "market_id", name="uq_selection_membership_venue_market"
        ),
        CheckConstraint(
            f"venue IN ({', '.join(repr(v) for v in _VENUE_VALUES)})",
            name="ck_selection_membership_venue_valid",
        ),
    )
