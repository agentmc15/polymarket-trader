"""T18 — cross-venue complement arbitrage (`cross_venue_arbitrage`).

Derived from the T18 brief/acceptance criteria in
`.claude/kits/market-edge/TASKS.md` and PLAN.md D8/D9/R1/R6, not by
reading the strategy and mirroring it back.

Every money figure below is computed BY HAND in the test body
(GUARDRAILS.md §5) from the two venues' PUBLISHED rates — Polymarket's
Politics category 0.04 and Kalshi's `Settings` 0.07 — never from a
literal fee inside the strategy (§1.5). The two fee shapes differ, and
the difference is load-bearing: Polymarket charges
`size * rate * p * (1 - p)` exactly, while Kalshi ceilings each FILL to
whole cents, so its per-contract cost depends on how many contracts the
fill carries.

The capital tests are the point of §1.6: with $5,000 on Polymarket and
$50 on Kalshi, the correct size is bounded by the KALSHI leg, and no
assertion here may be satisfiable by any code that adds the two balances.
GUARDRAILS.md §1.1/§1.2/§1.4: no network, no live mode, no real order —
the router test routes through a `PaperVenueAdapter` over a
`FixtureAdapter`.
"""
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, settings
from app.execution.ledger import CapitalLedger
from app.execution.router import OrderRouter
from app.models.event_link import EventLink
from app.models.trade import Order as OrderRow
from app.strategies import STRATEGIES, STRATEGY_CATEGORIES, get_strategy
from app.strategies.base import DOWNSIZE_TO_CAPITAL_KEY
from app.strategies.cross_venue_arbitrage import (
    LEDGER_KEY,
    CrossVenueArbitrageStrategy,
    LinkBook,
)
from app.utils.time import utcnow
from app.venues.paper import PaperVenueAdapter
from tests.helpers import make_book, make_snapshot
from tests.venues.fixture_adapter import FixtureAdapter, make_venue_market

PM_MARKET = "PM-BTC-100K"
KX_MARKET = "KXBTC100K-26DEC31"

#: The two venues' published taker rates, restated here so the hand
#: computations below are readable. The STRATEGY never sees these — it
#: sources them from `app/venues/fees.py` — but a test that asserts a
#: number has to say which rate produced it.
PM_POLITICS_RATE = 0.04
KALSHI_RATE = 0.07


def approved_link(
    confidence: float,
    *,
    link_id: int = 7,
    outcome_map: dict[str, str] | None = None,
) -> EventLink:
    """Build an APPROVED `EventLink` from Polymarket A to Kalshi B.

    Args:
        confidence: `p_same_resolution` — the probability the two
            contracts settle on the same fact.
        link_id: Stamped into `Intent.metadata["link_id"]`.
        outcome_map: Canonical A -> B outcome map. Defaults to the
            identity map the matcher produces for a binary pair.

    Returns:
        EventLink: Unsaved, but with `status` set explicitly to
            `"approved"` (an unsaved row's `status` is otherwise `None`,
            because `"proposed"` is a COLUMN default applied at INSERT).
    """
    link = EventLink(
        venue_a="polymarket",
        market_a=PM_MARKET,
        venue_b="kalshi",
        market_b=KX_MARKET,
        outcome_map=outcome_map if outcome_map is not None else {"YES": "YES", "NO": "NO"},
        confidence=confidence,
        evidence={"title_jaccard": 1.0},
        status="approved",
    )
    link.id = link_id
    return link


def seeded_strategy(
    *,
    confidence: float,
    pm_yes_ask: float,
    kx_no_ask: float,
    days_to_resolution: float = 30.0,
    config: dict[str, object] | None = None,
    available: dict[str, float] | None = None,
) -> CrossVenueArbitrageStrategy:
    """Build a strategy with both venues' books and snapshots already seen.

    The winning direction is always `YES@polymarket + NO@kalshi`; the
    other direction (`NO@polymarket + YES@kalshi`) is priced from real
    books too, but at 0.60 a side so it can never win. Depth is 500
    contracts a side, comfortably above the 50-contract probe.

    Args:
        confidence: The link's `p_same_resolution`.
        pm_yes_ask: Polymarket YES ask.
        kx_no_ask: Kalshi NO ask.
        days_to_resolution: Both markets' `end_date`, days from now.
        config: Strategy config overrides.
        available: If given, the per-venue free capital to
            `observe_capital()`.

    Returns:
        CrossVenueArbitrageStrategy: Both snapshots already fed through
            `on_market_data`, so a further call re-evaluates immediately.
    """
    strategy = CrossVenueArbitrageStrategy(
        config=dict(config or {}), links=[approved_link(confidence)]
    )
    if available is not None:
        strategy.observe_capital(available)

    end_date = utcnow() + timedelta(days=days_to_resolution)
    for outcome, price, venue, market in (
        ("YES", pm_yes_ask, "polymarket", PM_MARKET),
        ("NO", 0.60, "polymarket", PM_MARKET),
        ("YES", 0.60, "kalshi", KX_MARKET),
        ("NO", kx_no_ask, "kalshi", KX_MARKET),
    ):
        strategy.observe_book(
            make_book(
                bids=[(max(0.0, price - 0.02), 500.0)],
                asks=[(price, 500.0)],
                venue=venue,  # type: ignore[arg-type]
                market_id=market,
                outcome=outcome,
            )
        )

    strategy.on_market_data(
        make_snapshot(
            market_id=PM_MARKET,
            yes=pm_yes_ask,
            venue="polymarket",
            category="Politics",
            end_date=end_date,
        )
    )
    strategy.on_market_data(
        make_snapshot(
            market_id=KX_MARKET,
            yes=1.0 - kx_no_ask,
            venue="kalshi",
            category=None,
            end_date=end_date,
        )
    )
    return strategy


def kalshi_snapshot(days_to_resolution: float = 30.0):  # type: ignore[no-untyped-def]
    """Return the Kalshi-side snapshot that re-triggers an evaluation."""
    return make_snapshot(
        market_id=KX_MARKET,
        yes=0.5,
        venue="kalshi",
        category=None,
        end_date=utcnow() + timedelta(days=days_to_resolution),
    )


# ---------------------------------------------------------------------------
# 1. The arithmetic (PLAN.md D8) — asserted to 1e-9
# ---------------------------------------------------------------------------


def test_cost_and_net_edge_are_hand_computable_to_1e_9() -> None:
    """Polymarket YES 0.46 (Politics, 0.04) + Kalshi NO 0.50 (0.07).

    Every term is PER CONTRACT, because it is compared against the $1.00
    a winning contract redeems for. `probe_size` is 50 — the same 50 the
    books are walked for and the same 50 each venue's fee is priced at
    before being divided back, so the quoted ask and the quoted fee
    describe one trade.

        fee_A = 50 * 0.04 * 0.46 * 0.54 = 0.4968
                -> 0.4968 / 50           = 0.009936 / contract
        fee_B = 50 * 0.07 * 0.50 * 0.50 = 0.875, ceilinged by Kalshi to
                whole cents PER FILL     = 0.88
                -> 0.88 / 50             = 0.0176   / contract
        gas   = 2 * 0.05 / 50            = 0.002    / contract
                (redemption gas is charged PER POSITION, two positions,
                 amortized over the probe fill)

        cost       = 0.46 + 0.50 + 0.009936 + 0.0176 + 0.002 = 0.989536
        gross_edge = 1 - 0.989536                            = 0.010464

    At `p_same_resolution = 1.0` (a certainty no real link earns, used
    here so the haircut term is exactly zero and the naive number and the
    honest number coincide):

        worst_case_loss = max(0.46, 0.50) = 0.50
        net_edge = 0.010464 * 1.0 - 0.0 * 0.50 = 0.010464
    """
    strategy = seeded_strategy(confidence=1.0, pm_yes_ask=0.46, kx_no_ask=0.50)
    evaluation = strategy.evaluate(strategy.links.links[0], "YES")

    assert evaluation is not None
    assert evaluation.venue_a == "polymarket"
    assert evaluation.outcome_a == "YES"
    assert evaluation.venue_b == "kalshi"
    assert evaluation.outcome_b == "NO"
    assert evaluation.ask_a == pytest.approx(0.46, abs=1e-9)
    assert evaluation.ask_b == pytest.approx(0.50, abs=1e-9)
    assert evaluation.fee_a == pytest.approx(0.009936, abs=1e-9)
    assert evaluation.fee_b == pytest.approx(0.0176, abs=1e-9)
    assert evaluation.gas_per_contract == pytest.approx(0.002, abs=1e-9)
    assert evaluation.cost == pytest.approx(0.989536, abs=1e-9)
    assert evaluation.gross_edge == pytest.approx(0.010464, abs=1e-9)
    assert evaluation.p_same_resolution == pytest.approx(1.0, abs=1e-9)
    assert evaluation.worst_case_loss == pytest.approx(0.50, abs=1e-9)
    assert evaluation.net_edge == pytest.approx(0.010464, abs=1e-9)
    # Fees came from the published sources, not a literal or an operator
    # override (GUARDRAILS.md §1.5).
    assert evaluation.fee_schedule_source_a == "category_table"
    assert evaluation.fee_schedule_source_b == "settings_default"


def test_confidence_haircut_lowers_net_edge_and_reports_worst_case_loss() -> None:
    """The SAME pair at confidence 0.8 is a loss, not a smaller profit.

    This is the whole reason `net_edge` is not `1 - cost`. A 20% chance
    the two markets settle differently does not shave the edge — it
    inverts it, because when they diverge you hold YES on one venue and
    NO on the other, one pays zero, and the losing leg's entire cost is
    gone:

        gross_edge      = 0.010464          (unchanged — same prices)
        worst_case_loss = max(0.46, 0.50)   = 0.50
        net_edge = 0.010464 * 0.8 - (1 - 0.8) * 0.50
                 = 0.0083712 - 0.10
                 = -0.0916288

    At this confidence the gross edge would have to exceed 12.5% before
    the trade is worth taking at all.
    """
    certain = seeded_strategy(confidence=1.0, pm_yes_ask=0.46, kx_no_ask=0.50)
    doubtful = seeded_strategy(confidence=0.8, pm_yes_ask=0.46, kx_no_ask=0.50)

    sure = certain.evaluate(certain.links.links[0], "YES")
    unsure = doubtful.evaluate(doubtful.links.links[0], "YES")
    assert sure is not None
    assert unsure is not None

    # Same prices -> identical gross edge; only the haircut differs.
    assert unsure.gross_edge == pytest.approx(sure.gross_edge, abs=1e-9)
    assert unsure.worst_case_loss == pytest.approx(0.50, abs=1e-9)
    assert unsure.net_edge == pytest.approx(-0.0916288, abs=1e-9)
    assert unsure.net_edge < sure.net_edge

    # And the haircut is not decoration: it stops the trade.
    assert doubtful.on_market_data(kalshi_snapshot()) is None
    assert "worst_case_loss" in unsure.as_metadata()


# ---------------------------------------------------------------------------
# 2. Nothing trades on a link a human has not approved (PLAN.md D9)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["proposed", "rejected", None])
def test_a_link_that_is_not_approved_raises(status: str | None) -> None:
    """A `proposed` link must never reach the trading path.

    `None` is included because an UNSAVED `EventLink` carries
    `status is None` — `"proposed"` is a column default applied at
    INSERT, so a row that never met the database would slip through a
    check written as `status != "proposed"`.
    """
    link = approved_link(0.95)
    link.status = status  # type: ignore[assignment]

    with pytest.raises(ValueError, match="approved"):
        LinkBook([link])
    with pytest.raises(ValueError, match="approved"):
        CrossVenueArbitrageStrategy(links=[link])

    strategy = CrossVenueArbitrageStrategy(links=[approved_link(0.95)])
    with pytest.raises(ValueError, match="approved"):
        strategy.set_links([link])
    # The rejected book did not replace the good one.
    assert len(strategy.links) == 1


# ---------------------------------------------------------------------------
# 3. Sizing is bounded by the POORER venue (PLAN.md D8/R6, GUARDRAILS §1.6)
# ---------------------------------------------------------------------------


def test_sizing_caps_at_the_poorer_venue_and_never_sums_balances() -> None:
    """$5,000 on Polymarket and $50 on Kalshi funds ~104 contracts, not ~5,400.

    Hand computation, asks 0.45 (Polymarket YES) and 0.48 (Kalshi NO):

        polymarket bound = 5000 / 0.45 = 11111.111... contracts
        kalshi bound     =   50 / 0.48 =   104.1666... contracts
        max_contracts    =                 1000
        size = min(11111.111, 104.1666, 1000) = 104.1666...

    The number a summing implementation would produce is
    `(5000 + 50) / (0.45 + 0.48) = 5430.107...`, and it is asserted
    against explicitly: Kalshi dollars sit in a CFTC-regulated FCM
    account settling by ACH in days, so the Polymarket balance is not
    capital that can reach the Kalshi leg inside this trade. No transfer
    between venues is assumed anywhere.
    """
    strategy = seeded_strategy(
        confidence=0.98,
        pm_yes_ask=0.45,
        kx_no_ask=0.48,
        available={"polymarket": 5000.0, "kalshi": 50.0},
    )
    intent = strategy.on_market_data(kalshi_snapshot())

    assert intent is not None
    assert intent.kind == "cross_venue"
    assert len(intent.legs) == 2
    kalshi_bound = 50.0 / 0.48
    pooled = (5000.0 + 50.0) / (0.45 + 0.48)
    for leg in intent.legs:
        assert leg.side == "BUY"
        assert leg.size_contracts == pytest.approx(kalshi_bound, abs=1e-9)
        assert leg.size_contracts != pytest.approx(pooled, abs=1e-6)
    # Both legs equal in CONTRACTS, on two different venues, one YES and
    # one NO — equal DOLLARS would leave a naked residual on the cheaper
    # side.
    assert {leg.venue for leg in intent.legs} == {"polymarket", "kalshi"}
    assert {leg.outcome for leg in intent.legs} == {"YES", "NO"}
    assert intent.hold_to_resolution is True
    assert intent.atomicity == "all_or_none"
    assert intent.metadata["link_id"] == 7
    assert intent.metadata["capital_lockup_usd"] == pytest.approx(
        kalshi_bound * (0.45 + 0.48), abs=1e-9
    )


def test_calculate_position_size_refuses_the_pooled_portfolio_value() -> None:
    """The scalar `portfolio_value` must not be able to fund anything.

    `BaseStrategy.calculate_position_size` hands sizing a single
    cross-venue total. That number is exactly what GUARDRAILS.md §1.6
    forbids as a sizing input, so it is ignored: the same call with
    `portfolio_value` of $0 and of $1,000,000 must return the same size,
    which is set by `positions["__ledger__"]` alone.
    """
    from app.strategies.base import Signal, SignalType

    strategy = CrossVenueArbitrageStrategy()
    signal = Signal(
        type=SignalType.BUY,
        market_id=PM_MARKET,
        token_id=PM_MARKET,
        outcome="YES",
        price=0.45,
        size=0.0,
        confidence=0.98,
        metadata={"leg_asks": {"polymarket": 0.45, "kalshi": 0.48}},
    )
    positions = {LEDGER_KEY: {"polymarket": 5000.0, "kalshi": 50.0}}

    broke = strategy.calculate_position_size(signal, 0.0, positions)
    rich = strategy.calculate_position_size(signal, 1_000_000.0, positions)

    assert broke == pytest.approx(50.0 / 0.48, abs=1e-9)
    assert rich == pytest.approx(broke, abs=1e-9)

    # A venue the ledger was never seeded for funds NOTHING — it is never
    # covered from the venue that IS seeded.
    unseeded = strategy.calculate_position_size(
        signal, 1_000_000.0, {LEDGER_KEY: {"polymarket": 5000.0}}
    )
    assert unseeded == pytest.approx(0.0, abs=1e-12)


# ---------------------------------------------------------------------------
# 4. Time to resolution gates the trade (PLAN.md D10)
# ---------------------------------------------------------------------------


def test_no_intent_when_resolution_is_400_days_out() -> None:
    """A 4.05% edge locked up for 400 days is not worth the capital.

    Hand computation, asks 0.45 / 0.48 at confidence 1.0:

        fee_A = 50 * 0.04 * 0.45 * 0.55 = 0.495  -> 0.0099   / contract
        fee_B = ceil_cents(50 * 0.07 * 0.48 * 0.52 = 0.8736) = 0.88
                                                 -> 0.0176   / contract
        gas                                       = 0.002    / contract
        cost       = 0.45 + 0.48 + 0.0099 + 0.0176 + 0.002 = 0.9595
        gross_edge = net_edge (p = 1.0)                    = 0.0405

        400 days = 9600 h -> annualized = 0.0405 / 9600 * 8760 = 0.036956
        30 days  =  720 h -> annualized = 0.0405 /  720 * 8760 = 0.49275

    `settings.min_viable_annualized` is 0.05, so the 400-day version is
    refused and the 30-day version — identical in every other respect —
    is taken.
    """
    assert settings.min_viable_annualized == pytest.approx(0.05)

    far = seeded_strategy(
        confidence=1.0,
        pm_yes_ask=0.45,
        kx_no_ask=0.48,
        days_to_resolution=400.0,
        available={"polymarket": 5000.0, "kalshi": 5000.0},
    )
    evaluation = far.evaluate(far.links.links[0], "YES")
    assert evaluation is not None
    assert evaluation.net_edge == pytest.approx(0.0405, abs=1e-9)
    assert evaluation.annualized == pytest.approx(0.036956250, abs=1e-9)
    assert evaluation.net_edge >= 0.015  # clears the edge gate ...
    assert far.on_market_data(kalshi_snapshot(400.0)) is None  # ... but not this one

    near = seeded_strategy(
        confidence=1.0,
        pm_yes_ask=0.45,
        kx_no_ask=0.48,
        days_to_resolution=30.0,
        available={"polymarket": 5000.0, "kalshi": 5000.0},
    )
    near_eval = near.evaluate(near.links.links[0], "YES")
    assert near_eval is not None
    assert near_eval.annualized == pytest.approx(0.49275, abs=1e-9)
    assert near.on_market_data(kalshi_snapshot(30.0)) is not None


# ---------------------------------------------------------------------------
# 5. A large edge is evidence AGAINST the link (PLAN.md R1)
# ---------------------------------------------------------------------------


def test_an_eight_percent_edge_flags_the_link_as_suspect() -> None:
    """8%+ net says "probably a wrong link", not "probably free money".

    Hand computation, asks 0.42 (Polymarket YES) / 0.44 (Kalshi NO) at
    confidence 0.98:

        fee_A = 50 * 0.04 * 0.42 * 0.58 = 0.4872 -> 0.009744 / contract
        fee_B = ceil_cents(50 * 0.07 * 0.44 * 0.56 = 0.8624) = 0.87
                                                 -> 0.0174   / contract
        gas                                       = 0.002    / contract
        cost       = 0.42 + 0.44 + 0.009744 + 0.0174 + 0.002 = 0.889144
        gross_edge = 1 - 0.889144                            = 0.110856
        worst_case_loss = max(0.42, 0.44)                    = 0.44
        net_edge = 0.110856 * 0.98 - 0.02 * 0.44
                 = 0.10863888 - 0.0088
                 = 0.09983888   -> >= 0.08, so `suspect_link` is True

    Genuine cross-venue mispricings that large do not persist on liquid
    linked markets; two venues quoting complementary outcomes 10 cents
    apart are far more likely quoting two different questions.
    """
    suspicious = seeded_strategy(
        confidence=0.98,
        pm_yes_ask=0.42,
        kx_no_ask=0.44,
        available={"polymarket": 5000.0, "kalshi": 5000.0},
    )
    evaluation = suspicious.evaluate(suspicious.links.links[0], "YES")
    assert evaluation is not None
    assert evaluation.gross_edge == pytest.approx(0.110856, abs=1e-9)
    assert evaluation.net_edge == pytest.approx(0.09983888, abs=1e-9)
    assert evaluation.suspect_link is True

    intent = suspicious.on_market_data(kalshi_snapshot())
    assert intent is not None
    assert intent.metadata["suspect_link"] is True
    assert suspicious.get_stats()["suspect_links_seen"] >= 1

    # A believable 4% edge on the same pair is NOT flagged, so the flag
    # discriminates rather than firing on every arbitrage.
    ordinary = seeded_strategy(
        confidence=0.98,
        pm_yes_ask=0.45,
        kx_no_ask=0.48,
        available={"polymarket": 5000.0, "kalshi": 5000.0},
    )
    plain = ordinary.on_market_data(kalshi_snapshot())
    assert plain is not None
    assert plain.metadata["suspect_link"] is False


# ---------------------------------------------------------------------------
# 6. A price the book cannot fill is not a price (PLAN.md R4)
# ---------------------------------------------------------------------------


def test_a_book_too_thin_for_the_probe_size_produces_no_intent() -> None:
    """10 contracts offered is not a 50-contract price.

    The strategy walks each leg's book for `probe_size` contracts. A book
    that runs dry part-way through would give a size-weighted average
    over a fill that could never have happened; that is refused outright
    rather than quoted.
    """
    strategy = seeded_strategy(
        confidence=0.98,
        pm_yes_ask=0.42,
        kx_no_ask=0.44,
        available={"polymarket": 5000.0, "kalshi": 5000.0},
    )
    assert strategy.evaluate(strategy.links.links[0], "YES") is not None

    strategy.observe_book(
        make_book(
            bids=[(0.42, 500.0)],
            asks=[(0.44, 10.0)],
            venue="kalshi",
            market_id=KX_MARKET,
            outcome="NO",
        )
    )
    assert strategy.evaluate(strategy.links.links[0], "YES") is None
    assert strategy.on_market_data(kalshi_snapshot()) is None


# ---------------------------------------------------------------------------
# 7. The registry is back to nine keys
# ---------------------------------------------------------------------------


def test_registry_holds_nine_strategies_including_cross_venue() -> None:
    """T10 dropped `cross_platform_arbitrage` to 8; T18 restores the 9th."""
    assert len(STRATEGIES) == 9
    assert STRATEGIES["cross_venue_arbitrage"] is CrossVenueArbitrageStrategy
    assert "cross_venue_arbitrage" in STRATEGY_CATEGORIES["arbitrage"]
    assert "cross_platform_arbitrage" not in STRATEGIES

    built = get_strategy("cross_venue_arbitrage")
    assert isinstance(built, CrossVenueArbitrageStrategy)
    # No link book -> nothing to trade, and no crash.
    assert len(built.links) == 0
    assert built.on_market_data(kalshi_snapshot()) is None
    # GUARDRAILS.md §1.5: no fee number in the shipped config.
    assert set(built.config) == {
        "probe_size",
        "min_net_edge",
        "max_contracts",
        "suspect_link_net_edge",
    }


# ---------------------------------------------------------------------------
# 8. End to end: the poorer venue's bound governs what is actually PLACED
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def sessions(test_engine) -> async_sessionmaker[AsyncSession]:
    """Return a session factory over the in-memory test database."""
    return async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


def cross_venue_adapters(
    ledger: CapitalLedger,
) -> dict[str, PaperVenueAdapter]:
    """Build paper adapters over fixture books at 0.45 (PM) / 0.48 (Kalshi)."""
    pm_inner = FixtureAdapter("polymarket")
    pm_inner.add_market(make_venue_market("polymarket", PM_MARKET))
    pm_inner.set_book(
        make_book(
            bids=[(0.44, 500.0)],
            asks=[(0.45, 500.0)],
            venue="polymarket",
            market_id=PM_MARKET,
            outcome="YES",
        )
    )
    pm_inner.set_book(
        make_book(
            bids=[(0.55, 500.0)],
            asks=[(0.60, 500.0)],
            venue="polymarket",
            market_id=PM_MARKET,
            outcome="NO",
        )
    )
    kx_inner = FixtureAdapter("kalshi")
    kx_inner.add_market(make_venue_market("kalshi", KX_MARKET))
    kx_inner.set_book(
        make_book(
            bids=[(0.47, 500.0)],
            asks=[(0.48, 500.0)],
            venue="kalshi",
            market_id=KX_MARKET,
            outcome="NO",
        )
    )
    kx_inner.set_book(
        make_book(
            bids=[(0.55, 500.0)],
            asks=[(0.60, 500.0)],
            venue="kalshi",
            market_id=KX_MARKET,
            outcome="YES",
        )
    )
    return {
        "polymarket": PaperVenueAdapter(pm_inner, None, ledger),
        "kalshi": PaperVenueAdapter(kx_inner, None, ledger),
    }


async def test_a_50_dollar_kalshi_balance_bounds_both_legs_end_to_end(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """$50 Kalshi + $5,000 Polymarket places the KALSHI-bounded size on BOTH legs.

    The strategy sizes at D8's cash bound
    `min(5000 / 0.45, 50 / 0.48, 1000) = 104.1666...` contracts. The
    router then re-applies the same per-venue `min` against the
    authoritative ledger, this time including the worst-case fee it must
    hold, and floors to a whole contract:

        Kalshi reservation(c) = c * 0.48
                              + ceil_cents(c * 0.07 * 0.5 * 0.5)
                              + 0.10 headroom
        c = 100 -> 48.00 + 1.75 + 0.10 = 49.85 <= 50   (fits)
        c = 101 -> 48.48 + 1.77 + 0.10 = 50.35 >  50   (does not)

    so 100 contracts is the bound, and it is applied to BOTH legs — the
    hedge must stay equal in contracts. What is actually spent:

        Polymarket: 100 * 0.45 + 100 * 0.04 * 0.45 * 0.55
                  = 45.00 + 0.99 = 45.99   -> 5000 - 45.99 = 4954.01 free
        Kalshi:     100 * 0.48 + ceil_cents(100 * 0.07 * 0.48 * 0.52)
                  = 48.00 + 1.75 = 49.75   ->   50 - 49.75 =    0.25 free

    Before this task the router had no downsizing path at all: the first
    venue shortfall rejected the whole intent, so this account placed
    NOTHING. The assertion that matters most is the last one — the
    Polymarket balance funded ONLY the Polymarket leg. Nothing was drawn
    from it to cover Kalshi, and no transfer between the two was assumed.
    """
    router_settings = Settings(  # type: ignore[call-arg]
        trading_mode="paper",
        paper_starting_balances={"polymarket": 5000.0, "kalshi": 50.0},
        max_order_notional_usd=250.0,
        max_open_notional_usd=1000.0,
        max_daily_loss_usd=100.0,
        max_near_resolution_notional_usd=500.0,
    )
    ledger = CapitalLedger.paper(router_settings)
    adapters = cross_venue_adapters(ledger)
    router = OrderRouter(adapters, ledger, sessions, router_settings)  # type: ignore[arg-type]

    strategy = seeded_strategy(
        confidence=0.98,
        pm_yes_ask=0.45,
        kx_no_ask=0.48,
        available=ledger.available_by_venue(),  # type: ignore[arg-type]
    )
    intent = strategy.on_market_data(kalshi_snapshot())
    assert intent is not None
    assert intent.metadata[DOWNSIZE_TO_CAPITAL_KEY] is True
    # What the strategy asked for: the cash bound, Kalshi-set.
    for leg in intent.legs:
        assert leg.size_contracts == pytest.approx(50.0 / 0.48, abs=1e-9)

    routed = await router.submit(intent, "cross-venue-test")

    assert routed.status == "executed"
    assert len(routed.legs) == 2
    # Both legs downsized to the SAME whole-contract count, and it is the
    # count the POORER venue can fund — not the richer one, not a total.
    for leg in routed.legs:
        assert leg.filled_size == pytest.approx(100.0, abs=1e-9)

    async with sessions() as session:
        rows = (
            (await session.execute(select(OrderRow).order_by(OrderRow.id)))
            .scalars()
            .all()
        )
    assert len(rows) == 2
    assert {row.venue for row in rows} == {"polymarket", "kalshi"}
    for row in rows:
        assert row.size == pytest.approx(100.0, abs=1e-9)

    assert ledger.available("kalshi") == pytest.approx(0.25, abs=1e-9)
    assert ledger.available("polymarket") == pytest.approx(4954.01, abs=1e-9)
    # The richer venue was touched for its OWN leg and nothing more:
    # 5000 - 4954.01 = 45.99, exactly the Polymarket leg's cash + fee.
    assert 5000.0 - ledger.available("polymarket") == pytest.approx(45.99, abs=1e-9)
    assert ledger.locked("polymarket") == pytest.approx(0.0, abs=1e-9)
    assert ledger.locked("kalshi") == pytest.approx(0.0, abs=1e-9)


async def test_the_same_intent_without_the_downsize_flag_is_still_rejected(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The downsize is what makes the previous test pass — nothing else.

    Identical setup, identical intent, one difference: the
    `downsize_to_capital` flag is stripped. The router then behaves as it
    always did — the Kalshi shortfall rejects the whole intent, nothing is
    placed, and the Polymarket balance is untouched.

    That is still the RIGHT behavior for an intent that did not opt in
    (`app.strategies.base.DOWNSIZE_TO_CAPITAL_KEY`: a directional bet's
    size was chosen deliberately, and a third of it is a different
    trade). Asserting it here keeps the downsize honest: it is a
    strategy-declared property of a scale-free hedge, not a blanket
    loosening of the capital fence.
    """
    router_settings = Settings(  # type: ignore[call-arg]
        trading_mode="paper",
        paper_starting_balances={"polymarket": 5000.0, "kalshi": 50.0},
        max_order_notional_usd=250.0,
        max_open_notional_usd=1000.0,
        max_daily_loss_usd=100.0,
        max_near_resolution_notional_usd=500.0,
    )
    ledger = CapitalLedger.paper(router_settings)
    adapters = cross_venue_adapters(ledger)
    router = OrderRouter(adapters, ledger, sessions, router_settings)  # type: ignore[arg-type]

    strategy = seeded_strategy(
        confidence=0.98,
        pm_yes_ask=0.45,
        kx_no_ask=0.48,
        available=ledger.available_by_venue(),  # type: ignore[arg-type]
    )
    intent = strategy.on_market_data(kalshi_snapshot())
    assert intent is not None
    del intent.metadata[DOWNSIZE_TO_CAPITAL_KEY]

    routed = await router.submit(intent, "cross-venue-test")

    assert routed.status == "rejected"
    assert routed.reason == "insufficient_capital"
    assert ledger.available("polymarket") == pytest.approx(5000.0, abs=1e-9)
    assert ledger.available("kalshi") == pytest.approx(50.0, abs=1e-9)

    async with sessions() as session:
        rows = (await session.execute(select(OrderRow))).scalars().all()
    assert rows == []
