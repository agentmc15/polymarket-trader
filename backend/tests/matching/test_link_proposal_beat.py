"""The scheduled link-proposal pass (T30, PLAN.md D9).

Before T30 `app.services.matching.propose_links` had exactly ONE caller —
the manual `POST /links/propose` endpoint — so on a fresh install
`event_links` started empty and stayed empty, and
`app.services.scanner._build_strategies` (which filters to
`status="approved"` before constructing `LinkBook`) handed
`cross_venue_arbitrage` nothing to scan, permanently. These tests cover
the beat that feeds it, and the three things that beat must never do:
duplicate a queued proposal, resurrect a link a human REJECTED, or
produce an approved link by any path at all.

Every expected confidence below is computed BY HAND in the test body
from PLAN.md D9's formula (GUARDRAILS.md §5)::

    confidence = 0.55 x title_jaccard
               + 0.20 x close_score
               + 0.15 x threshold_score
               + 0.10 x source_score

    close_score = max(0, 1 - |close_delta_h| / 48)
    threshold/source score = 1.0 agree / 0.5 unknown / 0.0 disagree

No network (GUARDRAILS.md §1.4): both venues are
`tests.venues.fixture_adapter.FixtureAdapter`, substituted for
`app.tasks.matching.read_adapters` BEFORE it could construct a real
adapter, and the database is the in-memory SQLite engine from
`tests/conftest.py`.
"""
import ast
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings
from app.config import settings as app_settings
from app.models.event_link import EventLink
from app.services.matching import PROPOSED, persist_proposals
from app.tasks import celery_app
from app.tasks import matching as matching_task
from tests.venues.fixture_adapter import FixtureAdapter, make_venue_market

#: A fixed close time so no test depends on the wall clock.
CLOSE = datetime(2026, 3, 18, 18, 0, tzinfo=UTC)

#: The one pair that must link. Identical wording on both venues, so
#: `title_jaccard` is exactly 1.0 and every expectation below is a
#: two-term sum rather than a Jaccard the test would have to recompute.
FED = "Will the Fed cut rates?"

#: `app/` — resolved from this file (backend/tests/matching/) so the
#: source fence works whatever the process's working directory is.
APP_ROOT = Path(__file__).resolve().parents[2] / "app"

#: Lifecycle columns nothing in `app/services/matching/` may ASSIGN on a
#: persisted row. Writing any of them is a review decision, and review
#: decisions happen in `app/api/routes/links.py`, driven by a person.
LIFECYCLE_COLUMNS = frozenset({"status", "reviewed_by", "reviewed_at"})


def _market(
    venue: str,
    market_id: str,
    question: str,
    *,
    close: datetime = CLOSE,
) -> Any:
    """Build one binary `VenueMarket` with no threshold and no source.

    No number in the title and no `resolution_source` on either side, so
    both tri-state components score `UNKNOWN_SCORE` (0.5) and the hand
    arithmetic in each test stays short.
    """
    return make_venue_market(
        venue,  # type: ignore[arg-type]
        market_id,
        question=question,
        close_time=close,
        resolution_source=None,
        outcomes=("YES", "NO"),
        rules_text="Resolves per the venue's stated source.",
    )


def _adapters(
    *, kalshi_close: datetime = CLOSE
) -> dict[str, FixtureAdapter]:
    """Two in-memory venues: one pair that links, one pair that cannot.

    `PM-RAIN`/`KX-SNOW` share no content token at all, so
    `candidate_pairs` never even scores them — the pass must file exactly
    one proposal, not two.
    """
    polymarket = FixtureAdapter("polymarket")
    polymarket.add_market(_market("polymarket", "PM-FED", FED))
    polymarket.add_market(
        _market("polymarket", "PM-RAIN", "Will it rain in Seattle?")
    )
    kalshi = FixtureAdapter("kalshi")
    kalshi.add_market(_market("kalshi", "KX-FED", FED, close=kalshi_close))
    kalshi.add_market(_market("kalshi", "KX-SNOW", "Will it snow in Denver?"))
    return {"polymarket": polymarket, "kalshi": kalshi}


@pytest.fixture
def sessions(test_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """A session factory over the test engine, for the beat to use."""
    return async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def wired(
    monkeypatch: pytest.MonkeyPatch,
    sessions: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[dict[str, FixtureAdapter], None]:
    """Substitute the beat's two module-level dependencies.

    `read_adapters()` is replaced BEFORE it can construct a real adapter,
    so no venue is ever contacted (GUARDRAILS.md §1.4), and
    `async_session_factory` points at the in-memory test engine.
    Everything between them is the shipped code path.
    """
    adapters = _adapters()
    monkeypatch.setattr(matching_task, "read_adapters", lambda: adapters)
    monkeypatch.setattr(matching_task, "async_session_factory", sessions)
    yield adapters


async def _links(session_factory: async_sessionmaker[AsyncSession]) -> list[EventLink]:
    """Every persisted link, oldest first."""
    async with session_factory() as session:
        return list(
            (await session.execute(select(EventLink).order_by(EventLink.id)))
            .scalars()
            .all()
        )


# ---------------------------------------------------------------------------
# 1. The beat exists, is scheduled, and is reachable from the worker.
# ---------------------------------------------------------------------------


def test_the_beat_schedules_link_proposal_on_its_own_interval() -> None:
    """A task that is defined but unscheduled is the same defect moved.

    Three separate things are asserted because three separate mistakes
    each produce a proposer that never runs: the beat entry can be
    missing, the module can be absent from `celery_app.conf.include` (the
    worker would then never import it, so the name would resolve
    nowhere), and the interval can be silently aliased onto an existing
    scan knob.
    """
    schedule = celery_app.conf.beat_schedule
    entry = next(
        item
        for item in schedule.values()
        if item["task"] == "app.tasks.matching.propose_event_links"
    )

    # Reachable, not merely defined: `import_default_modules()` imports
    # exactly `conf.include` — what a worker imports on start — and the
    # scheduled name must resolve in the registry afterwards.
    assert "app.tasks.matching" in celery_app.conf.include
    celery_app.loader.import_default_modules()
    assert "app.tasks.matching.propose_event_links" in celery_app.tasks

    # Its own knob, not a reuse of either scan interval.
    assert entry["schedule"] == app_settings.link_proposal_interval_s
    assert (
        Settings.model_fields["link_proposal_interval_s"].alias
        == "LINK_PROPOSAL_INTERVAL_S"
    )
    assert Settings().link_proposal_interval_s == 3600.0
    assert app_settings.link_proposal_interval_s != app_settings.scan_interval_s
    assert (
        app_settings.link_proposal_interval_s
        != app_settings.near_resolution_scan_interval_s
    )


# ---------------------------------------------------------------------------
# 2. The pass itself.
# ---------------------------------------------------------------------------


async def test_the_beat_files_a_proposal_for_the_pair_that_matches(
    wired: dict[str, FixtureAdapter],
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """`run_link_proposal()` — the exact coroutine the beat runs.

    Identical titles -> title_jaccard 1.0; identical close times ->
    close_delta_h 0.0 -> close_score 1.0; no numbers in either title and
    no resolution source on either venue -> both tri-state components
    score 0.5:

        0.55 x 1.0 + 0.20 x 1.0 + 0.15 x 0.5 + 0.10 x 0.5
        = 0.55 + 0.20 + 0.075 + 0.05
        = 0.875

    Seattle-rain vs Denver-snow share no content token, so they are never
    scored: exactly ONE row, not two.
    """
    summary = await matching_task.run_link_proposal()

    assert summary["mode"] == "paper"
    assert summary["scanned_venues"] == ["kalshi", "polymarket"]
    assert summary["venue_pairs"] == ["kalshi+polymarket"]
    assert summary["markets_read"] == {"kalshi": 2, "polymarket": 2}
    assert summary["proposals"] == 1
    assert summary["created"] == 1
    assert summary["updated"] == 0
    assert summary["skipped_reviewed"] == 0
    assert summary["conflict"] is False

    rows = await _links(sessions)
    assert len(rows) == 1
    row = rows[0]
    assert (row.venue_a, row.market_a) == ("kalshi", "KX-FED")
    assert (row.venue_b, row.market_b) == ("polymarket", "PM-FED")
    assert row.confidence == pytest.approx(0.875)
    assert row.evidence["title_jaccard"] == pytest.approx(1.0)
    assert row.evidence["close_delta_h"] == pytest.approx(0.0)
    assert row.outcome_map == {"YES": "YES", "NO": "NO"}
    assert row.status == PROPOSED
    assert row.reviewed_by is None
    assert row.reviewed_at is None


async def test_a_second_pass_rescores_in_place_instead_of_duplicating(
    monkeypatch: pytest.MonkeyPatch,
    wired: dict[str, FixtureAdapter],
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The beat re-runs forever; it must not accumulate rows.

    Second pass with Kalshi's close time moved 24h later:

        close_score = 1 - 24 / 48 = 0.5
        0.55 x 1.0 + 0.20 x 0.5 + 0.15 x 0.5 + 0.10 x 0.5
        = 0.55 + 0.10 + 0.075 + 0.05
        = 0.775

    The undecided proposal is REFRESHED to that score — a queue showing
    the score from whenever a pair was first seen is worse than no score
    — and the row count stays at one.
    """
    first = await matching_task.run_link_proposal()
    assert first["created"] == 1

    moved = _adapters(kalshi_close=CLOSE + timedelta(hours=24))
    monkeypatch.setattr(matching_task, "read_adapters", lambda: moved)

    second = await matching_task.run_link_proposal()
    assert second["created"] == 0
    assert second["updated"] == 1
    assert second["skipped_reviewed"] == 0

    rows = await _links(sessions)
    assert len(rows) == 1
    assert rows[0].confidence == pytest.approx(0.775)
    assert rows[0].evidence["close_delta_h"] == pytest.approx(24.0)
    assert rows[0].status == PROPOSED


# ---------------------------------------------------------------------------
# 3. The human's decision survives every subsequent pass. (The point.)
# ---------------------------------------------------------------------------


async def test_the_beat_never_resurrects_a_link_a_human_rejected(
    monkeypatch: pytest.MonkeyPatch,
    wired: dict[str, FixtureAdapter],
    sessions: async_sessionmaker[AsyncSession],
    client: AsyncClient,
) -> None:
    """A rejection made through the real endpoint outlives the proposer.

    The matcher will re-propose a rejected pair on EVERY pass forever:
    its score has not changed, and nothing in the score can see the
    reason the pair was rejected ("Kalshi settles on the AP call,
    Polymarket on state certification"). A beat that re-filed it — or
    reset it to `"proposed"` — would make rejection meaningless and train
    the operator to ignore the queue, which is the one control PLAN.md R1
    depends on.
    """
    await matching_task.run_link_proposal()
    link_id = (await _links(sessions))[0].id

    rejected = await client.post(
        f"/api/v1/links/{link_id}/reject",
        json={
            "reviewed_by": "reviewer@example.com",
            "notes": "different resolution source; not the same fact",
        },
    )
    assert rejected.status_code == 200, rejected.text

    # Move the close time so the pair scores DIFFERENTLY on the next
    # pass: a proposer that rescored decided rows would show it here.
    moved = _adapters(kalshi_close=CLOSE + timedelta(hours=24))
    monkeypatch.setattr(matching_task, "read_adapters", lambda: moved)

    summary = await matching_task.run_link_proposal()
    assert summary["proposals"] == 1, "the pair must still be proposed by the matcher"
    assert summary["created"] == 0
    assert summary["updated"] == 0
    assert summary["skipped_reviewed"] == 1

    rows = await _links(sessions)
    assert len(rows) == 1, "a rejected pair must not get a second row"
    row = rows[0]
    assert row.id == link_id
    assert row.status == "rejected"
    assert row.reviewed_by == "reviewer@example.com"
    assert row.notes == "different resolution source; not the same fact"
    assert row.reviewed_at is not None
    # Untouched, not merely un-promoted: the score the reviewer decided
    # against is still the score on the row.
    assert row.confidence == pytest.approx(0.875)


async def test_the_beat_leaves_an_approved_link_exactly_as_the_reviewer_left_it(
    monkeypatch: pytest.MonkeyPatch,
    wired: dict[str, FixtureAdapter],
    sessions: async_sessionmaker[AsyncSession],
    client: AsyncClient,
) -> None:
    """The other decided case: an approval is not re-derived either.

    Rescoring an approved link would silently move the ground under a
    decision a person made by reading two rules texts, and it is the row
    `cross_venue_arbitrage` trades on.
    """
    await matching_task.run_link_proposal()
    link_id = (await _links(sessions))[0].id

    approved = await client.post(
        f"/api/v1/links/{link_id}/approve",
        json={"reviewed_by": "alice", "notes": "same 5pm print"},
    )
    assert approved.status_code == 200, approved.text

    moved = _adapters(kalshi_close=CLOSE + timedelta(hours=24))
    monkeypatch.setattr(matching_task, "read_adapters", lambda: moved)

    summary = await matching_task.run_link_proposal()
    assert summary["created"] == 0
    assert summary["updated"] == 0
    assert summary["skipped_reviewed"] == 1

    rows = await _links(sessions)
    assert len(rows) == 1
    assert rows[0].status == "approved"
    assert rows[0].reviewed_by == "alice"
    assert rows[0].confidence == pytest.approx(0.875)


# ---------------------------------------------------------------------------
# 4. No path in this code can produce an approved link.
# ---------------------------------------------------------------------------


async def test_no_row_any_pass_writes_is_ever_cleared_to_trade(
    monkeypatch: pytest.MonkeyPatch,
    wired: dict[str, FixtureAdapter],
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Confidence is a similarity score, not a guarantee of identity.

    A high-confidence pair is the most tempting thing to auto-approve and
    the most expensive one to get wrong: a false link is a position that
    believes it is hedged while both legs can lose at settlement. The
    0.875 pair here is far above any plausible auto-approval threshold
    and still comes out `"proposed"`, unreviewed, on every pass.
    """
    for _ in range(3):
        await matching_task.run_link_proposal()

    rows = await _links(sessions)
    assert rows, "the corpus must produce a link for this test to mean anything"
    assert all(row.confidence >= 0.85 for row in rows)
    assert {row.status for row in rows} == {PROPOSED}
    assert all(row.reviewed_by is None for row in rows)
    assert all(row.reviewed_at is None for row in rows)

    async with sessions() as session:
        cleared = await session.scalar(
            select(func.count()).select_from(EventLink).where(
                EventLink.status == "approved"
            )
        )
    assert cleared == 0


async def test_persist_proposals_refuses_a_row_that_is_not_a_proposal(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The runtime half of the guarantee, for callers yet to be written.

    `propose_links` cannot express a lifecycle value other than
    `"proposed"`, but the persistence helper is a public function and a
    later caller could hand it a hand-built row. It refuses rather than
    trusts, and refuses before writing anything.
    """
    forged = EventLink(
        venue_a="kalshi",
        market_a="KX-FED",
        venue_b="polymarket",
        market_b="PM-FED",
        outcome_map={"YES": "YES", "NO": "NO"},
        confidence=0.99,
        evidence={},
        status="approved",
        reviewed_by="not-a-human",
    )

    async with sessions() as session:
        with pytest.raises(ValueError, match="only files proposals"):
            await persist_proposals(session, [forged])
        await session.rollback()

    assert await _links(sessions) == []


def _writes_a_link(call: ast.Call) -> bool:
    """Whether a call could write an `event_links` row.

    The `EventLink(...)` constructor and SQLAlchemy's `update().values()`
    — narrow on purpose, so an unrelated `list_markets(status="open")`
    (a MARKET's status, not a link's lifecycle) is not mistaken for a
    review decision.
    """
    func = call.func
    name = (
        func.id
        if isinstance(func, ast.Name)
        else func.attr
        if isinstance(func, ast.Attribute)
        else ""
    )
    return name in {"EventLink", "values"}


def _is_the_proposed_value(kw: ast.keyword) -> bool:
    """Whether a lifecycle keyword carries the one permitted value."""
    if kw.arg != "status":
        return False
    return (isinstance(kw.value, ast.Name) and kw.value.id == "PROPOSED") or (
        isinstance(kw.value, ast.Constant) and kw.value.value == PROPOSED
    )


def _attribute_targets(target: ast.expr) -> list[ast.Attribute]:
    """Every `ast.Attribute` an assignment target ultimately binds.

    A plain `existing.status = ...` target IS the attribute. A tuple/list
    target — `existing.status, existing.reviewed_by = ...` — binds several
    at once, and a walk that only inspected `node.targets` at the top
    level (the pre-T39 rule) never looked inside the tuple, so a
    multi-assign was a free bypass (T39 F3, form 2).
    """
    if isinstance(target, ast.Attribute):
        return [target]
    if isinstance(target, ast.Tuple | ast.List):
        return [attr for elt in target.elts for attr in _attribute_targets(elt)]
    return []


def _dict_literal_lifecycle_keys(node: ast.expr) -> list[str]:
    """Lifecycle column names as literal keys in a `{...}` or `dict(...)`.

    `dict(status=_CLEARED, ...)` (T42 F1, forms 8b/8c) is the same
    mapping as `{"status": _CLEARED, ...}` spelled with call syntax
    instead of dict-literal syntax — its keys live on
    `ast.Call.keywords`, not `ast.Dict.keys`, so it needs its own branch
    rather than falling out of the check below. `dict(**other)` is
    unaffected: a `dict(...)` keyword with `arg is None` is a
    `**`-unpack, not a literal key, and is skipped exactly as `{**other}`
    already is.

    Only literal string keys are visible here — `{lifecycle_var: value}`
    (the key itself computed) is exactly the undecidable case this fence
    admits it cannot see; see `test_the_matching_package_cannot_write_a_
    lifecycle_value`'s docstring.
    """
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "dict"
    ):
        return [
            kw.arg
            for kw in node.keywords
            if kw.arg is not None and kw.arg in LIFECYCLE_COLUMNS
        ]
    if not isinstance(node, ast.Dict):
        return []
    return [
        key.value
        for key in node.keys
        if isinstance(key, ast.Constant) and key.value in LIFECYCLE_COLUMNS
    ]


def _is_dunder_dict_update(func: ast.expr) -> bool:
    """Whether `func` is `<anything>.__dict__.update` (T39 F3, form 4)."""
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "update"
        and isinstance(func.value, ast.Attribute)
        and func.value.attr == "__dict__"
    )


def _docstring_constant_ids(tree: ast.Module) -> set[int]:
    """`id()` of every module/class/function docstring's `Constant` node.

    So the literal-`"approved"` rule does not fire on the word appearing
    in prose (this very docstring says it), only on the value appearing
    as data.
    """
    return {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(
            node,
            ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
        )
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }


def _lifecycle_findings(tree: ast.Module, filename: str) -> list[str]:
    """Every lifecycle-write spelling this fence knows how to see.

    T39 F3: a red-team pass appended seven different syntactic spellings
    of a lifecycle write to a COPY of `persist.py`, and the pre-T39
    version of this fence (`ast.Assign` to a bare `ast.Attribute` target;
    an `EventLink(...)`/`.values(...)` keyword with a non-`None` `arg`;
    the bare constant `"approved"`) found none of them, because every one
    of the seven sourced its value from `get_args(EventLinkStatus)[1]`
    rather than the literal string, and used a syntactic shape rule 2/3
    never looked at.

    T42 F1: a second red-team pass found an EIGHTH shape the T39 fence
    still missed, and the most ordinary one of the lot —
    `sqlalchemy.Update.values()` takes its target mapping as its FIRST
    POSITIONAL argument, which is how `.values({...})` is commonly
    written, and the T39 fence's `_writes_a_link` branch only ever
    inspected `node.keywords`, never `node.args`. Nine of the ten forms
    are closed here:

    1. `setattr(existing, "status", _CLEARED)`
    2. `existing.status, existing.reviewed_by = _CLEARED, who` (tuple target)
    3. `existing.status: str = _CLEARED` (`ast.AnnAssign`, not `ast.Assign`)
    4. `existing.__dict__.update({"status": _CLEARED, ...})`
    6. `session.execute(update(EventLink), [{"id": i, "status": _CLEARED}])`
       (an executemany params list, no `.values()` call to match on)
    7. `EventLink(**{"status": _CLEARED, ...})` (`keyword(arg=None)`)
    8a. `update(EventLink).where(...).values({"status": _CLEARED, ...})`
        (`.values()`'s mapping passed POSITIONALLY — closed by walking
        `node.args` on a `_writes_a_link` call, not just `node.keywords`)
    8b. `update(EventLink).where(...).values(**dict(status=_CLEARED, ...))`
        (form 7's `**`-unpack, with `dict(...)` in place of `{...}`)
    8c. `EventLink(**dict(status=_CLEARED, ...))` (same substitution as 8b)

    THE ONE THIS FENCE CANNOT SEE, ON PURPOSE, ADMITTED RATHER THAN
    HIDDEN: form 5, `for column, value in payload.items(): setattr(
    existing, column, value)`. The written-to name is a runtime value —
    it might be `"status"` on some iteration and never on another — and
    no static walk over the source can decide that without running it.
    An AST fence is a syntactic check; a fully dynamic write like
    `setattr(obj, name_from_config, value_from_db)` is undecidable by
    construction, not merely unhandled.

    A SECOND, NARROWER GAP STAYS OPEN TOO, DISTINCT FROM FORM 5: this
    fence reads a mapping argument's shape only AT THE WRITE CALL ITSELF
    (`{...}` or `dict(...)`, literal or `**`-unpacked). A mapping BUILT
    ON AN EARLIER LINE and then passed by name — `payload = {"status":
    _CLEARED}` followed by `.values(**payload)` — is not traced back to
    that assignment, so the literal `"status"` key is invisible here even
    though it genuinely is source-level text, not a runtime value. Unlike
    form 5 this one is not undecidable in principle — a dataflow-aware
    fence could resolve a same-module local binding — it is simply not
    attempted here, because doing so risks the opposite failure (treating
    an unrelated variable's `"status"` key, on some dict that has nothing
    to do with an `EventLink`, as if it did) for a rewrite this package
    has not needed yet.

    So the claim actually true of this fence is narrower than "every
    spelling that names its target lifecycle column as source-level
    text": it is every SUCH SPELLING VISIBLE AT THE WRITE CALL ITSELF.
    The runtime guards (`persist_proposals`'s `ValueError` refusal of any
    non-`PROPOSED` row, and `LinkBook` reading only reviewed links) are
    what still stand between either open case and a trade, and they are
    unaffected by anything here.

    Args:
        tree: A parsed module.
        filename: Name to prefix each finding with (`path.name`, so a
            finding reads the same whether `path` is a real source file
            or a `tmp_path` copy carrying appended bypass forms).

    Returns:
        list[str]: One entry per violation found, empty if none.
    """
    docstrings = _docstring_constant_ids(tree)
    findings: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and node.value == "approved"
            and id(node) not in docstrings
        ):
            findings.append(f"{filename}:{node.lineno}: literal 'approved'")

        if isinstance(node, ast.Assign):
            for target in node.targets:
                for attr in _attribute_targets(target):
                    if attr.attr in LIFECYCLE_COLUMNS:
                        findings.append(
                            f"{filename}:{node.lineno}: assigns .{attr.attr}"
                        )

        if isinstance(node, ast.AnnAssign):
            for attr in _attribute_targets(node.target):
                if attr.attr in LIFECYCLE_COLUMNS:
                    findings.append(
                        f"{filename}:{node.lineno}: annotated-assigns .{attr.attr}"
                    )

        if isinstance(node, ast.Call):
            func = node.func

            if _writes_a_link(node):
                for kw in node.keywords:
                    if kw.arg in LIFECYCLE_COLUMNS and not _is_the_proposed_value(kw):
                        findings.append(
                            f"{filename}:{node.lineno}: writes {kw.arg}="
                            f"{ast.dump(kw.value)[:40]}"
                        )
                    elif kw.arg is None:
                        for key in _dict_literal_lifecycle_keys(kw.value):
                            findings.append(
                                f"{filename}:{node.lineno}: **dict unpack "
                                f"writes {key}="
                            )
                # T42 F1, form 8a: `Update.values()` also takes its
                # mapping as a POSITIONAL argument (`.values({...})`),
                # and that is how it is ordinarily called — a keyword-
                # only check missed the common case, not an edge one.
                for arg in node.args:
                    for key in _dict_literal_lifecycle_keys(arg):
                        findings.append(
                            f"{filename}:{node.lineno}: positional dict "
                            f"arg writes {key}="
                        )

            if isinstance(func, ast.Name) and func.id == "setattr" and len(
                node.args
            ) >= 2:
                name_arg = node.args[1]
                if (
                    isinstance(name_arg, ast.Constant)
                    and name_arg.value in LIFECYCLE_COLUMNS
                ):
                    findings.append(
                        f"{filename}:{node.lineno}: setattr(..., "
                        f"{name_arg.value!r}, ...)"
                    )

            if _is_dunder_dict_update(func):
                for arg in node.args:
                    for key in _dict_literal_lifecycle_keys(arg):
                        findings.append(
                            f"{filename}:{node.lineno}: __dict__.update writes "
                            f"{key}="
                        )

            if isinstance(func, ast.Attribute) and func.attr == "execute":
                for arg in node.args:
                    if isinstance(arg, ast.List | ast.Tuple):
                        for elt in arg.elts:
                            for key in _dict_literal_lifecycle_keys(elt):
                                findings.append(
                                    f"{filename}:{node.lineno}: bulk execute(...) "
                                    f"params write {key}="
                                )

    return findings


def test_the_matching_package_cannot_write_a_lifecycle_value() -> None:
    """A source fence, so this cannot rot back in silently.

    See `_lifecycle_findings` for exactly which spellings of a lifecycle
    write this catches (nine, closed by T39 F3 and T42 F1) and the two it
    admits it cannot (form 5's fully dynamic `setattr`, and a mapping
    built on an earlier line and passed by name) — stated there rather
    than implied here, because a fence that does not say what it cannot
    see reads as complete when it is not.
    """
    paths = sorted((APP_ROOT / "services" / "matching").glob("*.py"))
    paths.append(APP_ROOT / "tasks" / "matching.py")
    assert len(paths) >= 4, paths

    findings: list[str] = []
    for path in paths:
        findings.extend(_lifecycle_findings(ast.parse(path.read_text()), path.name))

    assert findings == [], findings


def _pre_t39_findings(tree: ast.Module, filename: str) -> list[str]:
    """The rule set this fence ran BEFORE T39, reproduced verbatim.

    Kept only so `test_the_extended_fence_catches_the_bypass_spellings_
    the_old_one_missed` can show it found nothing against the seven
    bypass forms — the red half of that test's red/green proof. Not
    derived from `_lifecycle_findings`; if it were, "the old rules" could
    accidentally regain the new coverage the day someone refactors.
    """
    docstrings = _docstring_constant_ids(tree)
    findings: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and node.value == "approved"
            and id(node) not in docstrings
        ):
            findings.append(f"{filename}:{node.lineno}: literal 'approved'")
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr in LIFECYCLE_COLUMNS
                ):
                    findings.append(
                        f"{filename}:{node.lineno}: assigns .{target.attr}"
                    )
        if isinstance(node, ast.Call) and _writes_a_link(node):
            for kw in node.keywords:
                if kw.arg in LIFECYCLE_COLUMNS and not _is_the_proposed_value(kw):
                    findings.append(
                        f"{filename}:{node.lineno}: writes {kw.arg}="
                        f"{ast.dump(kw.value)[:40]}"
                    )
    return findings


def test_the_extended_fence_catches_the_bypass_spellings_the_old_one_missed(
    tmp_path: Path,
) -> None:
    """T39 F3's own red/green proof, against a COPY, never the tracked file.

    The seven forms from the red-team pass are appended to a copy of the
    real `persist.py` under `tmp_path`. RED: `_pre_t39_findings` (the
    rule set before this task) finds NOTHING in it — the exact false
    confidence the finding described. GREEN: `_lifecycle_findings` (what
    `test_the_matching_package_cannot_write_a_lifecycle_value` now runs)
    flags all six closable forms by line, and — proving the admission in
    its docstring is a fact and not just a claim — flags NEITHER line of
    form 5, the one spelling this fence cannot and does not pretend to
    catch.
    """
    source = (APP_ROOT / "services" / "matching" / "persist.py").read_text()
    bypass = '''

async def _t39_f3_bypass_forms(existing, who, payload, session, i):
    """Seven spellings of a lifecycle write.

    None sources its value from the literal string "approved" -- exactly
    as in the real red-team probe, every one instead reads
    `get_args(EventLinkStatus)[1]`, so a rule that only looked for the
    bare literal would find nothing here either way.
    """
    _CLEARED = get_args(EventLinkStatus)[1]
    setattr(existing, "status", _CLEARED)                                    # form 1
    existing.status, existing.reviewed_by = _CLEARED, who                    # form 2
    existing.status: str = _CLEARED                                         # form 3
    existing.__dict__.update({"status": _CLEARED, "reviewed_by": who})       # form 4
    for column, value in payload.items():                                   # form 5
        setattr(existing, column, value)                                    # form 5 (undecidable)
    await session.execute(update(EventLink), [{"id": i, "status": _CLEARED}])  # form 6
    EventLink(**{"status": _CLEARED, "reviewed_by": who})                   # form 7
'''
    copy_path = tmp_path / "persist.py"
    copy_path.write_text(source + bypass)
    tree = ast.parse(copy_path.read_text())

    # RED: the pre-T39 rules see none of the seven forms.
    assert _pre_t39_findings(tree, copy_path.name) == []

    # GREEN: the extended rules see six of the seven, by line.
    findings = _lifecycle_findings(tree, copy_path.name)
    appended_lines = {
        i
        for i, line in enumerate(
            copy_path.read_text().splitlines(), start=1
        )
        if "form 1" in line
        or "form 2" in line
        or "form 3" in line
        or "form 4" in line
        or "form 6" in line
        or "form 7" in line
    }
    flagged_lines = {
        int(finding.split(":")[1]) for finding in findings
    }
    assert appended_lines, "the six closable forms must be present in the fixture"
    assert appended_lines <= flagged_lines, (appended_lines - flagged_lines, findings)

    form_5_lines = {
        i
        for i, line in enumerate(
            copy_path.read_text().splitlines(), start=1
        )
        if "form 5" in line
    }
    assert len(form_5_lines) == 2
    assert not (form_5_lines & flagged_lines), (
        "form 5 is the admitted, undecidable case -- it must NOT be flagged "
        "by a rule that got lucky, or the docstring's honesty claim is false"
    )


def _pre_t42_dict_literal_lifecycle_keys(node: ast.expr) -> list[str]:
    """`_dict_literal_lifecycle_keys` exactly as T39 F3 left it.

    Bare `ast.Dict` only, no `dict(...)`-call branch. Frozen, and NOT
    derived from `_dict_literal_lifecycle_keys` itself, for the same
    reason `_pre_t39_findings` is not derived from `_lifecycle_findings`
    (see its docstring): if it were, this "old rule" would silently
    regain T42 F1's coverage the moment someone touches the shared
    helper, and the red half of
    `test_the_extended_fence_catches_the_form_8_spellings_the_t39_fence_
    missed` would stop proving anything.
    """
    if not isinstance(node, ast.Dict):
        return []
    return [
        key.value
        for key in node.keys
        if isinstance(key, ast.Constant) and key.value in LIFECYCLE_COLUMNS
    ]


def _pre_t42_findings(tree: ast.Module, filename: str) -> list[str]:
    """The rule set this fence ran BEFORE T42 F1, reproduced verbatim.

    This is T39 F3's finished state — all seven of that task's forms are
    still closed here — reproduced only so
    `test_the_extended_fence_catches_the_form_8_spellings_the_t39_fence_
    missed` can show it finds NOTHING against the three form-8 bypass
    spellings, the red half of that test's red/green proof. The only
    difference from the current `_lifecycle_findings` is what T42 F1
    added: no `node.args` loop under `_writes_a_link`, and
    `_pre_t42_dict_literal_lifecycle_keys` in place of
    `_dict_literal_lifecycle_keys` (frozen for the same reason — see that
    function's docstring). Every other helper (`_writes_a_link`,
    `_is_the_proposed_value`, `_attribute_targets`,
    `_docstring_constant_ids`, `_is_dunder_dict_update`) is untouched by
    T42 F1, so it is safe to share those directly.
    """
    docstrings = _docstring_constant_ids(tree)
    findings: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and node.value == "approved"
            and id(node) not in docstrings
        ):
            findings.append(f"{filename}:{node.lineno}: literal 'approved'")

        if isinstance(node, ast.Assign):
            for target in node.targets:
                for attr in _attribute_targets(target):
                    if attr.attr in LIFECYCLE_COLUMNS:
                        findings.append(
                            f"{filename}:{node.lineno}: assigns .{attr.attr}"
                        )

        if isinstance(node, ast.AnnAssign):
            for attr in _attribute_targets(node.target):
                if attr.attr in LIFECYCLE_COLUMNS:
                    findings.append(
                        f"{filename}:{node.lineno}: annotated-assigns .{attr.attr}"
                    )

        if isinstance(node, ast.Call):
            func = node.func

            if _writes_a_link(node):
                for kw in node.keywords:
                    if kw.arg in LIFECYCLE_COLUMNS and not _is_the_proposed_value(kw):
                        findings.append(
                            f"{filename}:{node.lineno}: writes {kw.arg}="
                            f"{ast.dump(kw.value)[:40]}"
                        )
                    elif kw.arg is None:
                        for key in _pre_t42_dict_literal_lifecycle_keys(kw.value):
                            findings.append(
                                f"{filename}:{node.lineno}: **dict unpack "
                                f"writes {key}="
                            )

            if isinstance(func, ast.Name) and func.id == "setattr" and len(
                node.args
            ) >= 2:
                name_arg = node.args[1]
                if (
                    isinstance(name_arg, ast.Constant)
                    and name_arg.value in LIFECYCLE_COLUMNS
                ):
                    findings.append(
                        f"{filename}:{node.lineno}: setattr(..., "
                        f"{name_arg.value!r}, ...)"
                    )

            if _is_dunder_dict_update(func):
                for arg in node.args:
                    for key in _pre_t42_dict_literal_lifecycle_keys(arg):
                        findings.append(
                            f"{filename}:{node.lineno}: __dict__.update writes "
                            f"{key}="
                        )

            if isinstance(func, ast.Attribute) and func.attr == "execute":
                for arg in node.args:
                    if isinstance(arg, ast.List | ast.Tuple):
                        for elt in arg.elts:
                            for key in _pre_t42_dict_literal_lifecycle_keys(elt):
                                findings.append(
                                    f"{filename}:{node.lineno}: bulk execute(...) "
                                    f"params write {key}="
                                )

    return findings


def test_the_extended_fence_catches_the_form_8_spellings_the_t39_fence_missed(
    tmp_path: Path,
) -> None:
    """T42 F1's own red/green proof, against a COPY, never the tracked file.

    A second red-team pass found three more spellings T39 F3 missed —
    all real SQLAlchemy or ordinary Python, none of them exotic:

        8a. `.values({"status": _CLEARED, "reviewed_by": who})` —
            `Update.values()`'s target mapping passed POSITIONALLY, the
            ordinary way to call it, not an edge case.
        8b. `.values(**dict(status=_CLEARED, reviewed_by=who))` — form
            7's `**`-unpack with `dict(...)` in place of `{...}`.
        8c. `EventLink(**dict(status=_CLEARED, reviewed_by=who))` — same
            substitution as 8b, on the constructor instead of `.values()`.

    RED: `_pre_t42_findings` (`_lifecycle_findings` exactly as T39 F3
    left it) finds nothing — the same false confidence T39's own gap
    described, one shape later. GREEN: the current `_lifecycle_findings`
    flags all three by line, and — proving the docstring's narrowed claim
    is a fact and not just a restatement — form 5 is still not resurrected
    by the wider rule.
    """
    source = (APP_ROOT / "services" / "matching" / "persist.py").read_text()
    bypass = '''

async def _t42_f1_bypass_forms(who, i):
    """Three more spellings of a lifecycle write, all ordinary Python.

    None sources its value from the literal string "approved" -- as with
    the T39 forms, every one instead reads `get_args(EventLinkStatus)[1]`.
    """
    _CLEARED = get_args(EventLinkStatus)[1]
    update(EventLink).where(EventLink.id == i).values({"status": _CLEARED, "reviewed_by": who})  # form 8a
    update(EventLink).where(EventLink.id == i).values(**dict(status=_CLEARED, reviewed_by=who))  # form 8b
    EventLink(**dict(status=_CLEARED, reviewed_by=who))  # form 8c
'''
    copy_path = tmp_path / "persist.py"
    copy_path.write_text(source + bypass)
    tree = ast.parse(copy_path.read_text())

    # RED: the T39 fence (node.keywords only, `{...}` only) sees none.
    assert _pre_t42_findings(tree, copy_path.name) == []

    # GREEN: the extended fence sees all three, by line.
    findings = _lifecycle_findings(tree, copy_path.name)
    appended_lines = {
        i
        for i, line in enumerate(copy_path.read_text().splitlines(), start=1)
        if "form 8a" in line or "form 8b" in line or "form 8c" in line
    }
    flagged_lines = {int(finding.split(":")[1]) for finding in findings}
    assert len(appended_lines) == 3, appended_lines
    assert appended_lines <= flagged_lines, (appended_lines - flagged_lines, findings)


# ---------------------------------------------------------------------------
# 5. Failure modes a beat meets and a manual call never does.
# ---------------------------------------------------------------------------


async def test_a_venue_that_cannot_be_listed_does_not_stop_the_pass(
    monkeypatch: pytest.MonkeyPatch,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """One venue's outage leaves no pair to match, not an exploded beat.

    With a single readable venue there is genuinely nothing to propose —
    a cross-venue link needs two — so the correct behaviour is a quiet
    pass that files nothing and runs again next interval.
    """

    class Unreadable(FixtureAdapter):
        async def list_markets(  # type: ignore[no-untyped-def]
            self,
            status=None,  # noqa: ARG002 - part of the Protocol
            updated_since=None,  # noqa: ARG002 - part of the Protocol
        ):
            raise RuntimeError("venue returned 503")

    adapters = _adapters()
    adapters["kalshi"] = Unreadable("kalshi")
    monkeypatch.setattr(matching_task, "read_adapters", lambda: adapters)
    monkeypatch.setattr(matching_task, "async_session_factory", sessions)

    summary = await matching_task.run_link_proposal()

    assert summary["scanned_venues"] == ["kalshi", "polymarket"]
    assert summary["markets_read"] == {"polymarket": 2}
    assert summary["venue_pairs"] == []
    assert summary["proposals"] == 0
    assert summary["created"] == 0
    assert await _links(sessions) == []


async def test_a_commit_conflict_rolls_back_and_reports_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
    wired: dict[str, FixtureAdapter],
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A human on `POST /links/propose` can race the beat's commit.

    Both callers write the same UNIQUE `(venue_a, market_a, venue_b,
    market_b)` tuple, so the loser's commit raises. Nothing is lost by
    deferring — the matcher is deterministic, so the next interval refiles
    exactly the same proposals — but a pass that died with a traceback
    would leave the queue looking broken instead of merely late.
    """

    async def _conflict(session: AsyncSession, proposals: Any) -> Any:
        raise IntegrityError("INSERT INTO event_links", {}, Exception("unique"))

    monkeypatch.setattr(matching_task, "persist_proposals", _conflict)

    summary = await matching_task.run_link_proposal()

    assert summary["conflict"] is True
    assert summary["proposals"] == 1
    assert summary["created"] == 0
    assert await _links(sessions) == []
