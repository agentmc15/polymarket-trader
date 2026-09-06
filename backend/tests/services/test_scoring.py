"""T19 — `app.services.scoring` (PLAN.md D10).

Derived from the T19 brief/acceptance in `.claude/kits/market-edge/TASKS.md`
and PLAN.md D10, not by reading `app/services/scoring.py` and mirroring it
back. Every money/percentage figure is computed BY HAND in the test body
(GUARDRAILS.md §5).

No network, no live mode, no real order (GUARDRAILS.md §1.1/§1.2/§1.4):
every market/book here is a hand-built fixture via
`tests/venues/fixture_adapter.make_venue_market` and `tests/helpers
.make_book`, never a real adapter.
"""
from datetime import timedelta

import pytest

from app.config import settings
from app.services.scoring import (
    OpportunityScore,
    ScoreContext,
    UnscorableIntent,
    score,
)
from app.strategies.base import Intent, Leg
from app.strategies.multi_outcome_bundle_arbitrage import (
    MultiOutcomeBundleArbitrageStrategy,
)
from app.utils.time import utcnow
from app.venues.types import BookLevel, OrderBook
from tests.helpers import make_book, make_snapshot
from tests.venues.fixture_adapter import make_venue_market

PM = "polymarket"
KX = "kalshi"
LONG_RULES_TEXT = "This market resolves according to the stated rules. " * 5  # 260 chars


def _open_market(venue=PM, market_id="M1", **overrides):
    """A `VenueMarket` fixture with well-documented rules by default.

    Overriding `rules_text`/`resolution_source` is how a test isolates
    ONE `resolution_risk` penalty at a time — the defaults here are
    deliberately "good" (long rules text, a named source) so a test that
    does not care about those two penalties does not accidentally add
    them.
    """
    fields = {
        "rules_text": LONG_RULES_TEXT,
        "resolution_source": "Official Source",
        "close_time": utcnow() + timedelta(days=30),
    }
    fields.update(overrides)
    return make_venue_market(venue, market_id, **fields)


def test_hand_computed_composite_for_a_complement_intent():
    """One complement intent, every field computed by hand.

    net_edge = 0.02 (published by the strategy in metadata["edge"]).
    hours_to_resolution = 100 (well above the 6h floor and the 6h dispute
    window).
    annualized_return = 0.02 / 100 * 8760 = 1.752.
    fill_confidence = 1.0 (both legs' books fully cover the requested 100
    contracts at exactly the limit price).
    resolution_risk = 0.15 base only (rules_text is 260 chars >= 200,
    resolution_source is set, kind != cross_venue, hours >= 6).
    composite = 1.752 * 1.0 * (1 - 0.15) = 1.4892.
    capital_lockup_usd = 100*0.46 + 100*0.50 = 96.0.
    """
    now = utcnow()
    market = _open_market()
    intent = Intent(
        kind="complement",
        legs=[
            Leg(
                market_id="M1", outcome="YES", side="BUY", limit_price=0.46,
                size_contracts=100.0, venue=PM,
            ),
            Leg(
                market_id="M1", outcome="NO", side="BUY", limit_price=0.50,
                size_contracts=100.0, venue=PM,
            ),
        ],
        hold_to_resolution=True,
        atomicity="all_or_none",
        confidence=0.9,
        expected_resolution_ts=now + timedelta(hours=100),
        metadata={"edge": 0.02},
    )
    ctx = ScoreContext(
        now=now,
        books={
            (PM, "M1", "YES"): make_book(
                bids=[(0.44, 100.0)], asks=[(0.46, 100.0)], venue=PM, market_id="M1", outcome="YES"
            ),
            (PM, "M1", "NO"): make_book(
                bids=[(0.48, 100.0)], asks=[(0.50, 100.0)], venue=PM, market_id="M1", outcome="NO"
            ),
        },
        markets={(PM, "M1"): market},
        settings=settings,
    )

    result = score(intent, ctx)

    assert isinstance(result, OpportunityScore)
    assert result.net_edge == pytest.approx(0.02)
    assert result.hours_to_resolution == pytest.approx(100.0)
    assert result.annualized_return == pytest.approx(0.02 / 100.0 * 8760.0)
    assert result.annualized_return == pytest.approx(1.752)
    assert result.fill_confidence == pytest.approx(1.0)
    assert result.resolution_risk == pytest.approx(0.15)
    assert result.composite == pytest.approx(1.752 * 1.0 * 0.85)
    assert result.composite == pytest.approx(1.4892)
    assert result.capital_lockup_usd == pytest.approx(96.0)
    assert result.depth_source == "recorded"
    assert result.link_status is None


def _single_leg_intent(*, hours: float, market_id: str = "M1", size: float = 100.0):
    now = utcnow()
    return now, Intent(
        kind="single",
        legs=[
            Leg(
                market_id=market_id, outcome="YES", side="BUY", limit_price=0.50,
                size_contracts=size, venue=PM,
            ),
        ],
        hold_to_resolution=False,
        atomicity="best_effort",
        confidence=0.5,
        expected_resolution_ts=now + timedelta(hours=hours),
        metadata={"edge": 0.03},
    )


def test_more_hours_to_resolution_yields_lower_annualized_return():
    """Monotonicity: holding `net_edge` fixed, more hours -> lower annualized_return."""
    market = _open_market()
    book = make_book(bids=[(0.48, 100.0)], asks=[(0.50, 100.0)], venue=PM, market_id="M1", outcome="YES")

    now_short, intent_short = _single_leg_intent(hours=50.0)
    ctx_short = ScoreContext(
        now=now_short, books={(PM, "M1", "YES"): book}, markets={(PM, "M1"): market}, settings=settings
    )
    now_long, intent_long = _single_leg_intent(hours=500.0)
    ctx_long = ScoreContext(
        now=now_long, books={(PM, "M1", "YES"): book}, markets={(PM, "M1"): market}, settings=settings
    )

    short_result = score(intent_short, ctx_short)
    long_result = score(intent_long, ctx_long)

    assert short_result.annualized_return == pytest.approx(0.03 / 50.0 * 8760.0)
    assert long_result.annualized_return == pytest.approx(0.03 / 500.0 * 8760.0)
    assert short_result.annualized_return > long_result.annualized_return


def test_thinner_book_yields_lower_fill_confidence():
    """Monotonicity: same intent, thinner book -> lower fill_confidence.

    100 contracts requested at limit 0.50. A book offering the full 100
    at 0.50 fills completely (1.0); one offering only 40 fills 40/100 =
    0.4 before it runs dry.
    """
    market = _open_market()
    now, intent = _single_leg_intent(hours=48.0)
    thick_book = make_book(
        bids=[(0.48, 100.0)], asks=[(0.50, 100.0)], venue=PM, market_id="M1", outcome="YES"
    )
    thin_book = make_book(
        bids=[(0.48, 100.0)], asks=[(0.50, 40.0)], venue=PM, market_id="M1", outcome="YES"
    )

    thick_ctx = ScoreContext(
        now=now, books={(PM, "M1", "YES"): thick_book}, markets={(PM, "M1"): market}, settings=settings
    )
    thin_ctx = ScoreContext(
        now=now, books={(PM, "M1", "YES"): thin_book}, markets={(PM, "M1"): market}, settings=settings
    )

    thick_result = score(intent, thick_ctx)
    thin_result = score(intent, thin_ctx)

    assert thick_result.fill_confidence == pytest.approx(1.0)
    assert thin_result.fill_confidence == pytest.approx(40.0 / 100.0)
    assert thin_result.fill_confidence == pytest.approx(0.4)
    assert thick_result.fill_confidence > thin_result.fill_confidence


def test_annualized_return_is_floored_below_min_hours_for_annualization():
    """Below `settings.min_hours_for_annualization` (6h default), the floor engages.

    A market resolving in 1 hour reports the SAME annualized_return as
    one resolving in exactly 6 hours (both computed against the 6h
    floor) — not a 6x-larger number from dividing by 1. The raw
    (unfloored) `hours_to_resolution` still differs and is reported as
    such.
    """
    market = _open_market()
    book = make_book(bids=[(0.48, 100.0)], asks=[(0.50, 100.0)], venue=PM, market_id="M1", outcome="YES")

    now_1h, intent_1h = _single_leg_intent(hours=1.0)
    ctx_1h = ScoreContext(
        now=now_1h, books={(PM, "M1", "YES"): book}, markets={(PM, "M1"): market}, settings=settings
    )
    now_6h, intent_6h = _single_leg_intent(hours=6.0)
    ctx_6h = ScoreContext(
        now=now_6h, books={(PM, "M1", "YES"): book}, markets={(PM, "M1"): market}, settings=settings
    )

    result_1h = score(intent_1h, ctx_1h)
    result_6h = score(intent_6h, ctx_6h)

    expected_floored = 0.03 / 6.0 * 8760.0
    assert result_1h.annualized_return == pytest.approx(expected_floored)
    assert result_6h.annualized_return == pytest.approx(expected_floored)
    assert result_1h.annualized_return == pytest.approx(result_6h.annualized_return)
    # The raw field is NOT floored -- 1h and 6h stay visibly different.
    assert result_1h.hours_to_resolution == pytest.approx(1.0)
    assert result_6h.hours_to_resolution == pytest.approx(6.0)
    # And the dispute-window penalty (< 6h) correctly still fires at 1h
    # but not at 6h (the comparison is strict `<`).
    assert result_1h.resolution_risk == pytest.approx(0.15 + 0.15)
    assert result_6h.resolution_risk == pytest.approx(0.15)


def _cross_venue_intent(*, confidence: float):
    now = utcnow()
    intent = Intent(
        kind="cross_venue",
        legs=[
            Leg(market_id="M1", outcome="YES", side="BUY", limit_price=0.46, size_contracts=50.0, venue=PM),
            Leg(market_id="K1", outcome="NO", side="BUY", limit_price=0.50, size_contracts=50.0, venue=KX),
        ],
        hold_to_resolution=True,
        atomicity="all_or_none",
        confidence=confidence,
        expected_resolution_ts=now + timedelta(hours=200),
        metadata={"net_edge": 0.01, "link_id": 7},
    )
    return now, intent


def _cross_venue_ctx(now):
    return ScoreContext(
        now=now,
        books={
            (PM, "M1", "YES"): make_book(
                bids=[(0.44, 50.0)], asks=[(0.46, 50.0)], venue=PM, market_id="M1", outcome="YES"
            ),
            (KX, "K1", "NO"): make_book(
                bids=[(0.48, 50.0)], asks=[(0.50, 50.0)], venue=KX, market_id="K1", outcome="NO"
            ),
        },
        markets={
            (PM, "M1"): _open_market(PM, "M1"),
            (KX, "K1"): _open_market(KX, "K1"),
        },
        settings=settings,
    )


def test_cross_venue_confidence_below_095_adds_the_measured_penalty():
    """PLAN.md D10: cross_venue + confidence < 0.95 adds +0.25 to resolution_risk.

    This is the economically load-bearing term (see the module
    docstring's worked cross-venue example): below 0.95 confidence, a
    cross-venue pair's gross edge usually cannot cover the
    resolution-mismatch haircut at all.
    """
    now_low, intent_low = _cross_venue_intent(confidence=0.90)
    result_low = score(intent_low, _cross_venue_ctx(now_low))
    assert result_low.resolution_risk == pytest.approx(0.15 + 0.25)

    now_high, intent_high = _cross_venue_intent(confidence=0.95)
    result_high = score(intent_high, _cross_venue_ctx(now_high))
    # Strictly `< 0.95` -- exactly 0.95 does NOT trip the penalty.
    assert result_high.resolution_risk == pytest.approx(0.15)

    assert result_low.resolution_risk > result_high.resolution_risk


def test_link_status_rides_through_from_metadata():
    """`OpportunityScore.link_status` mirrors `intent.metadata["link_status"]` verbatim.

    PLAN.md D9: a `"proposed"` link may be SCORED (shown) in paper mode
    even though nothing may execute on it -- the payload must carry that
    label, not just a log line.
    """
    now, intent = _cross_venue_intent(confidence=0.99)
    intent.metadata["link_status"] = "proposed"

    result = score(intent, _cross_venue_ctx(now))

    assert result.link_status == "proposed"


def test_depth_source_is_recorded_only_when_every_leg_book_is_recorded():
    now, intent = _cross_venue_intent(confidence=0.99)
    result = score(intent, _cross_venue_ctx(now))
    assert result.depth_source == "recorded"


def test_depth_source_is_mixed_when_legs_disagree():
    now, intent = _cross_venue_intent(confidence=0.99)
    ctx = _cross_venue_ctx(now)
    synthetic_book = OrderBook(
        venue=KX,
        market_id="K1",
        outcome="NO",
        bids=(BookLevel(price=0.48, size=50.0),),
        asks=(BookLevel(price=0.50, size=50.0),),
        ts=now,
        metadata={"depth_source": "synthetic"},
    )
    mixed_books = dict(ctx.books)
    mixed_books[(KX, "K1", "NO")] = synthetic_book
    mixed_ctx = ScoreContext(now=now, books=mixed_books, markets=ctx.markets, settings=settings)

    result = score(intent, mixed_ctx)

    assert result.depth_source == "mixed"


def test_depth_source_defaults_to_synthetic_when_no_book_was_found():
    """No leg's book is on record at all -> the conservative default, not 'recorded'."""
    now, intent = _cross_venue_intent(confidence=0.99)
    ctx = ScoreContext(
        now=now,
        books={},
        markets={(PM, "M1"): _open_market(PM, "M1"), (KX, "K1"): _open_market(KX, "K1")},
        settings=settings,
    )

    result = score(intent, ctx)

    assert result.depth_source == "synthetic"
    # No book at all also means nothing was judged fillable.
    assert result.fill_confidence == pytest.approx(0.0)


def test_resolved_market_is_never_scored():
    """T09: no fill can occur on a resolved market -- score() refuses it."""
    now, intent = _single_leg_intent(hours=48.0)
    resolved_market = _open_market(status="resolved", result="YES")
    ctx = ScoreContext(
        now=now,
        books={(PM, "M1", "YES"): make_book(bids=[(0.48, 100.0)], asks=[(0.50, 100.0)], venue=PM, market_id="M1", outcome="YES")},
        markets={(PM, "M1"): resolved_market},
        settings=settings,
    )

    with pytest.raises(UnscorableIntent):
        score(intent, ctx)


def test_market_past_close_time_is_never_scored():
    now, intent = _single_leg_intent(hours=48.0)
    past_close_market = _open_market(close_time=now - timedelta(hours=1))
    ctx = ScoreContext(
        now=now,
        books={(PM, "M1", "YES"): make_book(bids=[(0.48, 100.0)], asks=[(0.50, 100.0)], venue=PM, market_id="M1", outcome="YES")},
        markets={(PM, "M1"): past_close_market},
        settings=settings,
    )

    with pytest.raises(UnscorableIntent):
        score(intent, ctx)


def test_missing_expected_resolution_ts_is_never_scored():
    """PLAN.md D10: time-to-resolution is a scoring axis on every intent."""
    now = utcnow()
    market = _open_market()
    intent = Intent(
        kind="single",
        legs=[Leg(market_id="M1", outcome="YES", side="BUY", limit_price=0.5, size_contracts=10.0, venue=PM)],
        hold_to_resolution=False,
        atomicity="best_effort",
        confidence=0.5,
        expected_resolution_ts=None,
        metadata={},
    )
    ctx = ScoreContext(
        now=now,
        books={(PM, "M1", "YES"): make_book(bids=[(0.48, 100.0)], asks=[(0.50, 100.0)], venue=PM, market_id="M1", outcome="YES")},
        markets={(PM, "M1"): market},
        settings=settings,
    )

    with pytest.raises(UnscorableIntent):
        score(intent, ctx)


def test_bundle_intent_scores_a_nonzero_net_edge_and_composite():
    """T21b defect fix: a `multi_outcome_bundle_arbitrage` intent must
    score a real `net_edge`/`composite`, not the `0.0` it collapsed to
    before the strategy published `metadata["net_edge"]` (it previously
    published only `metadata["profit_margin"]`, a key `_net_edge` never
    read).

    The `Intent` here is produced by the REAL strategy
    (`MultiOutcomeBundleArbitrageStrategy.on_market_data`), not
    hand-assembled, so this test exercises the actual production
    metadata contract between the strategy and the scorer. Only the
    arithmetic is computed by hand (GUARDRAILS.md §5), reusing the exact
    worked example from
    `tests/strategies/test_arbitrage_intents.py::test_bundle_emits_one_leg_per_outcome`
    (4-outcome market, category unknown -> 0.05 taker rate):

        total_cost = 0.20 + 0.20 + 0.20 + 0.15 = 0.75
        fees: 3 * (0.05*0.20*0.80) + 1 * (0.05*0.15*0.85)
            = 3*0.008 + 0.006375 = 0.030375
        profit_margin = 1 - 0.75 - 0.030375 = 0.219625

    Scoring, at 100 hours to resolution (above both the 6h
    annualization floor and the 6h dispute window), rules_text >= 200
    chars, a named resolution_source, kind != "cross_venue", and a book
    on every leg deep enough to fill the full 100-contract request
    (`min_position_size` default):

        net_edge           = 0.219625            (published, unhaircut)
        annualized_return   = 0.219625 / 100 * 8760 = 19.23915
        fill_confidence     = 1.0                  (every leg's book covers it)
        resolution_risk     = 0.15                 (base only)
        composite           = 19.23915 * 1.0 * 0.85 = 16.3532775
    """
    now = utcnow()
    strategy = MultiOutcomeBundleArbitrageStrategy()
    snapshot = make_snapshot(
        market_id="M1",
        orderbook={"outcomes": {"A": 0.20, "B": 0.20, "C": 0.20, "D": 0.15}},
        end_date=now + timedelta(hours=100),
    )

    intent = strategy.on_market_data(snapshot)
    assert isinstance(intent, Intent)
    assert intent.metadata["profit_margin"] == pytest.approx(0.219625, abs=1e-9)
    # The published `net_edge` is the strategy's own computed margin,
    # verbatim -- not a different quantity under the same name.
    assert intent.metadata["net_edge"] == pytest.approx(intent.metadata["profit_margin"])

    market = _open_market(PM, "M1")
    deep_book = {
        outcome: make_book(
            bids=[(price - 0.02, 500.0)], asks=[(price, 500.0)],
            venue=PM, market_id="M1", outcome=outcome,
        )
        for outcome, price in intent.metadata["outcome_prices"].items()
    }
    ctx = ScoreContext(
        now=now,
        books={(PM, "M1", outcome): book for outcome, book in deep_book.items()},
        markets={(PM, "M1"): market},
        settings=settings,
    )

    result = score(intent, ctx)

    assert result.net_edge == pytest.approx(0.219625, abs=1e-9)
    assert result.net_edge != 0.0
    assert result.annualized_return == pytest.approx(0.219625 / 100.0 * 8760.0)
    assert result.annualized_return == pytest.approx(19.23915)
    assert result.fill_confidence == pytest.approx(1.0)
    assert result.resolution_risk == pytest.approx(0.15)
    assert result.composite == pytest.approx(19.23915 * 1.0 * 0.85)
    assert result.composite == pytest.approx(16.3532775)
    assert result.composite != 0.0
