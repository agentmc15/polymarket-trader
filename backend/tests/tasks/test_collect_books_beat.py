"""`app.tasks.collection` — the mm-proveout T9 beat.

`DataCollector.collect_books` (T7/T8) recorded real order-book depth on
both venues but had NO periodic caller at all before this task existed —
only the manual `python -m app.scripts.collect_prices --books` CLI loop.
These tests cover the three things the brief's acceptance line asks for:

  1. The beat entry exists, is reachable from a worker, and is scheduled
     on its OWN `settings.book_collection_interval_s` knob, not a
     literal or a reuse of another interval — mirrors the pattern
     `tests/matching/test_link_proposal_beat.py` and
     `tests/services/test_near_resolution.py` already use for their own
     beats.
  2. The Celery task's synchronous entry point runs its async body
     through `run_async_task` (`app.database`), never a bare
     `asyncio.run` — `run_async_task`'s own docstring explains why a bare
     `asyncio.run` corrupts the engine pool on the SECOND beat tick in a
     real worker process.
  3. Per-venue isolation — extended, per the mm-proveout T9 correction,
     beyond the brief's original `VenueError`-only claim: a venue whose
     `list_markets` raises `VenueError` does not prevent the other
     venue's snapshot, AND (the case the T9 red-team confirmed actually
     fails without the fix) a venue whose processing raises a PLAIN,
     non-`VenueError` exception does not either. The second guarantee
     lives inside `DataCollector.collect_books` itself
     (`tests/services/test_book_collection_selection.py` pins it at that
     lower level directly); the tests here confirm it holds through the
     FULL beat path (`run_collect_books()`, the exact coroutine the
     Celery task runs).

GUARDRAILS.md §1.4: no network to a venue from a test, ever. Every
adapter here is `tests.venues.fixture_adapter.FixtureAdapter` or a local
subclass that raises a pre-constructed exception in process.
"""
import asyncio
import importlib
import logging
from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import app.tasks as tasks_module
import app.tasks.collection as collection_task
from app.config import Settings
from app.config import settings as app_settings
from app.models.book_snapshot import BookSnapshot
from app.tasks import celery_app
from app.venues.base import VenueError
from app.venues.types import MarketStatus, OrderBook, VenueId, VenueMarket
from tests.helpers import make_book
from tests.venues.fixture_adapter import FixtureAdapter, make_venue_market


async def _noop_preflight() -> None:
    """No-op stand-in for `collection_task.run_collection_preflight`.

    mm-proveout T9 follow-on: `run_collect_books()` now runs the
    collection preflight (DB head revision + a live field-name/reality
    check against THIS beat's own `read_adapters()`) before collecting
    anything -- see `tests/tasks/test_collection_preflight_and_
    escalation.py` for the dedicated tests of that gate. The tests below
    predate that gate and exist to pin per-venue isolation DURING
    collection (a listing failure, a write-time failure, cancellation) —
    an orthogonal concern that deliberately uses BROKEN adapters
    (`_VenueErrorListingAdapter`, `_UnwrappedPayloadBugAdapter`) which
    would otherwise ALSO trip the live field-name/reality check (the
    exact same adapters are what `read_adapters()` hands to it), making
    every one of those tests fail on the gate before ever reaching the
    write-time behaviour they exist to test. Monkeypatching the gate to
    a no-op here isolates that concern back to just collection itself,
    the same way `test_the_celery_task_runs_its_async_body_through_
    run_async_task` above isolates a different concern by faking
    `run_async_task` itself.
    """
    return None


# ---------------------------------------------------------------------------
# 1. The beat exists, is scheduled on its own interval, and is reachable.
# ---------------------------------------------------------------------------


def test_the_beat_schedules_collect_books_on_its_own_interval() -> None:
    """Three separate mistakes each produce a beat that never runs, so
    three separate things are asserted: the entry can be missing, the
    module can be absent from `celery_app.conf.include` (a worker would
    then never import it, so the task name would resolve nowhere), and
    the interval can be silently aliased onto an existing scan/collection
    knob.
    """
    schedule = celery_app.conf.beat_schedule
    entry = next(
        item
        for item in schedule.values()
        if item["task"] == "app.tasks.collection.collect_books"
    )

    assert "app.tasks.collection" in celery_app.conf.include
    celery_app.loader.import_default_modules()
    assert "app.tasks.collection.collect_books" in celery_app.tasks

    # Its own knob, not a literal and not a reuse of any other interval.
    assert entry["schedule"] == app_settings.book_collection_interval_s
    assert (
        Settings.model_fields["book_collection_interval_s"].alias
        == "BOOK_COLLECTION_INTERVAL_S"
    )
    assert Settings().book_collection_interval_s == 180.0
    assert app_settings.book_collection_interval_s != app_settings.scan_interval_s
    assert (
        app_settings.book_collection_interval_s
        != app_settings.near_resolution_scan_interval_s
    )
    assert (
        app_settings.book_collection_interval_s
        != app_settings.link_proposal_interval_s
    )


def test_the_beat_schedule_follows_a_changed_interval_setting_not_a_hardcoded_60(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The test above's `entry["schedule"] == app_settings.book_collection_
    interval_s` cannot tell "this reads the setting" apart from "this
    hardcodes 60.0" -- both sides of that equality are 60.0 by default, so
    a beat entry written as the literal `"schedule": 60.0` would satisfy
    it exactly as well as the real `"schedule": settings.book_collection_
    interval_s`.

    `celery_app.conf.beat_schedule` is a plain dict built ONCE, at
    `app.tasks` import time, from whatever `settings.book_collection_
    interval_s` held at that moment -- so proving genuine, dynamic
    sourcing means changing the setting BEFORE the dict is (re)built, not
    reading it back afterwards. This patches the setting to a value nothing
    in this file would ever produce by coincidence (137.5), reloads
    `app.tasks` so the module-level dict is rebuilt against the patched
    setting, and reloads it back to the original value afterwards (in a
    `finally`) so no other test in the session sees a mutated module.
    """
    sentinel = 137.5
    assert sentinel not in (
        60.0,
        app_settings.scan_interval_s,
        app_settings.near_resolution_scan_interval_s,
        app_settings.link_proposal_interval_s,
    )
    original = app_settings.book_collection_interval_s
    monkeypatch.setattr(app_settings, "book_collection_interval_s", sentinel)
    try:
        importlib.reload(tasks_module)
        entry = next(
            item
            for item in tasks_module.celery_app.conf.beat_schedule.values()
            if item["task"] == "app.tasks.collection.collect_books"
        )
        assert entry["schedule"] == sentinel
    finally:
        # Undo the patch *now* (not at fixture teardown) so the restoring
        # reload below rebuilds the schedule against the real setting.
        monkeypatch.undo()
        importlib.reload(tasks_module)
        restored = next(
            item
            for item in tasks_module.celery_app.conf.beat_schedule.values()
            if item["task"] == "app.tasks.collection.collect_books"
        )
        assert restored["schedule"] == original


# ---------------------------------------------------------------------------
# 2. The Celery task body runs through `run_async_task`.
# ---------------------------------------------------------------------------


def test_the_celery_task_runs_its_async_body_through_run_async_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`run_async_task` (`app.database`) exists precisely because a bare
    `asyncio.run` closes its loop on exit while the module-level `engine`
    pool holds connections bound to it -- the first beat tick would
    succeed and every tick after it would raise `RuntimeError: Event
    loop is closed` in a real worker. Every other beat in this repo
    already uses it; this pins that `collect_books` does too, by
    substituting a spy for `run_async_task` and confirming the task's
    body calls it exactly once with the `run_collect_books()` coroutine,
    rather than awaiting/running it some other way.
    """
    calls: list[object] = []

    def fake_run_async_task(coro: object) -> dict[str, object]:
        calls.append(coro)
        coro.close()  # type: ignore[attr-defined]  # never actually run
        return {"mode": "paper", "venues": [], "written": 0}

    monkeypatch.setattr(collection_task, "run_async_task", fake_run_async_task)

    result = collection_task.collect_books()

    assert len(calls) == 1
    assert result == {"mode": "paper", "venues": [], "written": 0}


# ---------------------------------------------------------------------------
# 3. Per-venue isolation through the FULL beat path.
# ---------------------------------------------------------------------------


class _VenueErrorListingAdapter(FixtureAdapter):
    """A read adapter whose `list_markets` raises `VenueError` -- the
    ordinary, already-anticipated failure (a rate limit, an outage)."""

    async def list_markets(
        self,
        status: MarketStatus | None = None,  # noqa: ARG002 - Protocol shape
        updated_since: datetime | None = None,  # noqa: ARG002 - Protocol shape
    ) -> list[VenueMarket]:
        raise VenueError("kalshi: rate limited")


class _UnwrappedPayloadBugAdapter(FixtureAdapter):
    """A read adapter whose `get_book` always raises a PLAIN
    `RuntimeError` -- the T9 red-team's confirmed non-`VenueError`
    failure mode ("an unwrapped payload bug"), as opposed to the faults
    `DataCollector.collect_books` isolates per-candidate (`VenueError`,
    and since mm-proveout T16, `httpx.HTTPError` too --
    `_MARKET_FETCH_FAULTS`). This raises from `get_book`, not
    `get_market`: T16 changed this beat to carry `list_markets`'
    `VenueMarket` objects straight through to `collect_books` with NO
    `get_market` re-fetch at all (see `app.tasks.collection`'s module
    docstring), so a bug reachable only through `get_market` is no
    longer reachable through this beat at all. `get_book` remains the
    live wire: it is still called, every tick, for every selected
    market's every outcome, regardless of T16 — this is where the
    `RuntimeError` actually fires, deep inside `collect_books`'s own
    per-venue body, never inside the `_MARKET_FETCH_FAULTS` catch around
    that same call.
    """

    async def get_book(self, market_id: str, outcome: str) -> OrderBook:
        raise RuntimeError(f"unwrapped payload bug for {market_id!r}/{outcome!r}")


def _healthy_polymarket_adapter(market_id: str = "PM-HEALTHY") -> FixtureAdapter:
    """A normal, two-sided, quotable Polymarket fixture adapter."""
    market = make_venue_market(
        venue="polymarket",
        market_id=market_id,
        outcomes=("YES",),
        raw={"bestBid": 0.40, "bestAsk": 0.55, "volume24hr": 500.0},
    )
    return (
        FixtureAdapter("polymarket")
        .add_market(market)
        .set_book(
            make_book(
                bids=[(0.40, 10.0)], asks=[(0.55, 10.0)],
                market_id=market_id, outcome="YES",
            )
        )
    )


class _CountingGetMarketAdapter(FixtureAdapter):
    """`FixtureAdapter` that counts `get_market` calls -- so a test can
    prove the FULL beat path never makes one (mm-proveout T16 part a)."""

    def __init__(self, venue: VenueId = "polymarket", **kwargs: object) -> None:
        super().__init__(venue, **kwargs)  # type: ignore[arg-type]
        self.get_market_calls = 0

    async def get_market(self, market_id: str) -> VenueMarket:
        self.get_market_calls += 1
        return await super().get_market(market_id)


@pytest.mark.asyncio
async def test_the_beat_makes_no_get_market_call_at_all(
    test_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """mm-proveout T16 part a. Measured live 2026-09-07: Kalshi's own
    open-market listing walk returned 99,588 markets in 10.46s while
    `get_market` averaged 0.096s/call -- re-fetching every candidate cost
    ~2.66 HOURS per tick against a 60-second beat
    (`settings.book_collection_interval_s`). `run_collect_books()` now
    carries `list_markets`' own `VenueMarket` objects straight through to
    `DataCollector.collect_books`, so the FULL beat path -- not just
    `collect_books` in isolation -- makes ZERO `get_market` calls for
    either venue, on ANY tick, regardless of how many candidates it
    lists.
    """
    sessions = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    kalshi = _CountingGetMarketAdapter("kalshi").add_market(
        make_venue_market(
            venue="kalshi",
            market_id="KALSHI-NO-REFETCH",
            outcomes=("YES",),
            raw={
                "yes_bid_dollars": "0.40",
                "yes_ask_dollars": "0.55",
                "volume_24h_fp": "500",
            },
        )
    ).set_book(
        make_book(
            bids=[(0.40, 10.0)], asks=[(0.55, 10.0)],
            venue="kalshi", market_id="KALSHI-NO-REFETCH", outcome="YES",
        )
    )
    polymarket = _CountingGetMarketAdapter("polymarket").add_market(
        make_venue_market(
            venue="polymarket",
            market_id="PM-NO-REFETCH",
            outcomes=("YES",),
            raw={"bestBid": 0.40, "bestAsk": 0.55, "volume24hr": 500.0},
        )
    ).set_book(
        make_book(
            bids=[(0.40, 10.0)], asks=[(0.55, 10.0)],
            market_id="PM-NO-REFETCH", outcome="YES",
        )
    )
    adapters = {"kalshi": kalshi, "polymarket": polymarket}
    monkeypatch.setattr(collection_task, "read_adapters", lambda: adapters)
    monkeypatch.setattr(collection_task, "run_collection_preflight", _noop_preflight)
    monkeypatch.setattr(collection_task, "async_session_factory", sessions)

    summary = await collection_task.run_collect_books()

    assert summary["written"] == {"kalshi": 1, "polymarket": 1}
    assert kalshi.get_market_calls == 0
    assert polymarket.get_market_calls == 0


@pytest.mark.asyncio
async def test_a_venue_error_in_one_venues_listing_does_not_block_the_others_snapshot(
    test_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kalshi's `list_markets` raises `VenueError`; Polymarket must still
    get a `BookSnapshot` row through the FULL `run_collect_books()` path
    (the coroutine the Celery task actually runs), not merely through
    `DataCollector.collect_books` in isolation.
    """
    sessions = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    adapters = {
        "kalshi": _VenueErrorListingAdapter("kalshi"),
        "polymarket": _healthy_polymarket_adapter(),
    }
    monkeypatch.setattr(collection_task, "read_adapters", lambda: adapters)
    monkeypatch.setattr(collection_task, "run_collection_preflight", _noop_preflight)
    monkeypatch.setattr(collection_task, "async_session_factory", sessions)

    summary = await collection_task.run_collect_books()

    assert summary["mode"] == "paper"
    assert summary["written"] == {"polymarket": 1}
    assert summary["venues"] == ["polymarket"]  # kalshi never produced candidates

    async with sessions() as session:
        rows = (await session.execute(select(BookSnapshot))).scalars().all()
    assert [row.market_id for row in rows] == ["PM-HEALTHY"]


@pytest.mark.asyncio
async def test_a_non_venue_error_exception_in_one_venue_does_not_block_the_others_snapshot(
    test_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The T9 correction's case: Kalshi's `get_book` raises a PLAIN
    `RuntimeError` (not `VenueError`, and not `httpx.HTTPError`) while
    WRITING, not listing. Confirmed live by the T9 red-team as the case
    that actually breaks `collect_books` without the fix (a
    `VenueError`-only, later `_MARKET_FETCH_FAULTS`-only, per-candidate
    boundary does not catch this). Kalshi's listing/selection succeed
    fine (`written["kalshi"] == 0`, not absent -- it DID produce a
    candidate, it just wrote none of it); Polymarket must still get its
    snapshot through the exact coroutine the Celery beat runs.
    """
    sessions = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    broken = _UnwrappedPayloadBugAdapter("kalshi").add_market(
        make_venue_market(
            venue="kalshi",
            market_id="KALSHI-BROKEN",
            outcomes=("YES",),
            raw={
                "yes_bid_dollars": "0.40",
                "yes_ask_dollars": "0.55",
                "volume_24h_fp": "500",
            },
        )
    )
    adapters: dict[VenueId, FixtureAdapter] = {
        "kalshi": broken,
        "polymarket": _healthy_polymarket_adapter(),
    }
    monkeypatch.setattr(collection_task, "read_adapters", lambda: adapters)
    monkeypatch.setattr(collection_task, "run_collection_preflight", _noop_preflight)
    monkeypatch.setattr(collection_task, "async_session_factory", sessions)

    summary = await collection_task.run_collect_books()

    assert summary["written"] == {"kalshi": 0, "polymarket": 1}
    assert set(summary["venues"]) == {"kalshi", "polymarket"}  # both LISTED candidates

    async with sessions() as session:
        rows = (await session.execute(select(BookSnapshot))).scalars().all()
    assert [row.market_id for row in rows] == ["PM-HEALTHY"]


@pytest.mark.asyncio
async def test_the_first_venues_commit_survives_the_second_venues_failure_in_production_order(
    test_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two isolation tests above both put the BROKEN venue FIRST in
    the mocked `adapters` dict (kalshi), so neither actually exercises
    "an EARLIER venue's already-committed rows survive a LATER venue's
    failure" -- the specific half of the T9 fix that moved
    `session.commit()` from once-at-the-end to once-per-venue. This test
    uses the PRODUCTION iteration order (`COLLECTED_VENUES = ("polymarket",
    "kalshi")`): Polymarket succeeds and commits first, Kalshi's
    processing raises second. Read back through a freshly opened session
    (`sessions()` called again, a new `AsyncSession`/identity map) rather
    than one this test wrote through, so a pass cannot be explained by a
    writer session's in-memory cache instead of a real, durable commit.
    """
    sessions = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    broken = _UnwrappedPayloadBugAdapter("kalshi").add_market(
        make_venue_market(
            venue="kalshi",
            market_id="KALSHI-BROKEN",
            outcomes=("YES",),
            raw={
                "yes_bid_dollars": "0.40",
                "yes_ask_dollars": "0.55",
                "volume_24h_fp": "500",
            },
        )
    )
    adapters: dict[VenueId, FixtureAdapter] = {
        "polymarket": _healthy_polymarket_adapter(),
        "kalshi": broken,
    }
    monkeypatch.setattr(collection_task, "read_adapters", lambda: adapters)
    monkeypatch.setattr(collection_task, "run_collection_preflight", _noop_preflight)
    monkeypatch.setattr(collection_task, "async_session_factory", sessions)

    summary = await collection_task.run_collect_books()

    assert summary["written"] == {"polymarket": 1, "kalshi": 0}

    async with sessions() as fresh_session:
        rows = (await fresh_session.execute(select(BookSnapshot))).scalars().all()
    assert [row.market_id for row in rows] == ["PM-HEALTHY"]


# ---------------------------------------------------------------------------
# 4. A `BaseException` (cancellation) is never swallowed; a caught
#    per-venue exception leaves evidence (logged with a traceback).
# ---------------------------------------------------------------------------


class _CancellingListingAdapter(FixtureAdapter):
    """A read adapter whose `list_markets` raises `asyncio.CancelledError`
    -- a `BaseException` subclass since Python 3.8, deliberately NOT an
    `Exception`. Simulates a Celery worker cancelling this task mid-tick
    (SIGTERM, `task_time_limit`, a soft timeout)."""

    async def list_markets(
        self,
        status: MarketStatus | None = None,  # noqa: ARG002 - Protocol shape
        updated_since: datetime | None = None,  # noqa: ARG002 - Protocol shape
    ) -> list[VenueMarket]:
        raise asyncio.CancelledError()


@pytest.mark.asyncio
async def test_a_cancelled_error_in_one_venues_listing_is_never_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both `run_collect_books`'s listing loop and `DataCollector.
    collect_books`'s per-venue body catch bare `except Exception`
    (never `except BaseException`) precisely so `asyncio.CancelledError`
    passes straight through instead of being treated as "one venue's
    trouble". This is really a Python-semantics guarantee (`Exception`
    does not match a `BaseException`-only subclass), not something a
    test can break by itself -- but it pins the actual call path against
    a plausible future regression: widening either catch to `except
    BaseException` (an easy, "make isolation even more robust"-looking
    mistake) would make a worker's own task cancellation disappear into
    a warning log instead of the task actually stopping.
    """
    adapters = {
        "kalshi": _CancellingListingAdapter("kalshi"),
        "polymarket": _healthy_polymarket_adapter(),
    }
    monkeypatch.setattr(collection_task, "read_adapters", lambda: adapters)
    monkeypatch.setattr(collection_task, "run_collection_preflight", _noop_preflight)

    with pytest.raises(asyncio.CancelledError):
        await collection_task.run_collect_books()


@pytest.mark.asyncio
async def test_a_non_venue_error_exception_is_logged_with_a_traceback(
    test_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A venue that fails every tick, forever (the T9 correction's new
    silent-failure mode: the beat reports success while that venue
    contributes zero rows), must at minimum leave evidence.
    `DataCollector.collect_books`'s per-venue `except Exception` calls
    `logger.exception(...)` (`app.services.data_collector`), which always
    logs at ERROR and attaches the real traceback (`exc_info`) -- not
    `logger.warning`, which would record only that *something* happened.
    This pins both properties through the FULL beat path
    (`run_collect_books`, the coroutine the Celery task actually runs),
    not merely that `collect_books` "logs something".
    """
    sessions = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    broken = _UnwrappedPayloadBugAdapter("kalshi").add_market(
        make_venue_market(
            venue="kalshi",
            market_id="KALSHI-BROKEN",
            outcomes=("YES",),
            raw={
                "yes_bid_dollars": "0.40",
                "yes_ask_dollars": "0.55",
                "volume_24h_fp": "500",
            },
        )
    )
    adapters: dict[VenueId, FixtureAdapter] = {
        "kalshi": broken,
        "polymarket": _healthy_polymarket_adapter(),
    }
    monkeypatch.setattr(collection_task, "read_adapters", lambda: adapters)
    monkeypatch.setattr(collection_task, "run_collection_preflight", _noop_preflight)
    monkeypatch.setattr(collection_task, "async_session_factory", sessions)

    with caplog.at_level(logging.WARNING, logger="app.services.data_collector"):
        await collection_task.run_collect_books()

    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records, "expected an ERROR-level record for the failed venue"
    assert any(r.exc_info for r in error_records), (
        "expected a real traceback (logger.exception, not logger.warning) "
        "for the failed venue"
    )
    assert any("kalshi" in r.getMessage() for r in error_records)
