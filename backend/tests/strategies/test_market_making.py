"""The quoting policy's decisions, including the ones that are refusals.

Every threshold here traces to a measurement recorded in the module
docstring, so the tests assert the POLICY, not arbitrary numbers: quote
only where passive quoting was measured to pay, never cross the touch,
and lean against the inventory drift that live fills actually produce.
"""
from datetime import UTC, datetime

import pytest

from app.strategies.market_making import (
    CONSERVATIVE_MIN_SPREAD,
    DEFAULT_EDGE_FRACTION,
    DEFAULT_MAX_INVENTORY,
    DEFAULT_MIN_SPREAD,
    MarketMaker,
    round_to_tick,
)
from app.venues.types import BookLevel, OrderBook

TICK = 0.01


def _book(bid: float | None, ask: float | None, *, size: float = 500.0) -> OrderBook:
    return OrderBook(
        venue="kalshi",
        market_id="KXTEST-1",
        outcome="YES",
        bids=() if bid is None else (BookLevel(bid, size),),
        asks=() if ask is None else (BookLevel(ask, size),),
        ts=datetime(2026, 9, 6, tzinfo=UTC),
    )


# -- refusals ---------------------------------------------------------


def test_a_tight_book_is_refused_because_quoting_it_lost_money() -> None:
    """Measured: spread <= 0.02 realized +0.0000 at the front of the queue
    and -0.0156 behind it. A tight book is not a smaller opportunity."""
    pair = MarketMaker().quote(_book(0.50, 0.51), tick_size=TICK)

    assert pair.bid is None and pair.ask is None
    assert pair.reason == "spread_below_minimum"


def test_a_wide_book_is_quoted_on_both_sides() -> None:
    pair = MarketMaker().quote(_book(0.40, 0.60), tick_size=TICK)

    assert pair.is_two_sided
    assert pair.reason == "quoting_two_sided"


def test_a_one_sided_book_is_refused_rather_than_given_an_invented_mid() -> None:
    assert MarketMaker().quote(_book(0.40, None), tick_size=TICK).reason == (
        "one_sided_book"
    )
    assert MarketMaker().quote(_book(None, 0.60), tick_size=TICK).reason == (
        "one_sided_book"
    )


def test_a_crossed_book_is_refused() -> None:
    pair = MarketMaker().quote(_book(0.60, 0.40), tick_size=TICK)

    assert pair.bid is None and pair.ask is None
    assert pair.reason == "crossed_or_locked_book"


# -- the quote itself -------------------------------------------------


def test_the_quote_improves_the_touch_without_ever_crossing_it() -> None:
    """Improving earns queue priority — the variable the measurement said
    decides everything — but crossing would pay the spread and the 7%
    taker fee instead of earning them."""
    pair = MarketMaker().quote(_book(0.40, 0.60), tick_size=TICK)

    assert 0.40 < pair.bid.price < pair.ask.price < 0.60


def test_the_quote_straddles_the_mid() -> None:
    pair = MarketMaker().quote(_book(0.40, 0.60), tick_size=TICK)

    assert pair.bid.price < 0.50 < pair.ask.price


@pytest.mark.parametrize("edge_fraction", [0.1, 0.5, 0.9, 1.0])
def test_a_wider_edge_fraction_never_produces_a_worse_price(edge_fraction) -> None:
    """More edge means quoting further from the mid, on both sides."""
    tight = MarketMaker(edge_fraction=0.1).quote(_book(0.30, 0.70), tick_size=TICK)
    wide = MarketMaker(edge_fraction=edge_fraction).quote(
        _book(0.30, 0.70), tick_size=TICK
    )

    assert wide.bid.price <= tight.bid.price
    assert wide.ask.price >= tight.ask.price


# -- inventory --------------------------------------------------------


def test_long_inventory_leans_the_quote_down_to_sell_it() -> None:
    # Half the configured limit, so the lean is exercised without the
    # withdrawal that fires AT the limit. Expressed relative to the
    # parameter rather than hardcoded, so recalibrating the limit does
    # not silently turn this into a test of something else.
    mm = MarketMaker()
    half = mm.max_inventory / 2.0
    flat = mm.quote(_book(0.30, 0.70), tick_size=TICK, inventory=0.0)
    long = mm.quote(_book(0.30, 0.70), tick_size=TICK, inventory=half)

    assert long.bid.price < flat.bid.price
    assert long.ask.price < flat.ask.price


def test_short_inventory_leans_the_quote_up_to_buy_it_back() -> None:
    """The live fill imbalance runs 1.5-2x toward sells, so this is the
    direction a two-sided quoter actually drifts."""
    mm = MarketMaker()
    half = mm.max_inventory / 2.0
    flat = mm.quote(_book(0.30, 0.70), tick_size=TICK, inventory=0.0)
    short = mm.quote(_book(0.30, 0.70), tick_size=TICK, inventory=-half)

    assert short.bid.price > flat.bid.price
    assert short.ask.price > flat.ask.price


def test_hitting_the_long_limit_withdraws_the_bid_only() -> None:
    pair = MarketMaker(max_inventory=100.0).quote(
        _book(0.30, 0.70), tick_size=TICK, inventory=100.0
    )

    assert pair.bid is None
    assert pair.ask is not None
    assert pair.reason == "inventory_long_limit"


def test_hitting_the_short_limit_withdraws_the_ask_only() -> None:
    pair = MarketMaker(max_inventory=100.0).quote(
        _book(0.30, 0.70), tick_size=TICK, inventory=-100.0
    )

    assert pair.ask is None
    assert pair.bid is not None
    assert pair.reason == "inventory_short_limit"


def test_skew_can_never_push_a_quote_through_the_touch() -> None:
    """Even at maximum inventory and maximum skew."""
    for inventory in (-100.0, 100.0):
        pair = MarketMaker(skew_strength=5.0, max_inventory=100.0).quote(
            _book(0.40, 0.60), tick_size=TICK, inventory=inventory
        )
        if pair.bid is not None:
            assert pair.bid.price < 0.60
        if pair.ask is not None:
            assert pair.ask.price > 0.40


# -- tick rounding ----------------------------------------------------


def test_rounding_only_ever_widens_the_quote() -> None:
    """A bid rounds down and an ask rounds up. Rounding a bid UP would
    pay more than the policy decided to pay."""
    assert round_to_tick(0.4567, 0.01, side="buy") == pytest.approx(0.45)
    assert round_to_tick(0.4567, 0.01, side="sell") == pytest.approx(0.46)


def test_rounding_stays_inside_the_unit_interval() -> None:
    assert round_to_tick(0.0001, 0.01, side="buy") == 0.0
    assert round_to_tick(0.9999, 0.01, side="sell") == 1.0


def test_a_zero_tick_is_rejected_rather_than_dividing_by_zero() -> None:
    with pytest.raises(ValueError, match="tick"):
        round_to_tick(0.5, 0.0, side="buy")


@pytest.mark.parametrize("tick", [0.5, 0.25, 0.2])
def test_a_coarse_tick_never_yields_a_quote_that_is_not_a_real_quote(tick) -> None:
    """Rounding on a coarse tick can push the bid to 0.00 and the ask to
    1.00, or invert the pair outright. Every such case must withdraw the
    offending side rather than rest an order that crosses our own book or
    sits outside the probability range.

    Asserted as a property rather than by matching one reason string,
    because several distinct guards can legitimately fire first and which
    one does is an implementation detail.
    """
    pair = MarketMaker(min_spread=0.05).quote(_book(0.45, 0.55), tick_size=tick)

    if pair.bid is not None:
        assert 0.0 < pair.bid.price < 0.55
    if pair.ask is not None:
        assert 0.45 < pair.ask.price < 1.0
    if pair.is_two_sided:
        assert pair.bid.price < pair.ask.price
    assert pair.reason != "quoting_two_sided" or pair.is_two_sided


# -- parameters -------------------------------------------------------


def test_the_thresholds_match_the_measurement_they_came_from() -> None:
    """Pinned so a later tune has to argue with the evidence.

    0.10 is the first spread bucket comfortably positive at the front of
    the queue and not negative behind it; 0.25 is the only bucket
    profitable under BOTH fill models.
    """
    assert DEFAULT_MIN_SPREAD == 0.10
    assert CONSERVATIVE_MIN_SPREAD == 0.25


def test_the_calibrated_defaults_are_the_measured_ones() -> None:
    """`edge_fraction` carries the whole result — it beat the previous
    0.5 default on 60 of 60 independent random halves — and the tight
    inventory limit is what replaces skew as the primary risk control."""
    assert DEFAULT_EDGE_FRACTION == 0.80
    assert DEFAULT_MAX_INVENTORY == 20.0


def test_raising_the_inventory_limit_without_skew_is_the_documented_tail() -> None:
    """A regression guard on the pairing, not on either value alone.

    Measured worst single-market P&L at edge 0.80: -8.90 at
    max_inventory 20, but -46.25 at 100 with skew 0. The two parameters
    are substitutes, so a default that loosens one while zeroing the
    other re-opens that tail. This pins that the SHIPPED default keeps
    skew active.
    """
    assert MarketMaker().skew_strength > 0.0


@pytest.mark.parametrize(
    "kwargs",
    [{"edge_fraction": 0.0}, {"edge_fraction": 1.5}, {"min_spread": 0.0},
     {"max_inventory": 0.0}, {"quote_size": 0.0}, {"skew_strength": -1.0}],
)
def test_out_of_range_parameters_are_rejected_at_construction(kwargs) -> None:
    with pytest.raises(ValueError):
        MarketMaker(**kwargs)
