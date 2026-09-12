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
    DEFAULT_TAPER_HOURS,
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
    # 0.40/0.60 (spread 0.20) is no longer wide enough: the calibrated
    # `DEFAULT_MIN_SPREAD` moved to 0.25 (the 1-minute Kalshi holdout,
    # `app/strategies/market_making.py`). 0.30/0.70 (spread 0.40) is the
    # fixture the inventory tests below already use as "genuinely wide".
    pair = MarketMaker().quote(_book(0.30, 0.70), tick_size=TICK)

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
    taker fee instead of earning them.

    0.30/0.70 (spread 0.40), not 0.40/0.60 (spread 0.20): the calibrated
    `DEFAULT_MIN_SPREAD` moved to 0.25, so 0.20 is refused outright.
    """
    pair = MarketMaker().quote(_book(0.30, 0.70), tick_size=TICK)

    assert 0.30 < pair.bid.price < pair.ask.price < 0.70


def test_the_quote_straddles_the_mid() -> None:
    pair = MarketMaker().quote(_book(0.30, 0.70), tick_size=TICK)

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
    """Even at maximum inventory and maximum skew.

    0.30/0.70 (spread 0.40), not 0.40/0.60 (spread 0.20): the calibrated
    `DEFAULT_MIN_SPREAD` moved to 0.25, and this test does not override
    it, so a 0.20-wide book would be refused outright -- both sides
    `None` -- which would make the assertions below pass VACUOUSLY
    (`if pair.bid is not None` never true) rather than actually
    exercising the skew-vs-touch guard.
    """
    for inventory in (-100.0, 100.0):
        pair = MarketMaker(skew_strength=5.0, max_inventory=100.0).quote(
            _book(0.30, 0.70), tick_size=TICK, inventory=inventory
        )
        if pair.bid is not None:
            assert pair.bid.price < 0.70
        if pair.ask is not None:
            assert pair.ask.price > 0.30


# -- taper (T6) ---------------------------------------------------------


def test_taper_zero_is_inert_regardless_of_hours_to_close() -> None:
    """`taper_hours=0.0` (`DEFAULT_TAPER_HOURS`) disables the taper
    unconditionally: every existing call to `quote()` -- one with no
    `hours_to_close` at all -- must produce IDENTICAL quotes to one that
    passes any `hours_to_close`, including a negative or huge value."""
    mm = MarketMaker(taper_hours=0.0)
    baseline = mm.quote(_book(0.30, 0.70), tick_size=TICK, inventory=30.0)

    for hours in (0.0, 1.0, 100.0, -5.0):
        tapered = mm.quote(
            _book(0.30, 0.70), tick_size=TICK, inventory=30.0,
            hours_to_close=hours,
        )
        assert tapered.bid == baseline.bid
        assert tapered.ask == baseline.ask
        assert tapered.reason == baseline.reason
        assert tapered.metadata["effective_max_inventory"] == mm.max_inventory


def test_hours_to_close_none_means_no_taper_regardless_of_taper_hours() -> None:
    """A configured taper (`taper_hours > 0`) must still do nothing when
    the caller does not know `hours_to_close` -- inventing a distance to
    close would be inventing the fact the taper depends on."""
    mm = MarketMaker(taper_hours=6.0)

    pair = mm.quote(_book(0.30, 0.70), tick_size=TICK, hours_to_close=None)

    assert pair.metadata["effective_max_inventory"] == mm.max_inventory


def test_the_effective_limit_shrinks_monotonically_toward_close() -> None:
    mm = MarketMaker(taper_hours=6.0)
    hours = [6.0, 4.0, 2.0, 1.0, 0.5, 0.0]

    limits = [
        mm.quote(
            _book(0.30, 0.70), tick_size=TICK, hours_to_close=h
        ).metadata["effective_max_inventory"]
        for h in hours
    ]

    assert limits == sorted(limits, reverse=True)
    assert len(set(limits)) == len(limits)  # strictly, not just weakly, falling
    assert limits[0] == mm.max_inventory  # hours_to_close == taper_hours: full limit
    assert limits[-1] == mm.quote_size  # at close: exactly one quote_size


def test_the_taper_floor_holds_past_close_rather_than_extrapolating(
) -> None:
    """A candle whose `end_ts` lands after the market's own `close_ts`
    (messy data, not the ordinary case) clamps to the floor rather than
    projecting the line below it."""
    mm = MarketMaker(taper_hours=6.0)

    pair = mm.quote(_book(0.30, 0.70), tick_size=TICK, hours_to_close=-3.0)

    assert pair.metadata["effective_max_inventory"] == mm.quote_size


def test_the_taper_withdraws_a_side_sooner_than_the_untapered_limit_would(
) -> None:
    """The whole mechanism, in one behavioral test: an inventory that the
    untapered limit still permits gets withdrawn once the taper has
    shrunk the effective limit below it, which is what "stops the policy
    from adding" means (`DEFAULT_TAPER_HOURS`'s docstring)."""
    mm = MarketMaker(taper_hours=6.0, max_inventory=50.0, quote_size=10.0)
    inventory = 20.0  # below max_inventory (50), above the close floor (10)

    far = mm.quote(
        _book(0.30, 0.70), tick_size=TICK, inventory=inventory,
        hours_to_close=6.0,
    )
    at_close = mm.quote(
        _book(0.30, 0.70), tick_size=TICK, inventory=inventory,
        hours_to_close=0.0,
    )

    assert far.bid is not None  # untapered: 20 < 50, still quoting
    assert at_close.bid is None  # tapered: 20 >= floor (10), withdrawn
    assert at_close.reason == "inventory_long_limit"


def test_default_taper_hours_is_disabled_pending_evidence() -> None:
    """Pinned to `.claude/kits/mm-proveout/reports/kalshi-taper.md`: the
    T6 sweep (`taper_hours` in {0, 1, 3, 6, 12, 24}, 11,911-market
    1-minute Kalshi holdout, `overlap=0` against the tuning cache, at the
    calibrated 0.90/0.25/50.0 defaults) measured `ci_low` FALLING
    monotonically as `taper_hours` rises -- test-split, pessimistic:
    `+0.4631` (taper 0) -> `+0.4395` -> `+0.3276` -> `+0.2469` ->
    `+0.0969` -> `+0.0341` (taper 24) -- never once above the untapered
    value. The CI does narrow (width 0.5953 -> 0.4431, -25.6%), but the
    mean falls faster (`+0.7717` -> `+0.2633`, -65.9%), so the narrowing
    never pays for itself: `two_split_rule` picked `taper_hours=0.0` as
    the argmax on all 60 random halves (`wins=0/60`, `candidate_key=
    None`), because ROC falls monotonically in `taper_hours` too. The
    default stays at 0.0."""
    assert DEFAULT_TAPER_HOURS == 0.0


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


# -- the 0.10-0.25 gap band, and behavior tied to the actual defaults --
#
# A Phase 1 review of the 2026-09-08 default change (0.80/0.10/20.0 ->
# 0.90/0.25/50.0) found the suite could detect that a default CHANGED
# but never that it was WRONG: mutating any one of DEFAULT_MIN_SPREAD,
# DEFAULT_EDGE_FRACTION or DEFAULT_MAX_INVENTORY back to its old value
# left all other tests green, because every existing behavioral test
# either uses a book far outside 0.10-0.25 (0.01 wide or 0.40 wide -- the
# only two widths this file used before now) or overrides the parameter
# under test explicitly rather than exercising the bare default. The
# tests below close that gap: each ties its assertion to a fixed,
# hand-computed literal so that changing the corresponding DEFAULT_*
# constant flips the outcome, rather than re-deriving the expectation
# from whatever the constant currently holds.


def test_a_gap_band_book_is_refused_at_the_default_and_quoted_at_the_looser_shipped_value() -> None:
    """No prior fixture in this file has a spread inside 0.10-0.25 -- the
    exact band the 2026-09-08 change is about. 0.40/0.55 (spread ~0.15)
    sits squarely inside it, comfortably clear of both edges.

    `min_spread=DEFAULT_MIN_SPREAD` (not a bare `MarketMaker()`) so a
    mutation of the constant itself changes what this test exercises:
    at today's calibrated 0.25 the book is refused; if the default ever
    reverts to 0.10 the SAME book must instead be quoted, which is
    exactly the branch below checks against 0.10 explicitly -- the
    other of the two values this kit has shipped or proposed, not
    incidental test data.
    """
    band_book = _book(0.40, 0.55)  # spread ~0.1500

    at_current_default = MarketMaker(min_spread=DEFAULT_MIN_SPREAD).quote(
        band_book, tick_size=TICK
    )
    at_the_looser_shipped_value = MarketMaker(min_spread=0.10).quote(
        band_book, tick_size=TICK
    )

    assert at_current_default.bid is None and at_current_default.ask is None
    assert at_current_default.reason == "spread_below_minimum"
    assert at_the_looser_shipped_value.is_two_sided
    assert at_the_looser_shipped_value.reason == "quoting_two_sided"


def test_the_default_inventory_cap_withdraws_before_the_prior_default_would() -> None:
    """Neither `test_hitting_the_long_limit_withdraws_the_bid_only` nor
    its short-side twin exercises `DEFAULT_MAX_INVENTORY`: both pass
    `max_inventory=100.0` explicitly, so that constant could move to any
    value and neither test would notice.

    30 contracts sits strictly between this kit's two calibrated caps
    (the prior default 20.0, the current 50.0). Under the current
    default a bare `MarketMaker()` must still be quoting at 30; a reader
    who reverts `DEFAULT_MAX_INVENTORY` to 20.0 should see this go red,
    because 30 would then be AT the (lowered) cap.
    """
    pair = MarketMaker().quote(_book(0.30, 0.70), tick_size=TICK, inventory=30.0)

    assert pair.bid is not None
    assert pair.reason == "quoting_two_sided"


def test_the_default_inventory_cap_withdraws_exactly_at_its_own_shipped_value() -> None:
    """The boundary itself, tied to the literal DEFAULT_MAX_INVENTORY
    currently ships with (50.0), not to `mm.max_inventory` read back at
    call time -- reading the attribute back would make this pass under
    ANY mutation of the constant, which is the trap GUARDRAILS §5 warns
    against (a test that computes its own expectation from the code
    under test)."""
    assert DEFAULT_MAX_INVENTORY == 50.0, (
        "this test's literal boundary (50.0) is pinned to today's "
        "shipped default -- update the literal deliberately, in the "
        "same commit as the constant, if it ever changes"
    )
    pair = MarketMaker().quote(_book(0.30, 0.70), tick_size=TICK, inventory=50.0)

    assert pair.bid is None
    assert pair.reason == "inventory_long_limit"


def test_the_default_edge_fraction_places_the_quote_at_an_exact_shipped_price() -> None:
    """0.30/0.70 book: spread 0.40, mid 0.50. At the CURRENT
    `DEFAULT_EDGE_FRACTION` (0.90) the policy keeps 90% of the half
    -spread as edge: `half_edge = 0.20 * 0.90 = 0.18`, so bid=0.32 and
    ask=0.68 -- both already on the 0.01 tick, so rounding is a no-op.
    Hand-computed, tied to today's literal 0.90 rather than to
    `mm.edge_fraction`, for the same reason as the inventory-boundary
    test above: reading the attribute back would survive a mutation of
    `DEFAULT_EDGE_FRACTION` instead of catching it."""
    assert DEFAULT_EDGE_FRACTION == 0.90, (
        "this test's literal prices (0.32/0.68) are pinned to today's "
        "shipped default -- update them deliberately if it changes"
    )
    pair = MarketMaker().quote(_book(0.30, 0.70), tick_size=TICK)

    assert pair.bid.price == pytest.approx(0.32)
    assert pair.ask.price == pytest.approx(0.68)


def test_edge_fraction_places_the_quote_at_an_exact_checkable_price() -> None:
    """The general formula, independent of whichever value
    `DEFAULT_EDGE_FRACTION` currently holds: an explicit `edge_fraction
    =0.5` on the same 0.30/0.70 book keeps exactly half the half-spread
    -- `half_edge = 0.20 * 0.5 = 0.10` -- so the quote sits exactly
    halfway between the touch and the mid on each side: bid=0.40,
    ask=0.60. Computed by hand, not with `MarketMaker`'s own formula."""
    pair = MarketMaker(edge_fraction=0.5).quote(_book(0.30, 0.70), tick_size=TICK)

    assert pair.bid.price == pytest.approx(0.40)
    assert pair.ask.price == pytest.approx(0.60)


# -- parameters -------------------------------------------------------


def test_the_thresholds_match_the_measurement_they_came_from() -> None:
    """Pinned so a later tune has to argue with the evidence.

    0.25 is the only spread bucket profitable under BOTH fill models
    (`0.10-0.25` was comfortably positive only at the front of the
    queue, not behind it). `DEFAULT_MIN_SPREAD` and
    `CONSERVATIVE_MIN_SPREAD` now name the same value — the calibrated
    default caught up to the conservative one.

    What stands: the old 0.10, replayed on 11,911 holdout markets at
    1-minute resolution, has an overall CI entirely below zero,
    `[-0.3143, -0.0434]` (n_trading=6,284). Quoting that tight loses
    money once adverse selection is measured at the resolution a live
    quoter experiences, and that result does not depend on the split.

    What does NOT stand is calling 0.25 "confirmed out of sample". The
    Phase 1 review rejected that holdout: it excluded by `market_id`
    only, so 6,073 of its 11,911 markets (51%) share an EVENT with the
    tuning cache while the CI is event-clustered, and its temporal test
    window is 1.70 days of one holiday weekend. On event-disjoint
    markets the 0.25 verdict spans zero. See the module docstring of
    `app/strategies/market_making.py` — these values are not certified
    by this kit's two-split rule, which was never run at this
    resolution for this grid.
    """
    assert DEFAULT_MIN_SPREAD == 0.25
    assert CONSERVATIVE_MIN_SPREAD == 0.25


def test_the_calibrated_defaults_are_the_measured_ones() -> None:
    """Pinned to the 1-minute Kalshi holdout
    (`.claude/kits/mm-proveout/reports/kalshi-holdout.md`), the clean
    out-of-sample test of this policy: 11,911 Kalshi markets that took
    no part in selecting these parameters (`overlap=0` against the
    tuning cache, verified independently three times), 10-day lookback,
    69.98% train / temporal-test split, 7,303,230 candles. The candidate
    `0.90/0.25/50.0` clears zero on the pessimistic test CI
    `[+0.4522, +1.0977]` (mean +$0.7717/trading market, n_trading=1477,
    1.48x Gate 1's 1,000-market power floor); the old defaults
    `0.80/0.10/20.0` span zero at `[-0.1646, +0.3154]` (n_trading=2150)
    and their overall CI is entirely below zero, `[-0.3143, -0.0434]`
    (n_trading=6,284) — the old defaults were calibrated on hourly
    candles that reported +$0.2913/market overall for this exact
    policy; the 1-minute replay measures -$0.1707. That is a sign flip,
    not a magnitude adjustment. `max_inventory=50.0` pairs with
    `skew_strength` staying at 1.0 (unchanged, see that constant's own
    docstring) as the primary risk control."""
    assert DEFAULT_EDGE_FRACTION == 0.90
    assert DEFAULT_MAX_INVENTORY == 50.0


def test_raising_the_inventory_limit_without_skew_is_the_documented_tail() -> None:
    """A regression guard on the pairing, not on either value alone.

    Originally measured worst single-market P&L at edge 0.80: -8.90 at
    max_inventory 20, but -46.25 at 100 with skew 0. The two parameters
    are substitutes, so a default that loosens one while zeroing the
    other re-opens that tail. `max_inventory` has since moved to 50.0
    (the 1-minute Kalshi holdout,
    `.claude/kits/mm-proveout/reports/kalshi-holdout.md`) precisely
    alongside `skew_strength` staying active, not being zeroed — this
    pins that the SHIPPED default still keeps skew active regardless of
    which `max_inventory` ships with it.
    """
    assert MarketMaker().skew_strength > 0.0


@pytest.mark.parametrize(
    "kwargs",
    [{"edge_fraction": 0.0}, {"edge_fraction": 1.5}, {"min_spread": 0.0},
     {"max_inventory": 0.0}, {"quote_size": 0.0}, {"skew_strength": -1.0},
     {"taper_hours": -1.0}],
)
def test_out_of_range_parameters_are_rejected_at_construction(kwargs) -> None:
    with pytest.raises(ValueError):
        MarketMaker(**kwargs)
