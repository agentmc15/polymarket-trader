"""Concurrent writers on `event_links` (T37, PLAN.md D9).

`tests/matching/test_link_proposal_beat.py` already proves the proposer
leaves a decided row alone. It proves it in the SEQUENTIAL ordering: the
human's approval commits, and only then does the next pass start. That
ordering is the easy half. This module covers the one the beat actually
meets — a reviewer approving a link WHILE a pass is running, which is
routine, because a real pass carries ~221 proposals and a person reading
two rules texts takes minutes.

WHY IT IS MONEY, NOT BOOKKEEPING. `app.strategies.cross_venue_arbitrage`
reads `p_same = float(link.confidence)` and prices every cross-venue
signal as::

    net_edge = gross_edge x p_same - (1 - p_same) x worst_case_loss

so `confidence` is a haircut on expected profit, not a ranking key. A
pair a reviewer accepted at 0.60 and a pass silently rescored to 0.97 is
priced with 37 points less identity risk than the human agreed to. With
`gross_edge = 0.03` and `worst_case_loss = 0.55` (the dearer of the two
legs), the difference is the difference between a trade and no trade::

    at 0.60:  0.03 x 0.60 - 0.40 x 0.55 = 0.018  - 0.220  = -0.202
    at 0.97:  0.03 x 0.97 - 0.03 x 0.55 = 0.0291 - 0.0165 = +0.0126

And `outcome_map` picks the B leg (`link.outcome_map.get(outcome_a)`).
On a multi-outcome pair the matcher stores `{}` and REFUSES to guess, so
the map is hand-built by the reviewer at approval time; a pass that
replaced it with a derived binary map would build two legs on the same
side of two different events while the position believed it was hedged.

HOW THE RACE IS MADE DETERMINISTIC. No threads, no sleeps, no
`asyncio.gather` — the interleaving is placed exactly, by
`_approving_between_read_and_write`, which wraps
`app.services.matching.persist._find_existing` in a delegating shim. The
shim returns the real function's result unchanged; all it adds is WHEN
the reviewer's `POST /links/{id}/approve` happens: after the pass has
read the row and judged it undecided, before the pass writes it. That is
the whole window the defect lives in, and pinning it is what makes the
test reproduce instead of flake.

The database here is a FILE-backed SQLite in pytest's `tmp_path`, not
`tests/conftest.py`'s shared in-memory engine, for one reason: that
engine is a `StaticPool` holding a single connection, so two sessions
over it are one transaction and a "concurrent" reviewer would be writing
inside the pass's own transaction. A file gives the reviewer a genuinely
separate connection and a genuinely separate commit — which is what
production has, and what the guard must survive. No network anywhere
(GUARDRAILS.md §1.4); the only adapters are
`tests.venues.fixture_adapter.FixtureAdapter`.
"""
from collections.abc import AsyncGenerator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.api.deps import get_market_data_adapters
from app.api.routes import links as links_route
from app.database import get_async_session
from app.main import app
from app.models import Base
from app.models.event_link import EventLink
from app.services.matching import PROPOSED, persist_proposals
from app.services.matching import persist as persist_module
from tests.venues.fixture_adapter import FixtureAdapter, make_venue_market

#: A fixed close time so nothing here depends on the wall clock.
CLOSE = datetime(2026, 3, 18, 18, 0, tzinfo=UTC)


def _proposal(
    *,
    market_a: str = "KX-FED",
    market_b: str = "PM-FED",
    confidence: float,
    outcome_map: dict[str, str] | None = None,
) -> EventLink:
    """One unsaved proposal, shaped exactly as `propose_links` builds it.

    Hand-built rather than scored, because these tests are about WHEN the
    row is written, not what it scores: a literal confidence makes the
    before/after numbers in each assertion readable. `status` is set
    explicitly for the same reason `propose_links` sets it — the column
    default is applied at INSERT, so an unsaved row carries `None` and
    `persist_proposals` would refuse it.

    Args:
        market_a: Kalshi-side market id (canonical order puts kalshi
            first: `("kalshi", ...) < ("polymarket", ...)`).
        market_b: Polymarket-side market id.
        confidence: The score this pass would file.
        outcome_map: The map this pass would file. Defaults to the
            binary map the matcher derives for a YES/NO pair; pass `{}`
            for the multi-outcome case, where it refuses to guess.

    Returns:
        EventLink: An unsaved `"proposed"` row.
    """
    return EventLink(
        venue_a="kalshi",
        market_a=market_a,
        venue_b="polymarket",
        market_b=market_b,
        outcome_map=dict(
            outcome_map if outcome_map is not None else {"YES": "YES", "NO": "NO"}
        ),
        confidence=confidence,
        evidence={"title_jaccard": 1.0},
        status=PROPOSED,
    )


@pytest_asyncio.fixture
async def race_engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine, None]:
    """A FILE-backed SQLite engine, so two sessions get two connections.

    `tests/conftest.py`'s engine is `:memory:` behind a `StaticPool`,
    which pins every session in the process to ONE physical connection —
    fine for ordinary tests, useless here, because the reviewer's commit
    would land inside the pass's own transaction and the race being
    tested could not exist. A file in pytest's `tmp_path` (under
    `$TMPDIR`, never the repo tree — GUARDRAILS.md §2) gives real
    connections and real independent commits.
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'links.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
def race_sessions(race_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Sessions shaped exactly like `app.database.async_session_factory`.

    `autoflush=False` matters and is not cosmetic: production's factory
    sets it, so an in-memory attribute assignment would not reach the
    database until the pass's final `commit()` — hundreds of awaits after
    the decidedness check that authorized it. Copying the setting is what
    makes a pass here fail the way a pass there fails.
    """
    return async_sessionmaker(
        race_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


@pytest_asyncio.fixture
async def reviewer(
    race_sessions: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncClient, None]:
    """The real app, with a FRESH session per request over `race_engine`.

    Per request, not the one shared session `tests/conftest.py`'s client
    uses: the reviewer must commit on their own connection for the race
    to be the race. The override mirrors
    `app.database.get_async_session` — commit on success, roll back on
    an exception — so the routes meet the transaction shape they meet in
    production.
    """

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with race_sessions() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

    app.dependency_overrides[get_async_session] = override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


def _approving_between_read_and_write(
    monkeypatch: pytest.MonkeyPatch,
    *,
    market_a: str,
    approve: Callable[[], Awaitable[None]],
) -> None:
    """Fire `approve()` inside the pass, between its read and its write.

    Wraps the module-level `_find_existing` the pass looks up by name.
    The shim delegates to the real function and returns its result
    UNCHANGED — it alters no behaviour, only the moment at which a second
    writer commits. That moment is the entire defect: the pass has read
    the row, `is_decided` has answered "no", and the write has not
    happened yet.

    Args:
        monkeypatch: The test's patcher.
        market_a: Fire only on the lookup for this pair, so a pass
            carrying several proposals stays deterministic.
        approve: The reviewer's request, awaited once.
    """
    real = persist_module._find_existing
    fired = False

    async def shim(session: AsyncSession, proposal: EventLink) -> EventLink | None:
        nonlocal fired
        found = await real(session, proposal)
        if found is not None and proposal.market_a == market_a and not fired:
            fired = True
            await approve()
        return found

    monkeypatch.setattr(persist_module, "_find_existing", shim)


async def _row(
    sessions: async_sessionmaker[AsyncSession], link_id: int
) -> EventLink:
    """Re-read one link on a session of its own, from the database."""
    async with sessions() as session:
        row = await session.get(EventLink, link_id)
        assert row is not None
        return row


async def _file(
    sessions: async_sessionmaker[AsyncSession], *proposals: EventLink
) -> list[int]:
    """File proposals through the real filer and return their row ids."""
    async with sessions() as session:
        outcome = await persist_proposals(session, list(proposals))
        return [row.id for row in outcome.rows]


# ---------------------------------------------------------------------------
# 1. The pass cannot overwrite a decision committed while it was running.
# ---------------------------------------------------------------------------


async def test_a_confidence_a_human_approved_survives_a_concurrent_rescore(
    monkeypatch: pytest.MonkeyPatch,
    race_sessions: async_sessionmaker[AsyncSession],
    reviewer: AsyncClient,
) -> None:
    """The reviewer approved 0.60. The pass must not leave 0.97 behind.

    `confidence` is `p_same_resolution` in
    `app.strategies.cross_venue_arbitrage`'s net-edge formula, so this is
    a change to the price of every signal on this link, not a display
    value. With `gross_edge = 0.03` and `worst_case_loss = 0.55`::

        approved at 0.60:  0.03 x 0.60 - 0.40 x 0.55 = -0.202  (no trade)
        rescored to 0.97:  0.03 x 0.97 - 0.03 x 0.55 = +0.0126 (trades)

    The row must come out approved, by alice, AT 0.60.
    """
    (link_id,) = await _file(race_sessions, _proposal(confidence=0.60))

    async def approve() -> None:
        response = await reviewer.post(
            f"/api/v1/links/{link_id}/approve",
            json={"reviewed_by": "alice", "notes": "same 5pm ET print"},
        )
        assert response.status_code == 200, response.text
        # The reviewer decided the row as it read at that moment.
        assert response.json()["confidence"] == pytest.approx(0.60)

    _approving_between_read_and_write(
        monkeypatch, market_a="KX-FED", approve=approve
    )

    async with race_sessions() as session:
        outcome = await persist_proposals(session, [_proposal(confidence=0.97)])

    row = await _row(race_sessions, link_id)
    assert row.status == "approved"
    assert row.reviewed_by == "alice"
    assert row.reviewed_at is not None
    assert row.confidence == pytest.approx(0.60), (
        "the pass overwrote a confidence a human approved"
    )

    assert outcome.created == 0
    assert outcome.updated == 0, "a decided row is not an update"
    assert outcome.skipped_reviewed == 1


async def test_a_reviewers_hand_built_outcome_map_survives_a_concurrent_rescore(
    monkeypatch: pytest.MonkeyPatch,
    race_sessions: async_sessionmaker[AsyncSession],
    reviewer: AsyncClient,
) -> None:
    """The multi-outcome variant, and the worse of the two failures.

    The matcher stores `{}` for a multi-outcome pair because it will not
    guess which of Polymarket's candidate labels answers to which of
    Kalshi's, so `POST /links/{id}/approve` REQUIRES the reviewer to
    supply the map by hand. Replacing that map with a derived binary one
    is not a lost edit: `cross_venue_arbitrage` picks the B leg with
    `link.outcome_map.get(outcome_a)`, so `{"YES": "YES", "NO": "NO"}` on
    a TRUMP/HARRIS pair either matches nothing or matches the wrong
    contract — a position long the same side of two different events
    while believing it is hedged.
    """
    (link_id,) = await _file(
        race_sessions,
        _proposal(
            market_a="KX-PRES",
            market_b="PM-PRES",
            confidence=0.60,
            outcome_map={},
        ),
    )

    reviewed_map = {"TRUMP": "REPUBLICAN", "HARRIS": "DEMOCRAT"}

    async def approve() -> None:
        response = await reviewer.post(
            f"/api/v1/links/{link_id}/approve",
            json={
                "reviewed_by": "alice",
                "notes": "candidate-to-party correspondence checked by hand",
                "outcome_map": reviewed_map,
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["outcome_map"] == reviewed_map

    _approving_between_read_and_write(
        monkeypatch, market_a="KX-PRES", approve=approve
    )

    async with race_sessions() as session:
        outcome = await persist_proposals(
            session,
            [
                _proposal(
                    market_a="KX-PRES",
                    market_b="PM-PRES",
                    confidence=0.97,
                    outcome_map={"YES": "YES", "NO": "NO"},
                )
            ],
        )

    row = await _row(race_sessions, link_id)
    assert row.status == "approved"
    assert row.outcome_map == reviewed_map, (
        "the pass replaced a map a human built by hand"
    )
    assert row.confidence == pytest.approx(0.60)

    assert outcome.updated == 0
    assert outcome.skipped_reviewed == 1


# ---------------------------------------------------------------------------
# 2. The guard is per row: the queue it protects still gets maintained.
# ---------------------------------------------------------------------------


async def test_the_pass_still_rescores_the_undecided_rows_around_a_decided_one(
    monkeypatch: pytest.MonkeyPatch,
    race_sessions: async_sessionmaker[AsyncSession],
    reviewer: AsyncClient,
) -> None:
    """A guard that froze the queue would defeat the queue.

    Same pass, two pairs. A reviewer approves the FED pair mid-pass; the
    BTC pair beside it is untouched by anyone and must be refreshed to
    today's score, because a review queue showing whatever a pair scored
    the first time it was ever seen is worse than no score at all
    (`persist.py` case 2).
    """
    fed_id, btc_id = await _file(
        race_sessions,
        _proposal(market_a="KX-FED", market_b="PM-FED", confidence=0.60),
        _proposal(market_a="KX-BTC", market_b="PM-BTC", confidence=0.60),
    )

    async def approve() -> None:
        response = await reviewer.post(
            f"/api/v1/links/{fed_id}/approve",
            json={"reviewed_by": "alice", "notes": "same 5pm ET print"},
        )
        assert response.status_code == 200, response.text

    _approving_between_read_and_write(
        monkeypatch, market_a="KX-FED", approve=approve
    )

    async with race_sessions() as session:
        outcome = await persist_proposals(
            session,
            [
                _proposal(market_a="KX-FED", market_b="PM-FED", confidence=0.97),
                _proposal(market_a="KX-BTC", market_b="PM-BTC", confidence=0.97),
            ],
        )

    decided = await _row(race_sessions, fed_id)
    assert decided.status == "approved"
    assert decided.confidence == pytest.approx(0.60)

    assert outcome.created == 0
    assert outcome.updated == 1
    assert outcome.skipped_reviewed == 1

    undecided = await _row(race_sessions, btc_id)
    assert undecided.status == PROPOSED
    assert undecided.reviewed_at is None
    assert undecided.confidence == pytest.approx(0.97), (
        "the guard must not freeze the queue it exists to maintain"
    )


async def test_an_uncontested_pass_rescores_an_undecided_row_in_place(
    race_sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Case 2 unchanged when nobody is racing: rescored, not duplicated."""
    (link_id,) = await _file(race_sessions, _proposal(confidence=0.60))

    async with race_sessions() as session:
        outcome = await persist_proposals(session, [_proposal(confidence=0.97)])

    assert (outcome.created, outcome.updated, outcome.skipped_reviewed) == (0, 1, 0)

    async with race_sessions() as session:
        rows = list((await session.execute(select(EventLink))).scalars().all())
    assert [row.id for row in rows] == [link_id]
    assert rows[0].confidence == pytest.approx(0.97)
    assert rows[0].status == PROPOSED


# ---------------------------------------------------------------------------
# 3. One reviewer does not silently overwrite another.
# ---------------------------------------------------------------------------


async def test_approving_a_link_another_reviewer_already_rejected_is_a_conflict(
    client: AsyncClient, test_session: AsyncSession
) -> None:
    """The reviewer whose note explains WHY keeps the row.

    Bob's rejection carries the only knowledge in this table no score can
    rediscover — the settlement difference he read. An approval arriving
    after it must not quietly replace it, because the result would be a
    link cleared to trade whose recorded reason says it must not be.
    """
    row = EventLink(
        venue_a="kalshi",
        market_a="KX-FED",
        venue_b="polymarket",
        market_b="PM-FED",
        outcome_map={"YES": "YES", "NO": "NO"},
        confidence=0.60,
        evidence={},
        status=PROPOSED,
    )
    test_session.add(row)
    await test_session.commit()

    rejected = await client.post(
        f"/api/v1/links/{row.id}/reject",
        json={
            "reviewed_by": "bob",
            "notes": "Kalshi settles on the AP call, Polymarket on certification",
        },
    )
    assert rejected.status_code == 200, rejected.text

    conflict = await client.post(
        f"/api/v1/links/{row.id}/approve",
        json={"reviewed_by": "alice", "notes": "looks the same to me"},
    )
    assert conflict.status_code == 409, conflict.text
    assert "already 'rejected'" in conflict.json()["detail"]

    # `GET /links`, not `GET /links/{id}`: the review surface reads both
    # venues through `get_market_data_adapters`, and this test has no
    # business constructing a venue adapter (GUARDRAILS.md §1.4).
    listing = (await client.get("/api/v1/links")).json()["links"]
    assert [link["status"] for link in listing] == ["rejected"]
    assert listing[0]["reviewed_by"] == "bob"
    assert "AP call" in listing[0]["notes"]


async def test_a_second_approval_of_an_approved_link_is_a_conflict(
    client: AsyncClient, test_session: AsyncSession
) -> None:
    """Including when the second approval would change the outcome map.

    An approved link is a live trading instruction. Re-approving it with
    a different map would repoint `cross_venue_arbitrage`'s B leg with no
    one having re-read the two rules texts, so the second writer is told
    to re-read the link instead of being allowed to win by arriving last.
    """
    row = EventLink(
        venue_a="kalshi",
        market_a="KX-PRES",
        venue_b="polymarket",
        market_b="PM-PRES",
        outcome_map={},
        confidence=0.60,
        evidence={},
        status=PROPOSED,
    )
    test_session.add(row)
    await test_session.commit()

    first = await client.post(
        f"/api/v1/links/{row.id}/approve",
        json={
            "reviewed_by": "alice",
            "notes": "checked by hand",
            "outcome_map": {"TRUMP": "REPUBLICAN", "HARRIS": "DEMOCRAT"},
        },
    )
    assert first.status_code == 200, first.text

    second = await client.post(
        f"/api/v1/links/{row.id}/approve",
        json={
            "reviewed_by": "carol",
            "notes": "",
            "outcome_map": {"TRUMP": "DEMOCRAT", "HARRIS": "REPUBLICAN"},
        },
    )
    assert second.status_code == 409, second.text
    assert "already 'approved'" in second.json()["detail"]

    body = (await client.get("/api/v1/links")).json()["links"][0]
    assert body["reviewed_by"] == "alice"
    assert body["outcome_map"] == {"TRUMP": "REPUBLICAN", "HARRIS": "DEMOCRAT"}


async def test_rejecting_an_approved_link_is_still_allowed(
    client: AsyncClient, test_session: AsyncSession
) -> None:
    """The asymmetry is deliberate: revocation is the fail-safe direction.

    Approval is guarded because it CLEARS capital to move. Rejection is
    not, because it stops it: the moment anyone spots the settlement
    difference, the link must be withdrawable without first undoing
    someone else's approval.
    """
    row = EventLink(
        venue_a="kalshi",
        market_a="KX-FED",
        venue_b="polymarket",
        market_b="PM-FED",
        outcome_map={"YES": "YES", "NO": "NO"},
        confidence=0.60,
        evidence={},
        status=PROPOSED,
    )
    test_session.add(row)
    await test_session.commit()

    approved = await client.post(
        f"/api/v1/links/{row.id}/approve",
        json={"reviewed_by": "alice", "notes": "same print"},
    )
    assert approved.status_code == 200, approved.text

    withdrawn = await client.post(
        f"/api/v1/links/{row.id}/reject",
        json={"reviewed_by": "bob", "notes": "different resolution source"},
    )
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["status"] == "rejected"
    assert withdrawn.json()["reviewed_by"] == "bob"

    assert (await client.get("/api/v1/links", params={"status": "approved"})).json()[
        "links"
    ] == []


# ---------------------------------------------------------------------------
# 4. L1 — a manual propose racing the beat's insert.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def proposing_client(
    test_session: AsyncSession,
) -> AsyncGenerator[AsyncClient, None]:
    """`POST /links/propose` wired to two in-memory venues.

    Shadows `tests/conftest.py`'s client for this fixture's users only:
    the propose route also depends on `get_market_data_adapters`, which
    would otherwise reach the registry and construct a real venue client
    (GUARDRAILS.md §1.4).
    """
    polymarket = FixtureAdapter("polymarket")
    polymarket.add_market(
        make_venue_market(
            "polymarket",
            "PM-FED",
            question="Will the Fed cut rates?",
            close_time=CLOSE,
            outcomes=("YES", "NO"),
            rules_text="Resolves per the venue's stated source.",
        )
    )
    kalshi = FixtureAdapter("kalshi")
    kalshi.add_market(
        make_venue_market(
            "kalshi",
            "KX-FED",
            question="Will the Fed cut rates?",
            close_time=CLOSE,
            outcomes=("YES", "NO"),
            rules_text="Resolves per the venue's stated source.",
        )
    )

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        yield test_session

    async def override_adapters() -> dict[str, Any]:
        return {"polymarket": polymarket, "kalshi": kalshi}

    app.dependency_overrides[get_async_session] = override_session
    app.dependency_overrides[get_market_data_adapters] = override_adapters
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def test_a_propose_that_loses_the_insert_race_is_a_conflict_not_a_500(
    monkeypatch: pytest.MonkeyPatch,
    proposing_client: AsyncClient,
    test_session: AsyncSession,
) -> None:
    """The beat and a human file the same new pair; the loser's commit raises.

    The beat already reads this race as "nothing is lost by deferring"
    (`app.tasks.matching` rolls the pass back and reports `conflict`),
    because the matcher is deterministic and the row the winner just
    filed is the row the loser would have filed. The route had no `try`
    at all, so the same harmless race reached the operator as a 500. It
    now reports the conflict, and writes nothing.
    """

    async def _conflict(session: AsyncSession, proposals: Any) -> Any:
        raise IntegrityError("INSERT INTO event_links", {}, Exception("unique"))

    monkeypatch.setattr(links_route, "persist_proposals", _conflict)

    response = await proposing_client.post("/api/v1/links/propose")

    assert response.status_code == 409, response.text
    assert "nothing was written" in response.json()["detail"]

    rows = (await test_session.execute(select(EventLink))).scalars().all()
    assert list(rows) == []
