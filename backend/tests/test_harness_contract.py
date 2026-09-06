"""Contract tests for the T02 test harness itself.

These tests do not exercise application behavior — they exercise the test
INFRASTRUCTURE that every later task in the `market-edge` kit will silently
depend on: `tests/conftest.py`'s engine/session/client fixtures and
`tests/helpers.py`'s builders. A hole here (isolation that doesn't isolate, a
`StaticPool` that isn't actually wired, an FK pragma that isn't actually live,
a `TRADING_MODE` guard that doesn't actually hold, a `client` fixture that
talks to a different database than the `test_session` fixture) would silently
poison every downstream task's test suite.

Per the test-author role, these were derived from the T02 brief/acceptance
criteria in `.claude/kits/market-edge/TASKS.md` and the harness-contract
properties named by the dispatch — NOT by reading `conftest.py`/`helpers.py`
and mirroring their internal logic. Fixture and helper *names* (`test_engine`,
`test_session`, `client`, `make_snapshot`) were read off the files to write
valid pytest code; the *assertions* below encode independently-formed
expectations about what those fixtures must guarantee.
"""
import os
from datetime import datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from app.api.deps import AsyncSessionDep
from app.models import Market, MarketPrice, Order
from app.models.trade import OrderSide
from app.utils.time import utcnow
from tests.helpers import make_snapshot

# ---------------------------------------------------------------------------
# Isolation: state written in one test must be invisible in another,
# order-independently (neither test may assume it runs before the other).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_isolation_marker_alpha_absent_then_created(
    test_session: AsyncSession,
) -> None:
    """A fresh DB per test function means no marker from a sibling test leaks in.

    Symmetric with `test_isolation_marker_bravo_absent_then_created` below: each
    test checks for *both* markers before creating its own, so the pair proves
    isolation regardless of which one pytest happens to run first (i.e. holds
    under `-p no:randomly` in either order).
    """
    for condition_id in ("iso-marker-alpha", "iso-marker-bravo"):
        result = await test_session.execute(
            select(Market).where(Market.condition_id == condition_id)
        )
        assert result.scalar_one_or_none() is None, (
            f"{condition_id!r} was visible at the start of this test — a "
            "previous test's data leaked through the fixture"
        )

    test_session.add(Market(condition_id="iso-marker-alpha", question="alpha"))
    await test_session.flush()


@pytest.mark.asyncio
async def test_isolation_marker_bravo_absent_then_created(
    test_session: AsyncSession,
) -> None:
    """See `test_isolation_marker_alpha_absent_then_created` — same pattern."""
    for condition_id in ("iso-marker-alpha", "iso-marker-bravo"):
        result = await test_session.execute(
            select(Market).where(Market.condition_id == condition_id)
        )
        assert result.scalar_one_or_none() is None, (
            f"{condition_id!r} was visible at the start of this test — a "
            "previous test's data leaked through the fixture"
        )

    test_session.add(Market(condition_id="iso-marker-bravo", question="bravo"))
    await test_session.flush()


# ---------------------------------------------------------------------------
# StaticPool: ONE in-memory SQLite database must survive across the several
# connections SQLAlchemy's async engine opens during a test.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_static_pool_shares_one_memory_db_across_connections(
    test_engine: AsyncEngine,
) -> None:
    """Two connections opened from `test_engine`, held open concurrently,
    must observe the same in-memory database — proving `StaticPool` (and
    `check_same_thread=False`) is actually wired into the fixture, not just
    documented as intended.
    """
    async with test_engine.connect() as conn1:
        await conn1.execute(
            text("CREATE TABLE _sp_probe (id INTEGER PRIMARY KEY, v TEXT)")
        )
        await conn1.execute(text("INSERT INTO _sp_probe (v) VALUES ('from-conn1')"))
        await conn1.commit()

        # conn2 is opened WHILE conn1 is still checked out, forcing the pool
        # to hand out what StaticPool guarantees is the same physical
        # connection rather than a fresh, private :memory: database.
        async with test_engine.connect() as conn2:
            result = await conn2.execute(text("SELECT v FROM _sp_probe"))
            rows = result.scalars().all()

    assert rows == ["from-conn1"], (
        f"expected the concurrently-opened second connection to see conn1's "
        f"insert via a single shared StaticPool connection, got {rows!r}"
    )


@pytest.mark.asyncio
async def test_without_static_pool_concurrent_connections_get_private_dbs() -> None:
    """Contrast case, built locally in this test (conftest.py is untouched):
    an engine to the same `sqlite+aiosqlite:///:memory:` URL, but with
    pooling that does NOT hand back the same physical connection, gives two
    concurrently-held connections two different, private, empty `:memory:`
    databases — the exact failure mode `StaticPool` in `conftest.py`'s
    `test_engine` fixture exists to prevent.

    Note on how this contrast is constructed: simply omitting
    `poolclass=StaticPool` is NOT a valid contrast here — verified
    empirically while writing this test, SQLAlchemy's aiosqlite dialect
    already auto-selects `StaticPool` by default for any `:memory:` URL
    (`type(engine.pool).__name__ == "StaticPool"` even with no `poolclass`
    kwarg at all). So this test forces `poolclass=NullPool` (no connection
    reuse whatsoever) to actually produce two distinct physical connections,
    which is the genuine contrast to what `StaticPool` guarantees.
    """
    from sqlalchemy.pool import NullPool

    bare_engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    try:
        async with bare_engine.connect() as conn1:
            await conn1.execute(
                text("CREATE TABLE _sp_probe2 (id INTEGER PRIMARY KEY, v TEXT)")
            )
            await conn1.execute(
                text("INSERT INTO _sp_probe2 (v) VALUES ('from-conn1')")
            )
            await conn1.commit()

            async with bare_engine.connect() as conn2:
                # conn2 is a distinct physical connection to its own private
                # :memory: database — the table conn1 created doesn't exist
                # here at all.
                with pytest.raises(OperationalError):
                    await conn2.execute(text("SELECT v FROM _sp_probe2"))
    finally:
        await bare_engine.dispose()


# ---------------------------------------------------------------------------
# FK enforcement: `PRAGMA foreign_keys=ON` must actually be live, not merely
# declared on the model columns.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fk_violation_raises_integrity_error(
    test_session: AsyncSession,
) -> None:
    """`MarketPrice.market_id` referencing a nonexistent `Market` must raise
    `IntegrityError` on flush. SQLite ignores `ForeignKey` declarations
    unless `PRAGMA foreign_keys=ON` has been issued on the connection — T01's
    red-team demonstrated this insert succeeding silently without it.
    """
    bad_price = MarketPrice(
        market_id=999_999_999,  # no Market with this id exists in this test's DB
        timestamp=utcnow(),
        outcome="YES",
        open=0.5,
        high=0.5,
        low=0.5,
        close=0.5,
    )
    test_session.add(bad_price)

    with pytest.raises(IntegrityError):
        await test_session.flush()


@pytest.mark.asyncio
async def test_foreign_keys_pragma_is_on_for_session_connection(
    test_session: AsyncSession,
) -> None:
    """The `connect` event listener that issues `PRAGMA foreign_keys=ON` must
    actually have fired for the connection backing `test_session` — this is
    what makes the previous test's `IntegrityError` real rather than
    coincidental.
    """
    result = await test_session.execute(text("PRAGMA foreign_keys"))
    value = result.scalar_one()
    assert value == 1, (
        f"PRAGMA foreign_keys reads back as {value!r} on the session's "
        "connection, expected 1 (ON) — FK constraints are decorative without it"
    )


# ---------------------------------------------------------------------------
# TRADING_MODE: paper, as seen by the process — not merely an env var.
# ---------------------------------------------------------------------------


def test_trading_mode_env_forced_to_paper_before_app_config_loads() -> None:
    """`conftest.py` must set `TRADING_MODE=paper` via `os.environ.setdefault`
    at import time, before `app.config`'s `lru_cache`d `Settings` is first
    constructed (a late `os.environ[...] = ...` after that point is a no-op
    for any already-cached `Settings()`). By the time any test module runs,
    the env var must already read back as the paper value.
    """
    assert os.environ.get("TRADING_MODE") == "paper"


def test_settings_trading_mode_matches_paper_or_field_not_yet_added() -> None:
    """The actual cached `Settings` object the app uses (not a raw env-var
    string) must report paper mode wherever it exposes the concept.

    FINDING (not a fixture defect): as of this kit's T02 stage,
    `app.config.Settings` has no `trading_mode` field at all — per
    `.claude/kits/market-edge/TASKS.md`/`PLAN.md`, `trading_mode` and
    `live_trading_confirmation` are added in T11 alongside
    `app/execution/fences.py`. This test is written so it will start
    asserting for real the moment that field exists, instead of silently
    passing either way; today it documents the gap via `pytest.skip` rather
    than fabricating a pass against an attribute that isn't there.
    """
    from app.config import get_settings

    settings_obj = get_settings()
    if not hasattr(settings_obj, "trading_mode"):
        pytest.skip(
            "Settings.trading_mode does not exist yet at T02 (see TASKS.md "
            "T11) — TRADING_MODE env-var enforcement is covered by "
            "test_trading_mode_env_forced_to_paper_before_app_config_loads"
        )
    assert settings_obj.trading_mode == "paper"


# ---------------------------------------------------------------------------
# `make_snapshot`: aware-UTC default; naive-passthrough behavior documented.
# ---------------------------------------------------------------------------


def test_make_snapshot_default_timestamp_is_aware() -> None:
    """`make_snapshot()` with no `ts` argument must default to an aware UTC
    datetime (via `utcnow()`), never a naive one — GUARDRAILS.md's datetime
    convention.
    """
    snapshot = make_snapshot()
    assert snapshot.timestamp.tzinfo is not None


def test_make_snapshot_naive_ts_now_raises() -> None:
    """T06 (PLAN.md R9) made `MarketSnapshot.__post_init__` call
    `ensure_aware(self.timestamp)` — the naive-datetime tripwire.
    `make_snapshot(ts=<naive>)` still forwards `ts` straight into
    `MarketSnapshot` unchanged (the helper itself does not call
    `ensure_aware` — see `tests/helpers.py`'s `make_snapshot` docstring:
    the domain type is the single source of truth for this validation,
    not the test helper), so a naive `ts` now raises at construction
    instead of silently passing through. This test previously asserted
    the opposite (`test_make_snapshot_naive_ts_is_passed_through_
    unvalidated`) — that documented a defect in a domain type, not a
    helper contract worth preserving once the domain type was fixed. A
    test that deliberately wants a `MarketSnapshot` with a naive
    timestamp must construct one directly, not through `make_snapshot`.
    """
    naive_ts = datetime(2026, 1, 1, 12, 0, 0)  # deliberately no tzinfo
    assert naive_ts.tzinfo is None

    with pytest.raises(ValueError, match="naive datetime"):
        make_snapshot(ts=naive_ts)


# ---------------------------------------------------------------------------
# `client` fixture: its DB override must bind to the SAME database the
# `test_session` fixture uses — not a second, disconnected one.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_fixture_shares_db_with_session_fixture(
    test_session: AsyncSession,
    client: AsyncClient,
) -> None:
    """A row created through `test_session` must be visible to a real HTTP
    request served by `client`.

    No existing route reads the `markets` table (`GET /api/v1/markets` is
    still a `# TODO: Implement market listing` stub returning a constant),
    so this registers one throwaway probe route on the live `app` instance
    for the duration of this test only, then removes it — this modifies no
    file, only the in-memory FastAPI app object for one test.
    """
    from app.main import app

    test_session.add(
        Market(condition_id="client-binding-marker", question="binding check")
    )
    await test_session.flush()

    async def _probe_market_visible(session: AsyncSessionDep) -> dict:
        result = await session.execute(
            select(Market).where(Market.condition_id == "client-binding-marker")
        )
        return {"found": result.scalar_one_or_none() is not None}

    probe_path = "/__test_harness_contract_probe__"
    app.add_api_route(probe_path, _probe_market_visible, methods=["GET"])
    try:
        response = await client.get(probe_path)
    finally:
        app.router.routes = [
            route
            for route in app.router.routes
            if getattr(route, "path", None) != probe_path
        ]

    assert response.status_code == 200
    assert response.json() == {"found": True}, (
        "the client fixture's request did not see the row inserted through "
        "the test_session fixture — client and test_session are not sharing "
        "one database"
    )


# ---------------------------------------------------------------------------
# `Base.__init__`: PHASE 0 remediation of a T01 defect. An earlier revision
# eagerly applied each unset column's Python-side default in `__init__` (so
# `Market(condition_id="x", question="q").extra_data` read `{}` immediately
# instead of `None`). That was reverted — see the long docstring on `Base`
# in `app/models/base.py` — because it silently corrupted persisted JSON
# columns through `session.merge()`: three candidate mechanisms (plain
# `setattr`, writing straight into `self.__dict__` to bypass the
# instrumented descriptor, and `sqlalchemy.orm.attributes.
# set_committed_value`) were all measured to still leave the attribute's
# key present in the instance's `__dict__`, which is the ONLY thing
# `sqlalchemy.orm.properties.ColumnProperty.merge` consults
# (`self.key in source_dict`) when deciding whether to overwrite a
# persisted value — it has no notion of "explicitly set by the caller" vs.
# "eagerly defaulted by `__init__`". These tests pin down the resulting,
# deliberately un-eager behavior: (A) a fresh instance reads `None` (not
# `{}`/`[]`/a scalar default) for any unset column, and mutating an unset
# JSON column pre-flush raises `TypeError`; (B) `session.merge()` of a
# detached, partially-populated instance leaves a persisted row's
# JSON columns untouched when the merge source didn't set them.
# ---------------------------------------------------------------------------


def test_fresh_market_json_columns_read_none_before_flush() -> None:
    """A `Market` built without `outcomes`/`extra_data` reads `None` for
    both, pre-flush — not the `{}`/`[]` a flush would eventually produce.
    This is the documented, deliberate limitation left in place by FIX 1
    (see `Base`'s docstring): SQLAlchemy applies `mapped_column(default=
    ...)` at INSERT-compile time only, and no eager substitute is applied
    in `__init__` any more, because the only mechanisms that could do that
    also make `session.merge()` silently clobber persisted data.
    """
    market = Market(condition_id="fresh-json-cols", question="q")
    assert market.outcomes is None
    assert market.extra_data is None


def test_mutating_unflushed_json_column_raises_type_error() -> None:
    """Attempting to mutate an unset JSON column before the first flush
    raises a loud `TypeError`, rather than silently succeeding against a
    throwaway value or silently corrupting a persisted row later. Per the
    dispatch's explicit tie-break: a loud failure here is preferred over
    the silent data loss `session.merge()` would otherwise produce.
    """
    market = Market(condition_id="fresh-json-mutate", question="q")
    with pytest.raises(TypeError):
        market.extra_data["k"] = 1  # extra_data is None pre-flush


def test_fresh_market_and_order_scalar_enum_defaults_read_none_before_flush() -> None:
    """Scalar/enum column defaults are likewise not applied until flush —
    `Market.is_active` and `Order.status`/`Order.order_type` all declare
    Python-side `default=`, and all read `None` on a freshly constructed,
    un-flushed instance. This is the scalar/enum half of FIX 2's corrected
    docstring: the previous docstring's claim that construction mirrors a
    flush held only for the `dict`/`list` JSON columns it actually looped
    over, never for scalars or enums.
    """
    market = Market(condition_id="fresh-scalar-defaults", question="q")
    assert market.is_active is None

    order = Order(
        order_id="order-fresh-scalar-defaults",
        market_id=1,
        token_id="token-1",
        side=OrderSide.BUY,
        price=0.5,
        size=10.0,
        remaining_size=10.0,
    )
    assert order.status is None
    assert order.order_type is None


@pytest.mark.asyncio
async def test_session_merge_does_not_overwrite_persisted_json_columns(
    test_session: AsyncSession,
) -> None:
    """FIX 1's core regression test — the reviewer's reproduction.

    Persist a `Market` with real `outcomes`/`extra_data`, then
    `session.merge()` a *detached, partially-populated* `Market` that only
    sets `id`/`condition_id`/`question` (the shape of a plausible T15/T16
    upsert that means to update just one field). The persisted
    `outcomes`/`extra_data` must survive untouched — not get overwritten
    with the empty defaults a naive eager-default `__init__` would have
    attached to the merge source's `__dict__`.
    """
    market = Market(
        condition_id="merge-c9",
        question="q9",
        outcomes=["YES", "NO"],
        extra_data={"keep": 1},
    )
    test_session.add(market)
    await test_session.commit()
    row_id = market.id

    await test_session.merge(
        Market(id=row_id, condition_id="merge-c9", question="q9-updated")
    )
    await test_session.commit()

    # Force a real reload from the database — not just a read of whatever
    # is cached in this session's identity map — so this proves the
    # persisted row itself, not merely an in-memory object graph.
    test_session.expire_all()
    result = await test_session.execute(
        select(Market).where(Market.id == row_id)
    )
    persisted = result.scalar_one()

    assert persisted.question == "q9-updated"
    assert persisted.outcomes == ["YES", "NO"], (
        f"persisted outcomes were clobbered by merge(): {persisted.outcomes!r}"
    )
    assert persisted.extra_data == {"keep": 1}, (
        f"persisted extra_data were clobbered by merge(): {persisted.extra_data!r}"
    )
