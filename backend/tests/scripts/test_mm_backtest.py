"""The replay harness's arithmetic, pinned without touching the network.

Derived from TASKS.md T2's acceptance lines (a)-(h), not from the
implementation. Every candle here is synthetic and every adapter is a
stub or an `httpx.MockTransport` (GUARDRAILS.md §1.4 / market-edge §1.4:
no test ever contacts `kalshi.com`).

The properties each test pins, and the wrong answer it would otherwise
produce:

(a) The fill model is the whole result. A print that only TOUCHES the
    resting bid fills at the front of the queue and not behind it; a
    harness that cannot tell those apart reports one number for two
    incompatible worlds (`passive_fill.py`'s module docstring: +0.0051
    vs -0.0095 on the same data).
(b) The mark is candle i+2's mid, never i+1's -- a property of
    `markout_pnl`, NOT of `pnl`. Marking inside the interval the fill
    happened in scores the trade against a price the fill itself helped
    set (PLAN.md Risks: "Look-ahead through the mark"). The brief's
    criterion (b) originally named `pnl`, which was unsatisfiable:
    cash-settled P&L is mark-INDEPENDENT by construction, so no pair of
    candles can make it differ. The orchestrator corrected the criterion
    onto `markout_pnl` and added its complement --
    `test_cash_pnl_is_mark_path_independent_while_markout_is_not` --
    which is the half that pins `pnl` being the money.
(c) Terminal inventory settles at the venue's real result, never at a
    mark (PLAN.md D4). Marking it turned a null result into a
    significant one.
(d) A market whose `result` is not `yes`/`no` is excluded AND counted --
    survivorship is reported, not silently dropped (PLAN.md Risks,
    GUARDRAILS.md §4.2).
(e) Collateral accrues per QUOTED hour. Counting hours the policy
    refused to quote inflates the denominator of `roc` and understates
    the capital intensity of the strategy.
(f) Every numeric block carries `fill_model` and `terminal`
    (GUARDRAILS.md §2.1: a figure with neither label is a defect).
(g) The temporal split is `close_ts < cutoff` -> train, `>=` -> test
    (PLAN.md D5; the go/no-go is scored on the later closes).
(h) The maker rebate is never inside P&L (PLAN.md D6, GUARDRAILS.md
    §2.3): a schedule paying 25% back must produce the IDENTICAL P&L.

Plus the T1 carry-forward: one market whose candles fail to parse must
be COUNTED, never silently skipped (which would reinstate the
"corrupt payload reads as zero candles" defect T1 removed) and never
allowed to abort a whole collection run.
"""
import json
import math
import random
import statistics
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.config import Settings
from app.scripts.calibration import cluster_bootstrap
from app.scripts.mm_backtest import (
    CI_FLOOR_EVENTS,
    MIN_CANDLES,
    MIN_POWER_POOL_EVENTS,
    PORTFOLIO_SIZES,
    SETTLED_LISTING_PROVENANCE,
    CollectResult,
    MarketCandles,
    MarketRow,
    ReplayResult,
    UniverseCache,
    _close_week,
    _collect_and_flush,
    _cutoff_ts,
    _exclude_collected,
    _exclude_events,
    _load_excluded_events,
    _load_excluded_market_ids,
    _power_table,
    _split,
    _stratified_sample_by_close_week,
    _universe_cache_path,
    _universe_or_load,
    collect,
    print_report,
    read_cache,
    replay,
    report,
    settled_universe,
    write_cache,
    write_universe_cache,
)
from app.strategies.market_making import MarketMaker, QuotePair
from app.venues.kalshi.adapter import KalshiAdapter
from app.venues.kalshi.candles import Candle
from app.venues.types import FeeSchedule, VenueMarket

#: A schedule that charges the maker nothing, so every expected number
#: below is exact arithmetic rather than arithmetic minus a fee. The
#: fee's presence is pinned separately by
#: `test_the_maker_fee_is_charged_and_comes_from_the_model`.
NO_MAKER_FEE = FeeSchedule(taker_rate=0.07, maker_rate=0.0, source="test")

#: Quotes with no inventory skew, so a quote is a pure function of the
#: book and the hand-computed prices below do not depend on the order
#: fills happened in. `skew_strength` is exercised by
#: `tests/strategies/test_market_making.py`, not here.
#:
#: `min_spread`/`edge_fraction`/`max_inventory` are pinned EXPLICITLY to
#: the values this file's arithmetic was hand-derived against, rather
#: than left to `MarketMaker`'s calibrated defaults. Those defaults move
#: when calibration evidence moves (`app/strategies/market_making.py`
#: docstring) -- most recently 0.80/0.10/20.0 -> 0.90/0.25/50.0 on the
#: 1-minute Kalshi holdout -- and this file tests `mm_backtest.py`'s
#: replay/report ARITHMETIC against a known policy, not the calibrated
#: values themselves. Letting the numbers below silently track whatever
#: ships as the default would be exactly the "test changes meaning
#: because a default moved under it" defect this kit's guardrails warn
#: against.
POLICY = MarketMaker(
    quote_size=10.0, skew_strength=0.0,
    min_spread=0.10, edge_fraction=0.80, max_inventory=20.0,
)

# A book of 0.30 / 0.71 gives mid 0.505 and half-spread 0.205; at
# `edge_fraction` 0.80 the policy wants 0.505 -/+ 0.164, i.e. 0.341 and
# 0.669, which round CONSERVATIVELY (down for the bid, up for the ask)
# to 0.34 and 0.67. Deliberately off the tick by 0.1 of a tick on both
# sides so no expected number here rides a floating-point boundary.
WIDE_BID, WIDE_ASK = 0.30, 0.71
QUOTED_BID, QUOTED_ASK = 0.34, 0.67
QUOTE_SIZE = 10.0
# Collateral locked by that two-sided quote: a resting buy ties up its
# price, a resting sell ties up (1 - price), both times the size.
# 0.34 * 10 + (1 - 0.67) * 10 = 3.40 + 3.30 = 6.70
WIDE_COLLATERAL = 6.70


def _candle(
    end_ts: int,
    *,
    bid: float | None = None,
    ask: float | None = None,
    low: float | None = None,
    high: float | None = None,
    close: float | None = None,
    volume: float = 0.0,
) -> Candle:
    """One synthetic candle; `px_*` default to `None` (no prints)."""
    return Candle(
        end_ts=end_ts,
        bid_close=bid,
        ask_close=ask,
        px_low=low,
        px_high=high,
        px_close=close,
        volume=volume,
        open_interest=None,
    )


def _market(
    candles: list[Candle],
    *,
    result: str = "yes",
    market_id: str = "M1",
    event: str = "E1",
    close_ts: int = 1_700_100_000,
) -> MarketCandles:
    return MarketCandles(
        venue="kalshi",
        market_id=market_id,
        event=event,
        series="KX",
        close_ts=close_ts,
        result=result,
        candles=tuple(candles),
    )


def _venue_market(
    market_id: str,
    *,
    result: str | None,
    volume_fp: str,
    volume_24h_fp: str = "0",
    event_id: str | None = "EV-1",
    close_time: datetime = datetime(2026, 9, 1, tzinfo=UTC),
) -> VenueMarket:
    """A settled `VenueMarket` for the universe-side helpers.

    `event_id` and `close_time` are parameters because the two exclusion
    contracts read them: `--exclude-events-from` keys on the event (with
    `None` falling back to the market id, exactly as `_collect_one`
    stamps `MarketCandles.event`), and the close-week stratifier buckets
    on the close. Both default to the single fixed value every older test
    in this file was written against, so nothing below changes meaning.
    """
    return VenueMarket(
        venue="kalshi",
        market_id=market_id,
        event_id=event_id,
        question="Will it?",
        outcomes=("YES", "NO"),
        outcome_ids={"YES": market_id, "NO": market_id},
        rules_text="",
        resolution_source=None,
        close_time=close_time,
        expected_settle_time=None,
        status="resolved",
        result=result,
        tick_size=0.01,
        min_size=1.0,
        fee=NO_MAKER_FEE,
        raw={
            "event_ticker": "KXTEST-26SEP",
            "volume_fp": volume_fp,
            "volume_24h_fp": volume_24h_fp,
        },
    )


class _StubAdapter:
    """Just enough `KalshiAdapter` for `settled_universe`: no network."""

    def __init__(self, markets: list[VenueMarket]) -> None:
        self._markets = markets
        self.statuses: list[str | None] = []

    async def list_markets(
        self, status: str | None = None, updated_since: object = None
    ) -> list[VenueMarket]:
        del updated_since  # part of the adapter's signature, unused here
        self.statuses.append(status)
        return list(self._markets)


# ---------------------------------------------------------------------------
# (a) The fill model decides whether a TOUCH is a fill.
# ---------------------------------------------------------------------------


def test_a_touch_at_the_bid_fills_optimistically_but_not_pessimistically() -> None:
    """A print AT the resting bid fills only at the front of the queue."""
    candles = [
        _candle(3600, bid=WIDE_BID, ask=WIDE_ASK),
        # Prints reach down to EXACTLY the resting bid (0.34) and no
        # further, and never up to the resting ask (0.67).
        _candle(
            7200, bid=WIDE_BID, ask=WIDE_ASK,
            low=QUOTED_BID, high=0.50, close=0.40, volume=100.0,
        ),
        _candle(10800, bid=0.40, ask=0.60),
    ]
    market = _market(candles)

    optimistic = replay(
        [market], policy=POLICY, fill_model="optimistic", schedule=NO_MAKER_FEE
    )
    pessimistic = replay(
        [market], policy=POLICY, fill_model="pessimistic", schedule=NO_MAKER_FEE
    )

    assert optimistic.rows[0].n_fills == 1
    assert pessimistic.rows[0].n_fills == 0
    # Both models quoted the same hour -- only the FILL differs.
    assert optimistic.rows[0].quote_hours == pessimistic.rows[0].quote_hours == 1


def test_a_print_through_the_bid_fills_under_both_models() -> None:
    candles = [
        _candle(3600, bid=WIDE_BID, ask=WIDE_ASK),
        _candle(
            7200, bid=WIDE_BID, ask=WIDE_ASK,
            low=0.20, high=0.50, close=0.40, volume=100.0,
        ),
        _candle(10800, bid=0.40, ask=0.60),
    ]
    market = _market(candles)

    for model in ("optimistic", "pessimistic"):
        result = replay(
            [market], policy=POLICY, fill_model=model, schedule=NO_MAKER_FEE
        )
        assert result.rows[0].n_fills == 1, model
        assert result.fill_model == model


def test_zero_volume_still_counts_the_quote_hour_and_its_collateral() -> None:
    """Capital is locked whether or not anything printed against it."""
    candles = [
        _candle(3600, bid=WIDE_BID, ask=WIDE_ASK),
        _candle(7200, bid=WIDE_BID, ask=WIDE_ASK, volume=0.0),
        _candle(10800, bid=0.40, ask=0.60),
    ]

    result = replay(
        [_market(candles)],
        policy=POLICY,
        fill_model="optimistic",
        schedule=NO_MAKER_FEE,
    )

    row = result.rows[0]
    assert row.n_fills == 0
    assert row.quote_hours == 1
    assert row.collateral_mean == pytest.approx(WIDE_COLLATERAL)


# ---------------------------------------------------------------------------
# (b) The mark is candle i+2's mid, never i+1's -- of `markout_pnl`.
# ---------------------------------------------------------------------------


def test_the_mark_is_two_candles_ahead_not_one() -> None:
    """Marking at i+1 scores a fill against the interval it happened in.

    A round trip whose two legs are marked at DIFFERENT mids is the only
    construction in which the mark is observable at all (with the
    position flat at the end there is no settlement term to absorb it),
    which is exactly why it is the construction used here.

    This asserts on `markout_pnl`, the mark-dependent quality statistic.
    It CANNOT be asserted on `pnl`: cash-settled P&L never reads a mark,
    so no choice of candles moves it. `pnl` is pinned here too, at the
    trade arithmetic a real ledger would show for a flat round trip.
    """
    candles = [
        # i=0 quotes 0.34 / 0.67 from this book.
        _candle(3600, bid=WIDE_BID, ask=WIDE_ASK),
        # i=0's fills: prints run down through 0.34, never up to 0.67.
        # i=1 quotes 0.34 / 0.67 again from this same book (skew is 0).
        _candle(
            7200, bid=WIDE_BID, ask=WIDE_ASK,
            low=0.20, high=0.50, close=0.40, volume=100.0,
        ),
        # i=0's MARK (mid 0.75), and i=1's fills: prints run up through
        # 0.67, never down to 0.34.
        _candle(
            10800, bid=0.70, ask=0.80,
            low=0.70, high=0.80, close=0.75, volume=100.0,
        ),
        # i=1's MARK (mid 0.95).
        _candle(14400, bid=0.90, ask=1.00),
    ]

    result = replay(
        [_market(candles)],
        policy=POLICY,
        fill_model="pessimistic",
        schedule=NO_MAKER_FEE,
    )

    row = result.rows[0]
    assert row.n_fills == 2
    # Bought 10 at 0.34, marked at candle 2's mid 0.75:
    #     +10 * (0.75 - 0.34) = +4.10
    # Sold 10 at 0.67, marked at candle 3's mid 0.95:
    #     -10 * (0.95 - 0.67) = -2.80
    # Total +1.30, and the position is flat, so nothing settles.
    assert row.markout_pnl == pytest.approx(1.30)
    assert row.terminal_inventory == pytest.approx(0.0)
    # Had the marks come from i+1 (mids 0.505 and 0.75) this would be
    # +10 * (0.505 - 0.34) - 10 * (0.75 - 0.67) = +0.85.
    assert row.markout_pnl != pytest.approx(0.85)
    # The money for the same two fills: bought 10 at 0.34 (-$3.40), sold
    # 10 at 0.67 (+$6.70), flat at the end so nothing settles. No mark
    # appears in it, which is why (b) cannot be asserted against it.
    assert row.pnl == pytest.approx(3.30)


def test_cash_pnl_is_mark_path_independent_while_markout_is_not() -> None:
    """The complement of (b), and the real invariant of the pair.

    Two markets with IDENTICAL fills and an identical venue result,
    differing only in a candle that is used for nothing but a mark:
    `candles[3]`'s bid/ask is `i=1`'s mark and nothing else (quotes come
    from `candles[0..2]`, fills from `candles[1..3]`'s trade ranges,
    which are held equal). `pnl` must be the SAME number -- it is the
    money, and the money does not depend on which candles a scorer chose
    to look at -- while `markout_pnl` must move, because measuring the
    quote against fair value one interval later is exactly what it is
    for.
    """

    def market_with(mark_bid: float, mark_ask: float) -> MarketCandles:
        return _market(
            [
                _candle(3600, bid=0.30, ask=0.71),
                _candle(7200, bid=0.30, ask=0.71, low=0.20, high=0.50,
                        volume=100.0),
                _candle(10800, bid=0.30, ask=0.71, low=0.50, high=0.80,
                        volume=100.0),
                # i=1's MARK lives here; its trade range (i=2's fills) is
                # held identical across both markets.
                _candle(14400, bid=mark_bid, ask=mark_ask, low=0.50,
                        high=0.80, volume=100.0),
                _candle(18000, bid=0.90, ask=1.00),
            ],
            result="yes",
        )

    high_path = replay(
        [market_with(0.70, 0.80)], policy=POLICY, fill_model="pessimistic",
        schedule=NO_MAKER_FEE,
    ).rows[0]
    low_path = replay(
        [market_with(0.20, 0.30)], policy=POLICY, fill_model="pessimistic",
        schedule=NO_MAKER_FEE,
    ).rows[0]

    # Same three fills either way: buy 10 @ 0.34, sell 10 @ 0.67 twice,
    # ending 10 short into a "yes" settlement.
    assert high_path.n_fills == low_path.n_fills == 3
    assert high_path.terminal_inventory == pytest.approx(-10.0)
    assert low_path.terminal_inventory == pytest.approx(-10.0)

    # -3.40 + 6.70 + 6.70 - 10.00 = 0.00, both times.
    assert high_path.pnl == pytest.approx(0.0)
    assert low_path.pnl == pytest.approx(0.0)
    assert high_path.pnl == pytest.approx(low_path.pnl)

    # markout collapses to 10 * (m1 - m2) here (m3 is also the last mark,
    # so it cancels against the settlement term): m1 = 0.505 throughout,
    # m2 = 0.75 on the high path and 0.25 on the low one.
    assert high_path.markout_pnl == pytest.approx(-2.45)
    assert low_path.markout_pnl == pytest.approx(2.55)
    assert high_path.markout_pnl != pytest.approx(low_path.markout_pnl)


# ---------------------------------------------------------------------------
# (c) Terminal inventory settles at the venue's real result.
# ---------------------------------------------------------------------------


def test_a_short_carried_into_a_yes_settlement_pays_one_minus_the_last_mark(
) -> None:
    candles = [
        _candle(3600, bid=WIDE_BID, ask=WIDE_ASK),
        # Prints run up through the resting ask (0.67) and never down to
        # the resting bid, so the book ends SHORT.
        _candle(
            7200, bid=WIDE_BID, ask=WIDE_ASK,
            low=0.60, high=0.70, close=0.68, volume=100.0,
        ),
        _candle(10800, bid=0.60, ask=0.70),
    ]

    result = replay(
        [_market(candles, result="yes")],
        policy=POLICY,
        fill_model="pessimistic",
        schedule=NO_MAKER_FEE,
    )

    row = result.rows[0]
    assert row.terminal_inventory == pytest.approx(-QUOTE_SIZE)
    assert row.held_into_settlement is True
    assert row.settled_short_into_yes is True
    # markout: sold 10 at 0.67, marked at candle 2's mid 0.65:
    #     -10 * (0.65 - 0.67) = +0.20
    # then 10 short settles into YES at 1.00 from a last mark of 0.65:
    #     -10 * (1.00 - 0.65) = -3.50
    assert row.markout_pnl == pytest.approx(0.20 - 3.50)
    # Cash: sold 10 at 0.67 (+$6.70), 10 short settles into YES
    # (-$10.00) -- the SAME -3.30, because with a single fill the marks
    # telescope out. That coincidence is why a one-fill construction
    # cannot tell the two conventions apart.
    assert row.pnl == pytest.approx(6.70 - 10.00)


def test_the_same_short_into_a_no_settlement_gains_the_last_mark() -> None:
    """The identical book with the opposite result must flip the sign."""
    candles = [
        _candle(3600, bid=WIDE_BID, ask=WIDE_ASK),
        _candle(
            7200, bid=WIDE_BID, ask=WIDE_ASK,
            low=0.60, high=0.70, close=0.68, volume=100.0,
        ),
        _candle(10800, bid=0.60, ask=0.70),
    ]

    result = replay(
        [_market(candles, result="no")],
        policy=POLICY,
        fill_model="pessimistic",
        schedule=NO_MAKER_FEE,
    )

    row = result.rows[0]
    assert row.settled_short_into_yes is False
    # -10 * (0.00 - 0.65) = +6.50, on top of the same +0.20 fill mark.
    assert row.markout_pnl == pytest.approx(0.20 + 6.50)
    # Cash: +$6.70 sold, 10 short settles into NO at 0.00, so nothing
    # is paid out -- +$6.70, the same number by the same telescoping.
    assert row.pnl == pytest.approx(6.70)


def test_a_market_with_an_unsettleable_result_cannot_enter_the_replay() -> None:
    """`replay` must never guess a settlement value (PLAN.md D4)."""
    with pytest.raises(ValueError, match="result"):
        _market([_candle(3600, bid=0.40, ask=0.60)], result="scalar")


# ---------------------------------------------------------------------------
# (d) Survivorship: a non-binary result is excluded AND counted.
# ---------------------------------------------------------------------------


async def test_settled_universe_excludes_non_binary_results_and_counts_them(
) -> None:
    adapter = _StubAdapter([
        _venue_market("M-YES", result="yes", volume_fp="5000"),
        _venue_market("M-NO", result="no", volume_fp="5000"),
        _venue_market("M-SCALAR", result="scalar", volume_fp="5000"),
        _venue_market("M-BLANK", result="", volume_fp="5000"),
    ])

    kept, excluded_by_result = await settled_universe(adapter, min_volume=2000.0)

    assert [m.market_id for m in kept] == ["M-YES", "M-NO"]
    assert excluded_by_result == 2
    assert adapter.statuses == ["resolved"]


async def test_settled_universe_does_not_count_thin_markets_as_survivorship(
) -> None:
    """A market dropped for VOLUME is not evidence about settlement."""
    adapter = _StubAdapter([
        _venue_market("M-THIN", result="scalar", volume_fp="10"),
        _venue_market("M-FAT", result="yes", volume_fp="5000"),
    ])

    kept, excluded_by_result = await settled_universe(adapter, min_volume=2000.0)

    assert [m.market_id for m in kept] == ["M-FAT"]
    assert excluded_by_result == 0


async def test_settled_universe_accepts_either_volume_field() -> None:
    """`volume_fp` OR `venue_volume` clearing the floor is enough."""
    adapter = _StubAdapter([
        _venue_market("M-24H", result="yes", volume_fp="10", volume_24h_fp="9000"),
    ])

    kept, _ = await settled_universe(adapter, min_volume=2000.0)

    assert [m.market_id for m in kept] == ["M-24H"]


# ---------------------------------------------------------------------------
# settled_universe survives a transport blip in its OWN list_markets call
# (Change 3, 2026-09-07); a universe cached at one --min-volume is never
# served to a run at another. A live run died ~80 minutes in on one dropped
# TLS read inside the up-to-150-page /events walk -- unlike the per-market
# candle fetch (Change 1's `_collect_one` catch), nothing on this path
# retried a transport error at all.
# ---------------------------------------------------------------------------


class _FlakyAdapter:
    """`list_markets` that raises a REAL `httpx` transport error `fail_times`
    times before succeeding.

    Raises the exception a dropped connection actually produces, from the
    handler, rather than returning a mocked 4xx/5xx response --
    `httpx.MockTransport` answers every request and can never itself
    produce one, which is exactly why four review roles missed the sibling
    defect this module's docstring records.
    """

    def __init__(self, markets: list[VenueMarket], *, fail_times: int) -> None:
        self._markets = markets
        self._fail_times = fail_times
        self.calls = 0

    async def list_markets(
        self, status: str | None = None, updated_since: object = None
    ) -> list[VenueMarket]:
        del status, updated_since
        self.calls += 1
        if self.calls <= self._fail_times:
            request = httpx.Request("GET", "https://api.elections.kalshi.com/events")
            raise httpx.ReadError("connection dropped mid-read", request=request)
        return list(self._markets)


async def test_a_transport_error_on_the_first_attempt_is_retried(
    monkeypatch,
) -> None:
    """One dropped read must not discard the whole walk.

    Before Change 3, `settled_universe`'s call to `list_markets` had no
    transport-error handling anywhere on it -- `_collect_one`'s catch
    (Change 1) guards a different, LATER call, over per-market candles,
    made only once collection has already started.
    """
    monkeypatch.setattr("app.scripts.mm_backtest._UNIVERSE_LIST_BACKOFF_S", 0)
    adapter = _FlakyAdapter(
        [_venue_market("M-OK", result="yes", volume_fp="5000")], fail_times=1
    )

    kept, excluded = await settled_universe(adapter, min_volume=2000.0)

    assert [m.market_id for m in kept] == ["M-OK"]
    assert excluded == 0
    assert adapter.calls == 2


async def test_persistent_transport_errors_exhaust_the_retries_and_name_the_count(
    monkeypatch,
) -> None:
    """Failures on every attempt must not retry forever, and must say so."""
    monkeypatch.setattr("app.scripts.mm_backtest._UNIVERSE_LIST_BACKOFF_S", 0)
    adapter = _FlakyAdapter([], fail_times=99)

    with pytest.raises(httpx.HTTPError, match="3 attempts"):
        await settled_universe(adapter, min_volume=2000.0)

    assert adapter.calls == 3


async def test_a_cached_universe_at_a_different_min_volume_is_ignored_and_rewalked(
    tmp_path,
) -> None:
    """A universe cached at one floor must never silently serve another.

    Serving `min_volume=500`'s cache to a `min_volume=2000` run would
    silently substitute a universe filtered at the wrong threshold --
    exactly the unlabelled substitution GUARDRAILS.md §7 forbids. The
    mismatch must be detected and the walk re-run instead of served.
    """
    cache_path = tmp_path / "run.json"
    write_universe_cache(
        _universe_cache_path(cache_path),
        UniverseCache(
            markets=[_venue_market("M-STALE", result="yes", volume_fp="5000")],
            excluded_by_result=0,
            min_volume=500.0,
            collected_at=datetime.now(tz=UTC).isoformat(),
        ),
    )
    adapter = _StubAdapter([
        _venue_market("M-FRESH", result="yes", volume_fp="5000"),
    ])

    kept, excluded = await _universe_or_load(
        adapter, min_volume=2000.0, cache_path=str(cache_path), refresh=False
    )

    assert [m.market_id for m in kept] == ["M-FRESH"]
    assert excluded == 0
    assert adapter.statuses == ["resolved"]


# ---------------------------------------------------------------------------
# T18: `--exclude-markets-from` must remove exactly the excluded ids, and a
# fresh sample drawn from what remains must have ZERO overlap with them. The
# candidate's 4.1x temporal-test ROC carries `temporal_win: true` because it
# was chosen partly for winning that split -- scoring it again on any of the
# 15,283 markets in `.cache/mm/kalshi-60m.json` is in-sample no matter how
# the parameters were tuned, so this is the property the whole task rests on.
# ---------------------------------------------------------------------------


def test_exclude_markets_from_removes_exactly_the_excluded_ids(tmp_path) -> None:
    """`_load_excluded_market_ids` reads ids only, and `_exclude_collected`
    drops exactly those from the universe -- no more, no fewer.
    """
    already_collected = CollectResult(
        markets=[
            _wide_market("M-OLD-1", "E-OLD-1", CUTOFF_TS),
            _wide_market("M-OLD-2", "E-OLD-2", CUTOFF_TS),
        ],
        failures=[],
        n_requested=2,
        n_short_history=0,
        n_payload_errors=0,
        n_request_errors=0,
        interval_minutes=60,
        lookback_days=10,
    )
    cache_path = tmp_path / "kalshi-60m.json"
    write_cache(cache_path, already_collected)

    universe = [
        _venue_market("M-OLD-1", result="yes", volume_fp="5000"),
        _venue_market("M-OLD-2", result="yes", volume_fp="5000"),
        _venue_market("M-NEW-1", result="yes", volume_fp="5000"),
        _venue_market("M-NEW-2", result="yes", volume_fp="5000"),
    ]

    excluded_ids = _load_excluded_market_ids(cache_path)
    assert excluded_ids == frozenset({"M-OLD-1", "M-OLD-2"})

    remaining = _exclude_collected(universe, excluded_ids)

    assert [m.market_id for m in remaining] == ["M-NEW-1", "M-NEW-2"]


def test_a_fresh_sample_has_zero_overlap_with_the_excluded_cache(tmp_path) -> None:
    """The end-to-end property T18 needs: sample from the filtered universe
    and it never contains a market the excluded cache already holds --
    reproducing the `_collect_or_load` order of operations (filter, THEN
    `random.sample`) rather than asserting on the filter in isolation.
    """
    excluded_cache = CollectResult(
        markets=[_wide_market(f"OLD-{i}", f"E-OLD-{i}", CUTOFF_TS) for i in range(50)],
        failures=[],
        n_requested=50,
        n_short_history=0,
        n_payload_errors=0,
        n_request_errors=0,
        interval_minutes=60,
        lookback_days=10,
    )
    cache_path = tmp_path / "kalshi-60m.json"
    write_cache(cache_path, excluded_cache)
    excluded_ids = _load_excluded_market_ids(cache_path)

    # A fresh universe that OVERLAPS the excluded cache on half its ids --
    # the overlap this test exists to prove drops to zero after filtering.
    universe = [
        _venue_market(f"OLD-{i}", result="yes", volume_fp="5000") for i in range(50)
    ] + [
        _venue_market(f"NEW-{i}", result="yes", volume_fp="5000") for i in range(50)
    ]
    assert not {m.market_id for m in universe}.isdisjoint(excluded_ids)  # sanity

    remaining = _exclude_collected(universe, excluded_ids)
    rng = random.Random(20260908)
    sample = rng.sample(remaining, 30)

    sample_ids = {m.market_id for m in sample}
    overlap = sample_ids & excluded_ids
    assert overlap == set(), f"sample overlapped the excluded cache: {overlap}"
    assert len(sample_ids) == 30
    assert sample_ids <= {f"NEW-{i}" for i in range(50)}


def test_exclude_markets_from_a_missing_file_raises_rather_than_excluding_nothing(
    tmp_path,
) -> None:
    """No silent no-op: a wrong `--exclude-markets-from` path must fail
    loudly rather than quietly excluding zero markets from the sample.
    """
    with pytest.raises(FileNotFoundError):
        _load_excluded_market_ids(tmp_path / "does-not-exist.json")


# ---------------------------------------------------------------------------
# T19: `--exclude-events-from`. Excluding by `market_id` is NOT enough, and
# this is not hypothetical -- measured on the shipped caches, the T18 holdout
# achieved a genuine `overlap=0` on ids while **3,183 events were shared** with
# the tuning cache and **6,073 of its 11,911 markets (51%) belonged to one of
# them**. The confidence interval is clustered BY EVENT, so the event is the
# unit of independence: a test market whose event also fed tuning shares its
# real-world outcome with a tuning market, which is exactly what an
# out-of-sample test exists to prevent. These tests pin the event contract,
# not merely the id contract.
# ---------------------------------------------------------------------------


def test_exclude_events_from_drops_a_market_the_id_filter_would_have_kept(
    tmp_path,
) -> None:
    """The whole reason this flag exists.

    `M-NEW-SHARED` has an id the tuning cache never held and an EVENT it
    did. `_exclude_collected` (ids) keeps it; `_exclude_events` drops it.
    Both are asserted here, side by side, so the gap is pinned rather
    than described.
    """
    already_collected = CollectResult(
        markets=[
            _wide_market("M-OLD-1", "E-SHARED", CUTOFF_TS),
            _wide_market("M-OLD-2", "E-OTHER", CUTOFF_TS),
        ],
        failures=[],
        n_requested=2,
        n_short_history=0,
        n_payload_errors=0,
        n_request_errors=0,
        interval_minutes=60,
        lookback_days=10,
    )
    cache_path = tmp_path / "kalshi-60m.json"
    write_cache(cache_path, already_collected)

    universe = [
        _venue_market("M-NEW-SHARED", result="yes", volume_fp="5000",
                      event_id="E-SHARED"),
        _venue_market("M-NEW-CLEAN", result="yes", volume_fp="5000",
                      event_id="E-FRESH"),
    ]

    assert _load_excluded_events(cache_path) == frozenset({"E-SHARED", "E-OTHER"})

    # The id filter -- what T18 shipped -- keeps the shared-event market.
    kept_by_ids = _exclude_collected(universe, _load_excluded_market_ids(cache_path))
    assert [m.market_id for m in kept_by_ids] == ["M-NEW-SHARED", "M-NEW-CLEAN"]

    # The event filter drops it.
    kept_by_events = _exclude_events(universe, _load_excluded_events(cache_path))
    assert [m.market_id for m in kept_by_events] == ["M-NEW-CLEAN"]


def test_a_fresh_sample_has_zero_event_overlap_with_the_excluded_cache(
    tmp_path,
) -> None:
    """The end-to-end property: filter by EVENT, then sample, and the drawn
    markets share no event with the excluded cache.

    Reproduces `_collect_or_load`'s own order of operations (exclude, THEN
    draw) rather than asserting on the filter in isolation, and asserts on
    the event set -- the id set coming back disjoint too is a consequence,
    not the property under test.
    """
    excluded_cache = CollectResult(
        markets=[
            _wide_market(f"OLD-{i}", f"E-OLD-{i % 10}", CUTOFF_TS) for i in range(50)
        ],
        failures=[],
        n_requested=50,
        n_short_history=0,
        n_payload_errors=0,
        n_request_errors=0,
        interval_minutes=60,
        lookback_days=10,
    )
    cache_path = tmp_path / "kalshi-60m.json"
    write_cache(cache_path, excluded_cache)
    excluded_events = _load_excluded_events(cache_path)
    assert len(excluded_events) == 10

    # Half the fresh universe carries ids the cache never saw but events it
    # did -- the 51% case, in miniature.
    universe = [
        _venue_market(f"NEW-SHARED-{i}", result="yes", volume_fp="5000",
                      event_id=f"E-OLD-{i % 10}")
        for i in range(50)
    ] + [
        _venue_market(f"NEW-CLEAN-{i}", result="yes", volume_fp="5000",
                      event_id=f"E-NEW-{i}")
        for i in range(50)
    ]
    assert {m.market_id for m in universe}.isdisjoint(
        _load_excluded_market_ids(cache_path)
    )  # sanity: an id-only test would already have "passed" here
    assert not {m.event_id for m in universe}.isdisjoint(excluded_events)

    remaining = _exclude_events(universe, excluded_events)
    rng = random.Random(20260912)
    sample = rng.sample(remaining, 30)

    sample_events = {m.event_id for m in sample}
    overlap = sample_events & excluded_events
    assert overlap == set(), f"sample overlapped the excluded events: {overlap}"
    assert len(sample) == 30
    assert {m.market_id for m in sample} <= {f"NEW-CLEAN-{i}" for i in range(50)}


def test_the_event_key_falls_back_to_the_market_id_on_both_sides(
    tmp_path,
) -> None:
    """A market with no `event_id` is its own cluster of one, and the
    exclusion must key on the SAME value `_collect_one` stamps.

    `MarketCandles.__post_init__` rewrites an empty `event` to the market
    id, so a cached eventless market is recorded under its own id. If the
    universe side read a bare `event_id` (None) instead of applying the
    same fallback, that market would be re-drawn -- a silent id-level
    overlap reintroduced by the event filter itself.
    """
    cached = CollectResult(
        markets=[_wide_market("M-NOEVENT", "", CUTOFF_TS)],
        failures=[],
        n_requested=1,
        n_short_history=0,
        n_payload_errors=0,
        n_request_errors=0,
        interval_minutes=60,
        lookback_days=10,
    )
    cache_path = tmp_path / "kalshi-60m.json"
    write_cache(cache_path, cached)

    assert _load_excluded_events(cache_path) == frozenset({"M-NOEVENT"})

    universe = [
        _venue_market("M-NOEVENT", result="yes", volume_fp="5000", event_id=None),
        _venue_market("M-OTHER", result="yes", volume_fp="5000", event_id=None),
    ]
    remaining = _exclude_events(universe, _load_excluded_events(cache_path))
    assert [m.market_id for m in remaining] == ["M-OTHER"]


def test_exclude_events_from_a_missing_file_raises_rather_than_excluding_nothing(
    tmp_path,
) -> None:
    """Same no-silent-no-op contract the id loader already has."""
    with pytest.raises(FileNotFoundError):
        _load_excluded_events(tmp_path / "does-not-exist.json")


# ---------------------------------------------------------------------------
# T19: stratified sampling by close week.
#
# WHY. Measured on `.cache/mm/kalshi-60m.universe.json` (50,470 settled
# markets, closes 2026-07-03 -> 2026-09-07): **89% of closes fall in the final
# 7 days of the 65.8-day span**, itself an artifact of the page-capped
# `list_markets(status="resolved")` listing (see SETTLED_LISTING_PROVENANCE).
# A natural draw therefore buys a 1.7-day test window at a 70%-by-count train
# share -- one holiday weekend. Drawing evenly ACROSS close weeks costs raw `n`
# in the sparse older weeks and buys a distribution in which a multi-week
# temporal split exists at all.
# ---------------------------------------------------------------------------


def _week_counts(markets) -> dict[str, int]:
    counts: dict[str, int] = {}
    for m in markets:
        counts[_close_week(m)] = counts.get(_close_week(m), 0) + 1
    return counts


def _spread(counts: dict[str, int], weeks) -> float:
    """Coefficient of variation of the per-week counts -- 0.0 is flat.

    Computed over EVERY week the universe holds, not only the weeks the
    sample happened to reach: a draw that lands entirely in one week has
    a spread of zero by that second reading, which is the opposite of
    what the statistic is for.
    """
    values = [counts.get(w, 0) for w in weeks]
    return statistics.pstdev(values) / statistics.fmean(values)


def _weekly_universe(per_week: dict[int, int]) -> list[VenueMarket]:
    """A universe with `per_week[day_offset_week]` markets in each week."""
    out: list[VenueMarket] = []
    base = datetime(2026, 7, 6, 12, tzinfo=UTC)  # a Monday
    for week_index, n in per_week.items():
        for i in range(n):
            out.append(_venue_market(
                f"W{week_index}-M{i}",
                result="yes",
                volume_fp="5000",
                event_id=f"W{week_index}-E{i}",
                close_time=base + timedelta(weeks=week_index, hours=i % 24),
            ))
    return out


def test_stratified_sampling_is_materially_flatter_than_a_natural_draw() -> None:
    """The property the flag exists for, measured as spread, not asserted
    as intent.

    One dominant week against five sparse ones -- the shape the real
    universe has. A natural `random.sample` reproduces the pool's skew;
    the stratified draw must be materially flatter, and here it is flat
    outright because every week can supply its quota.
    """
    universe = _weekly_universe({0: 40, 1: 40, 2: 40, 3: 40, 4: 40, 5: 4000})
    rng = random.Random(20260912)

    natural = rng.sample(universe, 120)
    stratified = _stratified_sample_by_close_week(universe, 120, rng=rng)

    assert len(stratified) == 120
    weeks = {_close_week(m) for m in universe}
    natural_counts = _week_counts(natural)
    stratified_counts = _week_counts(stratified)

    # Every week is represented, and each gets its equal share.
    assert len(stratified_counts) == 6
    assert set(stratified_counts.values()) == {20}
    # The natural draw is dominated by the one big week.
    assert max(natural_counts.values()) > 90
    assert _spread(stratified_counts, weeks) == 0.0
    assert _spread(natural_counts, weeks) > 2.0


def test_a_starved_week_gives_everything_it_has_and_the_rest_spills_over(
) -> None:
    """A week that cannot supply its quota is not a reason to fall back to
    a natural draw: it contributes all of itself and the shortfall is
    redistributed across the weeks that can still supply.

    This is the case the real venue is in -- after event-level exclusion
    the older weeks hold tens of markets each -- so the behaviour is
    pinned rather than discovered during a collection run.
    """
    universe = _weekly_universe({0: 3, 1: 5, 2: 500, 3: 500})
    stratified = _stratified_sample_by_close_week(
        universe, 200, rng=random.Random(7)
    )

    counts = _week_counts(stratified)
    assert len(stratified) == 200
    assert counts.get("2026-W28") == 3    # week 0, exhausted
    assert counts.get("2026-W29") == 5    # week 1, exhausted
    assert counts.get("2026-W30") == 96   # the shortfall spills evenly...
    assert counts.get("2026-W31") == 96   # ...onto the two weeks that supply


def test_a_stratified_draw_never_repeats_a_market_and_never_exceeds_the_pool(
) -> None:
    """Asking for more than exists returns the whole pool exactly once --
    the same contract `random.sample(universe, min(k, len(universe)))`
    already has in `_collect_or_load`.
    """
    universe = _weekly_universe({0: 10, 1: 10, 2: 10})
    stratified = _stratified_sample_by_close_week(
        universe, 10_000, rng=random.Random(1)
    )

    ids = [m.market_id for m in stratified]
    assert len(ids) == len(set(ids)) == 30
    assert set(ids) == {m.market_id for m in universe}


def test_the_close_week_key_is_the_iso_week_of_the_utc_close() -> None:
    """The bucket key is derived from the venue's own `close_time`, in UTC,
    as an ISO year-week -- so a market closing on a Sunday and one closing
    the following Monday are in different buckets, and the label sorts
    chronologically as a string.
    """
    sunday = _venue_market(
        "M-SUN", result="yes", volume_fp="5000",
        close_time=datetime(2026, 9, 6, 23, 0, tzinfo=UTC),
    )
    monday = _venue_market(
        "M-MON", result="yes", volume_fp="5000",
        close_time=datetime(2026, 9, 7, 1, 0, tzinfo=UTC),
    )

    assert _close_week(sunday) == "2026-W36"
    assert _close_week(monday) == "2026-W37"
    assert _close_week(sunday) < _close_week(monday)


# ---------------------------------------------------------------------------
# (e) Collateral accrues only for quoted hours.
# ---------------------------------------------------------------------------


def test_collateral_accrues_only_for_hours_the_policy_actually_quoted() -> None:
    candles = [
        _candle(3600, bid=WIDE_BID, ask=WIDE_ASK),      # i=0 quotes
        _candle(7200, bid=0.49, ask=0.51),              # i=1 too tight
        _candle(10800, bid=WIDE_BID, ask=WIDE_ASK),     # i=2 quotes
        _candle(14400, bid=0.40, ask=0.60),
        _candle(18000, bid=0.40, ask=0.60),
    ]

    result = replay(
        [_market(candles)],
        policy=POLICY,
        fill_model="optimistic",
        schedule=NO_MAKER_FEE,
    )

    row = result.rows[0]
    # Three intervals have i, i+1 and i+2 available; the 0.02-wide book
    # at i=1 is below POLICY's pinned `min_spread` (0.10), so the policy
    # withdraws both
    # sides and locks nothing.
    assert row.quote_hours == 2
    assert row.collateral_mean == pytest.approx(WIDE_COLLATERAL)
    # Averaging over all three intervals would give 6.70 * 2 / 3.
    assert row.collateral_mean != pytest.approx(WIDE_COLLATERAL * 2.0 / 3.0)
    assert row.n_fills == 0
    assert row.pnl == pytest.approx(0.0)


def test_a_market_that_never_quotes_reports_no_collateral_and_no_quote_hours(
) -> None:
    candles = [_candle(3600 * i, bid=0.49, ask=0.51) for i in range(1, 5)]

    result = replay(
        [_market(candles)],
        policy=POLICY,
        fill_model="optimistic",
        schedule=NO_MAKER_FEE,
    )

    row = result.rows[0]
    assert row.quote_hours == 0
    assert row.collateral_mean == pytest.approx(0.0)
    assert report(result, split="event")["overall"]["n_quoted"] == 0


# ---------------------------------------------------------------------------
# (f) Every numeric block carries `fill_model` and `terminal`.
# ---------------------------------------------------------------------------


def _numeric_blocks(node: object, path: str = "root"):
    """Yield `(path, dict)` for every dict holding a number DIRECTLY."""
    if isinstance(node, dict):
        if any(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            for value in node.values()
        ):
            yield path, node
        for key, value in node.items():
            yield from _numeric_blocks(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _numeric_blocks(value, f"{path}[{i}]")


def _wide_market(market_id: str, event: str, close_ts: int) -> MarketCandles:
    candles = [
        _candle(3600, bid=WIDE_BID, ask=WIDE_ASK),
        _candle(
            7200, bid=WIDE_BID, ask=WIDE_ASK,
            low=0.20, high=0.50, close=0.40, volume=100.0,
        ),
        _candle(10800, bid=0.40, ask=0.60),
    ]
    return _market(candles, market_id=market_id, event=event, close_ts=close_ts)


def test_every_numeric_block_of_a_report_names_its_fill_model_and_terminal(
) -> None:
    markets = [
        _wide_market(f"M{i}", f"E{i}", 1_700_000_000 + i * 86_400) for i in range(8)
    ]
    result = replay(
        markets, policy=POLICY, fill_model="pessimistic", schedule=NO_MAKER_FEE
    )

    payload = report(
        result, split="temporal", cutoff_ts=1_700_000_000 + 4 * 86_400
    )

    blocks = list(_numeric_blocks(payload))
    assert blocks, "the report carried no numbers at all"
    for path, block in blocks:
        assert block.get("fill_model") == "pessimistic", path
        assert block.get("terminal") == "settled", path


def test_a_report_is_strict_json_with_no_nan_leaking_into_it() -> None:
    """`NaN` is not JSON; an unavailable interval must serialize as null."""
    result = replay(
        [_wide_market("M1", "E1", 1_700_000_000)],
        policy=POLICY,
        fill_model="pessimistic",
        schedule=NO_MAKER_FEE,
    )

    payload = report(result, split="event")

    # Two events is far too few to cluster-bootstrap, so the interval is
    # unavailable -- and must say so as `null`, not as `NaN`.
    encoded = json.dumps(payload, allow_nan=False)
    assert json.loads(encoded)["overall"]["ci95_clustered_by_event"] == [None, None]


# ---------------------------------------------------------------------------
# (g) The temporal split.
# ---------------------------------------------------------------------------


CUTOFF_TS = 1_700_000_000


def test_the_temporal_split_puts_earlier_closes_in_train_and_the_rest_in_test(
) -> None:
    early = _wide_market("M-EARLY", "E-EARLY", CUTOFF_TS - 1)
    on_the_cutoff = _market(
        [
            _candle(3600, bid=WIDE_BID, ask=WIDE_ASK),
            _candle(7200, bid=WIDE_BID, ask=WIDE_ASK),
            _candle(10800, bid=0.40, ask=0.60),
        ],
        market_id="M-LATE",
        event="E-LATE",
        close_ts=CUTOFF_TS,
    )
    result = replay(
        [early, on_the_cutoff],
        policy=POLICY,
        fill_model="pessimistic",
        schedule=NO_MAKER_FEE,
    )

    payload = report(result, split="temporal", cutoff_ts=CUTOFF_TS)

    assert payload["split"] == "temporal"
    assert payload["cutoff_ts"] == CUTOFF_TS
    assert payload["train"]["n_quoted"] == 1
    assert payload["test"]["n_quoted"] == 1
    # Only the early market traded, so all of the P&L is in train and the
    # boundary market (close_ts == cutoff) is on the TEST side.
    assert payload["train"]["n_trading"] == 1
    assert payload["test"]["n_trading"] == 0
    assert payload["test"]["total_pnl"] == pytest.approx(0.0)
    assert payload["train"]["total_pnl"] != pytest.approx(0.0)


def test_the_verdict_is_computed_on_the_test_half_and_labelled() -> None:
    markets = [
        _wide_market(f"M{i}", f"E{i}", CUTOFF_TS + i * 86_400) for i in range(10)
    ]
    result = replay(
        markets, policy=POLICY, fill_model="pessimistic", schedule=NO_MAKER_FEE
    )

    payload = report(result, split="temporal", cutoff_ts=CUTOFF_TS + 5 * 86_400)

    verdict = payload["verdict"]
    assert verdict["basis"] == "temporal_test"
    assert verdict["fill_model"] == "pessimistic"
    assert verdict["terminal"] == "settled"
    assert set(verdict) >= {
        "ci95_lower", "lower_bound_above_zero", "n_trading", "n_events_trading",
    }


def test_an_event_split_reports_no_go_no_go_verdict() -> None:
    """Tuning and scoring on one split is not out of sample (§4.1)."""
    markets = [
        _wide_market(f"M{i}", f"E{i}", CUTOFF_TS + i * 86_400) for i in range(10)
    ]
    result = replay(
        markets, policy=POLICY, fill_model="pessimistic", schedule=NO_MAKER_FEE
    )

    payload = report(result, split="event")

    assert payload["verdict"] is None
    assert payload["train"]["n_quoted"] + payload["test"]["n_quoted"] == 10


def test_a_temporal_report_without_a_cutoff_refuses() -> None:
    result = replay(
        [_wide_market("M1", "E1", CUTOFF_TS)],
        policy=POLICY,
        fill_model="pessimistic",
        schedule=NO_MAKER_FEE,
    )

    with pytest.raises(ValueError, match="cutoff_ts"):
        report(result, split="temporal")


def test_the_report_is_reproducible_from_its_seed() -> None:
    markets = [
        _wide_market(f"M{i}", f"E{i}", CUTOFF_TS + i * 86_400) for i in range(12)
    ]
    result = replay(
        markets, policy=POLICY, fill_model="pessimistic", schedule=NO_MAKER_FEE
    )

    first = report(result, split="temporal", cutoff_ts=CUTOFF_TS + 6 * 86_400)
    second = report(result, split="temporal", cutoff_ts=CUTOFF_TS + 6 * 86_400)

    assert first == second


# ---------------------------------------------------------------------------
# (g), continued -- an EVENT that straddles the cutoff. Measured live on the
# real universe: 16 of 18,457 events (128 of 50,796 markets) have members on
# both sides of the median close. Splitting rows by `close_ts` alone leaves a
# test-split market sharing one correlated real-world outcome with a
# train-split market, which is the specific thing an out-of-sample test exists
# to prevent -- at 0.25%, but the fix costs 0.25% of the sample and the leak
# costs the meaning of the verdict.
# ---------------------------------------------------------------------------


def _straddling_markets() -> list[MarketCandles]:
    """Six clean events either side of the cutoff, plus one that spans it."""
    clean = [
        _wide_market(f"M{i}", f"E{i}", CUTOFF_TS + (i - 3) * 86_400)
        for i in range(6)
    ]
    return [
        *clean,
        _wide_market("M-STRADDLE-EARLY", "E-STRADDLE", CUTOFF_TS - 3600),
        _wide_market("M-STRADDLE-LATE", "E-STRADDLE", CUTOFF_TS + 3600),
    ]


def test_a_straddling_events_late_markets_are_kept_out_of_the_test_split(
) -> None:
    """No test market may share an event with a train market.

    `E-STRADDLE` has one market closing an hour before the cutoff and
    one an hour after. The early one stays in train (dropping it would
    throw away evidence the test half never sees anyway); the late one
    is DROPPED rather than scored, so nothing in the test half is
    correlated with anything in the train half.
    """
    result = replay(
        _straddling_markets(),
        policy=POLICY,
        fill_model="pessimistic",
        schedule=NO_MAKER_FEE,
    )

    parts = _split(
        result.rows, split="temporal", cutoff_ts=CUTOFF_TS, seed=1
    )

    train_events = {r.event for r in parts.train}
    test_events = {r.event for r in parts.test}
    assert "E-STRADDLE" in train_events
    assert "E-STRADDLE" not in test_events
    # The property the whole rule exists for.
    assert not (train_events & test_events)
    assert {r.market_id for r in parts.train} == {
        "M0", "M1", "M2", "M-STRADDLE-EARLY"
    }
    assert {r.market_id for r in parts.test} == {"M3", "M4", "M5"}
    assert parts.straddling_events == 1
    assert parts.dropped_from_test == 1

    payload = report(result, split="temporal", cutoff_ts=CUTOFF_TS)
    # Three clean early markets plus the straddler's early half.
    assert payload["train"]["n_markets"] == 4
    # Three clean late markets; the straddler's late half is gone.
    assert payload["test"]["n_markets"] == 3
    # `overall` still holds every row -- it is the whole sample, not the
    # union of the two halves.
    assert payload["overall"]["n_markets"] == 8


def test_the_report_states_what_the_straddle_rule_dropped() -> None:
    """A reader must see what was excluded, in both directions.

    Reporting the counts only when they are non-zero would make a clean
    split indistinguishable from a rule that silently stopped running,
    so the block is present either way.
    """
    with_straddle = report(
        replay(
            _straddling_markets(),
            policy=POLICY,
            fill_model="pessimistic",
            schedule=NO_MAKER_FEE,
        ),
        split="temporal",
        cutoff_ts=CUTOFF_TS,
    )
    clean = report(
        replay(
            [
                _wide_market(f"M{i}", f"E{i}", CUTOFF_TS + (i - 3) * 86_400)
                for i in range(6)
            ],
            policy=POLICY,
            fill_model="pessimistic",
            schedule=NO_MAKER_FEE,
        ),
        split="temporal",
        cutoff_ts=CUTOFF_TS,
    )

    block = with_straddle["split_exclusions"]
    assert block["straddling_events"] == 1
    assert block["markets_dropped_from_test"] == 1
    assert block["rule"] == "straddling_events_excluded_from_test"
    assert clean["split_exclusions"]["straddling_events"] == 0
    assert clean["split_exclusions"]["markets_dropped_from_test"] == 0
    # An event split cannot straddle: whole events go to one side.
    event_split = report(
        replay(
            _straddling_markets(),
            policy=POLICY,
            fill_model="pessimistic",
            schedule=NO_MAKER_FEE,
        ),
        split="event",
    )
    assert event_split["split_exclusions"]["straddling_events"] == 0


# ---------------------------------------------------------------------------
# (h) The rebate is never inside P&L.
# ---------------------------------------------------------------------------


def test_a_paying_rebate_does_not_change_a_single_pnl_figure() -> None:
    candles = [
        _candle(3600, bid=WIDE_BID, ask=WIDE_ASK),
        _candle(
            7200, bid=WIDE_BID, ask=WIDE_ASK,
            low=0.20, high=0.50, close=0.40, volume=100.0,
        ),
        _candle(10800, bid=0.40, ask=0.60),
    ]
    market = _market(candles)
    charged = FeeSchedule(
        taker_rate=0.07, maker_rate=0.0175, source="test", maker_rebate_rate=0.0
    )
    rebated = FeeSchedule(
        taker_rate=0.07, maker_rate=0.0175, source="test", maker_rebate_rate=0.25
    )

    without = replay(
        [market], policy=POLICY, fill_model="optimistic", schedule=charged
    )
    with_rebate = replay(
        [market], policy=POLICY, fill_model="optimistic", schedule=rebated
    )

    # BOTH P&L figures, not just the cash one. The module reports two
    # numbers and the rebate must be absent from each; guarding only
    # `pnl` left `markout_pnl` free to start crediting a programme
    # payout that GUARDRAILS.md 2.3 says is never inside a P&L.
    assert without.rows[0].pnl == with_rebate.rows[0].pnl
    assert without.rows[0].markout_pnl == with_rebate.rows[0].markout_pnl
    # And it is reported as its own line, not dropped on the floor.
    assert without.rows[0].rebate_if_paid == pytest.approx(0.0)
    assert with_rebate.rows[0].rebate_if_paid > 0.0
    # A guard that passed because neither number moved for an unrelated
    # reason would be no guard at all: the rebate is genuinely large
    # enough to have shifted either figure had it leaked in.
    assert with_rebate.rows[0].rebate_if_paid > 1e-3
    assert with_rebate.rows[0].pnl != pytest.approx(0.0)
    assert with_rebate.rows[0].markout_pnl != pytest.approx(0.0)


def test_the_maker_fee_is_charged_and_comes_from_the_model() -> None:
    """A zero-fee replay would report an edge Kalshi takes away."""
    candles = [
        _candle(3600, bid=WIDE_BID, ask=WIDE_ASK),
        _candle(
            7200, bid=WIDE_BID, ask=WIDE_ASK,
            low=0.20, high=0.50, close=0.40, volume=100.0,
        ),
        _candle(10800, bid=0.40, ask=0.60),
    ]
    market = _market(candles)
    charged = FeeSchedule(
        taker_rate=0.07, maker_rate=0.0175, source="test", maker_rebate_rate=0.0
    )

    free = replay(
        [market], policy=POLICY, fill_model="optimistic", schedule=NO_MAKER_FEE
    )
    paid = replay(
        [market], policy=POLICY, fill_model="optimistic", schedule=charged
    )

    assert paid.rows[0].pnl < free.rows[0].pnl


# ---------------------------------------------------------------------------
# Collection: a market that fails to parse is COUNTED, never dropped.
# ---------------------------------------------------------------------------


GOOD_CANDLES = [
    {
        "end_period_ts": 1_700_000_000 + 3600 * i,
        "yes_bid": {"close_dollars": "0.30"},
        "yes_ask": {"close_dollars": "0.71"},
        "price": {
            "low_dollars": "0.20", "high_dollars": "0.50", "close_dollars": "0.40"
        },
        "volume_fp": "100.00",
        "open_interest_fp": "500.00",
    }
    for i in range(6)
]


def _collect_adapter(bodies: dict[str, object]) -> KalshiAdapter:
    """Serve one candlestick body per market ticker; no network."""

    def handler(request: httpx.Request) -> httpx.Response:
        for ticker, body in bodies.items():
            if f"/markets/{ticker}/candlesticks" in request.url.path:
                return httpx.Response(200, json=body)
        raise AssertionError(request.url.path)

    return KalshiAdapter(
        transport=httpx.MockTransport(handler), settings_obj=Settings()
    )


async def test_one_unparseable_market_is_counted_not_silently_skipped() -> None:
    """T1's hardening must not be undone one level up.

    Skipping a `VenuePayloadError` quietly would reinstate exactly the
    "corrupt payload reads as zero candles" defect T1 removed -- at a
    level where it is even harder to see, because the market simply
    would not appear in the sample.
    """
    adapter = _collect_adapter({
        "M-GOOD": {"candlesticks": GOOD_CANDLES},
        "M-BAD": {"candlesticks": "corrupt"},
    })
    markets = [
        _venue_market("M-GOOD", result="yes", volume_fp="5000"),
        _venue_market("M-BAD", result="yes", volume_fp="5000"),
    ]

    collected = await collect(
        adapter, markets, interval_minutes=60, lookback_days=10
    )
    await adapter.aclose()

    assert [m.market_id for m in collected.markets] == ["M-GOOD"]
    assert collected.n_payload_errors == 1
    assert collected.n_request_errors == 0
    assert collected.failure_rate == pytest.approx(0.5)
    assert any(f.market_id == "M-BAD" for f in collected.failures)


async def test_a_market_with_too_little_history_is_counted_separately() -> None:
    adapter = _collect_adapter({
        "M-GOOD": {"candlesticks": GOOD_CANDLES},
        "M-SHORT": {"candlesticks": GOOD_CANDLES[: MIN_CANDLES - 1]},
    })
    markets = [
        _venue_market("M-GOOD", result="yes", volume_fp="5000"),
        _venue_market("M-SHORT", result="yes", volume_fp="5000"),
    ]

    collected = await collect(
        adapter, markets, interval_minutes=60, lookback_days=10
    )
    await adapter.aclose()

    assert [m.market_id for m in collected.markets] == ["M-GOOD"]
    assert collected.n_short_history == 1
    # Too little history is not a parse failure and must not be reported
    # as one -- it says nothing about whether the payload was honoured.
    assert collected.n_payload_errors == 0
    assert collected.failure_rate == pytest.approx(0.0)


async def test_collection_survives_one_bad_market_out_of_many() -> None:
    """One bad market must not abort a run over thousands."""
    bodies: dict[str, object] = {
        f"M{i}": {"candlesticks": GOOD_CANDLES} for i in range(10)
    }
    bodies["M5"] = {"candlesticks": [{"no_end_period_ts": 1}]}
    adapter = _collect_adapter(bodies)
    markets = [
        _venue_market(f"M{i}", result="yes", volume_fp="5000") for i in range(10)
    ]

    collected = await collect(
        adapter, markets, interval_minutes=60, lookback_days=10, concurrency=4
    )
    await adapter.aclose()

    assert len(collected.markets) == 9
    assert collected.n_payload_errors == 1


async def test_a_transport_read_error_is_counted_as_a_request_error_not_a_crash() -> (
    None
):
    """The defect that crashed three live runs, at samples 8,000/20,000/19,000.

    `httpx.ReadError` is an `httpx.TransportError`, not an `OSError`, so
    it used to escape `_collect_one`'s except tuple entirely, propagate
    out of `asyncio.gather`, and abort `collect()` for every OTHER market
    in flight along with it. Every other test in this file uses
    `httpx.MockTransport`, which answers every request and can never
    itself produce a transport error -- which is exactly why four review
    roles missed this. This raises the real exception from the handler,
    rather than returning a 4xx/5xx response, so it exercises the actual
    `httpx.AsyncClient.send` failure path.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if "/markets/M-DEAD/candlesticks" in request.url.path:
            raise httpx.ReadError("connection dropped mid-read", request=request)
        if "/markets/M-GOOD/candlesticks" in request.url.path:
            return httpx.Response(200, json={"candlesticks": GOOD_CANDLES})
        raise AssertionError(request.url.path)

    adapter = KalshiAdapter(
        transport=httpx.MockTransport(handler), settings_obj=Settings()
    )
    markets = [
        _venue_market("M-GOOD", result="yes", volume_fp="5000"),
        _venue_market("M-DEAD", result="yes", volume_fp="5000"),
    ]

    collected = await collect(
        adapter, markets, interval_minutes=60, lookback_days=10
    )
    await adapter.aclose()

    assert [m.market_id for m in collected.markets] == ["M-GOOD"]
    assert collected.n_request_errors == 1
    assert collected.n_payload_errors == 0
    assert any(
        f.market_id == "M-DEAD" and f.kind == "request" for f in collected.failures
    )


def test_a_cache_round_trip_preserves_the_candles_and_the_failure_counts(
    tmp_path,
) -> None:
    collected = CollectResult(
        markets=[_wide_market("M1", "E1", CUTOFF_TS)],
        failures=[],
        n_requested=3,
        n_short_history=1,
        n_payload_errors=1,
        n_request_errors=0,
        interval_minutes=60,
        lookback_days=10,
    )
    path = tmp_path / "cache.json"

    write_cache(path, collected)
    restored = read_cache(path)

    assert restored.markets == collected.markets
    assert restored.n_payload_errors == 1
    assert restored.n_short_history == 1
    assert restored.n_requested == 3
    assert restored.interval_minutes == 60


async def test_an_interrupted_collection_leaves_a_cache_of_what_it_had(
    tmp_path,
) -> None:
    """A crash mid-run must not discard the markets already fetched.

    `write_cache` used to run once, after collection finished, so a run
    that crashed partway -- as three live runs did -- left no cache file
    at all, and every already-collected market was thrown away. This
    forces `_collect_and_flush` to raise on its SECOND slice (an error
    outside `_collect_one`'s own except tuple entirely, standing in for
    "collection raises" generally rather than for any one exception
    type) and checks that `read_cache` can still load what the FIRST
    slice flushed before that.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if "/markets/M-BOOM/candlesticks" in request.url.path:
            raise RuntimeError("simulated crash, not a venue/transport error")
        return httpx.Response(200, json={"candlesticks": GOOD_CANDLES})

    adapter = KalshiAdapter(
        transport=httpx.MockTransport(handler), settings_obj=Settings()
    )
    markets = [
        _venue_market("M-FIRST", result="yes", volume_fp="5000"),
        _venue_market("M-BOOM", result="yes", volume_fp="5000"),
    ]
    cache_path = tmp_path / "interrupted.json"

    with pytest.raises(RuntimeError):
        await _collect_and_flush(
            adapter,
            markets,
            interval_minutes=60,
            lookback_days=10,
            concurrency=1,
            cache_path=cache_path,
            n_requested=len(markets),
            flush_every=1,
        )
    await adapter.aclose()

    restored = read_cache(cache_path)
    assert [m.market_id for m in restored.markets] == ["M-FIRST"]


def test_a_cached_run_reports_the_universe_it_actually_sampled(tmp_path) -> None:
    """A cached run must not quote a universe it never measured.

    Reading candles back from `--cache` skips `settled_universe`
    entirely, so unless the universe size and the survivorship count
    travel WITH the cache, a re-run silently reports "universe: 27,
    excluded by result: 0" -- a figure with no measurement behind it.
    """
    collected = CollectResult(
        markets=[_wide_market("M1", "E1", CUTOFF_TS)],
        failures=[],
        n_requested=30,
        n_short_history=3,
        n_payload_errors=0,
        n_request_errors=0,
        interval_minutes=60,
        lookback_days=10,
        n_universe=50_825,
        excluded_by_result=171,
    )
    path = tmp_path / "cache.json"

    write_cache(path, collected)
    summary = read_cache(path).summary()

    assert summary["n_universe"] == 50_825
    assert summary["excluded_by_result"] == 171
    assert math.isclose(summary["survivorship_share"], 171 / (171 + 50_825))


def test_the_report_carries_the_collection_counts_into_the_json(tmp_path) -> None:
    """A failure that never reaches the report is a failure nobody sees."""
    collected = CollectResult(
        markets=[_wide_market("M1", "E1", CUTOFF_TS)],
        failures=[],
        n_requested=3,
        n_short_history=1,
        n_payload_errors=1,
        n_request_errors=0,
        interval_minutes=60,
        lookback_days=10,
    )

    block = collected.summary(excluded_by_result=7, n_universe=99)

    assert block["n_payload_errors"] == 1
    assert block["n_short_history"] == 1
    assert block["excluded_by_result"] == 7
    assert block["n_universe"] == 99
    assert math.isclose(block["failure_rate"], 1 / 3)


# ---------------------------------------------------------------------------
# The universe's own provenance. `survivorship_share` covers exclusion by a
# non-binary `result` and NOTHING else -- in particular not the fact that
# `list_markets(status="resolved")` reads a listing truncated at
# `_MAX_EVENT_PAGES = 150`. Measured 2026-09-07 by walking `/events` past the
# cap: 50,826 of 133,977 tradeable settled markets are visible (38%), August
# is 94% missing, and the listing is not chronologically ordered, so the bias
# is month-correlated rather than a clean recent tail.
#
# T3's brief tells its author to write "the survivorship count" out of this
# JSON. Without a field of its own the caveat survives only if that author
# independently reads NOTES.md, which is the hand-off that fails
# (GUARDRAILS.md 7: no number without its provenance). So it is structural.
# ---------------------------------------------------------------------------


def test_the_collection_block_carries_the_listings_page_cap_caveat() -> None:
    """`n_universe` must not travel without saying what truncated it."""
    collected = CollectResult(
        markets=[],
        failures=[],
        n_requested=0,
        n_short_history=0,
        n_payload_errors=0,
        n_request_errors=0,
        interval_minutes=60,
        lookback_days=10,
    )

    provenance = collected.summary()["universe_provenance"]

    assert provenance["listing_is_page_capped"] is True
    assert provenance["sample_is_random_draw_from_venue"] is False
    assert "_MAX_EVENT_PAGES" in provenance["cap"]
    assert provenance["measured_on"] == "2026-09-07"
    # The share is the measurement, not a round number someone liked.
    visible = provenance["visible_tradeable_markets"]
    hidden = provenance["invisible_tradeable_markets"]
    assert math.isclose(
        provenance["visible_share_of_tradeable_universe"],
        visible / (visible + hidden),
        rel_tol=1e-3,
    )
    # And it is a DIFFERENT thing from survivorship, which the block also
    # reports; conflating them is the specific hand-off error this guards.
    assert "survivorship" in provenance["consequence"]
    assert provenance is not SETTLED_LISTING_PROVENANCE


def _cli_payload() -> dict:
    """The CLI's report payload, assembled the way `_main` assembles it."""
    markets = [
        _wide_market(f"M{i}", f"E{i}", CUTOFF_TS + (i - 4) * 86_400)
        for i in range(8)
    ]
    collected = CollectResult(
        markets=markets,
        failures=[],
        n_requested=8,
        n_short_history=0,
        n_payload_errors=0,
        n_request_errors=0,
        interval_minutes=60,
        lookback_days=10,
        n_universe=50_826,
        excluded_by_result=171,
    )
    payload: dict = {
        "data_window": {
            "venue": "kalshi",
            "interval_minutes": 60,
            "lookback_days": 10,
            "first_close": "2023-11-10T00:00:00+00:00",
            "last_close": "2023-11-18T00:00:00+00:00",
            "n_markets": len(markets),
            "n_candles": sum(len(m.candles) for m in markets),
        },
        "collection": {**collected.summary(), "min_volume": 2000.0},
        "policy": {
            "min_spread": 0.10, "edge_fraction": 0.80, "max_inventory": 20.0,
            "skew_strength": 0.0, "quote_size": 10.0,
            "fee_model": "KalshiFeeModel", "maker_rate": 0.0175,
            "taker_rate": 0.07, "fee_source": "test",
        },
    }
    for model in ("pessimistic", "optimistic"):
        payload[model] = report(
            replay(
                markets, policy=POLICY, fill_model=model, schedule=NO_MAKER_FEE
            ),
            split="temporal",
            cutoff_ts=CUTOFF_TS,
        )
    return payload


def test_the_printed_header_states_the_truncation_and_the_straddle_counts(
    capsys,
) -> None:
    """The caveats have to reach the reader of the table, not only the JSON.

    `print_report`'s stdout IS the deliverable for a run without
    `--out`, and it is what a human actually reads. This also exercises
    every field the printed table indexes -- a rename in the power block
    or the split block is a `KeyError` in the deliverable, and nothing
    else in this file runs that code path.
    """
    print_report(_cli_payload())

    out = capsys.readouterr().out
    assert "PAGE-CAPPED" in out
    assert "not a random draw" in out.lower()
    assert "_MAX_EVENT_PAGES" in out
    assert "straddle" in out.lower()
    # Pessimistic before optimistic (GUARDRAILS.md 2.2) survives the
    # extra header lines.
    assert out.index("fill_model=pessimistic") < out.index(
        "fill_model=optimistic"
    )


# ---------------------------------------------------------------------------
# Adversarial probes requested by the orchestrator on T2's "done" report.
# Written from the BRIEF and from first-principles arithmetic, not from
# reading the implementation's logic first -- the implementation was only
# consulted afterward to confirm call signatures and dataclass shapes.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Probe 1 -- the P&L convention. This probe originally found the harness
# reporting -$2.45 as `pnl` for a market that, for real money, broke exactly
# even. The orchestrator accepted the finding: `pnl` is now the cash-settled
# ledger and the mark-dependent number moved to `markout_pnl`. The test below
# is kept as the REGRESSION for that fix -- same candles, same three fills,
# now asserting that the money is $0.00 and that -$2.45 is carried by the
# quality statistic, where being mark-dependent is a feature rather than a
# defect.
#
# The arithmetic behind the gap is unchanged and worth keeping on the record:
# exact carry-marking telescopes to `sum(-dir*price*size) - fees +
# terminal_inventory*settle`, a quantity that does not depend on which candle
# is used as an intermediate mark at all -- which is why acceptance (b) as
# originally written ("construct candles where i+1 and i+2 differ and assert
# the P&L") was UNSATISFIABLE against a cash P&L, and why satisfying it was
# what pulled the mark-dependent convention into `pnl` in the first place.
# (b) now names `markout_pnl`; see
# `test_cash_pnl_is_mark_path_independent_while_markout_is_not` for the
# complement.
# ---------------------------------------------------------------------------


def test_a_multi_fill_position_diverges_from_cash_settled_pnl_by_a_real_amount(
) -> None:
    """Per-fill markout summed with a terminal-settlement patch is not the
    same number as a real quoter's ledger, once more than one fill occurs.

    Fills, by construction:
      i=0: BUY 10 @ 0.34 (candles[1] low=0.20 < 0.34, pessimistic).
      i=1: SELL 10 @ 0.67 (candles[2] high=0.80 > 0.67).
      i=2: SELL 10 @ 0.67 again (candles[3] high=0.80 > 0.67) -- opening a
           naked short of 10 that survives to the venue's "yes" result.

    TRUE cash-settled P&L, independent of ANY interim mark (plain trade
    arithmetic): buy 10 @ 0.34 (-$3.40), sell 10 @ 0.67 (+$6.70) closes a
    round trip at +$3.30; sell another 10 @ 0.67 opens a naked short
    (+$6.70) that settles into "yes" at $1.00/contract (-$10.00). Net:
    3.30 + 6.70 - 10.00 = $0.00 -- a market that, in reality, broke
    exactly even. That is what `pnl` must now report.

    The MARKOUT convention (`markout_pnl`, `mark_to_market`): each fill
    marked ONCE at its own i+2 mid (0.505, 0.75, 0.95 for the three fills
    respectively), summed, plus a terminal-settlement term using only the
    LAST fill's mark (0.95):
        +10*(0.505-0.34) - 10*(0.75-0.67) - 10*(0.95-0.67)
            - 10*(1.00-0.95)
      = 1.65 - 0.80 - 2.80 - 0.50 = -2.45

    The $2.45 gap is the "sum over intervals of
    inventory_before_interval * (mark_i - mark_prev)" term the module
    docstring used to name and dismiss as "pure directional noise... zero
    expectation under a martingale price". It is not noise on the money
    any more, because the money no longer contains it; it is the drift on
    carried inventory that the quality statistic deliberately charges to
    the quotes. Keeping BOTH numbers is what makes that separable, and
    the martingale premise is the one this repo has measured false
    (`market_making.py`: sell fills outnumber buy fills 1.5-2.0x at every
    price, i.e. informed, non-martingale order flow).
    """
    candles = [
        _candle(3600, bid=0.30, ask=0.71),
        _candle(7200, bid=0.30, ask=0.71, low=0.20, high=0.50, volume=100.0),
        _candle(10800, bid=0.30, ask=0.71, low=0.50, high=0.80, volume=100.0),
        _candle(14400, bid=0.70, ask=0.80, low=0.50, high=0.80, volume=100.0),
        _candle(18000, bid=0.90, ask=1.00),
    ]

    result = replay(
        [_market(candles, result="yes")],
        policy=POLICY,
        fill_model="pessimistic",
        schedule=NO_MAKER_FEE,
    )

    row = result.rows[0]
    assert row.n_fills == 3
    assert row.terminal_inventory == pytest.approx(-10.0)
    # The money: exactly zero, fees aside (this schedule charges the
    # maker nothing, so "fees aside" is exact here).
    assert row.pnl == pytest.approx(0.0, abs=1e-9)
    # And -2.45 is carried by the mark-dependent statistic, not by the
    # ledger. The gap between them is the carried-inventory re-marking
    # term, reported rather than hidden inside one number.
    assert row.markout_pnl == pytest.approx(-2.45)
    assert row.pnl - row.markout_pnl == pytest.approx(2.45)


# ---------------------------------------------------------------------------
# Probe 2 -- look-ahead, from angles (b) does not already cover: does the
# quote at `i` ever depend on `i+1`? Can a fill decided from `i+1`'s trade
# range ever be swayed by that SAME candle's own bid/ask close (end-of-
# period information, not what printed) or by `i+2`'s data (one interval
# further into the future than the fill itself)?
# ---------------------------------------------------------------------------


def test_the_quote_at_one_interval_is_unaffected_by_the_next_intervals_book(
) -> None:
    """Quoting at `i` must be a pure function of candle `i`'s own close.

    `candles[1]`'s bid/ask close is never a QUOTE source in this
    3-candle market (there is no `i=1` interval to quote from -- only
    `i=0` exists), so it must be inert. Two markets differing ONLY in
    `candles[1]`'s bid/ask -- one a plausible book, one a nonsensical
    near-zero one that would price completely differently if it ever
    leaked into `i=0`'s quote -- must replay identically.
    """

    def market_with(bid1: float, ask1: float) -> MarketCandles:
        candles = [
            _candle(3600, bid=WIDE_BID, ask=WIDE_ASK),
            _candle(7200, bid=bid1, ask=ask1, low=0.20, high=0.50, volume=100.0),
            _candle(10800, bid=0.40, ask=0.60),
        ]
        return _market(candles)

    plausible = replay(
        [market_with(0.30, 0.71)], policy=POLICY, fill_model="pessimistic",
        schedule=NO_MAKER_FEE,
    )
    poisoned = replay(
        [market_with(0.01, 0.02)], policy=POLICY, fill_model="pessimistic",
        schedule=NO_MAKER_FEE,
    )

    assert plausible.rows[0].quote_hours == poisoned.rows[0].quote_hours == 1
    assert plausible.rows[0].n_fills == poisoned.rows[0].n_fills == 1
    assert plausible.rows[0].collateral_mean == pytest.approx(
        poisoned.rows[0].collateral_mean
    )
    assert plausible.rows[0].pnl == pytest.approx(poisoned.rows[0].pnl)


def test_a_fill_is_decided_only_by_the_fill_candles_own_trade_range() -> None:
    """Only `candles[i+1].px_low/px_high/volume` may decide a fill.

    Neither that SAME candle's own `bid_close`/`ask_close` (where the
    book ended up, not what printed while the quote rested) nor
    `candles[i+2]`'s trade range (one interval further into the future
    than the fill itself) may move the answer. `candles[1]`'s close and
    `candles[2]`'s trade range are both built to look like a fill;
    only `candles[1]`'s own `px_low`/`px_high` correctly says "no
    fill", and that is the answer that must win.
    """
    candles = [
        _candle(3600, bid=WIDE_BID, ask=WIDE_ASK),
        # i=0's fill candle: prints never reach 0.34 or 0.67 -- but its
        # OWN close looks crossed/tight, which a bug reading bid/ask
        # close instead of the trade range would mistake for a fill.
        _candle(7200, bid=0.20, ask=0.30, low=0.50, high=0.60, volume=100.0),
        # i=0's mark candle -- its trade range also looks like a fill,
        # which a bug reading i+2's trades instead of i+1's would take.
        _candle(10800, bid=0.40, ask=0.60, low=0.10, high=0.90, volume=100.0),
    ]

    result = replay(
        [_market(candles)], policy=POLICY, fill_model="optimistic",
        schedule=NO_MAKER_FEE,
    )

    assert result.rows[0].n_fills == 0


def test_a_fill_is_never_decided_from_the_quote_candles_own_prints() -> None:
    """`candles[i]`'s own trade range must not decide `i`'s fill either.

    The quote at `i` is only observable once candle `i` has already
    CLOSED (T1), so nothing printed during candle `i`'s own period
    could have traded against an order that did not exist yet. Here
    `candles[0]` (the quote source) carries a loud touch in its own
    trade range, while `candles[1]` (the correct fill source) shows
    none.
    """
    candles = [
        _candle(3600, bid=WIDE_BID, ask=WIDE_ASK, low=0.10, high=0.90, volume=100.0),
        _candle(7200, bid=WIDE_BID, ask=WIDE_ASK, low=0.50, high=0.60, volume=100.0),
        _candle(10800, bid=0.40, ask=0.60),
    ]

    result = replay(
        [_market(candles)], policy=POLICY, fill_model="optimistic",
        schedule=NO_MAKER_FEE,
    )

    assert result.rows[0].n_fills == 0


# ---------------------------------------------------------------------------
# T6 -- replay() must hand quote() a correct hours_to_close, computed
# from the QUOTE candle's own end_ts and the market's close_ts, so a
# taper configured on the policy has something honest to act on.
# ---------------------------------------------------------------------------


class _RecordingPolicy:
    """A stand-in `MarketMaker` that records every `hours_to_close` it
    was called with and quotes nothing, so no fill/mark machinery is
    exercised -- this test is about the PLUMBING into `quote()`, not
    about what the taper does once it gets there (that is
    `tests/strategies/test_market_making.py`'s job)."""

    def __init__(self) -> None:
        self.hours_to_close_calls: list[float | None] = []

    def quote(
        self, book, *, tick_size, inventory=0.0, hours_to_close=None
    ) -> QuotePair:
        del tick_size, inventory  # unused; only hours_to_close is under test
        self.hours_to_close_calls.append(hours_to_close)
        return QuotePair(
            venue=book.venue, market_id=book.market_id, outcome=book.outcome,
            bid=None, ask=None, reason="test_stub", metadata={},
        )


def test_replay_passes_hours_to_close_computed_from_the_quote_candle() -> None:
    """`close_ts=36_000` (10 hours, in seconds); candle[0] ends at
    `end_ts=0` (10.0 hours to close) and candle[1] at `end_ts=18_000`
    (5.0 hours to close). Neither candle needs a trade range or a
    parseable mark -- `policy.quote` is called, and its `hours_to_close`
    recorded, before either is consulted."""
    candles = [
        _candle(0, bid=0.30, ask=0.71),
        _candle(18_000, bid=0.30, ask=0.71),
        _candle(20_000),
        _candle(22_000),
    ]
    market = _market(candles, close_ts=36_000)
    policy = _RecordingPolicy()

    replay([market], policy=policy, fill_model="pessimistic", schedule=NO_MAKER_FEE)

    assert policy.hours_to_close_calls == [pytest.approx(10.0), pytest.approx(5.0)]


def test_replay_hours_to_close_is_negative_past_the_markets_close_ts() -> None:
    """A candle ending after the market's own `close_ts` (messy data, not
    the ordinary case) must produce a NEGATIVE `hours_to_close` rather
    than clamping inside `replay()` -- clamping is `MarketMaker`'s job
    (`_effective_max_inventory`), and doing it twice would hide a bug in
    one place behind a guard in the other."""
    candles = [
        _candle(7_200, bid=0.30, ask=0.71),
        _candle(10_800, bid=0.30, ask=0.71),
        _candle(14_400),
    ]
    market = _market(candles, close_ts=3_600)  # closed before candle[0] even ends
    policy = _RecordingPolicy()

    replay([market], policy=policy, fill_model="pessimistic", schedule=NO_MAKER_FEE)

    assert policy.hours_to_close_calls == [pytest.approx(-1.0)]


# ---------------------------------------------------------------------------
# Probe 3 -- the temporal split boundary. The existing (g) tests already
# pin close_ts == cutoff -> test. These add: an empty train half, an empty
# test half (and that the verdict reports UNAVAILABLE rather than a
# fabricated NO-GO), and the CLI's own median-close default producing a
# degenerate split when every sampled close lands on the same timestamp.
# ---------------------------------------------------------------------------


def test_a_cutoff_before_every_close_leaves_train_empty_not_wrong() -> None:
    markets = [
        _wide_market(f"M{i}", f"E{i}", CUTOFF_TS + i * 86_400) for i in range(6)
    ]
    result = replay(
        markets, policy=POLICY, fill_model="pessimistic", schedule=NO_MAKER_FEE
    )

    payload = report(result, split="temporal", cutoff_ts=CUTOFF_TS - 1)

    assert payload["train"]["n_quoted"] == 0
    assert payload["train"]["n_trading"] == 0
    assert payload["train"]["roc"] is None
    assert payload["train"]["ci95_clustered_by_event"] == [None, None]
    assert payload["train"]["total_pnl"] == pytest.approx(0.0)
    assert payload["test"]["n_quoted"] == 6


def test_a_cutoff_after_every_close_leaves_test_empty_and_the_verdict_is_unavailable(
) -> None:
    markets = [
        _wide_market(f"M{i}", f"E{i}", CUTOFF_TS + i * 86_400) for i in range(6)
    ]
    result = replay(
        markets, policy=POLICY, fill_model="pessimistic", schedule=NO_MAKER_FEE
    )

    payload = report(
        result, split="temporal", cutoff_ts=CUTOFF_TS + 6 * 86_400
    )

    assert payload["test"]["n_quoted"] == 0
    assert payload["test"]["n_trading"] == 0
    verdict = payload["verdict"]
    # A NO-GO fabricated from zero test-half evidence is a worse failure
    # than admitting there is no answer -- this must come back `None`,
    # never `False`.
    assert verdict["lower_bound_above_zero"] is None
    assert verdict["ci95_lower"] is None
    assert verdict["n_trading"] == 0


def test_the_median_default_cutoff_can_leave_train_empty() -> None:
    """The CLI's median-close default is not immune to a degenerate split.

    If every sampled market happens to close at the SAME timestamp (a
    small `--sample` over a short `--days`, PLAN.md Risks), the median
    equals that shared timestamp, and `train = close_ts < cutoff` is
    empty by construction -- nothing is strictly less than its own
    median -- while `test` silently absorbs the entire sample. The
    report does not crash and does not fabricate a train-half number,
    but it also does not flag that the resulting "out of sample" test
    half is, in this case, the whole sample.
    """
    markets = [_wide_market(f"M{i}", f"E{i}", CUTOFF_TS) for i in range(6)]

    cutoff, source = _cutoff_ts(markets, None)

    assert cutoff == CUTOFF_TS
    assert source == "median_close"

    result = replay(
        markets, policy=POLICY, fill_model="pessimistic", schedule=NO_MAKER_FEE
    )
    payload = report(result, split="temporal", cutoff_ts=cutoff)

    assert payload["train"]["n_quoted"] == 0
    assert payload["test"]["n_quoted"] == 6


# ---------------------------------------------------------------------------
# Probe 4 -- n_quoted vs n_trading vs the roc denominator. `roc`'s
# numerator (`total_pnl`) is summed over ALL rows but is mechanically zero
# for any market that never filled; its denominator is documented as
# "n_quoted markets' worth of collateral", not n_trading's. This pins the
# exact arithmetic against a hand-built set where the three counts differ.
# ---------------------------------------------------------------------------


def test_roc_denominator_is_n_quoted_times_collateral_over_quoted_markets(
) -> None:
    """`roc`'s numerator and denominator must share ONE consistent basis.

    Three markets: A quotes but never fills (collateral, no pnl); B
    quotes AND fills; C never quotes at all (neither). `n_quoted` must
    count A and B only (2); `n_trading` must count B only (1); `roc`'s
    denominator must be built from exactly A and B's `collateral_mean`
    (mean of 10.0 and 20.0 = 15.0), never from n_trading's narrower set
    (which would give 20.0) and never diluted by C (which quoted
    nothing, so it has no collateral to average in).
    """
    rows = (
        MarketRow(
            market_id="A", event="EA", series="KX", close_ts=1,
            quote_hours=5, n_fills=0, n_two_sided=0, pnl=0.0,
            markout_pnl=0.0,
            collateral_mean=10.0, terminal_inventory=0.0,
            held_into_settlement=False, settled_short_into_yes=False,
            rebate_if_paid=0.0, last_mid=None,
        ),
        MarketRow(
            market_id="B", event="EB", series="KX", close_ts=2,
            quote_hours=3, n_fills=2, n_two_sided=1, pnl=7.0,
            markout_pnl=5.0,
            collateral_mean=20.0, terminal_inventory=0.0,
            held_into_settlement=False, settled_short_into_yes=False,
            rebate_if_paid=0.0, last_mid=0.5,
        ),
        MarketRow(
            market_id="C", event="EC", series="KX", close_ts=3,
            quote_hours=0, n_fills=0, n_two_sided=0, pnl=0.0,
            markout_pnl=0.0,
            collateral_mean=0.0, terminal_inventory=0.0,
            held_into_settlement=False, settled_short_into_yes=False,
            rebate_if_paid=0.0, last_mid=None,
        ),
    )
    result = ReplayResult(fill_model="pessimistic", rows=rows)

    overall = report(result, split="event", seed=1)["overall"]

    assert overall["n_quoted"] == 2
    assert overall["n_trading"] == 1
    assert overall["collateral_mean"] == pytest.approx(15.0)
    assert overall["total_pnl"] == pytest.approx(7.0)
    assert overall["roc"] == pytest.approx(7.0 / (2 * 15.0))
    # And explicitly NOT the n_trading-basis figure a denominator mix-up
    # would silently produce instead.
    assert overall["roc"] != pytest.approx(7.0 / (1 * 20.0))
    # `roc` is return on CAPITAL, so its numerator is the money, never
    # the markout statistic reported beside it.
    assert overall["total_markout_pnl"] == pytest.approx(5.0)
    assert overall["roc"] != pytest.approx(5.0 / (2 * 15.0))


# ---------------------------------------------------------------------------
# Orchestrator addendum -- the power table used to report overwhelming
# confidence from a resample pool of a handful of markets. The mechanism was
# arithmetic (resampling WITH REPLACEMENT from a tiny same-sign pool cannot
# produce a differently-signed total, at ANY portfolio size), not evidence
# about a real portfolio: reproduced live at `--sample 30`, where a test half
# of `n_trading=4` printed `P(profit) 1.0000` at 500 through 5000 directly
# beneath a verdict that correctly said its CI was unavailable.
#
# The FIRST fix put a floor on the number of MARKETS in the pool. The red team
# then found the same defect along a second axis: 40 markets that all belong to
# ONE event cleared a 30-market floor, so `ci95_clustered_by_event` came back
# `[None, None]` (correctly refusing at 1 < 5 events) in the same JSON object
# as `p_profit 1.0` at portfolio 5000. Concentration is live in the real
# universe -- `settled_universe(min_volume=2000)` returns 50,796 markets in
# 18,457 events, the largest being a 155-market golf bracket and a 49-market
# daily BTC threshold ladder that lands ENTIRELY in the test split at the
# harness's own median-close default cutoff.
#
# So the floor and the resample are now both EVENT-level, matching what the
# statistic actually assumes. The tests below pin: the concentrated pool the
# red team found, both sides of the event floor, the floor's relationship to
# `cluster_bootstrap`'s own, that whole events (not markets) are drawn, and
# that the empty-pool crash guard is independent of the statistical floor.
# ---------------------------------------------------------------------------


def _observations(
    pnls: list[float], *, n_events: int | None = None
) -> list[tuple[str, float]]:
    """`(event, pnl)` pairs; one event per market unless told otherwise.

    With `n_events` the markets are dealt round-robin into that many
    events, so a pool of a given SIZE can be built at any level of
    concentration.
    """
    total = len(pnls) if n_events is None else n_events
    return [(f"E{i % total}", pnl) for i, pnl in enumerate(pnls)]


def test_the_power_table_refuses_a_pool_concentrated_in_a_single_event(
) -> None:
    """The red team's exact reproduction: 40 markets, ONE event.

    Forty markets clears any market-count floor, but they are one
    correlated real-world outcome -- a golf bracket, a halftime prop
    family, one day's BTC threshold ladder -- so they are ONE draw, not
    forty. The power table must refuse exactly where the CI refuses; a
    report whose `ci95_clustered_by_event` is `[None, None]` beside a
    `p_profit` of 1.0 is two blocks of one document disagreeing about
    whether any evidence exists.
    """
    observations = [("EVENT-SINGLE", 1.0 + 0.1 * i) for i in range(40)]

    table = _power_table(observations, fill_model="pessimistic", seed=1)

    for size in PORTFOLIO_SIZES:
        block = table[str(size)]
        assert block["insufficient_sample"] is True
        assert block["p_profit"] is None
        assert block["pct5_total_pnl"] is None
        assert block["pool_n_markets"] == 40
        assert block["pool_n_events"] == 1
        assert block["pool_floor_events"] == MIN_POWER_POOL_EVENTS


def test_the_power_table_never_speaks_where_the_confidence_interval_refuses(
) -> None:
    """The power floor can never sit below `cluster_bootstrap`'s own.

    `CI_FLOOR_EVENTS` is not copied by hand: this asserts against the
    real `cluster_bootstrap`, which returns `(nan, nan)` at
    `CI_FLOOR_EVENTS - 1` distinct events and a real interval at
    `CI_FLOOR_EVENTS`. The power table assumes strictly MORE than the CI
    does (independence across the units it draws), so it must not be
    able to report a number where the CI declines to.
    """
    def interval(n_events: int) -> tuple[float, float]:
        sample = [
            {"event": f"E{i}", "pnl": 1.0 + i} for i in range(n_events)
        ]
        return cluster_bootstrap(
            sample, lambda rows: sum(r["pnl"] for r in rows) / len(rows), 50
        )

    assert math.isnan(interval(CI_FLOOR_EVENTS - 1)[0])
    assert not math.isnan(interval(CI_FLOOR_EVENTS)[0])
    assert MIN_POWER_POOL_EVENTS >= CI_FLOOR_EVENTS

    # And operationally: a pool with just too few events for the CI is
    # refused by the power table too.
    table = _power_table(
        _observations([1.0] * 40, n_events=CI_FLOOR_EVENTS - 1),
        fill_model="pessimistic",
        seed=1,
    )
    assert table["500"]["insufficient_sample"] is True


def test_the_power_table_reports_pool_composition_only_above_the_floor(
) -> None:
    """Above the floor the composition swing is a real finding; below it,
    both sides are refused.

    A pool with ONE event's sign flipped swings P(profit) from certain
    to near-zero at every portfolio size -- `n_trading` is identical in
    both cases, so the swing has nothing to do with portfolio size and
    everything to do with the pool's makeup. That is worth reporting
    when the pool is big enough to be evidence, and is exactly the
    "arithmetic, not evidence" failure mode when it is not.
    """
    floor = MIN_POWER_POOL_EVENTS
    all_positive = ([1.0, 2.0, 3.0, 4.0, 5.0] * (floor // 5 + 1))[:floor]
    one_bad_event = [*all_positive[:-1], -1000.0 * floor]

    good = _power_table(
        _observations(all_positive), fill_model="pessimistic", seed=1
    )
    bad = _power_table(
        _observations(one_bad_event), fill_model="pessimistic", seed=1
    )
    # The same two pools, one EVENT short of the floor.
    thin_good = _power_table(
        _observations(all_positive[:-1]), fill_model="pessimistic", seed=1
    )
    thin_bad = _power_table(
        _observations([*all_positive[:-2], -1000.0 * floor]),
        fill_model="pessimistic",
        seed=1,
    )

    for size in PORTFOLIO_SIZES:
        assert good[str(size)]["p_profit"] == pytest.approx(1.0)
        # Pool mean is deeply negative: a portfolio of ANY of these sizes
        # is reliably wiped out by the one bad event dominating the draw.
        assert bad[str(size)]["p_profit"] < 0.05
        # One event fewer and neither pool is evidence about anything.
        assert thin_good[str(size)]["p_profit"] is None
        assert thin_bad[str(size)]["p_profit"] is None


def test_the_power_block_self_describes_the_pool_it_was_drawn_from() -> None:
    """A reader of ONE power block must be able to tell it was resampled
    from three markets rather than three thousand.

    The pool size used to live only on the sibling `n_trading` field of
    the block the power table is nested inside, so a power block lifted
    out of its context -- which is how the printed table renders it --
    carried no way to discount it. The pool's market count, its EVENT
    count and the floor it was judged against all travel with the block.
    """
    block = _power_table(
        _observations([1.0, 2.0, 3.0]), fill_model="pessimistic", seed=1
    )["500"]

    assert set(block) == {
        "fill_model", "terminal", "portfolio_markets", "portfolio_events",
        "resample", "statistic", "pool_n_markets", "pool_n_events",
        "pool_floor_events", "mean_markets_per_event",
        "insufficient_sample", "pct5_total_pnl", "p_profit",
    }
    assert block["pool_n_markets"] == 3
    assert block["pool_n_events"] == 3
    assert block["resample"] == "whole_events_with_replacement"
    assert block["statistic"] == "cash_settled_pnl_per_trading_market"


def test_the_power_floor_is_pinned_on_both_sides() -> None:
    """One EVENT either side of `MIN_POWER_POOL_EVENTS`.

    The floor is a refusal, not a warning: at `floor - 1` events both
    figures are `null` and the block says `insufficient_sample`; at
    exactly `floor` they are real numbers. Pinned on a pool whose sign
    is mixed, so the numbers that appear above the floor are not
    themselves a forced-arithmetic artifact.
    """
    floor = MIN_POWER_POOL_EVENTS
    pool = ([1.0, -0.5] * floor)[:floor]

    below = _power_table(
        _observations(pool[:-1]), fill_model="pessimistic", seed=1
    )["1000"]
    at = _power_table(
        _observations(pool), fill_model="pessimistic", seed=1
    )["1000"]

    assert below["pool_n_events"] == floor - 1
    assert below["insufficient_sample"] is True
    assert below["p_profit"] is None
    assert below["pct5_total_pnl"] is None

    assert at["pool_n_events"] == floor
    assert at["insufficient_sample"] is False
    assert isinstance(at["p_profit"], float)
    assert isinstance(at["pct5_total_pnl"], float)


def test_the_resample_draws_whole_events_not_single_markets() -> None:
    """Drawing markets i.i.d. would understate the portfolio's variance.

    Forty events of twenty-five markets each; every market inside an
    event carries the SAME sign, which is what "one event is one
    correlated real-world outcome" means. A portfolio of 500 markets is
    then twenty whole events, so its total is `25 * sum(20 coin flips)`
    -- sd 25*sqrt(20) = 111.8 and a 5th percentile near -184. Drawing
    500 markets i.i.d. instead would give sd sqrt(500) = 22.4 and a 5th
    percentile near -37: a portfolio five times safer than the data
    supports, purely from treating 25 copies of one outcome as 25
    independent bets. The threshold below is unreachable by the i.i.d.
    draw (it would need a 3.5-sigma excursion of an estimate averaged
    over 500 replicates) and comfortable for the event draw.
    """
    observations = [
        (f"E{e}", 1.0 if e % 2 == 0 else -1.0)
        for e in range(40)
        for _ in range(25)
    ]

    block = _power_table(observations, fill_model="pessimistic", seed=1)["500"]

    assert block["pool_n_markets"] == 1000
    assert block["pool_n_events"] == 40
    assert block["mean_markets_per_event"] == pytest.approx(25.0)
    # 500 markets' worth of a 25-market event is twenty whole events.
    assert block["portfolio_events"] == 20
    assert block["pct5_total_pnl"] < -100.0


def test_an_empty_pool_is_insufficient_rather_than_merely_absent() -> None:
    """Zero trading markets is the extreme of the same refusal."""
    block = _power_table([], fill_model="pessimistic", seed=1)["500"]

    assert block["pool_n_markets"] == 0
    assert block["pool_n_events"] == 0
    assert block["insufficient_sample"] is True
    assert block["p_profit"] is None
    assert block["pct5_total_pnl"] is None


def test_the_empty_pool_guard_does_not_depend_on_the_evidence_floor(
    monkeypatch,
) -> None:
    """The crash guard and the statistical floor are separate checks.

    For one release they were the same constant: `rng.choices` raises
    `IndexError` on an empty population, and the only thing preventing
    it was `MIN_POWER_POOL_MARKETS` happening to be greater than zero.
    Deleting the floor for statistical reasons produced eleven test
    failures, seven of them unrelated tests crashing with `IndexError`.
    Anyone retuning the evidence floor was silently retuning crash
    behaviour, which is why the guard now owes nothing to the floor's
    value: with the floor at ZERO -- the setting that removes every
    statistical refusal -- an empty pool must still come back refused
    rather than raising.
    """
    monkeypatch.setattr(
        "app.scripts.mm_backtest.MIN_POWER_POOL_EVENTS", 0
    )

    block = _power_table([], fill_model="pessimistic", seed=1)["500"]

    assert block["insufficient_sample"] is True
    assert block["p_profit"] is None
    assert block["pct5_total_pnl"] is None
    # The floor really was disabled: one event now clears it.
    single = _power_table(
        [("E0", 1.0)], fill_model="pessimistic", seed=1
    )["500"]
    assert single["insufficient_sample"] is False


# ---------------------------------------------------------------------------
# Both P&L figures reach the report, and they are not the same number.
# ---------------------------------------------------------------------------


def _diverging_market(market_id: str, event: str, close_ts: int) -> MarketCandles:
    """The Probe-1 market: cash $0.00, markout -$2.45, three fills."""
    return _market(
        [
            _candle(3600, bid=0.30, ask=0.71),
            _candle(7200, bid=0.30, ask=0.71, low=0.20, high=0.50, volume=100.0),
            _candle(10800, bid=0.30, ask=0.71, low=0.50, high=0.80, volume=100.0),
            _candle(14400, bid=0.70, ask=0.80, low=0.50, high=0.80, volume=100.0),
            _candle(18000, bid=0.90, ask=1.00),
        ],
        result="yes",
        market_id=market_id,
        event=event,
        close_ts=close_ts,
    )


def test_every_report_block_carries_both_the_cash_pnl_and_the_markout() -> None:
    """`report()` must not make a reader choose between the two numbers.

    Eight copies of the Probe-1 market: each broke exactly even for real
    money and each was picked off for -$2.45 by the markout statistic.
    A report showing only one of those tells a reader either that the
    strategy is free or that it is bleeding, and both readings are
    wrong on their own.
    """
    markets = [
        _diverging_market(f"M{i}", f"E{i}", CUTOFF_TS + i * 86_400)
        for i in range(8)
    ]
    result = replay(
        markets, policy=POLICY, fill_model="pessimistic", schedule=NO_MAKER_FEE
    )

    payload = report(
        result, split="temporal", cutoff_ts=CUTOFF_TS + 4 * 86_400
    )

    for name in ("overall", "train", "test"):
        block = payload[name]
        assert block["pnl_basis"] == "cash_settled"
        assert block["markout_basis"] == "marked_at_i_plus_2"
        assert set(block) >= {
            "total_pnl", "mean_pnl_per_trading_market",
            "total_markout_pnl", "mean_markout_pnl_per_trading_market",
            "sd_markout_pnl_per_trading_market",
        }
    assert payload["overall"]["n_trading"] == 8
    assert payload["overall"]["total_pnl"] == pytest.approx(0.0, abs=1e-9)
    assert payload["overall"]["total_markout_pnl"] == pytest.approx(8 * -2.45)
    assert payload["overall"]["mean_pnl_per_trading_market"] == pytest.approx(
        0.0, abs=1e-9
    )
    assert payload["overall"][
        "mean_markout_pnl_per_trading_market"
    ] == pytest.approx(-2.45)
    # The verdict is the go/no-go, so it is computed on the money and
    # says which basis it used.
    assert payload["verdict"]["pnl_basis"] == "cash_settled"
    assert payload["verdict"]["statistic"] == "mean_pnl_per_trading_market"


def test_the_confidence_interval_is_computed_on_the_cash_pnl() -> None:
    """The CI -- and therefore the Gate 1 verdict -- must track the money.

    Every one of these markets is exactly break-even for real money and
    exactly -$2.45 by markout, so a CI computed on cash collapses onto
    zero while a CI computed on markout could not contain it. Pinning
    that is what stops the mark-dependent number from silently driving
    the verdict again.
    """
    markets = [
        _diverging_market(f"M{i}", f"E{i}", CUTOFF_TS + i * 86_400)
        for i in range(8)
    ]
    result = replay(
        markets, policy=POLICY, fill_model="pessimistic", schedule=NO_MAKER_FEE
    )

    overall = report(result, split="event")["overall"]

    lo, hi = overall["ci95_clustered_by_event"]
    assert lo == pytest.approx(0.0, abs=1e-9)
    assert hi == pytest.approx(0.0, abs=1e-9)
