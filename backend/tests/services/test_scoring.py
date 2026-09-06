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
from datetime import datetime, timedelta
from typing import Any

import pytest

from app.config import settings
from app.services.scoring import (
    OpportunityScore,
    ScoreContext,
    UnscorableIntent,
    score,
)
from app.strategies.base import (
    EDGE_BASIS_IDENTITY_ESTIMATED,
    EDGE_BASIS_OBSERVED,
    Intent,
    Leg,
)
from app.strategies.multi_outcome_bundle_arbitrage import (
    MultiOutcomeBundleArbitrageStrategy,
)
from app.utils.time import utcnow
from app.venues.types import BookLevel, OrderBook, VenueId, VenueMarket
from tests.helpers import make_book, make_snapshot
from tests.venues.fixture_adapter import make_venue_market

PM: VenueId = "polymarket"
KX: VenueId = "kalshi"
LONG_RULES_TEXT = "This market resolves according to the stated rules. " * 5  # 260 chars


def _open_market(
    venue: VenueId = PM, market_id: str = "M1", **overrides: Any
) -> VenueMarket:
    """A `VenueMarket` fixture with well-documented rules by default.

    Overriding `rules_text`/`resolution_source` is how a test isolates
    ONE `resolution_risk` penalty at a time — the defaults here are
    deliberately "good" (long rules text, a named source) so a test that
    does not care about those two penalties does not accidentally add
    them.
    """
    fields: dict[str, Any] = {
        "rules_text": LONG_RULES_TEXT,
        "resolution_source": "Official Source",
        "close_time": utcnow() + timedelta(days=30),
    }
    fields.update(overrides)
    return make_venue_market(venue, market_id, **fields)


def test_hand_computed_composite_for_a_complement_intent():
    """One complement intent, every field computed by hand.

    net_edge = 0.02 (published pre-risk by the strategy in
    metadata["edge"]; single market, so no identity haircut applies and
    the scored net_edge is the published edge unchanged).
    hours_to_resolution = 100 (well above the 6h floor and the 6h dispute
    window).
    units = 100 (contracts of every leg).
    capital_lockup_usd = 100*0.46 + 100*0.50 = 96.0.
    annualized_return = (0.02 * 100 / 96.0) / 100 * 8760
                      = 0.0208333... / 100 * 8760 = 1.825.
    fill_confidence = 1.0 (both legs' books fully cover the requested 100
    contracts at exactly the limit price).
    resolution_risk = 1 - (1 - 0.15)**1 = 0.15 (ONE distinct market;
    rules_text is 260 chars >= 200, resolution_source is set, hours >= 6).
    composite = 1.825 * 1.0 * (1 - 0.15) = 1.55125.

    T31 MOVED THESE NUMBERS. Before, `annualized_return` was the
    per-contract dollar edge annualized without dividing by capital:
    0.02 / 100 * 8760 = 1.752, composite 1.752 * 0.85 = 1.4892. The
    ratio is exactly 1 / 0.96 — the capital this pair actually locks up
    per unit — because $0.02 earned on $0.96 committed is a 2.083%
    return, not a 2% one, and `composite` now compares returns.
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
    assert result.capital_lockup_usd == pytest.approx(96.0)
    assert result.annualized_return == pytest.approx(0.02 * 100.0 / 96.0 / 100.0 * 8760.0)
    assert result.annualized_return == pytest.approx(1.825)
    assert result.fill_confidence == pytest.approx(1.0)
    assert result.resolution_risk == pytest.approx(0.15)
    assert result.composite == pytest.approx(1.825 * 1.0 * 0.85)
    assert result.composite == pytest.approx(1.55125)
    assert result.depth_source == "recorded"
    assert result.link_status is None
    # No identity confidence declared -> nothing estimated in the edge.
    assert result.edge_basis == EDGE_BASIS_OBSERVED


def _single_leg_intent(
    *, hours: float, market_id: str = "M1", size: float = 100.0
) -> tuple[datetime, Intent]:
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
    """Monotonicity: holding `net_edge` fixed, more hours -> lower annualized_return.

    `_single_leg_intent` buys 100 contracts at 0.50 with a published edge
    of 0.03, so capital_lockup_usd = 100 * 0.50 = 50.0 and the return
    over the whole hold is 0.03 * 100 / 50.0 = 0.06 (6%). Annualized:

        50h:  0.06 /  50 * 8760 = 0.0012  * 8760 = 10.512
        500h: 0.06 / 500 * 8760 = 0.00012 * 8760 =  1.0512

    (T31: before capital normalization these were 5.256 and 0.5256 —
    exactly half, because the leg costs $0.50 and a $0.03 edge on $0.50
    of capital is a 6% return, not a 3% one.)
    """
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

    assert short_result.capital_lockup_usd == pytest.approx(50.0)
    assert short_result.annualized_return == pytest.approx(0.06 / 50.0 * 8760.0)
    assert short_result.annualized_return == pytest.approx(10.512)
    assert long_result.annualized_return == pytest.approx(0.06 / 500.0 * 8760.0)
    assert long_result.annualized_return == pytest.approx(1.0512)
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

    # capital_lockup_usd = 100 * 0.50 = 50.0; return over the hold =
    # 0.03 * 100 / 50.0 = 0.06; floored at 6h: 0.06 / 6 * 8760 = 87.6.
    expected_floored = 0.06 / 6.0 * 8760.0
    assert expected_floored == pytest.approx(87.6)
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


def _cross_venue_intent(
    *, confidence: float, gross_edge: float = 0.01
) -> tuple[datetime, Intent]:
    """A cross-venue intent shaped like `cross_venue_arbitrage`'s output.

    Publishes what the T31 scoring contract requires: a PRE-risk edge
    under `"edge"` plus the two identity parameters the scorer needs to
    apply the haircut itself. `worst_case_loss` is `max(ask_a, ask_b) =
    0.50` — the losing leg's whole stake, which is what a resolution
    mismatch actually costs.
    """
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
        metadata={
            "edge": gross_edge,
            "p_same_resolution": confidence,
            "worst_case_loss": 0.50,
            "edge_basis": EDGE_BASIS_IDENTITY_ESTIMATED,
            "link_id": 7,
        },
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


def test_cross_venue_identity_risk_is_discounted_exactly_once():
    """T31: link confidence moves `net_edge` and NOTHING ELSE in the score.

    THE DEFECT THIS PINS. `cross_venue_arbitrage` used to publish a
    `net_edge` already multiplied by its link confidence, and `scoring`
    then ALSO added `+0.25` to `resolution_risk` for a cross-venue intent
    below 0.95 confidence, and `composite` multiplies by
    `(1 - resolution_risk)`. The same link confidence therefore
    discounted the same trade twice, while every single-market strategy
    was discounted neither time.

    Two intents, identical but for the link confidence. Both publish a
    PRE-risk edge of 0.08 and a worst_case_loss of 0.50 (the losing leg's
    whole stake). Legs: 50 contracts at 0.46 (PM) + 50 at 0.50 (KX), so
    capital_lockup_usd = 50*0.46 + 50*0.50 = 23.0 + 25.0 = 48.0, units =
    50, hours = 200.

    The haircut, applied ONCE, in `scoring`:

        p = 0.99: net_edge = 0.08*0.99 - 0.01*0.50 = 0.0792 - 0.005
                           = 0.0742
        p = 0.90: net_edge = 0.08*0.90 - 0.10*0.50 = 0.072  - 0.05
                           = 0.022

    Annualization is the same multiplier for both:
    units/capital/hours*8760 = 50 / 48.0 / 200 * 8760 = 45.625.

        p = 0.99: annualized = 0.0742 * 45.625 = 3.385375
        p = 0.90: annualized = 0.022  * 45.625 = 1.00375

    resolution_risk is IDENTICAL for the two — it no longer contains any
    confidence term at all, only the settlement risk of two distinct
    markets: 1 - (1 - 0.15)**2 = 0.2775.

        p = 0.99: composite = 3.385375 * 1.0 * 0.7225 = 2.4459334375
        p = 0.90: composite = 1.00375  * 1.0 * 0.7225 = 0.725209375

    THE BEFORE/AFTER at p = 0.90, all three ways round:

        old (haircut in the strategy AND +0.25 here, capital-blind):
            0.022 / 200 * 8760 * (1 - 0.40) = 0.9636 * 0.60 = 0.57816
        old formula with the second discount removed:
            0.9636 * 0.85                                   = 0.81906
        now (haircut once, return per dollar of capital):
                                                              0.725209375

    The single sharpest statement of "once, not twice": the ratio of the
    two composites is EXACTLY the ratio of the two net_edges,
    0.022 / 0.0742 = 0.2964959568733153. If confidence still entered a
    second time through `resolution_risk`, the ratio would be
    0.022*0.60 / (0.0742*0.85) = 0.2092912284046461 instead.
    """
    now_low, intent_low = _cross_venue_intent(confidence=0.90, gross_edge=0.08)
    result_low = score(intent_low, _cross_venue_ctx(now_low))
    now_high, intent_high = _cross_venue_intent(confidence=0.99, gross_edge=0.08)
    result_high = score(intent_high, _cross_venue_ctx(now_high))

    # The haircut lives in net_edge -- once.
    assert result_low.net_edge == pytest.approx(0.022)
    assert result_high.net_edge == pytest.approx(0.0742)

    # ... and nowhere else. Both risks are the two-market settlement
    # term, with no confidence component whatsoever.
    assert result_low.resolution_risk == pytest.approx(0.2775)
    assert result_high.resolution_risk == pytest.approx(0.2775)
    assert result_low.resolution_risk == pytest.approx(result_high.resolution_risk)
    # Explicitly NOT the old double-discounted value.
    assert result_low.resolution_risk != pytest.approx(0.15 + 0.25)

    assert result_low.capital_lockup_usd == pytest.approx(48.0)
    assert result_low.annualized_return == pytest.approx(0.022 * 45.625)
    assert result_low.annualized_return == pytest.approx(1.00375)
    assert result_high.annualized_return == pytest.approx(0.0742 * 45.625)
    assert result_high.annualized_return == pytest.approx(3.385375)

    assert result_low.composite == pytest.approx(0.725209375)
    assert result_high.composite == pytest.approx(2.4459334375)

    # Confidence enters the composite EXACTLY once: through net_edge.
    assert result_low.composite / result_high.composite == pytest.approx(
        0.022 / 0.0742
    )
    assert result_low.composite / result_high.composite == pytest.approx(
        0.2964959568733153
    )
    # The double-discounted ratio the old code produced.
    assert result_low.composite / result_high.composite != pytest.approx(
        0.2092912284046461
    )

    # An edge resting on an estimated probability says so in the payload.
    assert result_low.edge_basis == EDGE_BASIS_IDENTITY_ESTIMATED


def test_equivalent_economics_score_the_same_composite_across_strategies():
    """T31's invariant: `composite` means one thing, whoever produced it.

        Two intents with the same `composite` represent the same expected
        risk-adjusted return per dollar of capital locked up, regardless
        of which strategy produced them.

    PART 1 -- a `binary_complement_arbitrage`-shaped intent and a
    `multi_outcome_bundle_arbitrage`-shaped one with identical
    economics score IDENTICALLY, though they publish their edge under
    different keys ("edge" vs the legacy "net_edge"), have different leg
    counts (2 vs 3) and different prices per leg:

        complement: YES 0.48 + NO 0.50            = 0.98 per unit
        bundle:     A 0.40 + B 0.30 + C 0.28      = 0.98 per unit

    both at 100 contracts per leg (capital_lockup_usd = 98.0), both
    publishing a pre-risk edge of 0.02, both 100h out, both on one
    well-documented market:

        annualized_return = 0.02 * 100 / 98.0 / 100 * 8760
                          = (0.02 / 0.98) * 87.6 = 1.7877551020408163
        resolution_risk   = 1 - (1 - 0.15)**1 = 0.15   (ONE market each)
        composite         = 1.7877551020408163 * 1.0 * 0.85
                          = 1.5195918367346939

    Before T31 these two did NOT agree in general: the bundle's edge was
    read as if it were the same kind of number as a cross-venue haircut
    edge, and neither was divided by the capital it locked up, so two
    intents with equal returns on unequal capital scored differently.

    PART 2 -- the ONE residual difference, made explicit. A cross-venue
    pair on the same capital (PM YES 0.48 + KX NO 0.50) at link
    confidence 1.00 -- i.e. with the identity haircut worth exactly
    zero -- still carries the settlement risk of TWO independently
    adjudicating venues: 1 - (1 - 0.15)**2 = 0.2775 rather than 0.15. So
    it must earn more edge to rank equally, and exactly how much more is
    the point:

        equal composite  <=>  e * (1 - 0.2775) = 0.02 * (1 - 0.15)
                         <=>  e = 0.02 * 0.85 / 0.7225 = 0.02 / 0.85
                            = 0.023529411764705882

    i.e. 1/0.85 = 1.1765x the same-venue edge -- the price of the second
    venue's settlement risk, and NOTHING more. Under the old double
    discount a cross-venue pair below 0.95 confidence needed
    0.85/0.60 = 1.4167x on top of a haircut already taken inside its own
    edge, which is the artifact this task removed.
    """
    now = utcnow()
    market_pm = _open_market(PM, "M1")
    market_kx = _open_market(KX, "K1")

    def _book(outcome, price, venue=PM, market_id="M1"):
        return make_book(
            bids=[(price - 0.02, 200.0)], asks=[(price, 200.0)],
            venue=venue, market_id=market_id, outcome=outcome,
        )

    complement = Intent(
        kind="complement",
        legs=[
            Leg(market_id="M1", outcome="YES", side="BUY", limit_price=0.48,
                size_contracts=100.0, venue=PM),
            Leg(market_id="M1", outcome="NO", side="BUY", limit_price=0.50,
                size_contracts=100.0, venue=PM),
        ],
        hold_to_resolution=True,
        atomicity="all_or_none",
        confidence=0.2,
        expected_resolution_ts=now + timedelta(hours=100),
        metadata={"edge": 0.02},
    )
    bundle = Intent(
        kind="bundle",
        legs=[
            Leg(market_id="M1", outcome="A", side="BUY", limit_price=0.40,
                size_contracts=100.0, venue=PM),
            Leg(market_id="M1", outcome="B", side="BUY", limit_price=0.30,
                size_contracts=100.0, venue=PM),
            Leg(market_id="M1", outcome="C", side="BUY", limit_price=0.28,
                size_contracts=100.0, venue=PM),
        ],
        hold_to_resolution=True,
        atomicity="all_or_none",
        # Deliberately a DIFFERENT Intent.confidence from the
        # complement's: strategies set it from their own margin scale,
        # and it must not leak into the ranking.
        confidence=0.9,
        expected_resolution_ts=now + timedelta(hours=100),
        # The legacy key, to pin that it still means "pre-risk edge".
        metadata={"net_edge": 0.02},
    )
    same_venue_ctx = ScoreContext(
        now=now,
        books={
            (PM, "M1", "YES"): _book("YES", 0.48),
            (PM, "M1", "NO"): _book("NO", 0.50),
            (PM, "M1", "A"): _book("A", 0.40),
            (PM, "M1", "B"): _book("B", 0.30),
            (PM, "M1", "C"): _book("C", 0.28),
        },
        markets={(PM, "M1"): market_pm},
        settings=settings,
    )

    complement_result = score(complement, same_venue_ctx)
    bundle_result = score(bundle, same_venue_ctx)

    for result in (complement_result, bundle_result):
        assert result.net_edge == pytest.approx(0.02)
        assert result.capital_lockup_usd == pytest.approx(98.0)
        assert result.fill_confidence == pytest.approx(1.0)
        assert result.resolution_risk == pytest.approx(0.15)
        assert result.annualized_return == pytest.approx(0.02 / 0.98 * 8760.0 / 100.0)
        assert result.annualized_return == pytest.approx(1.7877551020408163)
        assert result.composite == pytest.approx(1.5195918367346939)
        assert result.edge_basis == EDGE_BASIS_OBSERVED

    # THE INVARIANT: equal economics -> equal composite, across strategies.
    assert complement_result.composite == pytest.approx(bundle_result.composite)

    # PART 2 -- the cross-venue exchange rate, in full.
    cross_venue = Intent(
        kind="cross_venue",
        legs=[
            Leg(market_id="M1", outcome="YES", side="BUY", limit_price=0.48,
                size_contracts=100.0, venue=PM),
            Leg(market_id="K1", outcome="NO", side="BUY", limit_price=0.50,
                size_contracts=100.0, venue=KX),
        ],
        hold_to_resolution=True,
        atomicity="all_or_none",
        confidence=1.0,
        expected_resolution_ts=now + timedelta(hours=100),
        metadata={
            "edge": 0.02 / 0.85,
            "p_same_resolution": 1.0,
            "worst_case_loss": 0.50,
            "edge_basis": EDGE_BASIS_IDENTITY_ESTIMATED,
        },
    )
    cross_venue_ctx = ScoreContext(
        now=now,
        books={
            (PM, "M1", "YES"): _book("YES", 0.48),
            (KX, "K1", "NO"): _book("NO", 0.50, venue=KX, market_id="K1"),
        },
        markets={(PM, "M1"): market_pm, (KX, "K1"): market_kx},
        settings=settings,
    )

    cross_venue_result = score(cross_venue, cross_venue_ctx)

    assert cross_venue_result.net_edge == pytest.approx(0.023529411764705882)
    assert cross_venue_result.capital_lockup_usd == pytest.approx(98.0)
    assert cross_venue_result.resolution_risk == pytest.approx(1.0 - 0.85 * 0.85)
    assert cross_venue_result.resolution_risk == pytest.approx(0.2775)
    assert cross_venue_result.composite == pytest.approx(1.5195918367346939)
    assert cross_venue_result.composite == pytest.approx(complement_result.composite)
    # Equal composite, but the two are NOT the same kind of number: one
    # rests on an estimated probability and says so in the payload.
    assert cross_venue_result.edge_basis == EDGE_BASIS_IDENTITY_ESTIMATED
    assert complement_result.edge_basis == EDGE_BASIS_OBSERVED


def test_an_intent_claiming_an_estimated_basis_without_a_confidence_is_refused():
    """The contract cannot rot back into T31's defect silently.

    A strategy that has ALREADY haircut its own edge (and says so with
    `edge_basis="identity_estimated"`) but publishes no
    `p_same_resolution` would have its post-risk number read as a
    pre-risk one and under-discounted — the same class of bug in the
    other direction. `score()` refuses rather than guessing.
    """
    now, intent = _cross_venue_intent(confidence=0.90)
    del intent.metadata["p_same_resolution"]

    with pytest.raises(UnscorableIntent):
        score(intent, _cross_venue_ctx(now))


def test_a_declared_identity_risk_with_no_worst_case_loss_is_refused():
    """`p < 1` with no stated stake is unscorable, not free.

    A resolution mismatch costs the losing leg's whole stake; assuming
    that stake is zero would turn a 10% chance of losing $0.50 into a
    10% chance of losing nothing and rank the pair far too highly.
    """
    now, intent = _cross_venue_intent(confidence=0.90)
    del intent.metadata["worst_case_loss"]

    with pytest.raises(UnscorableIntent):
        score(intent, _cross_venue_ctx(now))


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
    chars, a named resolution_source, one distinct market, and a book
    on every leg deep enough to fill the full 100-contract request
    (`min_position_size` default):

        net_edge           = 0.219625     (published pre-risk; a bundle
                                           declares no identity risk, so
                                           no haircut applies)
        units              = 100
        capital_lockup_usd = 100 * (0.20 + 0.20 + 0.20 + 0.15) = 75.0
        annualized_return  = 0.219625 * 100 / 75.0 / 100 * 8760
                           = (0.219625 / 0.75) * 87.6 = 25.6522
        fill_confidence    = 1.0          (every leg's book covers it)
        resolution_risk    = 1 - (1 - 0.15)**1 = 0.15   (ONE market)
        composite          = 25.6522 * 1.0 * 0.85 = 21.80437

    T31 MOVED THESE NUMBERS: before capital normalization,
    annualized_return was 0.219625 / 100 * 8760 = 19.23915 and composite
    16.3532775. The ratio is exactly 1 / 0.75 — a $0.219625 edge on
    $0.75 of committed capital is a 29.28% return, not a 21.96% one, and
    that under-reporting is precisely what let a cheap bundle and an
    expensive complement with the same dollar edge score alike.
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
    assert result.capital_lockup_usd == pytest.approx(75.0)
    assert result.annualized_return == pytest.approx(
        0.219625 * 100.0 / 75.0 / 100.0 * 8760.0
    )
    assert result.annualized_return == pytest.approx(25.6522)
    assert result.fill_confidence == pytest.approx(1.0)
    assert result.resolution_risk == pytest.approx(0.15)
    assert result.composite == pytest.approx(25.6522 * 1.0 * 0.85)
    assert result.composite == pytest.approx(21.80437)
    assert result.composite != 0.0
    assert result.edge_basis == EDGE_BASIS_OBSERVED
