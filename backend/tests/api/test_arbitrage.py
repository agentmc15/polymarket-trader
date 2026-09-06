"""T19/T25 — `app/api/routes/arbitrage.py` over `app.services.scanner`.

Derived from the T19 brief/acceptance in `.claude/kits/market-edge/TASKS.md`
("scan over FixtureAdapters with one planted complement gap ->
`/opportunities` returns >= 1 with all score fields present and
non-null"), not by reading the route/scanner and mirroring them back.
The T25 block at the bottom adds the near-resolution surface: that the
`settlement_edge` pass has a PRODUCTION caller reaching `/opportunities`
at all, and that the two passes coexist on one list.

GUARDRAILS.md §1.1/§1.2/§1.4: no network, no live mode, no real order —
market data comes from `tests.venues.fixture_adapter.FixtureAdapter`,
`get_market_data_adapters` is overridden the same way
`tests/api/test_trading.py` overrides `get_router`, and nothing here
calls `place_order`/`OrderRouter.submit`.

The planted gap uses `binary_complement_arbitrage` (same-venue, no
`EventLink` needed) so this test does not have to also stand up an
approved cross-venue link just to prove the scan -> score -> API path
works end to end.
"""
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta
from typing import Any

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.api.deps import get_market_data_adapters
from app.database import get_async_session
from app.main import app
from app.models.intent import IntentRecord
from app.utils.time import utcnow
from app.venues.base import MarketDataAdapter
from app.venues.types import VenueId
from tests.helpers import make_book
from tests.venues.fixture_adapter import FixtureAdapter, make_venue_market

PM_MARKET = "PM-GAP"
KX_MARKET = "KXDETERMINED-1"

#: Long enough to clear `app.services.scoring`'s 200-char `rules_text`
#: penalty; a named source clears the "no resolution_source" penalty.
#: Same convention as `tests/services/test_near_resolution.py`.
LONG_RULES_TEXT = "This market resolves according to the stated rules. " * 5

#: Every field T19's acceptance line names, verbatim.
REQUIRED_SCORE_FIELDS = (
    "net_edge",
    "annualized_return",
    "hours_to_resolution",
    "fill_confidence",
    "resolution_risk",
    "capital_lockup_usd",
    "composite",
)


@pytest_asyncio.fixture
async def session_factory(
    test_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """An `async_sessionmaker` over the shared in-memory `test_engine`."""
    return async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


def _planted_gap_adapter() -> FixtureAdapter:
    """A `FixtureAdapter` with one open market carrying a planted complement gap.

    YES ask 0.40 + NO ask 0.50 = 0.90 -> gross edge 10%, comfortably
    above `binary_complement_arbitrage`'s default 2% `min_profit_margin`
    even after Polymarket's unknown-category 5% taker fee and amortized
    redemption gas (by hand: yes_fee = 0.05*0.40*0.60 = 0.012, no_fee =
    0.05*0.50*0.50 = 0.0125, gas = 2*0.05/100 = 0.001, net edge =
    0.10 - 0.012 - 0.0125 - 0.001 = 0.0745 >= 0.02).
    """
    adapter = FixtureAdapter("polymarket")
    market = make_venue_market(
        "polymarket",
        PM_MARKET,
        close_time=utcnow() + timedelta(days=10),
        rules_text="This market resolves according to the stated rules. " * 5,
        resolution_source="Official Source",
        raw={"volume": 500_000.0},
    )
    adapter.add_market(market)
    adapter.set_book(
        make_book(
            bids=[(0.38, 200.0)], asks=[(0.40, 200.0)],
            venue="polymarket", market_id=PM_MARKET, outcome="YES",
        )
    )
    adapter.set_book(
        make_book(
            bids=[(0.48, 200.0)], asks=[(0.50, 200.0)],
            venue="polymarket", market_id=PM_MARKET, outcome="NO",
        )
    )
    return adapter


def _near_resolution_adapter() -> FixtureAdapter:
    """A Kalshi `FixtureAdapter` with one past-close, near-certain market.

    Shaped exactly like `tests/services/test_near_resolution.py`'s
    baseline case, and for the same reasons: `status="closed"` (which
    `scan()`'s `status="open"` filter drops, so this market contributes
    NOTHING to the arbitrage pass and cannot perturb the T19 tests
    above), `close_time` 1h behind now (so `outcome_determined` holds),
    and Kalshi's own `expected_settle_time` 30h AHEAD (so
    `in_dispute_window` is `False` and the strategy's shipped
    `allow_dispute_window=False` does not refuse it).

    Its complement asks sum to 0.98 + 0.03 = 1.01 > 1.00, so
    `binary_complement_arbitrage` finds no gap here either way.
    """
    now = utcnow()
    adapter = FixtureAdapter("kalshi")
    adapter.add_market(
        make_venue_market(
            "kalshi",
            KX_MARKET,
            status="closed",
            close_time=now - timedelta(hours=1),
            expected_settle_time=now + timedelta(hours=30),
            rules_text=LONG_RULES_TEXT,
            resolution_source="Official Source",
        )
    )
    adapter.set_book(
        make_book(
            bids=[(0.96, 500.0)], asks=[(0.98, 500.0)],
            venue="kalshi", market_id=KX_MARKET, outcome="YES",
        )
    )
    adapter.set_book(
        make_book(
            bids=[(0.01, 500.0)], asks=[(0.03, 500.0)],
            venue="kalshi", market_id=KX_MARKET, outcome="NO",
        )
    )
    return adapter


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncClient, None]:
    """An HTTP client wired to a fixture-backed adapter dict and a fresh-per-call session.

    Shadows `tests/conftest.py`'s `client` fixture for this module only,
    the same pattern `tests/api/test_trading.py` uses for `get_router`.
    """

    async def override_get_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

    async def override_adapters() -> dict[VenueId, MarketDataAdapter]:
        return {
            "polymarket": _planted_gap_adapter(),
            "kalshi": _near_resolution_adapter(),
        }

    app.dependency_overrides[get_async_session] = override_get_session
    app.dependency_overrides[get_market_data_adapters] = override_adapters

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


async def test_post_scan_finds_and_returns_the_planted_gap(client: AsyncClient) -> None:
    """`POST /scan` runs synchronously and reports the planted opportunity."""
    response = await client.post(
        "/api/v1/arbitrage/scan",
        params={"strategies": ["binary_complement_arbitrage"]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["mode"] == "paper"
    assert body["opportunities_found"] >= 1
    assert len(body["opportunities"]) == body["opportunities_found"]

    opportunity = body["opportunities"][0]
    assert opportunity["strategy"] == "binary_complement_arbitrage"
    assert opportunity["kind"] == "complement"
    for field in REQUIRED_SCORE_FIELDS:
        assert field in opportunity
        assert opportunity[field] is not None
    assert opportunity["net_edge"] > 0.0
    assert opportunity["composite"] > 0.0
    assert opportunity["depth_source"] == "recorded"
    # No cross-venue link is involved in a same-venue complement gap.
    assert opportunity["link_status"] is None


async def test_get_opportunities_returns_the_scanned_gap_with_every_score_field(
    client: AsyncClient,
) -> None:
    """After a scan, `/opportunities` surfaces >= 1 row with a complete, non-null score."""
    scan_response = await client.post(
        "/api/v1/arbitrage/scan",
        params={"strategies": ["binary_complement_arbitrage"]},
    )
    assert scan_response.status_code == 200

    response = await client.get("/api/v1/arbitrage/opportunities")

    assert response.status_code == 200
    body = response.json()
    assert body["count"] >= 1
    assert len(body["opportunities"]) == body["count"]

    opportunity = body["opportunities"][0]
    for field in REQUIRED_SCORE_FIELDS:
        assert field in opportunity
        assert opportunity[field] is not None
    assert "depth_source" in opportunity
    assert opportunity["depth_source"] is not None
    assert "link_status" in opportunity  # present in the payload; may legitimately be null
    assert opportunity["status"] == "pending"
    assert opportunity["mode"] == "paper"
    assert any(leg["venue"] == "polymarket" for leg in opportunity["legs"])


async def test_get_opportunities_min_composite_filters_out_everything(
    client: AsyncClient,
) -> None:
    """An impossibly high `min_composite` floor leaves nothing."""
    await client.post(
        "/api/v1/arbitrage/scan", params={"strategies": ["binary_complement_arbitrage"]}
    )

    response = await client.get(
        "/api/v1/arbitrage/opportunities", params={"min_composite": 1_000_000.0}
    )

    assert response.status_code == 200
    assert response.json() == {"opportunities": [], "count": 0}


async def test_get_opportunities_venue_filter_matches_the_planted_venue(
    client: AsyncClient,
) -> None:
    await client.post(
        "/api/v1/arbitrage/scan", params={"strategies": ["binary_complement_arbitrage"]}
    )

    matching = await client.get(
        "/api/v1/arbitrage/opportunities", params={"venue": "polymarket"}
    )
    non_matching = await client.get(
        "/api/v1/arbitrage/opportunities", params={"venue": "kalshi"}
    )

    assert matching.json()["count"] >= 1
    assert non_matching.json() == {"opportunities": [], "count": 0}


async def test_get_history_is_empty_before_anything_is_executed(client: AsyncClient) -> None:
    """`scan()` only ever writes `status="pending"` rows -- nothing is "executed" yet."""
    await client.post(
        "/api/v1/arbitrage/scan", params={"strategies": ["binary_complement_arbitrage"]}
    )

    response = await client.get("/api/v1/arbitrage/history")

    assert response.status_code == 200
    assert response.json() == {"trades": [], "total": 0}


# ---------------------------------------------------------------------------
# T25 -- the near-resolution surface: a PRODUCTION path, and coexistence
# ---------------------------------------------------------------------------


async def test_a_near_resolution_edge_reaches_opportunities_through_a_production_path(
    client: AsyncClient,
) -> None:
    """The headline ask, end to end, WITHOUT calling `near_resolution_pass`.

    Before T25 `near_resolution_pass()` had no caller anywhere in `app/`
    — no Celery beat entry, no route — so the only thing that had ever
    exercised it was a test calling it directly. That is precisely why
    nobody noticed: this test deliberately reaches it the way an operator
    does, over HTTP, and asserts the row comes back out of
    `/opportunities` under the `near_resolution=true` filter an operator
    would actually use.

    The general scan cannot substitute (see the sibling test below), so
    a green here is evidence about the near-resolution path specifically.

    `hours_to_resolution` is 30 (the fixture's stated
    `expected_settle_time`, 30h ahead), comfortably inside
    `settings.near_resolution_hours`'s 72, so `near_resolution=true`
    keeps it and `near_resolution=false` must not.
    """
    scan = await client.post("/api/v1/arbitrage/scan/near-resolution")

    assert scan.status_code == 200
    scan_body = scan.json()
    assert scan_body["status"] == "completed"
    assert scan_body["mode"] == "paper"
    assert scan_body["opportunities_found"] == 1
    assert scan_body["opportunities"][0]["strategy"] == "settlement_edge"

    response = await client.get(
        "/api/v1/arbitrage/opportunities", params={"near_resolution": True}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    opportunity = body["opportunities"][0]
    assert opportunity["strategy"] == "settlement_edge"
    # The tag every bucket fence keys on -- `check_order_limits`'s
    # `max_near_resolution_notional_usd` cap and
    # `OrderRouter._bucket_open_notional` both read it. An empty
    # opportunities list is what made that whole apparatus inert.
    assert opportunity["metadata"]["bucket"] == "near_resolution"
    # 30h to the stated `expected_settle_time`, modulo the microseconds
    # between the fixture's `utcnow()` and the pass's own.
    assert 29.99 < opportunity["hours_to_resolution"] <= 30.0
    assert opportunity["composite"] > 0.0
    assert any(leg["venue"] == "kalshi" for leg in opportunity["legs"])

    beyond = await client.get(
        "/api/v1/arbitrage/opportunities", params={"near_resolution": False}
    )
    assert beyond.json() == {"opportunities": [], "count": 0}


async def test_the_general_scan_refuses_settlement_edge_rather_than_answering_empty(
    client: AsyncClient,
) -> None:
    """`POST /scan?strategies=settlement_edge` must not report "found: 0".

    `scan()` calls `score(intent, ctx)` WITHOUT `allow_past_close=True`,
    and every settlement-edge intent is on a market whose `close_time`
    has passed by construction, so 100% of them raise `UnscorableIntent`
    and are silently skipped. The honest answer is a redirect to the pass
    that can score them, not a `200` whose empty list reads as "no
    settlement edges exist right now".

    The fixture Kalshi market this scan would look at is exactly the one
    the near-resolution pass DOES find an edge on (test above), so
    "nothing found" here would be a lie about data this very test has
    planted.
    """
    response = await client.post(
        "/api/v1/arbitrage/scan", params={"strategies": ["settlement_edge"]}
    )

    assert response.status_code == 400
    assert "near-resolution" in response.json()["detail"]


async def test_the_two_passes_do_not_hide_each_other_on_the_opportunities_list(
    client: AsyncClient,
) -> None:
    """Both passes' latest results are visible at once, in either order.

    `scan()` and `near_resolution_pass()` each mint their own `scan_id`
    and run on their own clock. With one global "newest `scan_id`",
    wiring the second pass up would have SUBTRACTED a surface: whichever
    pass ran last would erase the other's rows from `/opportunities`
    entirely. This runs them in both orders and requires both strategies
    to be present each time.
    """
    await client.post(
        "/api/v1/arbitrage/scan", params={"strategies": ["binary_complement_arbitrage"]}
    )
    await client.post("/api/v1/arbitrage/scan/near-resolution")

    after_near = await client.get("/api/v1/arbitrage/opportunities")
    strategies = {o["strategy"] for o in after_near.json()["opportunities"]}
    assert strategies == {"binary_complement_arbitrage", "settlement_edge"}

    # ...and running the arbitrage pass again must not evict the
    # near-resolution rows either.
    await client.post(
        "/api/v1/arbitrage/scan", params={"strategies": ["binary_complement_arbitrage"]}
    )

    after_arbitrage = await client.get("/api/v1/arbitrage/opportunities")
    strategies = {o["strategy"] for o in after_arbitrage.json()["opportunities"]}
    assert strategies == {"binary_complement_arbitrage", "settlement_edge"}


def _scored_row(
    row_id: str,
    *,
    strategy: str,
    created_at: datetime,
    extra_data: dict[str, Any],
) -> IntentRecord:
    """Build one `pending`, scored `IntentRecord` with an explicit timestamp.

    `created_at` is passed explicitly rather than left to the column's
    `server_default`: SQLite's `CURRENT_TIMESTAMP` has one-SECOND
    resolution, so rows written by two scans inside the same second would
    tie and "newest first" would be arbitrary. These tests are about
    WHICH row is newest, so the ordering has to be stated, not hoped for.
    """
    return IntentRecord(
        id=row_id,
        kind="complement",
        strategy=strategy,
        mode="paper",
        status="pending",
        created_at=created_at,
        legs=[
            {
                "venue": "polymarket",
                "market_id": PM_MARKET,
                "outcome": "YES",
                "side": "BUY",
                "limit_price": 0.40,
                "size_contracts": 100.0,
                "size_usd": 40.0,
            }
        ],
        score={
            "net_edge": 0.07,
            "annualized_return": 1.5,
            "hours_to_resolution": 240.0,
            "fill_confidence": 1.0,
            "resolution_risk": 0.15,
            "capital_lockup_usd": 40.0,
            "composite": 1.2,
            "link_status": None,
            "depth_source": "recorded",
        },
        extra_data=extra_data,
    )


async def test_a_scoreless_scan_id_cannot_disable_the_latest_scan_filter(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A pending row with NO `scan_id` must not turn "latest scan" into "everything".

    `OrderRouter._persist_pending` writes a `pending` `IntentRecord` at
    ROUTING time whose `extra_data` carries `atomicity`/
    `hold_to_resolution`/`confidence`/`tif`/`bucket` and NO `scan_id`
    (its `score` is `intent.metadata["score"]`, which is empty for every
    strategy shipped today — so this row shape is reachable but currently
    shielded by the route's own `if row.score` pre-filter; it is
    reproduced faithfully here because the bug is in the FILTER, which
    must not be able to switch itself off on missing data).

    The old code read `latest_scan_id` off the newest row, found `None`,
    and skipped the scan filter ENTIRELY — returning every pending scored
    intent from every historical scan as though it were the current list.
    Four rows planted, two of them stale or not scan output at all:

        S1 arbitrage       30m ago  -> stale, must be dropped
        S2 arbitrage       20m ago  -> newest arbitrage, must be kept
        S3 near_resolution 10m ago  -> newest near-resolution, must be kept
        (no scan_id)        now     -> not scanner output, must be dropped
    """
    now = utcnow()
    async with session_factory() as session:
        session.add_all(
            [
                _scored_row(
                    "arb-old",
                    strategy="binary_complement_arbitrage",
                    created_at=now - timedelta(minutes=30),
                    extra_data={"scan_id": "S1", "scan_pass": "arbitrage"},
                ),
                _scored_row(
                    "arb-new",
                    strategy="binary_complement_arbitrage",
                    created_at=now - timedelta(minutes=20),
                    extra_data={"scan_id": "S2", "scan_pass": "arbitrage"},
                ),
                _scored_row(
                    "near-new",
                    strategy="settlement_edge",
                    created_at=now - timedelta(minutes=10),
                    extra_data={"scan_id": "S3", "scan_pass": "near_resolution"},
                ),
                _scored_row(
                    "routed",
                    strategy="api",
                    created_at=now,
                    extra_data={
                        "atomicity": "best_effort",
                        "hold_to_resolution": False,
                        "confidence": 1.0,
                        "tif": "GTC",
                        "bucket": None,
                    },
                ),
            ]
        )
        await session.commit()

    response = await client.get("/api/v1/arbitrage/opportunities")

    assert response.status_code == 200
    body = response.json()
    assert {o["id"] for o in body["opportunities"]} == {"arb-new", "near-new"}
    assert body["count"] == 2


async def test_the_latest_scan_filter_is_per_pass_not_global(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Each pass replaces only its OWN previous rows.

    With one global newest `scan_id` and no untagged row in play, the
    newest row (`S3`, the near-resolution pass) would define "latest" for
    everything and the arbitrage pass's current results would vanish from
    the list — the exact way wiring the second pass up could have taken a
    working surface away.
    """
    now = utcnow()
    async with session_factory() as session:
        session.add_all(
            [
                _scored_row(
                    "arb-old",
                    strategy="binary_complement_arbitrage",
                    created_at=now - timedelta(minutes=30),
                    extra_data={"scan_id": "S1", "scan_pass": "arbitrage"},
                ),
                _scored_row(
                    "arb-new",
                    strategy="binary_complement_arbitrage",
                    created_at=now - timedelta(minutes=20),
                    extra_data={"scan_id": "S2", "scan_pass": "arbitrage"},
                ),
                _scored_row(
                    "near-new",
                    strategy="settlement_edge",
                    created_at=now - timedelta(minutes=10),
                    extra_data={"scan_id": "S3", "scan_pass": "near_resolution"},
                ),
            ]
        )
        await session.commit()

    response = await client.get("/api/v1/arbitrage/opportunities")

    body = response.json()
    assert {o["id"] for o in body["opportunities"]} == {"arb-new", "near-new"}
    assert body["count"] == 2


# ---------------------------------------------------------------------------
# T33 -- `edge_basis` as a first-class column, not a metadata blob entry
# ---------------------------------------------------------------------------


async def test_a_scanned_opportunity_carries_edge_basis_as_a_typed_field(
    client: AsyncClient,
) -> None:
    """`edge_basis` is a column of the payload, beside `composite`.

    It is the honesty label on the ranking: `"observed_costs"` means
    every term of `net_edge` is an observed price or a published fee/gas
    rate, `"identity_estimated"` means it also carries a haircut driven
    by the matcher's SIMILARITY score, which is not a calibrated
    probability. A consumer sorting by `composite` has to be able to see
    which rows rest on an estimate WITHOUT digging through the generic
    `metadata` dict, which is where it used to live only.

    The planted gap is a same-venue complement on ONE market, so its
    edge is pure arithmetic over two observed asks and Polymarket's
    published fee — `"observed_costs"`, with no identity estimate
    anywhere in it.
    """
    scan = await client.post(
        "/api/v1/arbitrage/scan",
        params={"strategies": ["binary_complement_arbitrage"]},
    )
    assert scan.status_code == 200
    scanned = scan.json()["opportunities"][0]
    assert scanned["edge_basis"] == "observed_costs"

    listed = await client.get("/api/v1/arbitrage/opportunities")
    persisted = listed.json()["opportunities"][0]

    # Both builders -- the fresh `ScoredIntent` one `POST /scan` uses and
    # the persisted-row one `GET /opportunities` uses -- must agree.
    assert persisted["edge_basis"] == "observed_costs"
    assert persisted["id"] == scanned["id"]


async def test_a_row_with_no_persisted_basis_reports_none_rather_than_observed(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An UNLABELED row must not be dressed up as an arithmetic one.

    `_scored_row`'s `score` dict is the pre-T31 shape: it carries no
    `edge_basis` at all, exactly like a row persisted before the field
    existed. Defaulting such a row to `"observed_costs"` would make it
    indistinguishable from a row whose edge genuinely contains no
    estimated parameter — the same "a default that looks like data"
    failure GUARDRAILS.md §1.7 exists to prevent. `None` says nothing,
    which is the truth about it.
    """
    async with session_factory() as session:
        session.add(
            _scored_row(
                "unlabeled",
                strategy="binary_complement_arbitrage",
                created_at=utcnow(),
                extra_data={"scan_id": "S9", "scan_pass": "arbitrage"},
            )
        )
        await session.commit()

    response = await client.get("/api/v1/arbitrage/opportunities")

    body = response.json()
    assert [o["id"] for o in body["opportunities"]] == ["unlabeled"]
    assert "edge_basis" in body["opportunities"][0]
    assert body["opportunities"][0]["edge_basis"] is None
