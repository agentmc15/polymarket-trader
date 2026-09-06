"""Event-equivalence subsystem: normalizer, matcher, blocking, review API.

Every expected confidence in this file is computed BY HAND in the test
body from PLAN.md D9's formula (GUARDRAILS.md §5 — a test that computes
its expectation with the code under test is not a test)::

    confidence = 0.55 x title_jaccard
               + 0.20 x close_score
               + 0.15 x threshold_score
               + 0.10 x source_score

    close_score = max(0, 1 - |close_delta_h| / 48)
    threshold/source score = 1.0 agree / 0.5 unknown / 0.0 disagree

No network anywhere (GUARDRAILS.md §1.4): both venues are
`tests.venues.fixture_adapter.FixtureAdapter`, which holds hand-written
`VenueMarket` values and has no order-placement methods at all. The API
tests override `app.api.deps.get_market_data_adapters` with those
fixtures, so the real registry — and therefore any real venue client —
is never constructed.

The load-bearing assertion in this file is
`test_matcher_never_advances_a_link_past_proposed`: PLAN.md R1 says a
wrong cross-venue equivalence turns an "arbitrage" into two uncorrelated
directional bets, so the matcher may only ever PROPOSE, and only a human
acting through `/links/{id}/approve` may clear one to trade.
"""
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_market_data_adapters
from app.database import get_async_session
from app.main import app
from app.models.event_link import EventLink
from app.services.matching import (
    DATE_TOKEN,
    NUM_TOKEN,
    THRESHOLD_MISMATCH_CAP,
    UNKNOWN_SCORE,
    candidate_pairs,
    compare_thresholds,
    content_tokens,
    extract_thresholds,
    normalize_title,
    propose_links,
    score_pair,
    tri_state_score,
)
from tests.venues.fixture_adapter import FixtureAdapter, make_venue_market

#: A fixed close time so no test depends on the wall clock.
CLOSE = datetime(2025, 12, 31, 23, 59, tzinfo=UTC)

#: The fixture pair used by both the matcher and the API tests.
BTC_PM = "Will Bitcoin be above $100,000 on December 31, 2025?"
BTC_KX = "Bitcoin above 100K by Dec 31 2025"
SENATE = "Which party controls the Senate after the 2028 election?"


def market(
    venue: str,
    market_id: str,
    question: str,
    *,
    close: datetime = CLOSE,
    source: str | None = None,
    outcomes: tuple[str, ...] = ("YES", "NO"),
    rules: str = "Resolves per the venue's stated source.",
):
    """Build one normalized `VenueMarket` for a matching test."""
    return make_venue_market(
        venue,  # type: ignore[arg-type]
        market_id,
        question=question,
        close_time=close,
        resolution_source=source,
        outcomes=outcomes,
        rules_text=rules,
    )


# --------------------------------------------------------------------
# normalize.py
# --------------------------------------------------------------------


def test_stopwords_and_d9_tokens_are_removed():
    """`will/the/by/before/after/on/in` and general function words go."""
    tokens = normalize_title("Will the vote be held before the deadline?")
    assert "will" not in tokens
    assert "the" not in tokens
    assert "before" not in tokens
    assert "be" not in tokens
    # The subject matter survives, stemmed.
    assert "vote" in tokens
    assert "deadlin" in tokens


def test_negations_and_comparisons_survive_normalization():
    """Dropping "not"/"above" would merge opposite events.

    A stock English stopword list contains both. If they were removed,
    "Will X happen?" and "Will X NOT happen?" — the two sides of the same
    contract — would normalize identically, and so would "above 100k" and
    "below 100k".
    """
    assert "not" in normalize_title("Will the bill not pass?")
    # Stemmed: Porter drops the terminal "e" of "above".
    assert "abov" in normalize_title("Bitcoin above 100k")
    assert "below" in normalize_title("Bitcoin below 100k")
    assert normalize_title("Bitcoin above 100k") != normalize_title(
        "Bitcoin below 100k"
    )


def test_dates_months_and_years_collapse_to_one_placeholder():
    """Every date shape becomes `<date>`, and no digits survive it."""
    for title in (
        "Resolves on December 31, 2025",
        "Resolves on 31 Dec 2025",
        "Resolves on 12/31/2025",
        "Resolves on 2025-12-31",
        "Resolves in 2025",
    ):
        tokens = normalize_title(title)
        assert DATE_TOKEN in tokens, title
        assert NUM_TOKEN not in tokens, title


def test_thresholds_are_extracted_in_every_common_spelling():
    """`100k`, `100,000`, `$100K` and `1.5m` all parse to their value."""
    assert extract_thresholds("above 100k") == [100_000.0]
    assert extract_thresholds("at least 100,000") == [100_000.0]
    assert extract_thresholds("over $100K") == [100_000.0]
    assert extract_thresholds("more than 1.5m") == [1_500_000.0]
    # The literal is replaced in the token stream even though its value
    # is kept — that is what makes "above 100k" and "above 150k" score
    # identically on wording and forces the threshold comparison to
    # carry the distinction.
    assert normalize_title("above 100k") == normalize_title("above 150k")


def test_a_year_is_a_date_not_a_threshold():
    """`2025` is a year; it must not turn into a numeric threshold."""
    assert extract_thresholds("Will it happen in 2025?") == []
    assert DATE_TOKEN in normalize_title("Will it happen in 2025?")


# --------------------------------------------------------------------
# score_pair
# --------------------------------------------------------------------


def test_near_identical_titles_same_close_score_at_least_0_85():
    """Two venues' wordings of one event, same close time.

    Both titles normalize to the same token set
    (`bitcoin above <num> <date>`), both state 100,000, both name the
    same source, and the close times are equal:

        title_jaccard = 1.0, close_score = 1.0,
        threshold_score = 1.0, source_score = 1.0
        0.55x1 + 0.20x1 + 0.15x1 + 0.10x1 = 1.00
    """
    a = market(
        "polymarket",
        "PM-BTC",
        "Will Bitcoin be above $100,000 on December 31, 2025?",
        source="CoinDesk BPI",
    )
    b = market(
        "kalshi",
        "KX-BTC",
        "Bitcoin above 100K by Dec 31 2025",
        source="coindesk bpi",
    )
    evidence = score_pair(a, b)
    assert evidence.title_jaccard == 1.0
    assert evidence.close_delta_h == 0.0
    assert evidence.threshold_match is True
    assert evidence.source_match is True
    assert evidence.confidence == pytest.approx(1.0, abs=1e-9)
    assert evidence.confidence >= 0.85


def test_same_title_ten_days_apart_scores_below_0_7():
    """Identical wording cannot carry a ten-day close-time gap.

        |delta| = 240h  ->  close_score = max(0, 1 - 240/48) = 0.0
        neither title states a number       -> threshold_score = 0.5
        neither venue names a source        -> source_score    = 0.5
        0.55x1.0 + 0.20x0.0 + 0.15x0.5 + 0.10x0.5
          = 0.55 + 0.0 + 0.075 + 0.05 = 0.675
    """
    question = "Will the Fed cut rates at its next meeting?"
    a = market("polymarket", "PM-FED", question)
    b = market("kalshi", "KX-FED", question, close=CLOSE + timedelta(days=10))
    evidence = score_pair(a, b)
    assert evidence.title_jaccard == 1.0
    assert evidence.close_delta_h == pytest.approx(240.0)
    assert evidence.confidence == pytest.approx(0.675, abs=1e-9)
    assert evidence.confidence < 0.7


def test_equivalent_thresholds_match_across_spellings():
    """"above 100k" and ">= 100,000" state the same strike."""
    a = market("polymarket", "PM-1", "Will Bitcoin close above 100k on Dec 31 2025?")
    b = market("kalshi", "KX-1", "Will Bitcoin close ≥ 100,000 on Dec 31 2025?")
    evidence = score_pair(a, b)
    assert evidence.threshold_match is True
    assert evidence.thresholds_a == (100_000.0,)
    assert evidence.thresholds_b == (100_000.0,)


def test_disagreeing_thresholds_are_vetoed_below_the_proposal_floor():
    """"above 100k" and "above 150k" are the same sentence, different events.

    On wording alone the weighted sum is high, because the number itself
    normalizes to `<num>` on both sides:

        title_jaccard = 1.0, close_score = 1.0,
        threshold_score = 0.0, source_score = 0.5 (neither names one)
        0.55x1 + 0.20x1 + 0.15x0 + 0.10x0.5 = 0.80

    A disagreeing threshold is a contradiction, not a weak signal, so it
    caps the result at `THRESHOLD_MISMATCH_CAP` — below the default
    `min_confidence`, so the pair is never even proposed.
    """
    a = market("polymarket", "PM-1", "Will Bitcoin close above 100k on Dec 31 2025?")
    b = market("kalshi", "KX-2", "Will Bitcoin close above 150k on Dec 31 2025?")
    evidence = score_pair(a, b)
    assert evidence.threshold_match is False
    assert evidence.threshold_capped is True
    assert evidence.confidence == pytest.approx(THRESHOLD_MISMATCH_CAP)
    assert evidence.confidence < 0.5
    assert propose_links([a], [b]) == []


def test_missing_information_scores_half_not_zero():
    """A tri-state `None` is ignorance, not disagreement.

    One venue naming no resolution source must not be punished like two
    venues naming DIFFERENT ones — that would systematically suppress
    legitimate pairs wherever one venue is simply terse.

        unknown source: 0.55x1 + 0.20x1 + 0.15x0.5 + 0.10x0.5 = 0.875
        disagreeing:    0.55x1 + 0.20x1 + 0.15x0.5 + 0.10x0.0 = 0.825
    """
    assert tri_state_score(None) == UNKNOWN_SCORE == 0.5
    assert tri_state_score(True) == 1.0
    assert tri_state_score(False) == 0.0

    question = "Will the Fed cut rates at its next meeting?"
    unknown = score_pair(
        market("polymarket", "PM-1", question),
        market("kalshi", "KX-1", question, source=None),
    )
    disagreeing = score_pair(
        market("polymarket", "PM-1", question, source="Federal Reserve H.15"),
        market("kalshi", "KX-1", question, source="Bloomberg terminal print"),
    )
    assert unknown.source_match is None
    assert disagreeing.source_match is False
    assert unknown.confidence == pytest.approx(0.875, abs=1e-9)
    assert disagreeing.confidence == pytest.approx(0.825, abs=1e-9)
    assert unknown.confidence > disagreeing.confidence


def test_one_sided_threshold_is_unknown_not_disagreement():
    """One title stating a number and the other none is not a conflict."""
    assert compare_thresholds([], [100_000.0]) is None
    assert compare_thresholds([100_000.0], []) is None
    assert compare_thresholds([100_000.0], [100_000.0]) is True
    assert compare_thresholds([100_000.0], [150_000.0]) is False
    # Partial overlap is ambiguous (one title mentions an extra number),
    # so it must NOT trigger the veto.
    assert compare_thresholds([100_000.0], [100_000.0, 5.0]) is None


def test_evidence_records_the_components_not_just_the_score():
    """A bare confidence number is unreviewable."""
    a = market("polymarket", "PM-1", "Will Bitcoin close above 100k on Dec 31 2025?")
    b = market("kalshi", "KX-1", "Will Ethereum close above 100k on Dec 31 2025?")
    blob = score_pair(a, b).as_dict()
    for key in (
        "title_jaccard",
        "close_delta_h",
        "threshold_match",
        "source_match",
        "close_score",
        "threshold_score",
        "source_score",
        "confidence",
        "weights",
        "shared_tokens",
        "distinct_tokens",
        "needs_outcome_map",
    ):
        assert key in blob, key
    # The reviewer can see exactly which words differed.
    assert "bitcoin" in blob["distinct_tokens"]
    assert "ethereum" in blob["distinct_tokens"]
    assert blob["weights"] == {
        "title": 0.55,
        "close": 0.20,
        "threshold": 0.15,
        "source": 0.10,
    }


def test_score_pair_is_symmetric():
    """Which venue was scanned first cannot change the score."""
    a = market("polymarket", "PM-1", "Will Bitcoin close above 100k on Dec 31 2025?")
    b = market("kalshi", "KX-1", "Bitcoin above 100,000 by Dec 31 2025", close=CLOSE)
    assert score_pair(a, b).confidence == score_pair(b, a).confidence


# --------------------------------------------------------------------
# outcome maps
# --------------------------------------------------------------------


def test_binary_pair_gets_a_canonical_outcome_map():
    """Gamma's "Yes"/"No" and Kalshi's "YES"/"NO" both map canonically.

    `Position.position_id` is `f"{venue}:{market_id}:{outcome}"` and is
    case-SENSITIVE, so a map stored as `{"Yes": "YES"}` would build a leg
    matching no position at all.
    """
    a = market("polymarket", "PM-1", "Will it rain in Seattle?", outcomes=("Yes", "No"))
    b = market("kalshi", "KX-1", "Will it rain in Seattle?", outcomes=("YES", "NO"))
    evidence = score_pair(a, b)
    assert evidence.outcome_map == {"YES": "YES", "NO": "NO"}
    assert evidence.needs_outcome_map is False


def test_multi_outcome_pair_refuses_to_guess_an_outcome_map():
    """A non-binary side yields `{}` plus `needs_outcome_map`."""
    question = "Which party controls the Senate after the 2028 election?"
    a = market(
        "polymarket", "PM-SEN", question, outcomes=("Democrats", "Republicans")
    )
    b = market("kalshi", "KX-SEN", question)
    evidence = score_pair(a, b)
    assert evidence.outcome_map == {}
    assert evidence.needs_outcome_map is True
    assert evidence.as_dict()["needs_outcome_map"] is True


# --------------------------------------------------------------------
# blocking
# --------------------------------------------------------------------


def _blocking_corpus() -> tuple[list, list]:
    """A corpus with overlaps, near-misses and disjoint subjects."""
    subjects_a = [
        "Will Bitcoin close above 100k on Dec 31 2025?",
        "Will Ethereum close above 10k on Dec 31 2025?",
        "Will the Fed cut rates at its next meeting?",
        "Will the Senate confirm the nominee?",
        "Will the hurricane make landfall in Florida?",
        "Will the spacecraft reach orbit on Dec 31 2025?",
        "Will unemployment fall under 4 percent?",
        "Will the treaty be ratified?",
    ]
    subjects_b = [
        "Bitcoin above 100,000 by Dec 31 2025",
        "Will the Fed raise rates at its next meeting?",
        "Will the Senate reject the nominee?",
        "Will a hurricane hit Texas?",
        "Will inflation fall under 3 percent?",
        "Will Ethereum trade above 10,000 by Dec 31 2025?",
        # Shares ONLY the date/number placeholders with everything else.
        "Resolves on Dec 31 2025 at 5",
        "Will the referendum carry?",
    ]
    return (
        [market("polymarket", f"PM-{i}", q) for i, q in enumerate(subjects_a)],
        [market("kalshi", f"KX-{i}", q) for i, q in enumerate(subjects_b)],
    )


def test_blocking_never_drops_a_pair_that_shares_a_content_token():
    """The blocking index must agree exactly with a brute-force scan.

    A blocking step that silently discards a true pair is worse than a
    quadratic scan: the miss is invisible — no exception, no counter,
    just an equivalence a reviewer never saw.
    """
    markets_a, markets_b = _blocking_corpus()
    blocked = set(candidate_pairs(markets_a, markets_b))
    brute_force = {
        (i, j)
        for i, ma in enumerate(markets_a)
        for j, mb in enumerate(markets_b)
        if content_tokens(ma.question) & content_tokens(mb.question)
    }
    assert blocked == brute_force
    assert blocked, "the corpus must contain overlapping pairs to be a real test"
    # ...and it really is blocking, not just returning the cross product.
    assert len(blocked) < len(markets_a) * len(markets_b)


def test_blocking_excludes_a_pair_whose_only_overlap_is_a_placeholder():
    """"has a date and a number" is not evidence of anything.

    Nearly every market question carries a date, so indexing on `<date>`
    would put the whole corpus in one bucket and buy nothing. This is the
    ONE documented exclusion, asserted here so it stays deliberate.
    """
    a = market("polymarket", "PM-1", "Will the treaty be ratified on Dec 31 2025?")
    b = market("kalshi", "KX-1", "Resolves on Dec 31 2025 at 5")
    assert content_tokens(a.question) & content_tokens(b.question) == frozenset()
    assert candidate_pairs([a], [b]) == []


def test_blocking_keeps_a_pair_sharing_exactly_one_content_word():
    """One shared subject word is enough to reach the scorer."""
    a = market("polymarket", "PM-1", "Will the hurricane make landfall in Florida?")
    b = market("kalshi", "KX-1", "Will a hurricane hit Texas?")
    assert candidate_pairs([a], [b]) == [(0, 0)]


# --------------------------------------------------------------------
# propose_links
# --------------------------------------------------------------------


def test_matcher_never_advances_a_link_past_proposed():
    """PLAN.md D9/R1: the matcher proposes, a human decides.

    Every row `propose_links` can produce is `"proposed"`. Promotion is a
    human act performed through `/links/{id}/approve`; a matcher that
    could promote its own guess would turn a token-overlap score into a
    real-money position on two contracts that may settle differently.
    """
    markets_a, markets_b = _blocking_corpus()
    links = propose_links(markets_a, markets_b, min_confidence=0.0)
    assert links, "the corpus must produce proposals for this test to mean anything"
    assert {link.status for link in links} == {"proposed"}
    assert all(link.reviewed_by is None for link in links)
    assert all(link.reviewed_at is None for link in links)


def test_propose_links_respects_the_confidence_floor_and_orders_by_score():
    """Only pairs at or above `min_confidence` are filed, best first."""
    markets_a, markets_b = _blocking_corpus()
    links = propose_links(markets_a, markets_b, min_confidence=0.5)
    assert links
    assert all(link.confidence >= 0.5 for link in links)
    confidences = [link.confidence for link in links]
    assert confidences == sorted(confidences, reverse=True)


def test_propose_links_puts_a_pair_in_the_same_column_order_either_way():
    """`event_links` is UNIQUE on the four columns; the order must be stable.

    Otherwise re-running the proposer with the venue lists swapped files
    a second row for the same equivalence and a reviewer decides the same
    pair twice.
    """
    a = market(
        "polymarket",
        "PM-BTC",
        "Will Bitcoin be above $100,000 on December 31, 2025?",
    )
    b = market("kalshi", "KX-BTC", "Bitcoin above 100K by Dec 31 2025")
    forward = propose_links([a], [b])
    backward = propose_links([b], [a])
    assert len(forward) == len(backward) == 1
    key = ("venue_a", "market_a", "venue_b", "market_b")
    assert [getattr(forward[0], f) for f in key] == [
        getattr(backward[0], f) for f in key
    ]
    assert forward[0].venue_a == "kalshi"  # sorts before "polymarket"


def test_propose_links_emits_one_row_per_pair():
    """`event_links` is UNIQUE on the four columns, so this must be too.

    A venue that listed the same market twice would otherwise produce two
    rows for one equivalence and the insert would fail at the database.
    """
    a = market("polymarket", "PM-BTC", BTC_PM)
    b = market("kalshi", "KX-BTC", BTC_KX)
    links = propose_links([a, a], [b, b], min_confidence=0.0)
    assert len(links) == 1


def test_propose_links_never_pairs_a_market_with_itself():
    """Handed the same list twice, a market is not evidence for itself."""
    a = market("polymarket", "PM-1", "Will Bitcoin close above 100k on Dec 31 2025?")
    links = propose_links([a], [a], min_confidence=0.0)
    assert links == []


# --------------------------------------------------------------------
# /links review API
# --------------------------------------------------------------------

@pytest.fixture
def fixture_adapters() -> dict:
    """Two in-memory read adapters, one per venue (no network).

    Each venue carries one binary BTC market that should link, and one
    Senate market whose Polymarket side is multi-outcome — so the matcher
    proposes the pair but refuses to guess its outcome map.
    """
    polymarket = FixtureAdapter("polymarket")
    polymarket.add_market(
        market(
            "polymarket",
            "PM-BTC",
            BTC_PM,
            source="CoinDesk BPI at 4pm ET",
            rules="Resolves YES if the CoinDesk BPI close exceeds $100,000.",
        )
    )
    polymarket.add_market(
        market(
            "polymarket",
            "PM-SEN",
            SENATE,
            outcomes=("Democrats", "Republicans"),
            rules="Resolves to the party holding 51 seats at swearing-in.",
        )
    )
    kalshi = FixtureAdapter("kalshi")
    kalshi.add_market(
        market(
            "kalshi",
            "KX-BTC",
            BTC_KX,
            source="CoinDesk BPI at 5pm ET",
            rules="Settles YES if the CoinDesk BPI print at 5pm ET exceeds 100000.",
        )
    )
    kalshi.add_market(
        market(
            "kalshi",
            "KX-SEN",
            SENATE,
            rules="Settles YES if the Democratic caucus holds 51 seats.",
        )
    )
    return {"polymarket": polymarket, "kalshi": kalshi}


@pytest_asyncio.fixture
async def client(
    test_session: AsyncSession, fixture_adapters: dict
) -> AsyncGenerator[AsyncClient, None]:
    """The real app with the DB session and both adapters overridden.

    Shadows `tests/conftest.py`'s `client` for this module only (a
    standard pytest fixture-resolution rule): the matcher route needs
    `get_market_data_adapters` replaced too, or it would reach for the
    registry and construct a real venue client.
    """

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        yield test_session

    async def override_adapters() -> dict:
        return fixture_adapters

    app.dependency_overrides[get_async_session] = override_session
    app.dependency_overrides[get_market_data_adapters] = override_adapters
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def _propose(client: AsyncClient) -> dict:
    """Run `POST /links/propose` and return the decoded body."""
    response = await client.post("/api/v1/links/propose")
    assert response.status_code == 200, response.text
    return response.json()


async def _link_id(client: AsyncClient, market_a: str, market_b: str) -> int:
    """Return the id of the link joining two given market ids."""
    listing = (await client.get("/api/v1/links")).json()["links"]
    for link in listing:
        if {link["market_a"], link["market_b"]} == {market_a, market_b}:
            return int(link["id"])
    raise AssertionError(f"no link for {market_a}/{market_b} in {listing}")


async def test_propose_files_proposals_and_lists_them(client: AsyncClient):
    """`POST /links/propose` writes the review queue; `GET /links` reads it."""
    body = await _propose(client)
    assert body["scanned_a"] == 2
    assert body["scanned_b"] == 2
    assert body["created"] >= 1
    assert {link["status"] for link in body["links"]} == {"proposed"}

    queue = (await client.get("/api/v1/links", params={"status": "proposed"})).json()
    assert len(queue["links"]) == body["created"]
    assert (await client.get("/api/v1/links", params={"status": "approved"})).json()[
        "links"
    ] == []


async def test_propose_is_idempotent_and_never_overwrites_a_review(
    client: AsyncClient, test_session: AsyncSession
):
    """A re-run refreshes unreviewed rows and leaves decided ones alone.

    Rescoring a decided link would discard the only thing in this table a
    machine cannot reproduce: a person's reading of two rules texts.
    """
    first = await _propose(client)
    btc_id = await _link_id(client, "PM-BTC", "KX-BTC")

    approved = await client.post(
        f"/api/v1/links/{btc_id}/approve",
        json={"reviewed_by": "reviewer@example.com", "notes": "same 5pm ET print"},
    )
    assert approved.status_code == 200, approved.text

    second = await _propose(client)
    assert second["created"] == 0
    assert second["skipped_reviewed"] >= 1
    assert second["updated"] == len(first["links"]) - second["skipped_reviewed"]

    row = await test_session.get(EventLink, btc_id)
    assert row is not None
    assert row.status == "approved"
    assert row.reviewed_by == "reviewer@example.com"
    assert row.notes == "same 5pm ET print"


async def test_get_link_returns_both_rules_texts_side_by_side(client: AsyncClient):
    """The review surface: same fields, same order, offsets visible."""
    await _propose(client)
    btc_id = await _link_id(client, "PM-BTC", "KX-BTC")

    body = (await client.get(f"/api/v1/links/{btc_id}")).json()
    assert body["market_a"] is not None
    assert body["market_b"] is not None

    # Both rules texts are present and comparable, which is what lets a
    # human catch a settlement-wording difference no token overlap sees.
    assert (
        body["market_a"]["rules_text"] != body["market_b"]["rules_text"]
    ), "the fixture pair must differ so the comparison is meaningful"
    assert "CoinDesk BPI" in (
        body["market_a"]["rules_text"] + body["market_b"]["rules_text"]
    )

    fields = [row["field"] for row in body["comparison"]]
    assert fields == [
        "venue",
        "market_id",
        "question",
        "close_time",
        "expected_settle_time",
        "resolution_source",
        "outcomes",
        "status",
        "rules_text",
    ]
    by_field = {row["field"]: row for row in body["comparison"]}
    assert by_field["close_time"]["same"] is True
    assert by_field["close_time"]["a"].endswith("+00:00")  # offset is visible
    assert by_field["close_time"]["b"].endswith("+00:00")
    assert by_field["rules_text"]["same"] is False
    assert by_field["resolution_source"]["same"] is False

    # A link that no one has approved says so, in the response.
    assert any("nothing may trade" in warning for warning in body["warnings"])


async def test_approve_then_reject_records_who_and_when(client: AsyncClient):
    """The full human flow, and the fields only a human may write."""
    await _propose(client)
    btc_id = await _link_id(client, "PM-BTC", "KX-BTC")

    approved = (
        await client.post(
            f"/api/v1/links/{btc_id}/approve",
            json={"reviewed_by": "alice", "notes": "checked both rules texts"},
        )
    ).json()
    assert approved["status"] == "approved"
    assert approved["reviewed_by"] == "alice"
    assert approved["reviewed_at"] is not None
    assert approved["outcome_map"] == {"YES": "YES", "NO": "NO"}

    rejected = (
        await client.post(
            f"/api/v1/links/{btc_id}/reject",
            json={
                "reviewed_by": "bob",
                "notes": "Kalshi settles on the 5pm ET print, Polymarket on 4pm ET",
            },
        )
    ).json()
    assert rejected["status"] == "rejected"
    assert rejected["reviewed_by"] == "bob"
    assert "5pm ET" in rejected["notes"]

    assert (await client.get("/api/v1/links", params={"status": "approved"})).json()[
        "links"
    ] == []


async def test_approving_a_multi_outcome_pair_without_a_map_is_422(
    client: AsyncClient,
):
    """An approved link with no outcome map is worse than no link.

    T18 would build legs whose `f"{venue}:{market_id}:{outcome}"`
    position ids match nothing, so the matcher's refusal to guess has to
    survive all the way to the approval gate.
    """
    await _propose(client)
    senate_id = await _link_id(client, "PM-SEN", "KX-SEN")

    detail = await client.get(f"/api/v1/links/{senate_id}")
    assert detail.json()["link"]["outcome_map"] == {}
    assert any(
        "could not derive an outcome map" in warning
        for warning in detail.json()["warnings"]
    )

    refused = await client.post(
        f"/api/v1/links/{senate_id}/approve",
        json={"reviewed_by": "alice", "notes": ""},
    )
    assert refused.status_code == 422, refused.text
    assert "outcome_map" in refused.json()["detail"]

    listing = (await client.get("/api/v1/links")).json()["links"]
    senate = next(link for link in listing if link["id"] == senate_id)
    assert senate["status"] == "proposed"


async def test_approving_with_a_supplied_map_canonicalizes_its_casing(
    client: AsyncClient,
):
    """A reviewer-typed map is canonicalized before it is stored."""
    await _propose(client)
    senate_id = await _link_id(client, "PM-SEN", "KX-SEN")

    approved = await client.post(
        f"/api/v1/links/{senate_id}/approve",
        json={
            "reviewed_by": "alice",
            "notes": "Democrats == YES on the Kalshi side",
            "outcome_map": {"Democrats": "yes", "Republicans": "No"},
        },
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["outcome_map"] == {
        "Democrats": "YES",
        "Republicans": "NO",
    }


async def test_review_routes_404_on_an_unknown_link(client: AsyncClient):
    """Every review route reports a missing link the same way."""
    assert (await client.get("/api/v1/links/9999")).status_code == 404
    assert (
        await client.post(
            "/api/v1/links/9999/approve", json={"reviewed_by": "alice"}
        )
    ).status_code == 404
    assert (
        await client.post("/api/v1/links/9999/reject", json={"reviewed_by": "alice"})
    ).status_code == 404


async def test_reviewer_is_required(client: AsyncClient):
    """An anonymous approval is not a review."""
    await _propose(client)
    btc_id = await _link_id(client, "PM-BTC", "KX-BTC")
    assert (
        await client.post(
            f"/api/v1/links/{btc_id}/approve", json={"reviewed_by": ""}
        )
    ).status_code == 422
