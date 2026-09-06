"""Tests for `app.execution.fill_engine` (T07).

Every money number in this file is stated as a hand computation in a
comment and asserted against a literal — never against a value produced
by the code under test (GUARDRAILS.md §5). The one exception is the
per-fill fee comparison, which deliberately calls `FeeModel.fee()`
DIRECTLY (not through the engine) to prove the engine sums per fill
rather than charging one aggregate call.
"""
import logging
import math
import random
from datetime import UTC, datetime
from typing import Any

import pytest

from app.execution.fill_engine import (
    CROSSED_QUOTES_KEY,
    FEE_SOURCE_KEY,
    TICK_VALIDATED_KEY,
    FillResult,
    SimulatedFillEngine,
    synthesize_book,
)
from app.venues.base import FeeModel
from app.venues.fees import KalshiFeeModel, PolymarketFeeModel
from app.venues.types import (
    BookLevel,
    FeeSchedule,
    Fill,
    OrderBook,
    OrderRequest,
    TimeInForce,
    VenueId,
    VenueMarket,
)
from tests.helpers import make_book, make_snapshot

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
NAIVE_NOW = datetime(2026, 9, 4, 12, 0)

# Test-local fee rates. Real code sources these from a venue payload,
# the category table, or `Settings` (GUARDRAILS.md §1.5); a test states
# them explicitly so the expected dollar figures below can be computed by
# hand.
POLY_RATE = 0.05
KALSHI_RATE = 0.07

POLY_SCHEDULE = FeeSchedule(taker_rate=POLY_RATE, maker_rate=0.0, source="test_table")
KALSHI_SCHEDULE = FeeSchedule(
    taker_rate=KALSHI_RATE, maker_rate=0.0, source="test_table"
)


def _schedules(venue: VenueId, market_id: str) -> FeeSchedule:
    """Resolve a `FeeSchedule` the way the engine's `schedules` callable must."""
    assert market_id  # the resolver is keyed by market, not just venue
    return KALSHI_SCHEDULE if venue == "kalshi" else POLY_SCHEDULE


def _engine(latency_ms: int = 0) -> SimulatedFillEngine:
    """Build an engine wired to both venues' real fee models."""
    models: dict[VenueId, FeeModel] = {
        "polymarket": PolymarketFeeModel(),
        "kalshi": KalshiFeeModel(),
    }
    return SimulatedFillEngine(
        fee_models=models,
        schedules=_schedules,
        latency_ms=latency_ms,
        rng_seed=7,
    )


def _order(
    side: str = "BUY",
    price: float = 0.41,
    size: float = 150.0,
    tif: TimeInForce = "GTC",
    venue: VenueId = "polymarket",
    market_id: str = "m1",
    outcome: str = "YES",
    post_only: bool = False,
) -> OrderRequest:
    """Build an `OrderRequest` matching `tests.helpers.make_book`'s defaults."""
    return OrderRequest(
        venue=venue,
        market_id=market_id,
        outcome=outcome,
        side="BUY" if side == "BUY" else "SELL",
        price=price,
        size=size,
        tif=tif,
        client_order_id="intent-1:0:0",
        post_only=post_only,
    )


def _market(
    tick_size: float = 0.01,
    min_size: float = 0.0,
    venue: VenueId = "polymarket",
    market_id: str = "m1",
) -> VenueMarket:
    """Build a `VenueMarket` carrying just the microstructure under test."""
    return VenueMarket(
        venue=venue,
        market_id=market_id,
        event_id=None,
        question="Will it?",
        outcomes=("YES", "NO"),
        outcome_ids={"YES": "t1", "NO": "t2"},
        rules_text="rules",
        resolution_source=None,
        close_time=NOW,
        expected_settle_time=None,
        status="open",
        result=None,
        tick_size=tick_size,
        min_size=min_size,
        fee=POLY_SCHEDULE,
        raw={},
    )


# The canonical book from the T07 brief: 100 contracts at 0.40, then 100
# more at 0.41.
ASKS = [(0.40, 100.0), (0.41, 100.0)]
# Its mirror on the bid side: 100 at 0.60, then 100 at 0.59.
BIDS = [(0.60, 100.0), (0.59, 100.0)]


# ---------------------------------------------------------------------------
# BUY: depth walking, size-weighted average price, partials
# ---------------------------------------------------------------------------


def test_buy_walks_two_levels_and_fills_completely() -> None:
    """Buy 150 with limit 0.41 takes both levels, at a size-WEIGHTED price."""
    result = _engine().fill(_order(price=0.41, size=150.0), make_book([], ASKS), NOW)

    assert result.status == "filled"
    assert result.filled_size == pytest.approx(150.0, abs=1e-9)
    assert result.remaining_size == 0.0
    assert result.levels_consumed == 2
    # (100 x 0.40 + 50 x 0.41) / 150 = (40.00 + 20.50) / 150 = 0.40333333...
    # NOT the mean of the level prices, (0.40 + 0.41) / 2 = 0.405, which
    # would understate the cost of every multi-level fill.
    assert result.avg_price == pytest.approx(0.4033333333333333, abs=1e-9)
    assert result.avg_price != pytest.approx(0.405, abs=1e-3)
    assert [(f.price, f.size) for f in result.fills] == [(0.40, 100.0), (0.41, 50.0)]


def test_buy_limit_below_second_level_fills_partially() -> None:
    """Limit 0.40 reaches only the first level: 100 of 150."""
    result = _engine().fill(_order(price=0.40, size=150.0), make_book([], ASKS), NOW)

    assert result.status == "partial"
    assert result.filled_size == pytest.approx(100.0, abs=1e-9)
    assert result.remaining_size == pytest.approx(50.0, abs=1e-9)
    assert result.levels_consumed == 1
    assert result.avg_price == pytest.approx(0.40, abs=1e-9)


def test_buy_never_fills_the_whole_size_at_the_top_level() -> None:
    """PLAN.md R4: depth, not top-of-book. The 0.41 level must be paid for."""
    result = _engine().fill(_order(price=0.41, size=150.0), make_book([], ASKS), NOW)

    assert result.fills[1].price == 0.41
    assert result.avg_price is not None and result.avg_price > 0.40


def test_buy_beyond_all_depth_is_partial_not_an_error() -> None:
    """A dry book returns what it had; it never raises and never invents depth."""
    result = _engine().fill(_order(price=0.41, size=500.0), make_book([], ASKS), NOW)

    assert result.status == "partial"
    assert result.filled_size == pytest.approx(200.0, abs=1e-9)
    assert result.remaining_size == pytest.approx(300.0, abs=1e-9)


def test_buy_with_no_eligible_level_is_unfilled() -> None:
    """A limit below every ask fills nothing (and reports the full remainder)."""
    result = _engine().fill(_order(price=0.39, size=150.0), make_book([], ASKS), NOW)

    assert result.status == "unfilled"
    assert result.filled_size == 0.0
    assert result.remaining_size == pytest.approx(150.0, abs=1e-9)
    assert result.avg_price is None
    assert result.total_fee == 0.0
    assert result.levels_consumed == 0


# ---------------------------------------------------------------------------
# SELL mirrors BUY
# ---------------------------------------------------------------------------


def test_sell_walks_bids_downward_and_fills_completely() -> None:
    """Sell 150 with limit 0.59 takes both bid levels, size-weighted."""
    result = _engine().fill(
        _order(side="SELL", price=0.59, size=150.0), make_book(BIDS, []), NOW
    )

    assert result.status == "filled"
    assert result.filled_size == pytest.approx(150.0, abs=1e-9)
    assert result.remaining_size == 0.0
    assert result.levels_consumed == 2
    # (100 x 0.60 + 50 x 0.59) / 150 = (60.00 + 29.50) / 150 = 0.59666666...
    assert result.avg_price == pytest.approx(0.5966666666666667, abs=1e-9)
    assert [(f.price, f.size) for f in result.fills] == [(0.60, 100.0), (0.59, 50.0)]


def test_sell_limit_above_second_level_fills_partially() -> None:
    """Limit 0.60 reaches only the best bid: 100 of 150."""
    result = _engine().fill(
        _order(side="SELL", price=0.60, size=150.0), make_book(BIDS, []), NOW
    )

    assert result.status == "partial"
    assert result.filled_size == pytest.approx(100.0, abs=1e-9)
    assert result.remaining_size == pytest.approx(50.0, abs=1e-9)
    assert result.avg_price == pytest.approx(0.60, abs=1e-9)


def test_sell_fok_below_full_size_is_killed() -> None:
    """FOK mirrors on the sell side too."""
    result = _engine().fill(
        _order(side="SELL", price=0.60, size=150.0, tif="FOK"), make_book(BIDS, []), NOW
    )

    assert result.status == "unfilled"
    assert result.remaining_size == pytest.approx(150.0, abs=1e-9)


# ---------------------------------------------------------------------------
# Time in force
# ---------------------------------------------------------------------------


def test_fok_is_all_or_nothing() -> None:
    """FOK with limit 0.40 can only reach 100 of 150 -> nothing trades."""
    result = _engine().fill(
        _order(price=0.40, size=150.0, tif="FOK"), make_book([], ASKS), NOW
    )

    assert result.status == "unfilled"
    assert result.filled_size == 0.0
    assert result.remaining_size == pytest.approx(150.0, abs=1e-9)
    assert result.fills == ()
    assert result.total_fee == 0.0
    assert result.levels_consumed == 0


def test_fok_that_can_be_completed_fills() -> None:
    """FOK with a limit that reaches full size is a normal complete fill."""
    result = _engine().fill(
        _order(price=0.41, size=150.0, tif="FOK"), make_book([], ASKS), NOW
    )

    assert result.status == "filled"
    assert result.filled_size == pytest.approx(150.0, abs=1e-9)


@pytest.mark.parametrize("tif", ["IOC", "GTC"])
def test_ioc_and_gtc_allow_partials(tif: TimeInForce) -> None:
    """Both keep what they walked; the GTC residual is REPORTED, never rested."""
    result = _engine().fill(
        _order(price=0.40, size=150.0, tif=tif), make_book([], ASKS), NOW
    )

    assert result.status == "partial"
    assert result.filled_size == pytest.approx(100.0, abs=1e-9)
    assert result.remaining_size == pytest.approx(50.0, abs=1e-9)
    # The engine produced fills for the taken part only — no resting
    # order, no phantom fill of the residual.
    assert len(result.fills) == 1


# ---------------------------------------------------------------------------
# tick_size and min_size (sourced from `VenueMarket`)
# ---------------------------------------------------------------------------


def test_off_tick_limit_price_raises() -> None:
    """0.405 is not a multiple of a 0.01 tick: the venue would reject it."""
    with pytest.raises(ValueError, match=r"tick_size"):
        _engine().fill(
            _order(price=0.405),
            make_book([], ASKS),
            NOW,
            market=_market(tick_size=0.01),
        )


def test_on_tick_limit_price_is_accepted() -> None:
    """The same price is fine on a market whose tick is 0.005."""
    result = _engine().fill(
        _order(price=0.405, size=100.0),
        make_book([], ASKS),
        NOW,
        market=_market(tick_size=0.005),
    )

    assert result.status == "filled"
    assert result.avg_price == pytest.approx(0.40, abs=1e-9)


def test_tick_check_tolerates_float_representation_noise() -> None:
    """A limit reached by arithmetic (0.40 + 0.01) is still on a 0.01 tick."""
    limit = 0.40 + 0.01  # == 0.41000000000000003 in IEEE-754
    assert limit != 0.41
    result = _engine().fill(
        _order(price=limit, size=150.0),
        make_book([], ASKS),
        NOW,
        market=_market(tick_size=0.01),
    )

    assert result.status == "filled"
    assert result.levels_consumed == 2


def test_tick_size_is_not_enforced_without_market_metadata() -> None:
    """`tick_size` lives on `VenueMarket`; with no market the engine cannot guess."""
    result = _engine().fill(_order(price=0.405, size=100.0), make_book([], ASKS), NOW)

    assert result.status == "filled"


def test_fill_below_min_size_is_not_filled() -> None:
    """A walked quantity under the venue minimum does not trade at all."""
    thin = make_book([], [(0.40, 3.0)])
    result = _engine().fill(
        _order(price=0.41, size=150.0),
        thin,
        NOW,
        market=_market(min_size=10.0),
    )

    assert result.status == "unfilled"
    assert result.filled_size == 0.0
    assert result.remaining_size == pytest.approx(150.0, abs=1e-9)
    assert result.fills == ()


def test_residual_below_min_size_does_not_block_the_filled_part() -> None:
    """min_size gates the FILLED quantity, not the leftover remainder.

    100 contracts fill (>= min_size 10); the 50-contract residual is
    reported as `remaining_size`, not dropped or force-filled.
    """
    result = _engine().fill(
        _order(price=0.40, size=150.0),
        make_book([], ASKS),
        NOW,
        market=_market(min_size=10.0),
    )

    assert result.status == "partial"
    assert result.filled_size == pytest.approx(100.0, abs=1e-9)
    assert result.remaining_size == pytest.approx(50.0, abs=1e-9)


# ---------------------------------------------------------------------------
# Fees: charged ONCE PER FILL, summed
# ---------------------------------------------------------------------------


def test_fee_equals_the_fee_model_summed_per_level() -> None:
    """Polymarket: each level is fee'd independently and the fees add up.

    Compared against `PolymarketFeeModel.fee()` called directly, level by
    level, AND against the hand computation:
      100 x 0.05 x 0.40 x 0.60 = 1.20
       50 x 0.05 x 0.41 x 0.59 = 0.60475
      total                     = 1.80475
    """
    result = _engine().fill(_order(price=0.41, size=150.0), make_book([], ASKS), NOW)
    model = PolymarketFeeModel()

    direct = [
        model.fee(0.40, 100.0, "taker", POLY_SCHEDULE),
        model.fee(0.41, 50.0, "taker", POLY_SCHEDULE),
    ]
    assert [f.fee for f in result.fills] == pytest.approx(direct, abs=1e-12)
    assert direct[0] == pytest.approx(1.20, abs=1e-9)
    assert direct[1] == pytest.approx(0.60475, abs=1e-9)
    assert result.total_fee == pytest.approx(1.80475, abs=1e-9)


def test_kalshi_multi_level_walk_costs_strictly_more_than_one_aggregate_call() -> None:
    """Kalshi's fee ceiling is PER FILL, so fragmentation genuinely costs more.

    Walking (0.40, 100), (0.41, 100), (0.42, 50) at rate 0.07:
      ceil_cents(100 x 0.07 x 0.40 x 0.60) = ceil_cents(1.68)  = 1.68
      ceil_cents(100 x 0.07 x 0.41 x 0.59) = ceil_cents(1.6933) = 1.70
      ceil_cents( 50 x 0.07 x 0.42 x 0.58) = ceil_cents(0.8526) = 0.86
      per-fill total                                            = 4.24
    while ONE aggregate call for the same 250 contracts at the best price
    is 250 x 0.07 x 0.40 x 0.60 = 4.20. Fee'ing an order once on its
    aggregate size therefore understates the real cost.
    """
    book = make_book(
        [], [(0.40, 100.0), (0.41, 100.0), (0.42, 50.0)], venue="kalshi"
    )
    result = _engine().fill(
        _order(price=0.42, size=250.0, venue="kalshi"), book, NOW
    )
    model = KalshiFeeModel()

    assert result.levels_consumed == 3
    assert [f.fee for f in result.fills] == pytest.approx([1.68, 1.70, 0.86], abs=1e-9)
    assert result.total_fee == pytest.approx(4.24, abs=1e-9)

    aggregate = model.fee(0.40, 250.0, "taker", KALSHI_SCHEDULE)
    assert aggregate == pytest.approx(4.20, abs=1e-9)
    assert result.total_fee > aggregate


def test_kalshi_per_fill_ceiling_matches_direct_per_level_calls() -> None:
    """The engine's fees are exactly `fee()` per level — not a blended figure."""
    book = make_book([], [(0.99, 1.0), (0.99, 1.0)], venue="kalshi")
    result = _engine().fill(_order(price=0.99, size=2.0, venue="kalshi"), book, NOW)
    model = KalshiFeeModel()

    # Two 1-contract fills at 0.99 cost $0.01 each = $0.02; one 2-contract
    # call costs $0.01. The fragmentation is real, and the engine's walk
    # is what produces it.
    assert result.total_fee == pytest.approx(0.02, abs=1e-9)
    assert model.fee(0.99, 2.0, "taker", KALSHI_SCHEDULE) == pytest.approx(
        0.01, abs=1e-9
    )


def test_missing_fee_model_for_venue_raises() -> None:
    """An unconfigured venue must not simulate an implicitly free fill."""
    engine = SimulatedFillEngine(
        fee_models={"polymarket": PolymarketFeeModel()}, schedules=_schedules
    )
    book = make_book([], ASKS, venue="kalshi")

    with pytest.raises(ValueError, match=r"no FeeModel configured"):
        engine.fill(_order(venue="kalshi"), book, NOW)


# ---------------------------------------------------------------------------
# Fill metadata: latency and depth provenance
# ---------------------------------------------------------------------------


def test_latency_ms_is_recorded_on_every_fill_and_does_not_move_ts() -> None:
    """`latency_ms` is informational for now (T08 uses next-snapshot fills)."""
    result = _engine(latency_ms=250).fill(
        _order(price=0.41, size=150.0), make_book([], ASKS), NOW
    )

    assert [f.metadata["latency_ms"] for f in result.fills] == [250, 250]
    assert all(f.ts == NOW for f in result.fills)


def test_depth_source_recorded_propagates_onto_fills() -> None:
    """A real book tags its fills `"recorded"`."""
    book = make_book([], ASKS)
    assert book.depth_source == "recorded"

    result = _engine().fill(_order(price=0.41, size=150.0), book, NOW)

    assert all(f.metadata["depth_source"] == "recorded" for f in result.fills)


def test_depth_source_synthetic_propagates_onto_fills() -> None:
    """GUARDRAILS.md §1.7: a fill walked out of invented depth stays labeled."""
    snapshot = make_snapshot(yes=0.50, spread=0.02, volume_24h=50_000.0)
    book = synthesize_book(snapshot, 0.02)
    result = _engine().fill(_order(price=0.51, size=10.0), book, NOW)

    assert result.status == "filled"
    assert all(f.metadata["depth_source"] == "synthetic" for f in result.fills)


def test_fills_carry_market_and_outcome_context() -> None:
    """`Fill` has no market/outcome field; the engine records them in metadata."""
    result = _engine().fill(_order(price=0.41, size=150.0), make_book([], ASKS), NOW)

    assert all(f.metadata["market_id"] == "m1" for f in result.fills)
    assert all(f.metadata["outcome"] == "YES" for f in result.fills)
    assert all(f.order_id == "intent-1:0:0" for f in result.fills)


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def test_naive_now_raises() -> None:
    """Aware UTC only (GUARDRAILS.md §4)."""
    with pytest.raises(ValueError, match=r"naive"):
        _engine().fill(_order(), make_book([], ASKS), NAIVE_NOW)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"venue": "kalshi"}, r"venue"),
        ({"market_id": "other"}, r"market_id"),
        ({"outcome": "NO"}, r"outcome"),
    ],
)
def test_book_that_is_not_this_orders_book_raises(
    kwargs: dict[str, Any], match: str
) -> None:
    """Filling against another market's depth is a silent money error."""
    with pytest.raises(ValueError, match=match):
        _engine().fill(_order(**kwargs), make_book([], ASKS), NOW)


def test_outcome_match_is_case_insensitive() -> None:
    """Venues disagree on casing ("Yes" vs "yes"); YES-vs-NO is still caught."""
    book = make_book([], ASKS, outcome="Yes")
    result = _engine().fill(_order(price=0.41, size=100.0, outcome="YES"), book, NOW)

    assert result.status == "filled"


def test_outcome_match_tolerates_a_stray_trailing_space() -> None:
    """T21f: a book outcome carrying whitespace must not abort the fill.

    `_check_book_matches` used to compare on a bare `.casefold()`, which
    catches casing but not whitespace, so a book outcome of `"Trump "`
    (as could arrive via the scanner/adapter path) raised `ValueError`
    against an order outcome of `"Trump"` and aborted the whole
    backtest run rather than just this one fill. It now compares on
    `outcome_key()` — `app/strategies/base.py`'s single identity
    canonicalization, which also strips — so the two agree.
    """
    book = make_book([], ASKS, outcome="Trump ")
    result = _engine().fill(
        _order(price=0.41, size=100.0, outcome="Trump"), book, NOW
    )

    assert result.status == "filled"


def test_outcome_match_tolerates_whitespace_on_the_binary_pair() -> None:
    """T21f: the whitespace fix also applies to the YES/NO pair's own path."""
    book = make_book([], ASKS, outcome="Yes ")
    result = _engine().fill(_order(price=0.41, size=100.0, outcome="YES"), book, NOW)

    assert result.status == "filled"


def test_outcome_mismatch_still_raises_beyond_the_binary_pair() -> None:
    """T21f: a genuine label mismatch must still raise, whitespace aside."""
    book = make_book([], ASKS, outcome="Biden")
    with pytest.raises(ValueError, match=r"outcome"):
        _engine().fill(_order(price=0.41, size=100.0, outcome="Trump"), book, NOW)


def test_post_only_order_is_not_simulated(caplog: pytest.LogCaptureFixture) -> None:
    """This engine models taking liquidity only; maker fills are T14's business."""
    with caplog.at_level(logging.WARNING, logger="app.execution.fill_engine"):
        result = _engine().fill(
            _order(price=0.41, size=150.0, post_only=True), make_book([], ASKS), NOW
        )

    assert result.status == "unfilled"
    assert result.filled_size == 0.0
    assert "post_only" in caplog.text


def test_zero_size_order_fills_nothing() -> None:
    """A zero-size order is not a completed fill; it is simply nothing."""
    result = _engine().fill(_order(price=0.41, size=0.0), make_book([], ASKS), NOW)

    assert result.status == "unfilled"
    assert result.filled_size == 0.0
    assert result.remaining_size == 0.0


def test_negative_latency_is_rejected() -> None:
    """Latency is milliseconds, `>= 0`."""
    with pytest.raises(ValueError, match=r"latency_ms"):
        SimulatedFillEngine(
            fee_models={"polymarket": PolymarketFeeModel()},
            schedules=_schedules,
            latency_ms=-1,
        )


def _a_fill(size: float = 10.0) -> Fill:
    """One consistent `Fill` for `FillResult` invariant tests."""
    return Fill(
        venue="polymarket",
        order_id="intent-1:0:0",
        price=0.4,
        size=size,
        fee=0.0,
        ts=NOW,
        liquidity="taker",
    )


def test_fill_result_rejects_partial_with_no_remainder() -> None:
    """The status/size invariant is enforced at the type, for every producer."""
    with pytest.raises(ValueError, match=r"partial"):
        FillResult(
            fills=(_a_fill(),),
            filled_size=10.0,
            remaining_size=0.0,
            avg_price=0.4,
            total_fee=0.0,
            status="partial",
            levels_consumed=1,
        )


def test_fill_result_rejects_filled_with_a_remainder() -> None:
    """The other half of the same invariant."""
    with pytest.raises(ValueError, match=r"filled"):
        FillResult(
            fills=(_a_fill(),),
            filled_size=10.0,
            remaining_size=5.0,
            avg_price=0.4,
            total_fee=0.0,
            status="filled",
            levels_consumed=1,
        )


def test_fill_result_rejects_a_filled_size_that_does_not_match_its_fills() -> None:
    """Internal consistency: `filled_size` is the sum of the fill sizes."""
    with pytest.raises(ValueError, match=r"filled_size"):
        FillResult(
            fills=(_a_fill(size=10.0),),
            filled_size=25.0,
            remaining_size=5.0,
            avg_price=0.4,
            total_fee=0.0,
            status="partial",
            levels_consumed=1,
        )


# ---------------------------------------------------------------------------
# synthesize_book: invented depth, labeled as such
# ---------------------------------------------------------------------------


def test_synthetic_book_sizes_follow_the_formula() -> None:
    """size = liquidity_fraction x volume_24h / price, per side.

    volume_24h = 50,000; liquidity_fraction = 0.02 -> 1,000 USD notional.
      bid at 0.49: 1000 / 0.49 = 2040.816326530612... contracts
      ask at 0.51: 1000 / 0.51 = 1960.784313725490... contracts
    """
    snapshot = make_snapshot(yes=0.50, spread=0.02, volume_24h=50_000.0)
    assert snapshot.yes_bid == pytest.approx(0.49, abs=1e-12)
    assert snapshot.yes_ask == pytest.approx(0.51, abs=1e-12)

    book = synthesize_book(snapshot, 0.02)

    assert len(book.bids) == 1
    assert len(book.asks) == 1
    assert book.bids[0].price == pytest.approx(0.49, abs=1e-12)
    assert book.bids[0].size == pytest.approx(2040.8163265306123, abs=1e-9)
    assert book.asks[0].price == pytest.approx(0.51, abs=1e-12)
    assert book.asks[0].size == pytest.approx(1960.7843137254902, abs=1e-9)
    assert book.ts == snapshot.timestamp
    assert book.market_id == snapshot.market_id
    assert book.venue == snapshot.venue


def test_synthetic_book_is_labeled_synthetic() -> None:
    """GUARDRAILS.md §1.7: invented depth is always labeled."""
    book = synthesize_book(make_snapshot(volume_24h=50_000.0), 0.02)

    assert book.depth_source == "synthetic"
    assert book.metadata["liquidity_fraction"] == 0.02
    assert book.metadata["volume_24h"] == 50_000.0


def test_recorded_book_is_labeled_recorded() -> None:
    """Every book that was not synthesized is a real one."""
    assert make_book(BIDS, ASKS).depth_source == "recorded"


def test_synthetic_book_uses_no_quotes_for_outcome_no() -> None:
    """Outcome NO reads `no_bid`/`no_ask`, not the YES pair.

    yes = 0.30 -> no = 0.70, spread 0.02 -> no_bid 0.69, no_ask 0.71.
      bid: 1000 / 0.69 = 1449.2753623188405...
      ask: 1000 / 0.71 = 1408.4507042253522...
    """
    snapshot = make_snapshot(yes=0.30, spread=0.02, volume_24h=50_000.0)
    book = synthesize_book(snapshot, 0.02, outcome="NO")

    assert book.outcome == "NO"
    assert book.bids[0].price == pytest.approx(0.69, abs=1e-12)
    assert book.bids[0].size == pytest.approx(1449.2753623188405, abs=1e-9)
    assert book.asks[0].price == pytest.approx(0.71, abs=1e-12)
    assert book.asks[0].size == pytest.approx(1408.4507042253522, abs=1e-9)


def test_synthetic_book_price_floor_bounds_fabricated_depth() -> None:
    """As price -> 0 the 1/price term would fabricate unbounded depth.

    Unfloored, an ask at 0.001 would rest 1000 / 0.001 = 1,000,000
    contracts — a million dollars of notional conjured onto a market that
    traded fifty thousand, and exactly where a real market is thinnest.
    The divisor is floored at one cent: 1000 / 0.01 = 100,000 contracts.
    The level's own PRICE is untouched.
    """
    snapshot = make_snapshot(volume_24h=50_000.0, yes_bid=None, yes_ask=0.001)
    book = synthesize_book(snapshot, 0.02)

    assert book.asks[0].price == pytest.approx(0.001, abs=1e-12)
    assert book.asks[0].size == pytest.approx(100_000.0, abs=1e-6)
    assert book.asks[0].size < 1_000_000.0
    assert book.metadata["synthetic_price_floor_applied"] is True
    assert book.metadata["synthetic_price_floor"] == 0.01


def test_synthetic_book_handles_a_zero_price_without_dividing_by_zero() -> None:
    """price = 0.0 must not raise; the floored divisor covers it."""
    snapshot = make_snapshot(volume_24h=50_000.0, yes_bid=0.0, yes_ask=0.02)
    book = synthesize_book(snapshot, 0.02)

    # 1000 / 0.01 = 100,000 contracts (floored), not ZeroDivisionError.
    assert book.bids[0].size == pytest.approx(100_000.0, abs=1e-6)
    assert book.metadata["synthetic_price_floor_applied"] is True


def test_synthetic_book_does_not_apply_the_floor_above_one_cent() -> None:
    """The floor is a bound on a pathology, not a routine adjustment."""
    book = synthesize_book(make_snapshot(yes=0.50, volume_24h=50_000.0), 0.02)

    assert book.metadata["synthetic_price_floor_applied"] is False


def test_synthetic_book_omits_a_side_with_no_quote() -> None:
    """A missing NO quote is never derived as `1 - yes_*`."""
    snapshot = make_snapshot(volume_24h=50_000.0, no_bid=None, no_ask=0.71)
    book = synthesize_book(snapshot, 0.02, outcome="NO")

    assert book.bids == ()
    assert len(book.asks) == 1


def test_synthetic_book_rejects_a_negative_liquidity_fraction() -> None:
    """A negative fraction would produce negative depth."""
    with pytest.raises(ValueError, match=r"liquidity_fraction"):
        synthesize_book(make_snapshot(volume_24h=50_000.0), -0.01)


def test_synthetic_book_rejects_an_unknown_outcome() -> None:
    """Only YES/NO exist on a binary snapshot."""
    with pytest.raises(ValueError, match=r"outcome"):
        synthesize_book(make_snapshot(volume_24h=50_000.0), 0.02, outcome="MAYBE")


def test_synthetic_depth_is_walked_partially_like_any_other_book() -> None:
    """PLAN.md R4: even invented depth is finite and produces partials."""
    snapshot = make_snapshot(yes=0.50, spread=0.02, volume_24h=1_000.0)
    book = synthesize_book(snapshot, 0.02)
    # 0.02 x 1000 / 0.51 = 39.2156862745098... contracts at the ask.
    assert book.asks[0].size == pytest.approx(39.21568627450981, abs=1e-9)

    result = _engine().fill(_order(price=0.51, size=100.0), book, NOW)

    assert result.status == "partial"
    assert result.filled_size == pytest.approx(39.21568627450981, abs=1e-9)


# ---------------------------------------------------------------------------
# Crossed books: a data fault, never an arbitrage
#
# `bids=[(0.60, 100)]` with `asks=[(0.55, 100)]` means someone is bidding
# MORE than someone else is asking. Buying the 0.55 ask and hitting the
# 0.60 bid is 100 x (0.60 - 0.55) = $5.00 of riskless profit on a 100-lot
# — which no real book offers. It is a stale, mis-keyed, or merged quote
# row, and filling it credits a backtest with money that never existed.
# ---------------------------------------------------------------------------

CROSSED_BIDS = [(0.60, 100.0)]
CROSSED_ASKS = [(0.55, 100.0)]
# Locked, NOT crossed: a zero spread is legal and routinely observed.
LOCKED_BIDS = [(0.60, 100.0)]
LOCKED_ASKS = [(0.60, 100.0)]


def test_crossed_book_buy_is_declined_and_does_not_fill(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Buying the 0.55 ask against a 0.60 bid would fabricate $5.00."""
    book = make_book(CROSSED_BIDS, CROSSED_ASKS)
    with caplog.at_level(logging.WARNING, logger="app.execution.fill_engine"):
        result = _engine().fill(_order(price=0.55, size=100.0), book, NOW)

    assert result.status == "unfilled"
    assert result.reason == "crossed_book"
    assert result.filled_size == 0.0
    assert result.fills == ()
    assert result.avg_price is None
    assert result.total_fee == 0.0
    assert result.remaining_size == pytest.approx(100.0, abs=1e-9)
    assert "crossed book" in caplog.text


def test_crossed_book_sell_is_declined_too() -> None:
    """The refusal is about the BOOK, not about which way the order leans."""
    book = make_book(CROSSED_BIDS, CROSSED_ASKS)
    result = _engine().fill(
        _order(side="SELL", price=0.60, size=100.0), book, NOW
    )

    assert result.status == "unfilled"
    assert result.reason == "crossed_book"


def test_crossed_book_does_not_raise() -> None:
    """T08 replays real history: one bad row must not abort the whole run."""
    engine = _engine()
    book = make_book(CROSSED_BIDS, CROSSED_ASKS)

    # No `pytest.raises`: the call returns normally, and the very next
    # (good) book still fills.
    engine.fill(_order(price=0.55, size=100.0), book, NOW)
    good = engine.fill(_order(price=0.41, size=150.0), make_book([], ASKS), NOW)

    assert good.status == "filled"


def test_crossed_book_skips_are_counted_not_only_logged() -> None:
    """The skip is a data-quality NUMBER a run can report, not a log line."""
    engine = _engine()
    book = make_book(CROSSED_BIDS, CROSSED_ASKS)

    assert engine.crossed_book_skips == 0
    for _ in range(3):
        engine.fill(_order(price=0.55, size=100.0), book, NOW)

    assert engine.crossed_book_skips == 3
    assert engine.unfilled_counts["crossed_book"] == 3


def test_locked_book_is_not_crossed_and_fills_normally() -> None:
    """`best_bid == best_ask` is a zero spread, not free money.

    Buying 100 at a 0.60 limit takes the 0.60 ask: 100 x 0.60 = $60.00 of
    notional at the touch, no arbitrage implied.
    """
    book = make_book(LOCKED_BIDS, LOCKED_ASKS)
    result = _engine().fill(_order(price=0.60, size=100.0), book, NOW)

    assert result.status == "filled"
    assert result.reason is None
    assert result.avg_price == pytest.approx(0.60, abs=1e-9)
    assert result.filled_size == pytest.approx(100.0, abs=1e-9)


def test_one_tick_cross_is_still_a_cross() -> None:
    """0.41 bid vs 0.40 ask is a single tick of fabricated edge — refused."""
    book = make_book([(0.41, 100.0)], [(0.40, 100.0)])
    result = _engine().fill(_order(price=0.40, size=100.0), book, NOW)

    assert result.status == "unfilled"
    assert result.reason == "crossed_book"


def test_an_explicit_crossed_quotes_tag_is_honoured() -> None:
    """A producer's tag outranks what the finished book still shows."""
    book = OrderBook(
        venue="polymarket",
        market_id="m1",
        outcome="YES",
        bids=(),
        asks=(BookLevel(price=0.40, size=100.0),),
        ts=NOW,
        metadata={CROSSED_QUOTES_KEY: True},
    )
    result = _engine().fill(_order(price=0.41, size=100.0), book, NOW)

    assert result.status == "unfilled"
    assert result.reason == "crossed_book"


# ---------------------------------------------------------------------------
# synthesize_book propagates crossed quotes instead of repairing them
# ---------------------------------------------------------------------------


def test_synthetic_book_tags_crossed_quotes() -> None:
    """yes_bid 0.60 > yes_ask 0.40 is tagged, and the quotes are untouched."""
    snapshot = make_snapshot(volume_24h=50_000.0, yes_bid=0.60, yes_ask=0.40)
    book = synthesize_book(snapshot, 0.02)

    assert book.metadata[CROSSED_QUOTES_KEY] is True
    # NOT "fixed": swapping or averaging the quotes would invent data
    # that was never recorded.
    assert book.bids[0].price == pytest.approx(0.60, abs=1e-12)
    assert book.asks[0].price == pytest.approx(0.40, abs=1e-12)


def test_synthetic_book_tags_uncrossed_quotes_false() -> None:
    """The key is always present, so a consumer never reads "absent" as safe."""
    book = synthesize_book(make_snapshot(yes=0.50, volume_24h=50_000.0), 0.02)

    assert book.metadata[CROSSED_QUOTES_KEY] is False


def test_synthetic_book_from_crossed_quotes_never_fills() -> None:
    """Defect 1 and Defect 2 compound; the engine must stop the pair.

    Unstopped, buying 1000 contracts of the 0.40 ask while the same book
    bids 0.60 fabricates 1000 x 0.05 = $50.00 out of one bad history row.
    """
    snapshot = make_snapshot(volume_24h=50_000.0, yes_bid=0.60, yes_ask=0.40)
    book = synthesize_book(snapshot, 0.02)
    result = _engine().fill(_order(price=0.40, size=1000.0), book, NOW)

    assert result.status == "unfilled"
    assert result.reason == "crossed_book"
    assert result.total_fee == 0.0


def test_a_locked_snapshot_is_not_tagged_crossed() -> None:
    """bid == ask is legal; only a STRICT cross is a fault."""
    snapshot = make_snapshot(volume_24h=50_000.0, yes_bid=0.50, yes_ask=0.50)
    book = synthesize_book(snapshot, 0.02)

    assert book.metadata[CROSSED_QUOTES_KEY] is False
    assert _engine().fill(_order(price=0.50, size=10.0), book, NOW).status == "filled"


# ---------------------------------------------------------------------------
# synthesize_book: a zero-size side is NO side
# ---------------------------------------------------------------------------


def test_zero_volume_yields_empty_sides_not_phantom_levels() -> None:
    """A market that traded nothing in 24h is not quotable.

    0.02 x 0 / 0.51 = 0.0 contracts. Emitting `BookLevel(0.51, 0.0)`
    would read as quotable to every consumer that tests presence rather
    than `.size`.
    """
    book = synthesize_book(make_snapshot(yes=0.50, volume_24h=0.0), 0.02)

    assert book.bids == ()
    assert book.asks == ()
    assert book.best_bid() is None
    assert book.best_ask() is None


def test_zero_liquidity_fraction_yields_empty_sides() -> None:
    """"Assume no resting liquidity" is a legal, if degenerate, config."""
    book = synthesize_book(make_snapshot(yes=0.50, volume_24h=50_000.0), 0.0)

    assert book.bids == ()
    assert book.asks == ()
    assert book.best_ask() is None


def test_an_empty_synthetic_book_is_unfilled_with_no_eligible_levels() -> None:
    """The empty side reaches the engine as a reason, not as a zero fill."""
    book = synthesize_book(make_snapshot(yes=0.50, volume_24h=0.0), 0.02)
    result = _engine().fill(_order(price=0.51, size=10.0), book, NOW)

    assert result.status == "unfilled"
    assert result.reason == "no_eligible_levels"


def test_negative_liquidity_fraction_raises_as_a_configuration_error() -> None:
    """A negative fraction cannot come from data, only from a mis-set config.

    It is therefore fixed loudly rather than absorbed into an empty book
    the way `0.0` is.
    """
    with pytest.raises(ValueError, match=r"liquidity_fraction"):
        synthesize_book(make_snapshot(volume_24h=50_000.0), -0.02)


# ---------------------------------------------------------------------------
# `schedules()` is validated: no implicitly-free fill
# ---------------------------------------------------------------------------


def _engine_returning(schedule: Any) -> SimulatedFillEngine:
    """Build an engine whose `schedules` resolver returns `schedule` verbatim."""

    def resolver(venue: VenueId, market_id: str) -> Any:
        return schedule

    models: dict[VenueId, FeeModel] = {"polymarket": PolymarketFeeModel()}
    return SimulatedFillEngine(fee_models=models, schedules=resolver)


def test_schedules_returning_none_raises_a_typed_value_error() -> None:
    """A broken resolver must not surface as an AttributeError from the fee model."""
    engine = _engine_returning(None)

    with pytest.raises(ValueError, match=r"schedules\(\) must return a FeeSchedule"):
        engine.fill(_order(price=0.41, size=150.0), make_book([], ASKS), NOW)


def test_schedules_returning_a_non_schedule_raises() -> None:
    """The same guard covers a resolver that returns the rate itself."""
    engine = _engine_returning(0.05)

    with pytest.raises(ValueError, match=r"schedules\(\) must return a FeeSchedule"):
        engine.fill(_order(price=0.41, size=150.0), make_book([], ASKS), NOW)


@pytest.mark.parametrize("source", ["fee_waiver", "category_table", "clob_market"])
def test_a_declared_zero_taker_rate_is_accepted_silently(
    source: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Polymarket Geopolitics really is 0.0 and a Kalshi waiver is real.

    A declared zero fills for free with NO warning: 150 contracts at
    rate 0.0 cost 150 x 0.0 x p x (1 - p) = $0.0000 by construction.
    """
    engine = _engine_returning(
        FeeSchedule(taker_rate=0.0, maker_rate=0.0, source=source)
    )
    with caplog.at_level(logging.WARNING, logger="app.execution.fill_engine"):
        result = engine.fill(_order(price=0.41, size=150.0), make_book([], ASKS), NOW)

    assert result.status == "filled"
    assert result.total_fee == 0.0
    assert caplog.text == ""
    assert engine.undeclared_zero_fee_markets == frozenset()


def test_an_undeclared_zero_taker_rate_warns_and_is_recorded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A default that fell through to zero deletes the real cost.

    The correct schedule (taker_rate 0.05) charges, per level:
      100 x 0.05 x 0.40 x 0.60 = 1.20
       50 x 0.05 x 0.41 x 0.59 = 0.60475
      total                     = 1.80475
    A zero rate makes that $0.0000 — an edge that does not exist. It is
    still allowed to fill (raising would abort a replay), but it can
    never be silent.
    """
    engine = _engine_returning(
        FeeSchedule(taker_rate=0.0, maker_rate=0.0, source="settings_default")
    )
    with caplog.at_level(logging.WARNING, logger="app.execution.fill_engine"):
        result = engine.fill(_order(price=0.41, size=150.0), make_book([], ASKS), NOW)

    assert result.status == "filled"
    assert result.total_fee == 0.0
    assert "undeclared zero taker fee" in caplog.text
    # The warning names the venue and the market it applies to.
    assert "polymarket" in caplog.text
    assert "m1" in caplog.text
    # And it is discoverable afterwards, not only in the log stream.
    assert engine.undeclared_zero_fee_markets == frozenset({("polymarket", "m1")})


def test_the_undeclared_zero_fee_warning_is_emitted_once_per_market(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A replay calls this per snapshot; 100k identical warnings is noise."""
    engine = _engine_returning(
        FeeSchedule(taker_rate=0.0, maker_rate=0.0, source="settings_default")
    )
    with caplog.at_level(logging.WARNING, logger="app.execution.fill_engine"):
        for _ in range(5):
            engine.fill(_order(price=0.41, size=150.0), make_book([], ASKS), NOW)

    assert caplog.text.count("undeclared zero taker fee") == 1
    assert engine.undeclared_zero_fee_markets == frozenset({("polymarket", "m1")})


def test_operator_zeroed_polymarket_rate_is_flagged_like_kalshi(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Phase-1 remediation FIX 3.

    `POLYMARKET_TAKER_FEE_OVERRIDES={"politics": 0.0}` used to reach here
    stamped `source="category_table"` — the same TRUSTED label the
    published category table's genuine zero (Geopolitics) uses — so it
    was silently accepted with no warning and no
    `undeclared_zero_fee_markets` entry, unlike Kalshi's equivalent
    (`source="settings_default"`, already outside the trusted set).
    `app.venues.fees.category_fee_schedule` now stamps an override hit
    `"settings_override"` instead, which is deliberately NOT in
    `_ZERO_RATE_DECLARED_SOURCES`, so a fill priced off this schedule
    must warn and be recorded exactly like Kalshi's operator-zeroed rate.
    """
    engine = _engine_returning(
        FeeSchedule(taker_rate=0.0, maker_rate=0.0, source="settings_override")
    )
    with caplog.at_level(logging.WARNING, logger="app.execution.fill_engine"):
        result = engine.fill(_order(price=0.41, size=150.0), make_book([], ASKS), NOW)

    assert result.status == "filled"
    assert result.total_fee == 0.0
    assert "undeclared zero taker fee" in caplog.text
    assert engine.undeclared_zero_fee_markets == frozenset({("polymarket", "m1")})


def test_a_nonzero_rate_from_any_source_never_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The guard is about a ZERO rate, not about the source name."""
    engine = _engine_returning(
        FeeSchedule(taker_rate=0.05, maker_rate=0.0, source="settings_default")
    )
    with caplog.at_level(logging.WARNING, logger="app.execution.fill_engine"):
        result = engine.fill(_order(price=0.41, size=150.0), make_book([], ASKS), NOW)

    # 100 x 0.05 x 0.40 x 0.60 + 50 x 0.05 x 0.41 x 0.59 = 1.20 + 0.60475
    assert result.total_fee == pytest.approx(1.80475, abs=1e-9)
    assert caplog.text == ""
    assert engine.undeclared_zero_fee_markets == frozenset()


def test_every_fill_records_the_fee_schedule_source() -> None:
    """A $0.00 fee must be traceable to whatever declared it."""
    result = _engine().fill(_order(price=0.41, size=150.0), make_book([], ASKS), NOW)

    assert all(f.metadata[FEE_SOURCE_KEY] == "test_table" for f in result.fills)


def test_total_fee_is_a_separate_cost_not_baked_into_avg_price() -> None:
    """The convention T08 and T14 both consume, asserted as arithmetic.

    Buying 150 contracts: 100 x 0.40 + 50 x 0.41 = $60.50 of notional, so
    `avg_price` is 60.50 / 150 = 0.40333... The $1.80475 of fees is
    ON TOP: total cash out = 60.50 + 1.80475 = $62.30475. If the fee were
    already netted into the price, `avg_price x size` would be $62.30475
    on its own and a caller subtracting the fee again would double-count.
    """
    result = _engine().fill(_order(price=0.41, size=150.0), make_book([], ASKS), NOW)

    assert result.avg_price is not None
    notional = result.avg_price * result.filled_size
    assert notional == pytest.approx(60.50, abs=1e-9)
    assert result.total_fee == pytest.approx(1.80475, abs=1e-9)
    assert notional + result.total_fee == pytest.approx(62.30475, abs=1e-9)


# ---------------------------------------------------------------------------
# `tick_validated`: an unenforced venue constraint is labeled, not hidden
# ---------------------------------------------------------------------------


def test_fill_without_a_market_is_tagged_not_tick_validated() -> None:
    """Off-tick fills stay possible (T08 needs that) but stop being invisible."""
    result = _engine().fill(_order(price=0.405, size=100.0), make_book([], ASKS), NOW)

    assert result.status == "filled"
    assert result.metadata[TICK_VALIDATED_KEY] is False


def test_fill_with_a_market_is_tagged_tick_validated() -> None:
    """A caller that supplied a `VenueMarket` had both constraints enforced."""
    result = _engine().fill(
        _order(price=0.41, size=100.0),
        make_book([], ASKS),
        NOW,
        market=_market(tick_size=0.01),
    )

    assert result.status == "filled"
    assert result.metadata[TICK_VALIDATED_KEY] is True


def test_unfilled_results_carry_the_tick_validated_tag_too() -> None:
    """A report that counts declines must be able to bucket them the same way."""
    result = _engine().fill(_order(price=0.39, size=150.0), make_book([], ASKS), NOW)

    assert result.status == "unfilled"
    assert result.metadata[TICK_VALIDATED_KEY] is False


def test_fill_result_metadata_is_immutable() -> None:
    """A result cannot be re-labeled tick-validated after the fact."""
    result = _engine().fill(_order(price=0.41, size=100.0), make_book([], ASKS), NOW)

    with pytest.raises(TypeError):
        result.metadata[TICK_VALIDATED_KEY] = True  # type: ignore[index]


# ---------------------------------------------------------------------------
# The `reason` vocabulary: every refusal explains itself
# ---------------------------------------------------------------------------


def test_post_only_reason_is_distinguishable_from_no_liquidity() -> None:
    """Both are "unfilled"; only one means the book had nothing to take.

    The post_only book here is DEEP (200 contracts at or under the
    limit) — a router that reads the bare zeros would retry it forever.
    """
    post_only = _engine().fill(
        _order(price=0.41, size=150.0, post_only=True), make_book([], ASKS), NOW
    )
    no_liquidity = _engine().fill(
        _order(price=0.39, size=150.0), make_book([], ASKS), NOW
    )

    assert post_only.status == no_liquidity.status == "unfilled"
    assert post_only.reason == "post_only_taker_engine"
    assert no_liquidity.reason == "no_eligible_levels"
    assert post_only.reason != no_liquidity.reason


def test_below_min_size_has_its_own_reason() -> None:
    """3 contracts walked against a 10-contract venue minimum."""
    result = _engine().fill(
        _order(price=0.41, size=150.0),
        make_book([], [(0.40, 3.0)]),
        NOW,
        market=_market(min_size=10.0),
    )

    assert result.reason == "below_min_size"


def test_fok_kill_has_its_own_reason() -> None:
    """A killed FOK is a market condition, not a structural rejection."""
    result = _engine().fill(
        _order(price=0.40, size=150.0, tif="FOK"), make_book([], ASKS), NOW
    )

    assert result.reason == "fok_insufficient_depth"


def test_zero_size_order_has_its_own_reason() -> None:
    """Nothing was requested, so nothing is missing."""
    result = _engine().fill(_order(price=0.41, size=0.0), make_book([], ASKS), NOW)

    assert result.reason == "zero_size_order"


def test_a_filled_result_carries_no_reason() -> None:
    """`reason is None` is the caller's "this worked" test."""
    filled = _engine().fill(_order(price=0.41, size=150.0), make_book([], ASKS), NOW)
    partial = _engine().fill(_order(price=0.40, size=150.0), make_book([], ASKS), NOW)

    assert filled.status == "filled" and filled.reason is None
    assert partial.status == "partial" and partial.reason is None


def test_unfilled_counts_tally_by_reason() -> None:
    """One tally, keyed by the same vocabulary the results carry."""
    engine = _engine()
    engine.fill(_order(price=0.39, size=150.0), make_book([], ASKS), NOW)
    engine.fill(_order(price=0.39, size=150.0), make_book([], ASKS), NOW)
    engine.fill(_order(price=0.40, size=150.0, tif="FOK"), make_book([], ASKS), NOW)

    assert dict(engine.unfilled_counts) == {
        "no_eligible_levels": 2,
        "fok_insufficient_depth": 1,
    }


def test_fill_result_rejects_an_unfilled_with_no_reason() -> None:
    """An unexplained zero is the defect the field exists to prevent."""
    with pytest.raises(ValueError, match=r"requires a reason"):
        FillResult(
            fills=(),
            filled_size=0.0,
            remaining_size=10.0,
            avg_price=None,
            total_fee=0.0,
            status="unfilled",
            levels_consumed=0,
        )


def test_fill_result_rejects_an_unknown_reason() -> None:
    """A typo'd reason would silently fall out of every downstream tally."""
    with pytest.raises(ValueError, match=r"FillReason"):
        FillResult(
            fills=(),
            filled_size=0.0,
            remaining_size=10.0,
            avg_price=None,
            total_fee=0.0,
            status="unfilled",
            levels_consumed=0,
            reason="crossed_boook",  # type: ignore[arg-type]
        )


def test_fill_result_rejects_a_reason_on_a_completed_fill() -> None:
    """`reason` explains a refusal; a fill has nothing to explain."""
    with pytest.raises(ValueError, match=r"must not carry a reason"):
        FillResult(
            fills=(_a_fill(),),
            filled_size=10.0,
            remaining_size=0.0,
            avg_price=0.4,
            total_fee=0.0,
            status="filled",
            levels_consumed=1,
            reason="crossed_book",
        )


# ---------------------------------------------------------------------------
# `rng_seed` is inert (recorded, not relied on)
# ---------------------------------------------------------------------------


def test_rng_seed_does_not_change_any_result() -> None:
    """The docstring's claim, machine-checked: the engine is deterministic."""
    models: dict[VenueId, FeeModel] = {"polymarket": PolymarketFeeModel()}
    outcomes = []
    for seed in (1, 2, 3, 4, 5):
        engine = SimulatedFillEngine(
            fee_models=models, schedules=_schedules, rng_seed=seed
        )
        result = engine.fill(_order(price=0.41, size=150.0), make_book([], ASKS), NOW)
        outcomes.append((result.filled_size, result.avg_price, result.total_fee))

    assert len(set(outcomes)) == 1


# ---------------------------------------------------------------------------
# Property test: the status/size invariants over 200 random books
# ---------------------------------------------------------------------------


def _random_book(rng: random.Random, side: str) -> tuple[OrderBook, float]:
    """Build a random book on the side `side` will consume, plus a limit price."""
    n_levels = rng.randint(0, 5)
    ticks = sorted(rng.sample(range(1, 99), n_levels)) if n_levels else []
    if side == "BUY":
        levels = [(t / 100.0, round(rng.uniform(0.5, 200.0), 4)) for t in ticks]
        book = make_book([], levels)
    else:
        levels = [
            (t / 100.0, round(rng.uniform(0.5, 200.0), 4)) for t in reversed(ticks)
        ]
        book = make_book(levels, [])
    return book, rng.randint(1, 99) / 100.0


def test_status_and_remaining_size_never_disagree() -> None:
    """T07 acceptance 2, over 200 random books with a fixed seed.

    `"partial"` never comes with `remaining_size == 0`; `"filled"` never
    with `remaining_size > 0`. Also re-derives, independently of the
    engine, the weighted average price, the per-fill fee sum, and the
    limit-price constraint on every fill.
    """
    rng = random.Random(20260904)
    engine = _engine()
    model = PolymarketFeeModel()
    seen = {"filled": 0, "partial": 0, "unfilled": 0}

    for i in range(200):
        side = "BUY" if i % 2 == 0 else "SELL"
        tif: TimeInForce = ("GTC", "IOC", "FOK")[i % 3]
        book, limit = _random_book(rng, side)
        size = round(rng.uniform(0.5, 400.0), 4)
        order = _order(side=side, price=limit, size=size, tif=tif)

        result = engine.fill(order, book, NOW)
        seen[result.status] += 1

        # --- the acceptance-2 invariants -------------------------------
        if result.status == "filled":
            assert result.remaining_size == 0.0
        if result.status == "partial":
            assert result.remaining_size > 0.0
            assert result.filled_size > 0.0
        if result.status == "unfilled":
            assert result.filled_size == 0.0
            assert result.fills == ()
            assert result.avg_price is None
            # Every refusal explains itself; a filled/partial never does.
            assert result.reason is not None
        else:
            assert result.reason is None

        # No `VenueMarket` was supplied, so nothing here was tick-checked
        # and every result says so.
        assert result.metadata[TICK_VALIDATED_KEY] is False

        # --- conservation: nothing is created or lost -------------------
        assert result.filled_size + result.remaining_size == pytest.approx(
            size, rel=1e-9, abs=1e-9
        )
        assert result.filled_size <= size + 1e-9
        assert result.levels_consumed == len(result.fills)

        # --- FOK is all-or-nothing --------------------------------------
        if tif == "FOK":
            assert result.status in ("filled", "unfilled")

        # --- fills respect the limit and the book -----------------------
        for fill in result.fills:
            if side == "BUY":
                assert fill.price <= limit + 1e-12
            else:
                assert fill.price >= limit - 1e-12
            assert fill.liquidity == "taker"

        # --- independently recomputed price and fee ----------------------
        if result.fills:
            expected_avg = math.fsum(f.price * f.size for f in result.fills) / math.fsum(
                f.size for f in result.fills
            )
            assert result.avg_price == pytest.approx(expected_avg, abs=1e-9)
            expected_fee = math.fsum(
                model.fee(f.price, f.size, "taker", POLY_SCHEDULE) for f in result.fills
            )
            assert result.total_fee == pytest.approx(expected_fee, abs=1e-12)

    # The sweep must actually exercise all three statuses, or it proves
    # nothing about the invariants it claims to check.
    assert all(count > 0 for count in seen.values()), seen
