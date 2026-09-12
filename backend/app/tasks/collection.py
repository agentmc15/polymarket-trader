"""Celery task for scheduled order-book depth collection (mm-proveout T9).

`collect_books` runs `DataCollector.collect_books` (`app.services.
data_collector`, T7/T8) on the `"collect-books"` beat
(`app/tasks/__init__.py`, every `settings.book_collection_interval_s`
seconds, 180s default — see `app/config.py`) — the production caller
that method never had outside the manual
`python -m app.scripts.collect_prices --books` CLI
loop (`app.scripts.collect_prices.collect_books_once`, which this
module's `run_collect_books()` otherwise mirrors closely).

WHY A BEAT AT ALL (PLAN.md D2, D8, D9). Kalshi's Gate 1 (T1-T6) is
retrospective, replayed from settled candle history; Polymarket exposes
none of that (PLAN.md: `/prices-history` carries no bid/ask/volume,
"Polymarket cannot be backtested retrospectively"). Forward collection —
recording what a live quoter would actually have seen, on both venues,
for as long as T11-T13 need — is the ONLY path to a Polymarket verdict,
and a beat is what starts that clock without a human re-running a CLI
loop by hand for weeks.

ADAPTERS COME FROM `get_read_adapter`, same rationale as
`app.tasks.scanner`/`app.tasks.execution`: in `"live"` mode `get_adapter`
constructs an order-PLACING adapter behind `assert_live_allowed()`, and
this task places nothing, ever (GUARDRAILS.md §1.1). Using
`get_read_adapter` means an engaged kill switch (which only blocks
PLACEMENT) has no bearing on whether collection keeps running, and this
pass genuinely never touches the placement fence at all.

CANDIDATE GATHERING IS ISOLATED PER VENUE HERE, mirroring
`app.tasks.scanner.read_adapters`/`app.tasks.execution.reconcile_venues`'s
own shape exactly (bare `except Exception`, `# noqa: BLE001`, at VENUE
granularity): a venue whose `get_read_adapter` or `list_markets` call
fails is logged and simply contributes no candidates, rather than
aborting the other venue's listing.

CANDIDATES ARE `VenueMarket` OBJECTS, NEVER RE-FETCHED (mm-proveout T16,
Phase 2 review part a — the reason this beat could never finish a Kalshi
pass at all before this fix). `adapter.list_markets(status="open")`
already returns a fully-built `VenueMarket` per candidate; this used to
be thrown away (`[m.market_id for m in markets]`) and handed to
`DataCollector.collect_books` as bare ids, which then called
`adapter.get_market(market_id)` once PER CANDIDATE to rebuild the exact
same object. Measured live 2026-09-07 against the real Kalshi API: the
listing walk itself (`/events?status=open&with_nested_markets=true`,
paginated to exhaustion) returned 99,588 open markets in 10.46s, while
`get_market` averaged 0.096s/call — 99,588 candidates x 0.096s is
~2.66 HOURS of redundant re-fetching per tick, against a 60-second beat
(`settings.book_collection_interval_s`). Polymarket showed the same
shape at smaller scale (1,918 open markets, listing walk 10.37s,
`get_market` averaging 0.197s/call in this measurement — cache-warmed by
the preceding listing call's own `_event_id_by_market_id` memo; a
colder-cache average could be higher — either way `1,918 x that average`
is minutes of pure waste on a 60-second beat). `list_markets` and
`get_market` build a `VenueMarket` through the SAME `_build_market`
method on both venues, so the objects the listing walk already built are
not a lesser substitute for a fresh `get_market()` call — carrying them
through costs nothing extra and removes the redundant fetch entirely.
`DataCollector.collect_books` accepts either a bare market id (resolved
with `get_market`, kept for `app.scripts.collect_prices.
collect_books_once` and this repo's pre-T16 tests) or an already-built
`VenueMarket` (used as-is) in the same candidate list — see that
method's docstring for the full field-equivalence evidence. This beat
always passes the listing's own objects.

WRITING IS ISOLATED PER VENUE INSIDE `DataCollector.collect_books`
ITSELF, not duplicated here (mm-proveout T9 red-team, see that method's
own docstring for the full defect and fix): before T9, a non-`VenueError`
exception raised while WRITING one venue's snapshots (as opposed to
merely listing its candidates) propagated out of the entire
`collect_books` call, so a healthy second venue's markets were never
even attempted, and — because the old method committed once at the very
end — an already-processed first venue's rows were lost too. That fix
lives in `DataCollector.collect_books` (the single place both this beat
and the manual CLI call through), not copied into this task, so both
callers get it for free and can never drift apart the way two
independent copies eventually do in this repo.

A 404 ON ONE MARKET NO LONGER COSTS THE VENUE (mm-proveout T16, Phase 2
review part b). `DataCollector.collect_books`'s per-candidate catches
used to be `except VenueError` only, but Kalshi's `raise_for_venue_error`
deliberately lets a 404 fall through as `httpx.HTTPStatusError` (that
mapping is not this task's to change — see that function's own
docstring), so an ordinary delisted/settled ticker escaped straight past
the per-candidate catch and took the whole venue's tick with it. Verified
live: 3 of 5 real tickers sampled from Kalshi's own open listing returned
404 on `GET /markets/{ticker}`. `collect_books` now catches
`_MARKET_FETCH_FAULTS` (`VenueError`, `httpx.HTTPError`) at that grain
instead — see that module's docstring for the fix and its test coverage.
This beat's OWN per-venue try/except (below and in `read_adapters()`)
was never the problem; it already isolated one venue's failure from the
other's. The problem was one candidate's failure escaping to that
coarser boundary and taking every OTHER candidate in the same venue down
with it.

PER-VENUE WRITTEN COUNTS, NOT A BARE TOTAL (mm-proveout T16, Phase 2
review part b, second half). `collect_books()` used to return one `int`
summed across every venue in one call, and this task's summary echoed
back `"venues": <the venues that produced CANDIDATES>` — so a caller
(`app.scripts.collection_health`, a future alert) could not tell "Kalshi
listed 500 candidates and wrote 500 books" from "Kalshi listed 500
candidates and wrote zero, because every one of them raised" without
parsing logs. `run_collect_books()` now calls `DataCollector.
collect_books` ONCE PER VENUE (a call with a single-venue mapping is
identical in every other respect — `collect_books` already isolates and
commits at that same grain internally) and returns `"written"` as a
`{venue: count}` mapping. A venue absent from that mapping means "never
listed a candidate at all" (its own adapter or `list_markets` call
failed); a venue present with `0` means "listed candidates, wrote none"
— two states a bare total, or a single combined call, could not tell
apart.

OVERLAP: NOT PREVENTED, DELIBERATELY (T9 hazard note). `DataCollector.
collect_books` is unsafe on a shared `AsyncSession` under concurrency —
documented SQLAlchemy behaviour, not a bug in that code — and this is a
180-second beat (mm-proveout T9 follow-on measurement, `app/config.py`)
over what may be ~500 Kalshi + ~130 Polymarket quotable
markets (`settings.book_collection_top_n`), one `get_book` per outcome
(no per-market `get_market` call at all, since T16 above), paced at
Kalshi's own rate limit. If a pass ever runs long enough to overlap its
own next tick, Celery's usual answer is
`--concurrency=1` plus a distributed lock (e.g. a Redis lock via
`celery.contrib.abortable` or a custom `SETNX`). That machinery is
deliberately NOT added here, for two structural reasons:

  1. `run_collect_books()` opens its OWN fresh `AsyncSession` per
     invocation (`async_session_factory()`, never a session shared
     across ticks) — two overlapping ticks each get their own engine
     connection and session object, so the specific hazard (racing
     `flush()`/`commit()` calls on ONE shared session,
     `ResourceClosedError`/`IllegalStateChangeError`) cannot occur
     BETWEEN ticks. It remains true WITHIN one tick that both venues
     share one session, which is exactly why `collect_books` was
     changed to commit and roll back per venue rather than once at the
     end — see that method's docstring.
  2. `_upsert_book_snapshot`'s natural key is `(venue, market_id,
     outcome, ts)`. Two overlapping ticks reading the SAME book at
     nearly the same wall-clock instant either observe the same `ts`
     (Kalshi stamps `utcnow()`; colliding would need both ticks' Kalshi
     calls within microseconds of each other) and one becomes a no-op
     UPSERT hit, or they observe different `ts` values and both rows are
     legitimately distinct observations. An overlap therefore produces,
     at worst, one slightly-early extra snapshot — never a corrupted row
     or a crash.

If a production pass is ever MEASURED to exceed `book_collection_
interval_s` (this module's `logger.info` line on every run reports the
venues collected, so `app.scripts.collection_health` — T10 — can be
checked for a rising median-gap signal), the fix is to lengthen the
interval or add a lock — not to disable pacing or widen a threshold to
hide the overlap (GUARDRAILS.md §2.6's spirit, applied to scheduling
rather than a reported statistic).
"""
import logging
from typing import Any

from app.config import settings
from app.database import async_session_factory, run_async_task
from app.scripts import collection_health
from app.scripts.preflight import (
    _check_database_async,
    _collection_group,
    _database_group,
    _expected_head_revision,
    check_collection_for_venue,
)
from app.services.data_collector import DataCollector
from app.tasks import celery_app
from app.venues.base import MarketDataAdapter
from app.venues.registry import get_read_adapter
from app.venues.types import VenueId, VenueMarket

logger = logging.getLogger(__name__)


class CollectionPreflightError(RuntimeError):
    """Raised by `run_collect_books()` when the pre-collection preflight
    (DB head revision + live field-name/reality check) fails.

    This is the fix for the failure mode this task exists to close: a
    venue silently renaming a field previously produced zero written
    rows, a beat that still reported `exit 0`, and no alarm anywhere for
    as long as three weeks. Raising here — rather than logging a warning
    and collecting anyway — makes that failure LOUD: a Celery task that
    raises is recorded as a FAILED task result (visible to anything
    watching task state), not a quietly-successful one that happened to
    write nothing, and the message names exactly which check failed
    rather than requiring an operator to go compare a fixture against
    today's payload by hand.
    """


async def run_collection_preflight() -> None:
    """Gate `run_collect_books()` on the same collection preflight
    `python3 -m app.scripts.preflight --check-collection` already runs
    by hand (mm-proveout T9/T10) — reusing its existing check functions
    rather than writing new ones, per the module's own "PLAN.md D8"
    rationale for `check_collection_for_venue`.

    Two checks, both already defined in `app.scripts.preflight`:

    1. Database schema drift (`_database_group`, fed by
       `_check_database_async` and `_expected_head_revision`) — the
       exact machinery `app.scripts.preflight.main()` uses for its own
       (never-run-here) `alembic upgrade head` warning. A schema behind
       head means a beat writing rows a newer model expects to read
       differently, or a migration's new column silently absent.
    2. Live field-name/reality drift (`_collection_group`, fed by
       `check_collection_for_venue` against THIS task's own
       `read_adapters()`) — the check `default_check_collection()`
       wraps for the CLI. `default_check_collection`/`default_check_
       database` are not called directly here because both call
       `asyncio.run(...)` internally (see their own docstrings/`main()`)
       and this coroutine already runs INSIDE the event loop
       `run_async_task` opened for it — nesting `asyncio.run` inside a
       running loop raises `RuntimeError` immediately. Awaiting the same
       underlying async pieces (`_check_database_async`,
       `check_collection_for_venue`) directly avoids that without
       duplicating any check logic.

    Raises:
        CollectionPreflightError: If either check group contains a
            `"fail"`-status `Check` — the exception message names every
            failing check line, not just that "preflight failed".
    """
    db_check = await _check_database_async(settings, timeout_s=3.0)
    try:
        expected_head = _expected_head_revision()
    except Exception:  # noqa: BLE001 - reported as "could not determine", not fatal here
        expected_head = None
    db_group = _database_group(db_check, expected_head)

    adapters = read_adapters()
    collection_checks = [
        await check_collection_for_venue(venue, adapter) for venue, adapter in adapters.items()
    ]
    collection_group = _collection_group(collection_checks)

    failures = [
        f"[{group.name}] {check.message}"
        for group in (db_group, collection_group)
        for check in group.checks
        if check.status == "fail"
    ]
    if failures:
        for line in failures:
            logger.error(
                "collection",
                extra={"event": "collect_books_preflight_failed", "detail": line},
            )
        raise CollectionPreflightError(
            "collection preflight failed, refusing to collect: " + " | ".join(failures)
        )

#: Venues collected on every pass. Mirrors `app.venues.types.VenueId`,
#: `app.tasks.scanner.SCANNED_VENUES` and
#: `app.tasks.execution.RECONCILED_VENUES` — a third venue means adding
#: it here too, same convention as those two.
COLLECTED_VENUES: tuple[VenueId, ...] = ("polymarket", "kalshi")


class VenueEscalationError(RuntimeError):
    """Raised by `run_collect_books()` when one venue has written zero
    rows for `_ZERO_WRITE_ESCALATION_THRESHOLD` consecutive ticks.

    The per-tick `DataCollector.collect_books` failure path already
    calls `logger.exception(...)` (ERROR, with a real traceback) for a
    write-time fault — but that fires identically on tick 1 of a
    transient blip and tick 500 of a venue that has been silently dead
    for a day and a half, and a per-tick ERROR line is easy to lose in
    ordinary worker log volume. Raising here instead makes a
    PERSISTENTLY dead venue register as a FAILED Celery task result —
    a distinct, queryable signal (`AsyncResult.state`, the result
    backend) — rather than one more line among thousands of routine
    per-tick log entries, which is the closest thing this repo has to
    "paging" without Prometheus/statsd/Sentry.
    """


#: How many CONSECUTIVE ticks a venue may write zero rows (present in
#: `written_by_venue` with `0`, not merely absent — see
#: `run_collect_books`'s docstring) before `run_collect_books` raises
#: `VenueEscalationError` instead of returning normally.
#:
#: 5, against the `book_collection_interval_s` default of 180.0 (mm-
#: proveout T9 measurement, `app/config.py`): that is 15 minutes of a
#: venue writing NOTHING on every single tick. Long enough that one
#: slow or rate-limited tick, or the kind of one-candidate transient
#: fault `DataCollector.collect_books`'s own per-candidate isolation
#: already absorbs (see that method's docstring), cannot trip it by
#: itself; short enough that a genuinely dead venue (a renamed field,
#: an expired credential, a listing call failing every time) is
#: escalated well within the hour, not left running silently for the
#: three weeks this task's brief cites as the status quo failure mode.
_ZERO_WRITE_ESCALATION_THRESHOLD = 5

#: Consecutive zero-write tick count per venue. Module-level, not a
#: `Settings`/database field: it only needs to survive between beat
#: ticks WITHIN one worker process (the same assumption `run_async_task`
#: already makes about the module-level `engine` — see `app.database`),
#: and resets naturally on a worker restart, which is an acceptable
#: (in fact desirable — a fresh deploy deserves a clean slate) cost for
#: something this small. Tests reset it directly
#: (`collection_task._consecutive_zero_writes.clear()`), the same way
#: they monkeypatch other module-level collaborators in this file.
_consecutive_zero_writes: dict[VenueId, int] = {}


def read_adapters() -> dict[VenueId, MarketDataAdapter]:
    """Return one READ-ONLY adapter per `COLLECTED_VENUES` entry.

    A venue whose read adapter cannot be constructed is skipped (logged),
    not fatal — the exact shape `app.tasks.scanner.read_adapters` and
    `app.tasks.execution.reconcile_venues` already use for "one venue's
    trouble does not blank out the other's". Kept as its OWN copy here
    rather than imported from `app.tasks.scanner`, matching this repo's
    existing convention of one small, local per-task-module copy
    (`SCANNED_VENUES`/`RECONCILED_VENUES` are not shared either) rather
    than a cross-module import for a five-line loop.

    Returns:
        dict[VenueId, MarketDataAdapter]: The adapters that could be
            built; possibly empty, which is a collection pass over
            nothing rather than an error.
    """
    adapters: dict[VenueId, MarketDataAdapter] = {}
    for venue in COLLECTED_VENUES:
        try:
            adapter = get_read_adapter(venue, settings.trading_mode)
        except Exception as exc:  # noqa: BLE001 - one venue must not stop the rest
            logger.warning(
                "collection",
                extra={
                    "event": "collect_books_adapter_unavailable",
                    "venue": venue,
                    "mode": settings.trading_mode,
                    "reason": type(exc).__name__,
                },
            )
            continue
        if isinstance(adapter, MarketDataAdapter):
            adapters[venue] = adapter
    return adapters


async def run_collect_books() -> dict[str, Any]:
    """Run one order-book collection pass on every reachable venue.

    Candidate gathering (`list_markets(status="open")`) is isolated per
    venue right here — a venue whose read adapter or listing call fails
    contributes no candidates and does not stop the other venue's
    listing (see module docstring). The `VenueMarket` objects that
    listing call returns are carried straight into `DataCollector.
    collect_books` (mm-proveout T16 part a — see the module docstring's
    "CANDIDATES ARE `VenueMarket` OBJECTS" section for the measured cost
    of the redundant `get_market` re-fetch this replaces).

    The write phase calls `collect_books` ONCE PER VENUE, not once for
    every venue together (mm-proveout T16 part b, second half): a
    single-venue call is identical in every other respect to a combined
    one — `collect_books` already isolates and commits at that same
    per-venue grain internally (see its own docstring for the T9 fix) —
    but calling it once per venue is what lets this function report a
    PER-VENUE written count rather than one combined total, so a caller
    can distinguish "this venue never produced a candidate" (absent from
    `written`) from "this venue produced candidates and wrote zero of
    them" (present with `0`).

    `run_collection_preflight()` runs FIRST, before any candidate is
    listed or written — a failing DB-head or live field-name/reality
    check raises `CollectionPreflightError` and this pass never starts.
    See that function's own docstring for why (mm-proveout T9 follow-on:
    a silently-renamed venue field previously produced zero rows and an
    `exit 0` beat for as long as three weeks, with nothing to alert on).

    Returns:
        dict[str, Any]: `{"mode", "venues": [...venues that produced at
            least one candidate...], "written": {venue: count}}`. `mode`
            and `venues` keep the shape `app.tasks.scanner.run_scan` and
            `app.tasks.execution.reconcile_venues` already return for a
            consistent beat-result shape across this repo's tasks;
            `written` is now a per-venue mapping rather than a bare
            `int` — see the module docstring's "PER-VENUE WRITTEN
            COUNTS" section for why a combined total could not answer
            "which venue is dead".

    Once written, each `COLLECTED_VENUES` entry's consecutive-zero-write
    streak (`_consecutive_zero_writes`) is updated from `written_by_venue`
    and, if any venue has now reached `_ZERO_WRITE_ESCALATION_THRESHOLD`,
    `VenueEscalationError` is raised instead of returning — see that
    threshold's own docstring for the count and why. A venue ABSENT from
    `written_by_venue` this tick (never produced a candidate — a
    `list_markets` failure, not a write failure) resets its streak rather
    than extending it; that is a different, already-logged failure mode
    (`collect_books_list_markets_failed`), not the one this escalation
    tracks.

    Raises:
        CollectionPreflightError: If `run_collection_preflight()` finds
            the schema behind head or a venue's live payload no longer
            matching the fields collection depends on.
        VenueEscalationError: If a venue has now written zero rows for
            `_ZERO_WRITE_ESCALATION_THRESHOLD` consecutive ticks.
    """
    await run_collection_preflight()

    adapters = read_adapters()

    markets_per_venue: dict[VenueId, list[VenueMarket]] = {}
    for venue, adapter in adapters.items():
        try:
            markets = await adapter.list_markets(status="open")
        except Exception as exc:  # noqa: BLE001 - one venue must not stop the rest
            logger.warning(
                "collection",
                extra={
                    "event": "collect_books_list_markets_failed",
                    "venue": venue,
                    "reason": type(exc).__name__,
                },
            )
            continue
        markets_per_venue[venue] = markets

    written_by_venue: dict[VenueId, int] = {}
    async with async_session_factory() as session:
        collector = DataCollector(session)
        try:
            for venue, markets in markets_per_venue.items():
                written_by_venue[venue] = await collector.collect_books(
                    {venue: adapters[venue]}, {venue: markets}, session
                )
        finally:
            await collector.close()

    for venue in COLLECTED_VENUES:
        if venue not in written_by_venue:
            _consecutive_zero_writes[venue] = 0
        elif written_by_venue[venue] == 0:
            _consecutive_zero_writes[venue] = _consecutive_zero_writes.get(venue, 0) + 1
        else:
            _consecutive_zero_writes[venue] = 0

    escalated = {
        venue: streak
        for venue, streak in _consecutive_zero_writes.items()
        if streak >= _ZERO_WRITE_ESCALATION_THRESHOLD
    }
    if escalated:
        detail = ", ".join(
            f"{venue}: {streak} consecutive zero-write ticks"
            for venue, streak in sorted(escalated.items())
        )
        logger.error(
            "collection",
            extra={
                "event": "collect_books_venue_escalation",
                "venues": escalated,
                "threshold": _ZERO_WRITE_ESCALATION_THRESHOLD,
            },
        )
        raise VenueEscalationError(
            f"venue(s) at or beyond the {_ZERO_WRITE_ESCALATION_THRESHOLD}-tick "
            f"zero-write escalation threshold: {detail}"
        )

    return {
        "mode": settings.trading_mode,
        "venues": list(markets_per_venue),
        "written": written_by_venue,
    }


@celery_app.task(name="app.tasks.collection.collect_books")
def collect_books() -> dict[str, Any]:
    """Celery entry point for `run_collect_books()`.

    Uses `run_async_task` (`app.database`), not a bare `asyncio.run`, per
    that helper's own docstring: a bare `asyncio.run` closes its loop on
    exit while the module-level `engine`'s pool holds connections bound
    to it, so the FIRST beat tick in a worker process would succeed and
    every tick after it would raise `RuntimeError: Event loop is closed`
    — observed directly against a live Postgres for exactly this beat
    shape, and the reason every other beat in this file already uses
    this helper.

    Returns:
        dict[str, Any]: The collection summary from `run_collect_books()`.
    """
    return run_async_task(run_collect_books())


@celery_app.task(name="app.tasks.collection.run_collection_health")
def run_collection_health() -> dict[str, Any]:
    """Celery entry point for `app.scripts.collection_health` (mm-proveout
    T10), on its own `"collection-health"` beat
    (`app/tasks/__init__.py`, every `settings.collection_health_interval_s`
    seconds).

    Calls `collection_health._run` directly — the same async CLI seam
    `app.scripts.collection_health.main()` calls, minus the argparse
    parsing and the bare `asyncio.run` `main()` wraps it in (see that
    function's own docstring) — through `run_async_task` (`app.database`),
    exactly like `collect_books` above and for the identical reason:
    `run_async_task`'s own docstring explains why a bare `asyncio.run`
    corrupts the module-level `engine` pool on the SECOND beat tick in a
    real worker process, and `collection_health._run` opens its own
    session through that same module-level `engine`
    (`app.database.async_session_factory`).

    `hours=24.0` and `out=None` match `main()`'s own argparse defaults
    (`--hours` defaults to `24.0`, `--out` to `None`) — this beat is not
    asked to persist a JSON file to disk, only to run the check and
    leave its result in the Celery task result / logs, the same way
    `collect_books` above reports through its return value rather than a
    file.

    This task is NOT the collection preflight gate `run_collect_books`
    runs on every tick (`run_collection_preflight`, above) — that gate
    is synchronous with collection itself and blocks a single tick;
    this is a much-less-frequent, retrospective health read over the
    last 24 hours of already-written `book_snapshots` rows, intended to
    catch a slow-forming gap or staleness pattern collection preflight's
    single-tick field check cannot see.

    Returns:
        dict[str, Any]: `{"exit_code": ...}` — `0` if both venues had at
            least one snapshot in the lookback window, `1` otherwise
            (`collection_health._run`'s own return contract).
    """
    exit_code = run_async_task(collection_health._run(24.0, None))
    return {"exit_code": exit_code}
