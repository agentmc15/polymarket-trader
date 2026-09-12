"""`app.tasks.collection` — the mm-proveout T9 follow-on tasks: the
collection preflight gate and the persistently-dead-venue escalation.

Both close the same failure mode the brief describes: a venue silently
renaming a field (or a schema left behind head) previously produced zero
written rows, an `exit 0` beat, and no alarm anywhere — for as long as
three weeks. These tests pin that a beat now REFUSES to run rather than
silently writing nothing, and that a venue writing zero rows tick after
tick eventually becomes impossible to miss rather than one more
`logger.exception` line among thousands.

GUARDRAILS.md §1.4: no network to a venue from a test, ever. Every
adapter here is `tests.venues.fixture_adapter.FixtureAdapter`; every
database check is a hand-built `DatabaseCheck` fed in through a
monkeypatched `app.scripts.preflight._check_database_async`, the same
"inject the network-shaped fact" pattern `app.scripts.preflight`'s own
module docstring describes for `tests/test_preflight.py`.
"""
import importlib

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import app.tasks as tasks_module
import app.tasks.collection as collection_task
from app.config import settings as app_settings
from app.models.book_snapshot import BookSnapshot
from app.scripts.preflight import DatabaseCheck
from app.tasks import celery_app
from app.venues.types import OrderBook
from tests.helpers import make_book
from tests.venues.fixture_adapter import FixtureAdapter, make_venue_market

# ---------------------------------------------------------------------------
# Helpers shared by the two preflight tests below.
# ---------------------------------------------------------------------------


async def _fake_healthy_db(settings_obj: object, timeout_s: float = 3.0) -> DatabaseCheck:  # noqa: ARG001
    """Stand-in for `_check_database_async` -- reachable, at the real head."""
    return DatabaseCheck(
        reachable=True,
        display_url="postgresql+asyncpg://x:***@localhost/db",
        current_revision="009",
    )


def _quotable_kalshi_adapter(market_id: str = "KALSHI-HEALTHY") -> FixtureAdapter:
    """A normal, two-sided, quotable Kalshi fixture adapter -- passes
    `check_collection_for_venue` cleanly so a test can isolate a FAILING
    check to just the other one under test."""
    market = make_venue_market(
        venue="kalshi",
        market_id=market_id,
        outcomes=("YES", "NO"),
        raw={
            "yes_bid_dollars": "0.40",
            "yes_ask_dollars": "0.55",
            "volume_24h_fp": "500",
        },
    )
    return (
        FixtureAdapter("kalshi")
        .add_market(market)
        .set_book(
            make_book(
                bids=[(0.40, 10.0)], asks=[(0.55, 10.0)],
                venue="kalshi", market_id=market_id, outcome="YES",
            )
        )
    )


def _quotable_polymarket_adapter(market_id: str = "PM-HEALTHY") -> FixtureAdapter:
    """A normal, two-sided, quotable Polymarket fixture adapter -- same
    role as `_quotable_kalshi_adapter` above, for the other venue."""
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


# ---------------------------------------------------------------------------
# 1. The beat refuses to collect when the collection preflight fails.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_beat_refuses_to_collect_when_the_collection_preflight_fails(
    test_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither venue has a single market registered, so `check_collection_
    for_venue` finds nothing quotable to sample on either side -- exactly
    the "a field was renamed and nothing parses any more" shape this
    task exists to catch (see `app.tasks.collection.run_collection_
    preflight`'s docstring). The database check is faked HEALTHY
    (`_fake_healthy_db`) so this failure is unambiguously attributable
    to the collection check, not a coincidental database problem, and
    `run_collect_books()` must raise `CollectionPreflightError` before
    writing anything -- not merely log a warning and proceed.
    """
    sessions = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(collection_task, "async_session_factory", sessions)
    monkeypatch.setattr(collection_task, "_check_database_async", _fake_healthy_db)
    monkeypatch.setattr(
        collection_task,
        "read_adapters",
        lambda: {"kalshi": FixtureAdapter("kalshi"), "polymarket": FixtureAdapter("polymarket")},
    )

    with pytest.raises(collection_task.CollectionPreflightError) as exc_info:
        await collection_task.run_collect_books()

    message = str(exc_info.value)
    assert "collection preflight failed" in message
    assert "no quotable market was found to sample" in message

    async with sessions() as session:
        rows = (await session.execute(select(BookSnapshot))).scalars().all()
    assert rows == []  # refused BEFORE writing anything


# ---------------------------------------------------------------------------
# 2. The beat refuses to collect when the DB head revision is behind.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_beat_refuses_to_collect_when_the_db_head_revision_is_behind(
    test_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both venues are HEALTHY, quotable fixtures (isolating this failure
    to the database check alone), but the faked `DatabaseCheck` reports
    `current_revision="008"` while this repo's real, local migration
    files' head is `"009"` (two unapplied migrations, `008` then `009` --
    `_expected_head_revision()` is called for REAL here, unmocked, and
    reads that "009" head from the actual `alembic/versions/` files, the
    same mechanism `app.scripts.preflight.main()` uses). `_database_
    group` (also real, unmocked) must therefore report FAIL, and
    `run_collect_books()` must raise before writing anything.
    """
    sessions = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(collection_task, "async_session_factory", sessions)

    async def _fake_behind_db(settings_obj: object, timeout_s: float = 3.0) -> DatabaseCheck:  # noqa: ARG001
        return DatabaseCheck(
            reachable=True,
            display_url="postgresql+asyncpg://x:***@localhost/db",
            current_revision="008",
        )

    monkeypatch.setattr(collection_task, "_check_database_async", _fake_behind_db)
    monkeypatch.setattr(
        collection_task,
        "read_adapters",
        lambda: {
            "kalshi": _quotable_kalshi_adapter(),
            "polymarket": _quotable_polymarket_adapter(),
        },
    )

    assert collection_task._expected_head_revision() == "009"

    with pytest.raises(collection_task.CollectionPreflightError) as exc_info:
        await collection_task.run_collect_books()

    message = str(exc_info.value)
    assert "the schema is behind" in message
    assert "'008'" in message
    assert "'009'" in message

    async with sessions() as session:
        rows = (await session.execute(select(BookSnapshot))).scalars().all()
    assert rows == []  # refused BEFORE writing anything


# ---------------------------------------------------------------------------
# 3. The health beat entry exists and reads its own interval setting.
# ---------------------------------------------------------------------------


def test_the_health_beat_entry_exists_and_reads_a_changed_interval_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirrors `tests/tasks/test_collect_books_beat.py::test_the_beat_
    schedule_follows_a_changed_interval_setting_not_a_hardcoded_60`: the
    beat entry existing and equalling the CURRENT `settings.collection_
    health_interval_s` cannot tell "reads the setting" apart from "hard-
    codes today's default", since both sides would be equal by
    coincidence. This patches the setting to a value nothing in this
    file would produce by accident (911.0, not the 1800.0 default and
    not equal to `book_collection_interval_s`), reloads `app.tasks` so
    the module-level `beat_schedule` dict is rebuilt against the patched
    setting, and restores the original afterwards (in a `finally`) so no
    other test in the session sees a mutated module.
    """
    from app.config import Settings

    assert (
        Settings.model_fields["collection_health_interval_s"].alias
        == "COLLECTION_HEALTH_INTERVAL_S"
    )
    assert "app.tasks.collection" in celery_app.conf.include
    celery_app.loader.import_default_modules()
    assert "app.tasks.collection.run_collection_health" in celery_app.tasks

    sentinel = 911.0
    assert sentinel not in (1800.0, app_settings.book_collection_interval_s)
    original = app_settings.collection_health_interval_s
    monkeypatch.setattr(app_settings, "collection_health_interval_s", sentinel)
    try:
        importlib.reload(tasks_module)
        entry = next(
            item
            for item in tasks_module.celery_app.conf.beat_schedule.values()
            if item["task"] == "app.tasks.collection.run_collection_health"
        )
        assert entry["schedule"] == sentinel
    finally:
        monkeypatch.undo()
        importlib.reload(tasks_module)
        restored = next(
            item
            for item in tasks_module.celery_app.conf.beat_schedule.values()
            if item["task"] == "app.tasks.collection.run_collection_health"
        )
        assert restored["schedule"] == original


# ---------------------------------------------------------------------------
# 4. N consecutive zero-write ticks for one venue escalates; N-1 does not.
# ---------------------------------------------------------------------------


class _AlwaysBrokenGetBookAdapter(FixtureAdapter):
    """A read adapter whose `get_book` always raises a PLAIN
    `RuntimeError` -- the same "unwrapped payload bug" shape `tests/
    tasks/test_collect_books_beat.py::_UnwrappedPayloadBugAdapter` uses,
    proven there to make `DataCollector.collect_books` report `0`
    written for this venue (present in `written`, not absent) on every
    single tick, forever -- exactly the persistent failure this
    escalation exists to catch.
    """

    async def get_book(self, market_id: str, outcome: str) -> OrderBook:
        raise RuntimeError(f"unwrapped payload bug for {market_id!r}/{outcome!r}")


@pytest.mark.asyncio
async def test_n_consecutive_zero_write_ticks_escalates_but_n_minus_1_does_not(
    test_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runs `run_collect_books()` repeatedly against a Kalshi adapter
    whose `get_book` always fails -- `written["kalshi"] == 0` every tick,
    never absent, so the escalation counter (`_consecutive_zero_writes`)
    increments every time. The collection preflight gate (change 3) is
    replaced with a no-op here: this test isolates the ESCALATION
    mechanism (change 4), which is unreachable through the real preflight
    when the very same broken adapter is also what the preflight's own
    `check_collection_for_venue` would sample from.

    `_ZERO_WRITE_ESCALATION_THRESHOLD` (not a hardcoded number) is read
    directly off the module, so this test tracks whatever N the module
    actually uses rather than asserting a number that could silently
    drift out of sync with it.
    """
    threshold = collection_task._ZERO_WRITE_ESCALATION_THRESHOLD
    assert threshold >= 2, "a 1-tick threshold can't distinguish N from N-1"

    sessions = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(collection_task, "async_session_factory", sessions)
    monkeypatch.setattr(collection_task, "_consecutive_zero_writes", {})

    async def _noop_preflight() -> None:
        return None

    monkeypatch.setattr(collection_task, "run_collection_preflight", _noop_preflight)

    broken = _AlwaysBrokenGetBookAdapter("kalshi").add_market(
        make_venue_market(
            venue="kalshi",
            market_id="KALSHI-DEAD",
            outcomes=("YES",),
            raw={
                "yes_bid_dollars": "0.40",
                "yes_ask_dollars": "0.55",
                "volume_24h_fp": "500",
            },
        )
    )
    monkeypatch.setattr(collection_task, "read_adapters", lambda: {"kalshi": broken})

    for tick in range(1, threshold):  # N-1 ticks: 1 .. threshold-1
        summary = await collection_task.run_collect_books()
        assert summary["written"] == {"kalshi": 0}
        assert collection_task._consecutive_zero_writes["kalshi"] == tick

    # The Nth consecutive zero-write tick escalates instead of returning.
    with pytest.raises(collection_task.VenueEscalationError) as exc_info:
        await collection_task.run_collect_books()
    assert "kalshi" in str(exc_info.value)
    assert str(threshold) in str(exc_info.value)
