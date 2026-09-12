#!/usr/bin/env python
"""Collection health report -- snapshot cadence and completeness, per venue.

WHY THIS EXISTS (mm-proveout T10, PLAN.md D9/§Phase 2). Forward collection
(`DataCollector.collect_books`, T7-T9) is the ONLY data source Polymarket's
proof-out can ever have ("Polymarket cannot be backtested retrospectively"
-- PLAN.md verified facts), and until this script existed nothing counted
snapshots, checked whether volume/fee travel with them, or noticed a
collector that had silently stopped polling. This is read-only against
`book_snapshots` for the last `--hours`; it places no order and contacts
no venue.

WHY `share_volume_non_null` NEVER TREATS `None` AS `0.0` (T8, migration
`008`). `BookSnapshot.volume`/`taker_fee_rate`/`maker_rebate_rate` are
NULLABLE with NO backfill -- every row collected before migration `008`
reads `None` on all three, and `None` there means "collected before T8",
never a real zero (see `BookSnapshot`'s module docstring, "All three are
NULLABLE with no backfill"). `compute_venue_health` below tests
`volume is not None` throughout; the tempting idiom `row.volume or 0.0`
(present elsewhere in this codebase, on a different model --
`app/services/backtesting/data_replay.py:454`) is never used here, because
it would silently count a missing volume as a present zero and make this
report say collection is healthier than it actually is.

WHY A GAP MEANS SOMETHING DIFFERENT ON EACH VENUE (T8's
`_upsert_book_snapshot`, read its docstring's "THE FEE IN FORCE" section
before touching `n_gaps_over_2x_interval` below). `KalshiAdapter.get_book`
stamps `ts=utcnow()` on every observation, so under healthy collection
every poll of a quotable Kalshi `(market, outcome)` writes a NEW row
roughly `book_collection_interval_s` seconds after the last one -- a large
gap there really does mean the collector stopped polling it.
`PolymarketAdapter`'s `OrderBook.ts` is instead the CLOB payload's own
timestamp -- the instant the book last MOVED, not poll time -- and
`_upsert_book_snapshot` refreshes `volume`/`taker_fee_rate`/
`maker_rebate_rate` IN PLACE on a same-`ts` poll rather than writing a new
row. A quiet Polymarket market can therefore go many polls without a new
`BookSnapshot` row while collection is running perfectly. This script
computes the IDENTICAL gap statistic for both venues, by design -- a
Kalshi outage must not be able to hide behind a Polymarket-shaped excuse
-- but attaches a venue-specific `gap_semantics` string to every report
(`_GAP_SEMANTICS` below, printed as a `NOTE` line in the table and carried
in the JSON) so a reader never mistakes Polymarket quiescence for a
Kalshi-style collection failure.

`book_collection_interval_s` landed on `Settings` at `app/config.py:711`
(mm-proveout T9, `default=60.0`) before this script shipped, so
`_interval_s_and_source`'s `getattr` fallback to `_DEFAULT_INTERVAL_S` is
no longer live conditional behaviour against the real
`app.config.settings` singleton -- `getattr` always finds the attribute
there now, so production always returns `source="settings"`. The
fallback branch still exists and is still exercised in tests (a
hand-built stand-in object that lacks the attribute), but it is dead
code through `settings` itself, kept only so a future rename or an
accidentally-deleted field fails safe with a labelled default instead of
raising `AttributeError`.

WHY THIS SCRIPT ALSO COMPARES `last_ts` TO REPORT-GENERATION TIME, NOT
JUST CONSECUTIVE ROWS (mm-proveout T10 RETRY -- reproduced twice against
seeded SQLite). `n_gaps_over_2x_interval`/`median_seconds_between_
snapshots` above are computed ONLY between rows a collector already
wrote (`zip(ts_list, ts_list[1:])`). Once a collector for a venue dies,
the rows it wrote while still alive stay perfectly on-cadence FOREVER --
there is no "next" row left to diff against, so no gap is ever counted
and the report calls the venue healthy no matter how long the silence
has run. Reproduced live: 60 missed 60s Kalshi polls (the collector
died an hour before `generated_at`) read as `n_gaps_over_2x_interval=0`,
`exit_code=0`; a single Kalshi row 23 hours old in a 24-hour window read
as `n_gaps=0`, `median=None`, `exit_code=0`. `VenueHealth.
seconds_since_last_snapshot`/`is_stale` close this hole by comparing
each venue's most recent snapshot to `generated_at` -- the SAME
timestamp `compute_report` already computed for the query window, never
a second `utcnow()` call, so the reported staleness can never drift from
the window the rest of the report describes. This is computed at the
VENUE level (the max `ts` across every market and outcome that venue
collects), never per `(market, outcome)`: a single quiet Polymarket
book legitimately writes no new row for a long stretch (see above), so
a per-market staleness check would fire constantly on healthy
collection, but ALL of a venue's ~500 (Kalshi) or ~130 (Polymarket)
quotable markets going silent at the same instant is a dead collector,
not a market condition, on either venue. See `_STALENESS_MULTIPLIER`
below for the per-venue threshold and why Polymarket's is looser than
Kalshi's.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session_factory
from app.models.book_snapshot import BookSnapshot
from app.utils.time import utcnow
from app.venues.types import VenueId

#: `T9`'s stated default for `settings.book_collection_interval_s`, mirrored
#: here ONLY as a fallback for when that setting has not landed yet (see
#: module docstring). Not a second source of truth once T9 lands: once
#: `settings.book_collection_interval_s` exists, `_interval_s_and_source`
#: always prefers it.
_DEFAULT_INTERVAL_S = 60.0

#: Both venues this report covers, Kalshi first (the venue with an
#: unambiguous gap signal -- see module docstring).
_VENUES: tuple[VenueId, ...] = ("kalshi", "polymarket")

#: Per-venue caveat attached to `n_gaps_over_2x_interval`/
#: `median_seconds_between_snapshots` -- see the module docstring's "WHY A
#: GAP MEANS SOMETHING DIFFERENT ON EACH VENUE".
_GAP_SEMANTICS: dict[str, str] = {
    "kalshi": (
        "ts=utcnow() on every poll (KalshiAdapter.get_book) -- a gap here "
        "means the collector was not polling this (market, outcome), not "
        "that the book was quiet. Trust this gap metric directly."
    ),
    "polymarket": (
        "ts is the venue's own book-move time (OrderBook.ts, from the CLOB "
        "payload), not poll time -- a same-ts poll refreshes volume/fee in "
        "place instead of writing a new row (_upsert_book_snapshot), so a "
        "large gap can mean the book was quiet, not that collection "
        "stopped. Do NOT read a large gap here as an outage on its own; "
        "cross-check n_snapshots and last_ts first."
    ),
}

#: `now - last_ts`, at the VENUE level (see module docstring's "WHY THIS
#: SCRIPT ALSO COMPARES `last_ts` TO REPORT-GENERATION TIME"), must
#: exceed `multiplier * interval_s` before `VenueHealth.is_stale` reports
#: that venue's collector as likely dead. Kalshi keeps the SAME `2x`
#: multiplier `n_gaps_over_2x_interval` already uses: `ts=utcnow()` on
#: every Kalshi poll (see `_GAP_SEMANTICS["kalshi"]`) means a live
#: collector's most-recent row across the WHOLE venue should lag
#: `generated_at` by roughly one beat's worth of polling every quotable
#: market, plus scheduling slop -- never a full extra beat -- so `2x`
#: catches a dead collector (reproduced live: 60 missed 60s polls is
#: 3600s of silence, 30x this threshold) without flagging a beat that is
#: merely still in flight. Polymarket's multiplier is 15x looser:
#: `interval_s` is still the same `book_collection_interval_s` beat
#: (`DataCollector.collect_books` processes both venues in one tick),
#: but Polymarket's `ts` is the CLOB payload's own book-move time, not
#: poll time, so a single quiet market legitimately contributes no fresh
#: `ts` for a long stretch (see `_GAP_SEMANTICS["polymarket"]`). `15 *
#: interval_s` is 15 minutes at the 60s default: long enough that a
#: real, SYNCHRONIZED quiet quarter hour across every one of the ~130
#: tracked Polymarket markets at once is implausible, short enough to
#: still catch a dead collector in well under a trading day -- and, per
#: GUARDRAILS.md's instruction never to pick a threshold loose enough to
#: pass reproduction 1 above, both multipliers stay orders of magnitude
#: below the 3600s (60x the Kalshi threshold, 4x the Polymarket one)
#: that reproduction actually measures.
_STALENESS_MULTIPLIER: dict[str, float] = {"kalshi": 2.0, "polymarket": 15.0}

#: Multiplier for a venue not in `_STALENESS_MULTIPLIER` -- never hit for
#: `_VENUES` today; a defensive default only, matching Kalshi's (the
#: stricter of the two) rather than silently going lenient.
_DEFAULT_STALENESS_MULTIPLIER = 2.0


def _interval_s_and_source(settings_obj: Any = None) -> tuple[float, str]:
    """Read `book_collection_interval_s` defensively (mm-proveout T9 races this task).

    Args:
        settings_obj: The `Settings`-shaped object to read from. Defaults
            to the process-wide `app.config.settings` singleton; a test
            can pass a stand-in object instead.

    Returns:
        tuple[float, str]: `(interval_seconds, source)` where `source` is
            `"settings"` when `book_collection_interval_s` exists on
            `settings_obj`, else a note that the default was used because
            T9 has not landed the setting yet.
    """
    obj = settings if settings_obj is None else settings_obj
    value = getattr(obj, "book_collection_interval_s", None)
    if value is None:
        return (
            _DEFAULT_INTERVAL_S,
            "default (Settings.book_collection_interval_s not defined yet -- "
            "mm-proveout T9 has not landed; using T9's own stated default)",
        )
    return float(value), "settings"


def _ensure_utc(ts: datetime) -> datetime:
    """Attach `UTC` to a naive datetime; pass an already-aware one through.

    See `compute_venue_health`'s docstring for why a naive `ts` can come
    back from a column-only SQLite `select()` even though `BookSnapshot.ts`
    is always written aware UTC.

    Args:
        ts: A `BookSnapshot.ts` value just fetched from the database.

    Returns:
        datetime: `ts`, guaranteed aware (UTC if it was naive).
    """
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


@dataclass(frozen=True)
class VenueHealth:
    """One venue's collection-health metrics over the report window.

    Attributes:
        venue: `"kalshi"` or `"polymarket"`.
        n_snapshots: Row count for this venue in the window.
        n_distinct_markets: Distinct `market_id` values.
        n_distinct_market_outcome: Distinct `(market_id, outcome)` pairs.
        share_volume_non_null: Fraction of rows with `volume is not None`
            (T8: `None` means "collected before migration 008", never a
            real zero), or `None` when `n_snapshots == 0`.
        share_two_sided: Fraction of rows whose `bids` AND `asks` are both
            non-empty, or `None` when `n_snapshots == 0`.
        median_seconds_between_snapshots: Median gap, pooled over every
            `(market_id, outcome)` group's consecutive `ts` values, or
            `None` when no group in the window has 2+ rows.
        n_gaps_over_2x_interval: Count of consecutive-pair gaps exceeding
            `2 * interval_s`, pooled the same way. See `gap_semantics`
            before treating this as "collection stopped".
        first_ts: Earliest `ts` in the window, or `None` if empty.
        last_ts: Latest `ts` in the window, or `None` if empty.
        seconds_since_last_snapshot: `report_generated_at - last_ts`, at
            the VENUE level (the max `ts` across every market and
            outcome this venue collects -- see module docstring's "WHY
            THIS SCRIPT ALSO COMPARES `last_ts` TO REPORT-GENERATION
            TIME"), or `None` when `n_snapshots == 0` (that case is
            already reported by `n_snapshots`/`empty_venues`, not by
            staleness).
        staleness_threshold_s: `_STALENESS_MULTIPLIER[venue] *
            interval_s` -- the threshold `seconds_since_last_snapshot`
            must exceed for `is_stale` to be `True`. Carried in the
            report so a reader never has to reconstruct it from
            `interval_s` and a constant they cannot see.
        is_stale: `True` when `n_snapshots > 0` and
            `seconds_since_last_snapshot > staleness_threshold_s` --
            i.e. this venue HAS data, but its most recent row is old
            enough that the collector looks dead. Always `False` when
            `n_snapshots == 0` (zero rows is `empty_venues`'s failure,
            not this one, so the two never double-count the same
            outage). Wired into `CollectionHealthReport.exit_code` via
            `stale_venues`.
        gap_semantics: The venue-specific caveat for how to read
            `n_gaps_over_2x_interval` and `median_seconds_between_snapshots`.
    """

    venue: str
    n_snapshots: int
    n_distinct_markets: int
    n_distinct_market_outcome: int
    share_volume_non_null: float | None
    share_two_sided: float | None
    median_seconds_between_snapshots: float | None
    n_gaps_over_2x_interval: int
    first_ts: datetime | None
    last_ts: datetime | None
    seconds_since_last_snapshot: float | None
    staleness_threshold_s: float
    is_stale: bool
    gap_semantics: str

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe representation (stable schema, see acceptance)."""
        return {
            "venue": self.venue,
            "n_snapshots": self.n_snapshots,
            "n_distinct_markets": self.n_distinct_markets,
            "n_distinct_market_outcome": self.n_distinct_market_outcome,
            "share_volume_non_null": self.share_volume_non_null,
            "share_two_sided": self.share_two_sided,
            "median_seconds_between_snapshots": self.median_seconds_between_snapshots,
            "n_gaps_over_2x_interval": self.n_gaps_over_2x_interval,
            "first_ts": self.first_ts.isoformat() if self.first_ts else None,
            "last_ts": self.last_ts.isoformat() if self.last_ts else None,
            "seconds_since_last_snapshot": self.seconds_since_last_snapshot,
            "staleness_threshold_s": self.staleness_threshold_s,
            "is_stale": self.is_stale,
            "gap_semantics": self.gap_semantics,
        }


async def compute_venue_health(
    session: AsyncSession,
    venue: VenueId,
    *,
    since: datetime,
    interval_s: float,
    now: datetime,
) -> VenueHealth:
    """Compute one venue's `VenueHealth` over `[since, now)`.

    A single query fetches only the columns needed (never `bids`/`asks`
    levels beyond presence, never the market's full row) and every
    aggregate is then computed in Python, because the median and the
    per-`(market, outcome)` gap count both need the sorted `ts` sequence
    within each group -- not expressible as one portable SQL aggregate
    across SQLite (tests) and Postgres (production) without a dialect
    branch, the same reasoning `_upsert_book_snapshot` uses for its own
    check-then-insert.

    Every fetched `ts` is normalized to aware UTC before use
    (`_ensure_utc` below): a Core, column-only `select()` on SQLite loses
    the column's timezone even though `BookSnapshot.ts` is declared
    `DateTime(timezone=True)` and every row is written aware UTC -- the
    ORM entity loader masks this in a warm session (identity-map reuse of
    the already-aware Python object), but a fresh column-only fetch comes
    back naive. Postgres (`asyncpg`, production) never loses it. Since
    every value this table ever writes IS UTC (`app.utils.time.utcnow()`/
    `ensure_aware()` -- GUARDRAILS.md: "aware UTC only... a naive-vs-aware
    TypeError is a defect, not an environment quirk"), re-attaching `UTC`
    to a naive read-back is a safe, correct normalization, not a guess.

    Args:
        session: Session to query. Tests pass the `test_session` fixture
            from `tests/conftest.py` directly; `main()` opens one from
            `app.database.async_session_factory`.
        venue: `"kalshi"` or `"polymarket"`.
        since: Window start (inclusive), aware UTC.
        interval_s: `book_collection_interval_s`, for the gap threshold
            (`2 * interval_s`) and the staleness threshold
            (`_STALENESS_MULTIPLIER[venue] * interval_s`).
        now: Report-generation time, aware UTC. MUST be the same value
            `compute_report` used for `since` (its `generated_at`) --
            never a fresh `utcnow()` call here -- so
            `seconds_since_last_snapshot` can never drift from the
            window the rest of the report describes (see module
            docstring's "WHY THIS SCRIPT ALSO COMPARES `last_ts` TO
            REPORT-GENERATION TIME"). Required, not defaulted: a keyword
            a caller forgets to pass should fail loudly, not silently
            call `utcnow()` a second time.

    Returns:
        VenueHealth: See its docstring for field semantics.
    """
    result = await session.execute(
        select(
            BookSnapshot.market_id,
            BookSnapshot.outcome,
            BookSnapshot.ts,
            BookSnapshot.volume,
            BookSnapshot.bids,
            BookSnapshot.asks,
        ).where(BookSnapshot.venue == venue, BookSnapshot.ts >= since)
    )
    rows = result.all()

    n_snapshots = len(rows)
    markets: set[str] = set()
    market_outcomes: set[tuple[str, str]] = set()
    n_volume_non_null = 0
    n_two_sided = 0
    ts_by_key: dict[tuple[str, str], list[datetime]] = defaultdict(list)
    first_ts: datetime | None = None
    last_ts: datetime | None = None

    for market_id, outcome, ts, volume, bids, asks in rows:
        ts = _ensure_utc(ts)
        markets.add(market_id)
        market_outcomes.add((market_id, outcome))
        if volume is not None:
            n_volume_non_null += 1
        if bids and asks:
            n_two_sided += 1
        ts_by_key[(market_id, outcome)].append(ts)
        if first_ts is None or ts < first_ts:
            first_ts = ts
        if last_ts is None or ts > last_ts:
            last_ts = ts

    deltas: list[float] = []
    n_gaps = 0
    gap_threshold = 2.0 * interval_s
    for ts_list in ts_by_key.values():
        ts_list.sort()
        for earlier, later in zip(ts_list, ts_list[1:], strict=False):
            delta_s = (later - earlier).total_seconds()
            deltas.append(delta_s)
            if delta_s > gap_threshold:
                n_gaps += 1

    seconds_since_last_snapshot = (
        (now - last_ts).total_seconds() if last_ts is not None else None
    )
    staleness_threshold_s = (
        _STALENESS_MULTIPLIER.get(venue, _DEFAULT_STALENESS_MULTIPLIER) * interval_s
    )
    is_stale = (
        n_snapshots > 0
        and seconds_since_last_snapshot is not None
        and seconds_since_last_snapshot > staleness_threshold_s
    )

    return VenueHealth(
        venue=venue,
        n_snapshots=n_snapshots,
        n_distinct_markets=len(markets),
        n_distinct_market_outcome=len(market_outcomes),
        share_volume_non_null=(n_volume_non_null / n_snapshots) if n_snapshots else None,
        share_two_sided=(n_two_sided / n_snapshots) if n_snapshots else None,
        median_seconds_between_snapshots=statistics.median(deltas) if deltas else None,
        n_gaps_over_2x_interval=n_gaps,
        first_ts=first_ts,
        last_ts=last_ts,
        seconds_since_last_snapshot=seconds_since_last_snapshot,
        staleness_threshold_s=staleness_threshold_s,
        is_stale=is_stale,
        gap_semantics=_GAP_SEMANTICS.get(
            venue, "unknown venue -- no gap semantics documented for it."
        ),
    )


@dataclass(frozen=True)
class CollectionHealthReport:
    """The full report: every venue's `VenueHealth`, plus the window/interval.

    Attributes:
        generated_at: When this report was computed, aware UTC.
        hours: The `--hours` lookback window used.
        since: `generated_at - hours`, aware UTC -- the query lower bound.
        interval_s: `book_collection_interval_s` used for the gap
            threshold.
        interval_source: `"settings"` or a note that a default was used
            (see `_interval_s_and_source`).
        venues: `{venue: VenueHealth}`, one entry per `_VENUES`.
    """

    generated_at: datetime
    hours: float
    since: datetime
    interval_s: float
    interval_source: str
    venues: dict[str, VenueHealth]

    @property
    def empty_venues(self) -> list[str]:
        """Venues with zero snapshots in the window, in `_VENUES` order."""
        return [v for v in _VENUES if v in self.venues and self.venues[v].n_snapshots == 0]

    @property
    def stale_venues(self) -> list[str]:
        """Venues with data whose most recent snapshot is older than that
        venue's staleness threshold (`VenueHealth.is_stale`), in
        `_VENUES` order. Disjoint from `empty_venues` by construction:
        `is_stale` is always `False` when `n_snapshots == 0` (see
        `VenueHealth.is_stale`'s docstring), so a venue is never listed
        in both -- zero rows and stale rows are reported as two distinct
        failures, never conflated."""
        return [v for v in _VENUES if v in self.venues and self.venues[v].is_stale]

    @property
    def exit_code(self) -> int:
        """`1` if any venue has zero snapshots in the window OR any
        venue's most recent snapshot is stale (`stale_venues`), else `0`.

        Staleness is wired into the exit code, not just printed/carried
        in the JSON, because a statistic nothing can fail on repeats the
        exact defect this field was added to fix (mm-proveout T10
        retry): a collector that died silently must make this script
        fail, not merely note a number a reader has to remember to
        check.
        """
        return 1 if (self.empty_venues or self.stale_venues) else 0

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe representation. Schema is pinned by
        `tests/scripts/test_collection_health.py`; extend, don't rename."""
        return {
            "generated_at": self.generated_at.isoformat(),
            "hours": self.hours,
            "since": self.since.isoformat(),
            "book_collection_interval_s": self.interval_s,
            "book_collection_interval_s_source": self.interval_source,
            "venues": {v: h.to_dict() for v, h in self.venues.items()},
            "empty_venues": self.empty_venues,
            "stale_venues": self.stale_venues,
            "exit_code": self.exit_code,
        }

    def render(self) -> str:
        """Render the printed table plus per-venue gap-semantics notes."""
        lines: list[str] = [
            "Collection health (app.scripts.collection_health)",
            f"window: last {self.hours:g}h, since {self.since.isoformat()}",
            f"book_collection_interval_s={self.interval_s:g} ({self.interval_source})",
            "=" * 78,
        ]

        def _fmt(value: Any) -> str:
            if value is None:
                return "n/a"
            if isinstance(value, float):
                return f"{value:.3f}"
            return str(value)

        rows: list[tuple[str, list[str]]] = [
            ("snapshots", [str(self.venues[v].n_snapshots) for v in _VENUES]),
            ("distinct markets", [str(self.venues[v].n_distinct_markets) for v in _VENUES]),
            (
                "distinct (market, outcome)",
                [str(self.venues[v].n_distinct_market_outcome) for v in _VENUES],
            ),
            (
                "share volume non-null",
                [_fmt(self.venues[v].share_volume_non_null) for v in _VENUES],
            ),
            ("share two-sided", [_fmt(self.venues[v].share_two_sided) for v in _VENUES]),
            (
                "median seconds between snapshots",
                [_fmt(self.venues[v].median_seconds_between_snapshots) for v in _VENUES],
            ),
            (
                f"gaps > 2x interval ({2 * self.interval_s:g}s)",
                [str(self.venues[v].n_gaps_over_2x_interval) for v in _VENUES],
            ),
            (
                "first ts",
                [_fmt(self.venues[v].first_ts.isoformat() if self.venues[v].first_ts else None) for v in _VENUES],
            ),
            (
                "last ts",
                [_fmt(self.venues[v].last_ts.isoformat() if self.venues[v].last_ts else None) for v in _VENUES],
            ),
            (
                "seconds since last snapshot",
                [_fmt(self.venues[v].seconds_since_last_snapshot) for v in _VENUES],
            ),
            ("is stale", [str(self.venues[v].is_stale) for v in _VENUES]),
        ]

        header = f"{'metric':<36}" + "".join(f"{v:<16}" for v in _VENUES)
        lines.append(header)
        lines.append("-" * len(header))
        for label, values in rows:
            lines.append(f"{label:<36}" + "".join(f"{val:<16}" for val in values))

        lines.append("")
        for venue in _VENUES:
            if venue in self.venues:
                health = self.venues[venue]
                lines.append(f"NOTE {venue}: {health.gap_semantics}")
                lines.append(
                    f"NOTE {venue} staleness: flagged stale when "
                    f"(now - last_ts) > {health.staleness_threshold_s:g}s."
                )

        if self.empty_venues:
            lines.append("")
            for venue in self.empty_venues:
                lines.append(
                    f"FAIL: {venue} has zero snapshots in the last {self.hours:g}h "
                    "window -- collection has not produced any rows there yet "
                    "(the correct answer before collection starts)."
                )

        if self.stale_venues:
            lines.append("")
            for venue in self.stale_venues:
                health = self.venues[venue]
                lines.append(
                    f"FAIL: {venue}'s last snapshot is "
                    f"{health.seconds_since_last_snapshot:.0f}s old, over its "
                    f"{health.staleness_threshold_s:.0f}s staleness threshold -- "
                    "collection may have stopped polling this venue even "
                    "though its earlier rows look perfectly on-cadence."
                )

        lines.append("")
        lines.append("=" * 78)
        if not self.empty_venues and not self.stale_venues:
            lines.append("OK -- both venues have snapshots in the window and neither is stale.")
        else:
            failures = [
                *(f"{v} empty" for v in self.empty_venues),
                *(f"{v} stale" for v in self.stale_venues),
            ]
            lines.append(f"FAILED -- {', '.join(failures)} (exit code {self.exit_code}).")
        return "\n".join(lines)


async def compute_report(
    session: AsyncSession,
    *,
    hours: float,
    interval_s: float,
    interval_source: str = "settings",
    now: datetime | None = None,
) -> CollectionHealthReport:
    """Compute the full `CollectionHealthReport` for `[now - hours, now)`.

    Args:
        session: Session to query, already open (see
            `compute_venue_health`'s Args for the test-vs-production seam).
        hours: Lookback window in hours.
        interval_s: `book_collection_interval_s` (or its fallback).
        interval_source: Provenance label for `interval_s`, carried
            through to the report/JSON unchanged.
        now: Report generation time, aware UTC. Defaults to
            `app.utils.time.utcnow()`; tests pass a fixed value so the
            window and gap arithmetic are deterministic. This EXACT
            value (`generated_at` below) is also what every venue's
            `seconds_since_last_snapshot` is measured against -- never a
            second `utcnow()` call -- so staleness can never drift from
            the window this report's `since`/`generated_at` describe.

    Returns:
        CollectionHealthReport: One `VenueHealth` per `_VENUES`.
    """
    if hours <= 0:
        raise ValueError(f"hours must be > 0, got {hours!r}")
    generated_at = now if now is not None else utcnow()
    since = generated_at - timedelta(hours=hours)

    venues: dict[str, VenueHealth] = {}
    for venue in _VENUES:
        venues[venue] = await compute_venue_health(
            session, venue, since=since, interval_s=interval_s, now=generated_at
        )

    return CollectionHealthReport(
        generated_at=generated_at,
        hours=hours,
        since=since,
        interval_s=interval_s,
        interval_source=interval_source,
        venues=venues,
    )


async def _run(hours: float, out: str | None) -> int:
    """Real entry point: opens a session from `async_session_factory`.

    Never used by tests directly (they call `compute_report` with an
    injected SQLite session -- see `tests/scripts/test_collection_health.py`
    and `tests/conftest.py`'s `test_session` fixture); this is the seam
    `main()` uses against the CONFIGURED database.
    """
    interval_s, interval_source = _interval_s_and_source()
    async with async_session_factory() as session:
        report = await compute_report(
            session, hours=hours, interval_s=interval_s, interval_source=interval_source
        )

    print(report.render())

    if out:
        Path(out).write_text(json.dumps(report.to_dict(), indent=2))
        print(f"\nwrote {out}")

    if report.empty_venues:
        print(
            f"\nFAIL: {', '.join(report.empty_venues)} -- zero snapshots in the "
            f"last {hours:g}h window.",
            file=sys.stderr,
        )
    if report.stale_venues:
        print(
            f"\nFAIL: {', '.join(report.stale_venues)} -- most recent snapshot "
            "is past its staleness threshold; collection may have stopped.",
            file=sys.stderr,
        )

    return report.exit_code


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Arguments to parse, excluding the program name. Defaults to
            `sys.argv[1:]` (via `argparse`'s own default).

    Returns:
        int: `0` if both venues had at least one snapshot in the window,
            `1` otherwise.
    """
    parser = argparse.ArgumentParser(
        prog="python3 -m app.scripts.collection_health",
        description=(
            "Report book_snapshots collection health (snapshot cadence and "
            "completeness) per venue, for the last --hours."
        ),
    )
    parser.add_argument(
        "--hours", type=float, default=24.0, help="Lookback window in hours (default 24)."
    )
    parser.add_argument(
        "--out", type=str, default=None, help="Path to write the JSON report (optional)."
    )
    args = parser.parse_args(argv)
    return asyncio.run(_run(args.hours, args.out))


if __name__ == "__main__":
    sys.exit(main())
