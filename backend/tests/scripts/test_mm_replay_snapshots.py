"""`app.scripts.mm_replay_snapshots` (mm-proveout T11).

Derived from TASKS.md T11's brief and its CORRECTION block, not from the
implementation:

  * The through-model fills exactly when the NEXT snapshot's own best bid is
    BELOW the resting quote's bid (buy) / best ask is ABOVE the resting
    quote's ask (sell) -- `passive_fill.py`'s pessimistic `_filled`, fed a
    `TradeRange` built from the next snapshot's book (module docstring's
    "THE MAPPING").
  * `volume_lifetime`, never `volume`, and never defaulted: an unknown delta
    excludes an interval from BOTH fill models (optimistic is `n/a` without
    it, computed with it).
  * Unsettled markets are excluded from the verdict and COUNTED, never
    assigned a mark.
  * The maker rebate never changes `pnl`/`markout_pnl` -- it is its own
    reported line.
  * The report is produced by T2's OWN `report()`/`replay()` -- asserted by
    IDENTITY of the imported callable, not merely "produces the same
    numbers".

Uses `tests/conftest.py`'s `test_session` fixture (SQLite,
`Base.metadata.create_all`) for every database-touching test -- never a
live database, and no test ever contacts a venue (stub adapters only,
GUARDRAILS.md §1.4/§3.1).
"""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import app.scripts.mm_backtest as mm_backtest
import app.scripts.mm_replay_snapshots as mrs
from app.models.book_snapshot import BookSnapshot
from app.scripts.mm_backtest import MarketCandles, ReplayResult, replay, report
from app.scripts.mm_replay_snapshots import (
    CASH_PNL_UNAVAILABLE,
    MARKOUT_ONLY_TERMINAL,
    LoadedMarket,
    _merge_replay_results,
    _strip_terminal_settlement,
    _volume_delta,
    all_markets,
    fee_model_and_fallback,
    fee_schedule_for_market,
    load_market_candles,
    render_header,
    replay_snapshots,
    resolved_markets,
    snapshots_to_candles,
    to_markout_only_report,
)
from app.strategies.market_making import MarketMaker
from app.venues.fees import PolymarketFeeModel
from app.venues.types import FeeSchedule, VenueMarket

T0 = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)
T1 = T0 + timedelta(hours=1)
T2 = T0 + timedelta(hours=2)
T3 = T0 + timedelta(hours=3)

#: No maker fee, no rebate: every expected P&L below is exact arithmetic.
NO_MAKER_FEE = FeeSchedule(taker_rate=0.05, maker_rate=0.0, source="test")

#: Against a 0.30/0.71 book this quotes 0.34/0.67 -- the SAME
#: hand-computed numbers `tests/scripts/test_mm_backtest.py`'s
#: `POLICY`/`WIDE_BID`/`WIDE_ASK`/`QUOTED_BID`/`QUOTED_ASK` pin, reused
#: here rather than re-derived. `min_spread`/`edge_fraction`/
#: `max_inventory` are pinned EXPLICITLY, matching that file, rather
#: than left to `MarketMaker`'s calibrated defaults -- which move when
#: calibration evidence moves (`app/strategies/market_making.py`
#: docstring) and would otherwise silently change what 0.34/0.67 means
#: here.
POLICY = MarketMaker(
    quote_size=10.0, skew_strength=0.0,
    min_spread=0.10, edge_fraction=0.80, max_inventory=20.0,
)
WIDE_BID, WIDE_ASK = 0.30, 0.71
QUOTED_BID, QUOTED_ASK = 0.34, 0.67


def _row(**overrides: Any) -> BookSnapshot:
    fields: dict[str, Any] = {
        "venue": "polymarket",
        "market_id": "M1",
        "outcome": "YES",
        "ts": T0,
        "bids": [{"price": WIDE_BID, "size": 100.0}],
        "asks": [{"price": WIDE_ASK, "size": 100.0}],
        "tick_size": 0.01,
        "min_size": 1.0,
    }
    fields.update(overrides)
    return BookSnapshot(**fields)


def _venue_market(
    market_id: str = "M1", *, result: str | None = "yes", event_id: str | None = "E1"
) -> VenueMarket:
    return VenueMarket(
        venue="polymarket",
        market_id=market_id,
        event_id=event_id,
        question="Will it?",
        outcomes=("YES", "NO"),
        outcome_ids={"YES": market_id, "NO": market_id},
        rules_text="",
        resolution_source=None,
        close_time=T3,
        expected_settle_time=None,
        status="resolved" if result else "open",
        result=result,
        tick_size=0.01,
        min_size=1.0,
        fee=NO_MAKER_FEE,
        raw={},
    )


class _StubAdapter:
    """Just enough for `resolved_markets`: no network."""

    def __init__(self, markets: list[VenueMarket]) -> None:
        self._markets = markets

    async def list_markets(self, status: str | None = None) -> list[VenueMarket]:
        assert status == "resolved"
        return list(self._markets)


def _market(candles, *, market_id: str = "M1", result: str = "yes") -> MarketCandles:
    return MarketCandles(
        venue="polymarket",
        market_id=market_id,
        event="E1",
        series="",
        close_ts=int(T3.timestamp()),
        result=result,
        candles=tuple(candles),
    )


async def _seed(session: AsyncSession, *rows: BookSnapshot) -> None:
    for row in rows:
        session.add(row)
    await session.commit()


# ---------------------------------------------------------------------------
# `_volume_delta` -- unknown/negative is None, never 0.0 or 1.0.
# ---------------------------------------------------------------------------


def test_a_known_non_decreasing_delta_is_returned() -> None:
    assert _volume_delta(1000.0, 1010.0) == 10.0


def test_a_zero_delta_is_a_real_zero_not_unknown() -> None:
    assert _volume_delta(1000.0, 1000.0) == 0.0


@pytest.mark.parametrize("previous,current", [(None, 10.0), (10.0, None), (None, None)])
def test_either_side_missing_is_unknown(previous, current) -> None:
    assert _volume_delta(previous, current) is None


def test_a_decrease_is_unknown_never_negative_never_the_smaller_value() -> None:
    assert _volume_delta(1010.0, 1000.0) is None


# ---------------------------------------------------------------------------
# `snapshots_to_candles` -- the mapping, and the two exclusion counts.
# ---------------------------------------------------------------------------


def test_bid_close_and_ask_close_are_each_snapshots_own_book() -> None:
    rows = [
        _row(ts=T0, bids=[{"price": 0.30, "size": 10.0}], asks=[{"price": 0.71, "size": 10.0}]),
        _row(
            ts=T1,
            bids=[{"price": 0.35, "size": 10.0}],
            asks=[{"price": 0.60, "size": 10.0}],
            volume_lifetime=110.0,
        ),
    ]
    rows[0].volume_lifetime = 100.0
    built = snapshots_to_candles(rows)
    assert (built.candles[0].bid_close, built.candles[0].ask_close) == (0.30, 0.71)
    assert (built.candles[1].bid_close, built.candles[1].ask_close) == (0.35, 0.60)


def test_the_second_candles_trade_range_is_the_delta_and_its_own_book() -> None:
    rows = [
        _row(ts=T0, volume_lifetime=1000.0),
        _row(
            ts=T1,
            bids=[{"price": 0.20, "size": 10.0}],
            asks=[{"price": 0.50, "size": 10.0}],
            volume_lifetime=1010.0,
        ),
    ]
    built = snapshots_to_candles(rows)
    second = built.candles[1]
    assert second.volume == 10.0
    assert (second.px_low, second.px_high) == (0.20, 0.50)


def test_the_very_first_candle_never_carries_a_real_trade_range() -> None:
    """Placeholder only -- `replay()` never reads index 0's trade fields."""
    rows = [_row(ts=T0, volume_lifetime=1000.0)]
    built = snapshots_to_candles(rows)
    assert built.candles[0].px_low is None
    assert built.candles[0].px_high is None
    assert built.candles[0].volume == 0.0


def test_unknown_volume_blanks_the_trade_range_and_is_counted() -> None:
    """A restated (decreasing) `volume_lifetime` excludes that ONE interval
    from both fill models (no `px_low`/`px_high`) without tainting the
    NEXT interval, whose own delta is computed from the restated value
    forward (`_volume_delta`'s docstring: one restatement resets the
    baseline instead of vetoing every later reading)."""
    rows = [
        _row(ts=T0, volume_lifetime=1000.0),
        _row(ts=T1, volume_lifetime=990.0),  # a restatement: decrease
        _row(ts=T2, volume_lifetime=1020.0),  # known again, baseline reset
        _row(ts=T3, volume_lifetime=1040.0),
    ]
    built = snapshots_to_candles(rows)
    assert built.candles[1].px_low is None
    assert built.candles[1].px_high is None
    assert built.candles[2].volume == 30.0  # 1020 - 990, not tainted
    assert built.n_unknown_volume_intervals == 1
    assert built.n_incomplete_fill_book_intervals == 0


def test_a_known_delta_with_a_one_sided_book_is_incomplete_not_unknown() -> None:
    rows = [
        _row(ts=T0, volume_lifetime=1000.0),
        _row(ts=T1, asks=[], volume_lifetime=1010.0),  # no ask at all
        _row(ts=T2, volume_lifetime=1020.0),
        _row(ts=T3, volume_lifetime=1030.0),
    ]
    built = snapshots_to_candles(rows)
    assert built.candles[1].px_low is None
    assert built.candles[1].px_high is None
    assert built.n_unknown_volume_intervals == 0
    assert built.n_incomplete_fill_book_intervals == 1


def test_a_crossed_book_is_incomplete_and_does_not_raise() -> None:
    """A crossed `(bid > ask)` `TradeRange` would raise inside `_trades()` if
    ever constructed -- this must never build one."""
    rows = [
        _row(ts=T0, volume_lifetime=1000.0),
        _row(
            ts=T1,
            bids=[{"price": 0.80, "size": 10.0}],
            asks=[{"price": 0.20, "size": 10.0}],
            volume_lifetime=1010.0,
        ),
        _row(ts=T2, volume_lifetime=1020.0),
        _row(ts=T3, volume_lifetime=1030.0),
    ]
    built = snapshots_to_candles(rows)  # must not raise
    assert built.candles[1].px_low is None
    assert built.candles[1].px_high is None
    assert built.n_incomplete_fill_book_intervals == 1


def test_an_unknown_delta_ending_at_the_last_candle_is_never_counted() -> None:
    """`replay()` never reads the LAST candle's trade fields as a fill
    interval (module docstring: `j` in `[1, len(candles) - 2]` only) -- an
    unknown volume there must not inflate the reported exclusion count,
    even though the earlier, in-range deltas are perfectly known."""
    rows = [
        _row(ts=T0, volume_lifetime=1000.0),
        _row(ts=T1, volume_lifetime=1010.0),
        _row(ts=T2, volume_lifetime=1020.0),
        _row(ts=T3, volume_lifetime=None),  # last index's own delta: never read
    ]
    built = snapshots_to_candles(rows)
    assert built.n_unknown_volume_intervals == 0


# ---------------------------------------------------------------------------
# The through-model: fills exactly when the next best bid/ask crosses the
# quote (T11's acceptance criterion).
# ---------------------------------------------------------------------------


def _triple(fill_bid: float, fill_ask: float, *, volume_lifetime: float = 1010.0) -> MarketCandles:
    rows = [
        _row(ts=T0, bids=[{"price": WIDE_BID, "size": 10.0}], asks=[{"price": WIDE_ASK, "size": 10.0}], volume_lifetime=1000.0),
        _row(ts=T1, bids=[{"price": fill_bid, "size": 10.0}], asks=[{"price": fill_ask, "size": 10.0}], volume_lifetime=volume_lifetime),
        _row(ts=T2, bids=[{"price": 0.40, "size": 10.0}], asks=[{"price": 0.60, "size": 10.0}], volume_lifetime=1020.0),
    ]
    built = snapshots_to_candles(rows)
    return _market(built.candles)


def test_a_touch_at_the_bid_fills_optimistically_but_not_pessimistically() -> None:
    market = _triple(fill_bid=QUOTED_BID, fill_ask=0.50)
    optimistic = replay([market], policy=POLICY, fill_model="optimistic", schedule=NO_MAKER_FEE)
    pessimistic = replay([market], policy=POLICY, fill_model="pessimistic", schedule=NO_MAKER_FEE)
    assert optimistic.rows[0].n_fills == 1
    assert pessimistic.rows[0].n_fills == 0


def test_a_next_best_bid_below_the_quote_bid_fills_under_both_models() -> None:
    market = _triple(fill_bid=0.20, fill_ask=0.50)
    for model in ("optimistic", "pessimistic"):
        result = replay([market], policy=POLICY, fill_model=model, schedule=NO_MAKER_FEE)
        assert result.rows[0].n_fills == 1, model


def test_a_touch_at_the_ask_fills_optimistically_but_not_pessimistically() -> None:
    market = _triple(fill_bid=0.50, fill_ask=QUOTED_ASK)
    optimistic = replay([market], policy=POLICY, fill_model="optimistic", schedule=NO_MAKER_FEE)
    pessimistic = replay([market], policy=POLICY, fill_model="pessimistic", schedule=NO_MAKER_FEE)
    assert optimistic.rows[0].n_fills == 1
    assert pessimistic.rows[0].n_fills == 0


def test_a_next_best_ask_above_the_quote_ask_fills_under_both_models() -> None:
    market = _triple(fill_bid=0.50, fill_ask=0.80)
    for model in ("optimistic", "pessimistic"):
        result = replay([market], policy=POLICY, fill_model=model, schedule=NO_MAKER_FEE)
        assert result.rows[0].n_fills == 1, model


def test_optimistic_is_n_a_without_volume_and_computed_with_it() -> None:
    """A touch-at-the-bid interval: optimistic-fillable in principle, but
    only when the volume delta is known."""
    unknown_volume_market = _triple(fill_bid=QUOTED_BID, fill_ask=0.50, volume_lifetime=None)
    known_volume_market = _triple(fill_bid=QUOTED_BID, fill_ask=0.50, volume_lifetime=1010.0)

    unknown_payload = report(
        replay([unknown_volume_market], policy=POLICY, fill_model="optimistic", schedule=NO_MAKER_FEE),
        split="event",
        seed=1,
    )
    known_payload = report(
        replay([known_volume_market], policy=POLICY, fill_model="optimistic", schedule=NO_MAKER_FEE),
        split="event",
        seed=1,
    )

    assert unknown_payload["overall"]["n_trading"] == 0
    assert unknown_payload["overall"]["mean_pnl_per_trading_market"] is None

    assert known_payload["overall"]["n_trading"] == 1
    assert known_payload["overall"]["mean_pnl_per_trading_market"] is not None


def test_rebate_is_reported_but_never_changes_pnl() -> None:
    through_market = _triple(fill_bid=0.20, fill_ask=0.50)
    no_rebate = replace(through_market, fee=NO_MAKER_FEE)
    with_rebate = replace(
        through_market,
        fee=FeeSchedule(taker_rate=0.05, maker_rate=0.0, source="test", maker_rebate_rate=0.30),
    )

    result_no_rebate = replay([no_rebate], policy=POLICY, fill_model="pessimistic", schedule=NO_MAKER_FEE)
    result_with_rebate = replay([with_rebate], policy=POLICY, fill_model="pessimistic", schedule=NO_MAKER_FEE)

    assert result_no_rebate.rows[0].pnl == result_with_rebate.rows[0].pnl
    assert result_no_rebate.rows[0].markout_pnl == result_with_rebate.rows[0].markout_pnl
    assert result_no_rebate.rows[0].rebate_if_paid == 0.0
    assert result_with_rebate.rows[0].rebate_if_paid > 0.0


# ---------------------------------------------------------------------------
# `fee_schedule_for_market` -- the fee in force, never a partial substitute.
# ---------------------------------------------------------------------------


def test_the_latest_complete_fee_reading_wins() -> None:
    rows = [
        _row(
            ts=T0, taker_fee_rate=0.05, maker_fee_rate=0.02,
            fee_source="category_table", maker_rebate_rate=0.0,
        ),
        _row(
            ts=T1, taker_fee_rate=0.07, maker_fee_rate=0.0175,
            fee_source="venue_schedule", maker_rebate_rate=0.25,
        ),
    ]
    schedule, is_fallback = fee_schedule_for_market(rows, FeeSchedule(0.05, 0.0, "fallback"))
    assert is_fallback is False
    assert (schedule.taker_rate, schedule.maker_rate, schedule.source, schedule.maker_rebate_rate) == (
        0.07, 0.0175, "venue_schedule", 0.25,
    )


def test_a_partial_row_is_never_treated_as_complete() -> None:
    """Missing even ONE of the four fee columns is not a genuine reading."""
    rows = [
        _row(
            ts=T0, taker_fee_rate=0.05, maker_fee_rate=0.02,
            fee_source="category_table", maker_rebate_rate=0.0,
        ),
        _row(ts=T1, taker_fee_rate=0.07, maker_fee_rate=None, fee_source="venue_schedule", maker_rebate_rate=0.25),
    ]
    schedule, is_fallback = fee_schedule_for_market(rows, FeeSchedule(0.05, 0.0, "fallback"))
    assert is_fallback is False
    assert schedule.source == "category_table"  # the earlier, COMPLETE row


def test_no_complete_reading_anywhere_falls_back() -> None:
    rows = [_row(ts=T0), _row(ts=T1)]  # no fee columns at all
    fallback = FeeSchedule(0.05, 0.0, "fallback")
    schedule, is_fallback = fee_schedule_for_market(rows, fallback)
    assert is_fallback is True
    assert schedule is fallback


# ---------------------------------------------------------------------------
# `replay_snapshots` -- grouped by tick size, merged, T2's `replay()`.
# ---------------------------------------------------------------------------


def test_replay_snapshots_groups_by_tick_size_and_merges(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[float] = []
    real_replay = mrs.replay

    def _recording_replay(markets, **kwargs):
        calls.append(kwargs["tick_size"])
        return real_replay(markets, **kwargs)

    monkeypatch.setattr(mrs, "replay", _recording_replay)

    market_a = LoadedMarket(candles=_triple(0.20, 0.50, volume_lifetime=1010.0), tick_size=0.01)
    market_b = LoadedMarket(
        candles=_market(
            snapshots_to_candles(
                [
                    _row(ts=T0, market_id="M2", volume_lifetime=1000.0),
                    _row(ts=T1, market_id="M2", bids=[{"price": 0.20, "size": 10.0}], asks=[{"price": 0.50, "size": 10.0}], volume_lifetime=1010.0),
                    _row(ts=T2, market_id="M2", volume_lifetime=1020.0),
                ]
            ).candles,
            market_id="M2",
        ),
        tick_size=0.001,
    )

    result = mrs.replay_snapshots(
        [market_a, market_b],
        policy=POLICY,
        fill_model="pessimistic",
        fee_model=PolymarketFeeModel(),
        schedule=NO_MAKER_FEE,
    )

    assert sorted(calls) == [0.001, 0.01]
    assert {row.market_id for row in result.rows} == {"M1", "M2"}
    assert result.n_intervals == sum(r.n_intervals for r in [
        replay([market_a.candles], policy=POLICY, fill_model="pessimistic", tick_size=0.01, fee_model=PolymarketFeeModel(), schedule=NO_MAKER_FEE),
        replay([market_b.candles], policy=POLICY, fill_model="pessimistic", tick_size=0.001, fee_model=PolymarketFeeModel(), schedule=NO_MAKER_FEE),
    ])


def test_replay_snapshots_on_no_markets_returns_an_empty_result() -> None:
    result = mrs.replay_snapshots(
        [], policy=POLICY, fill_model="pessimistic", fee_model=PolymarketFeeModel(), schedule=NO_MAKER_FEE
    )
    assert result.rows == ()
    assert result.n_intervals == 0


def test_merge_refuses_to_mix_fill_models() -> None:
    a = ReplayResult(fill_model="pessimistic", rows=())
    b = ReplayResult(fill_model="optimistic", rows=())
    with pytest.raises(ValueError, match="different fill models"):
        _merge_replay_results([a, b])


def test_merge_refuses_an_empty_sequence() -> None:
    with pytest.raises(ValueError):
        _merge_replay_results([])


# ---------------------------------------------------------------------------
# `resolved_markets` / `fee_model_and_fallback`
# ---------------------------------------------------------------------------


async def test_resolved_markets_indexes_by_market_id() -> None:
    adapter = _StubAdapter([_venue_market("A"), _venue_market("B")])
    resolved = await resolved_markets(adapter)
    assert set(resolved) == {"A", "B"}


def test_fee_model_and_fallback_is_venue_specific() -> None:
    kalshi_model, kalshi_schedule = fee_model_and_fallback("kalshi")
    poly_model, poly_schedule = fee_model_and_fallback("polymarket")
    assert type(kalshi_model).__name__ == "KalshiFeeModel"
    assert type(poly_model).__name__ == "PolymarketFeeModel"
    assert kalshi_schedule.source == "settings_default"
    assert poly_schedule.source == "category_table"


def test_fee_model_and_fallback_refuses_an_unknown_venue() -> None:
    with pytest.raises(ValueError):
        fee_model_and_fallback("nasdaq")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# `load_market_candles` -- the loader T12 reuses, and every exclusion count.
# ---------------------------------------------------------------------------


async def test_an_empty_table_reports_zero_and_nothing_to_replay(test_session: AsyncSession) -> None:
    loaded, diagnostics = await load_market_candles(
        test_session,
        "polymarket",
        since=T0 - timedelta(days=1),
        until=T3 + timedelta(days=1),
        resolved={},
        fallback_schedule=NO_MAKER_FEE,
    )
    assert loaded == []
    assert diagnostics.n_snapshot_rows == 0
    assert diagnostics.n_candidate_markets == 0
    assert diagnostics.n_replayed_markets == 0
    assert "snapshot rows 0" in render_header(diagnostics)


async def test_a_market_not_in_the_resolved_listing_is_unsettled_and_counted(
    test_session: AsyncSession,
) -> None:
    await _seed(
        test_session,
        _row(ts=T0, volume_lifetime=1000.0),
        _row(ts=T1, volume_lifetime=1010.0),
        _row(ts=T2, volume_lifetime=1020.0),
        _row(ts=T3, volume_lifetime=1030.0),
    )
    loaded, diagnostics = await load_market_candles(
        test_session,
        "polymarket",
        since=T0 - timedelta(hours=1),
        until=T3 + timedelta(hours=1),
        resolved={},  # M1 not found -> not yet resolved
        fallback_schedule=NO_MAKER_FEE,
    )
    assert loaded == []
    assert diagnostics.n_unsettled == 1
    assert diagnostics.n_candidate_markets == 1


async def test_a_non_binary_result_is_unsettled_and_counted(test_session: AsyncSession) -> None:
    await _seed(
        test_session,
        _row(ts=T0, volume_lifetime=1000.0),
        _row(ts=T1, volume_lifetime=1010.0),
        _row(ts=T2, volume_lifetime=1020.0),
        _row(ts=T3, volume_lifetime=1030.0),
    )
    loaded, diagnostics = await load_market_candles(
        test_session,
        "polymarket",
        since=T0 - timedelta(hours=1),
        until=T3 + timedelta(hours=1),
        resolved={"M1": _venue_market(result="scalar")},
        fallback_schedule=NO_MAKER_FEE,
    )
    assert loaded == []
    assert diagnostics.n_unsettled == 1


async def test_too_few_snapshots_is_short_history_and_counted(test_session: AsyncSession) -> None:
    await _seed(test_session, _row(ts=T0, volume_lifetime=1000.0), _row(ts=T1, volume_lifetime=1010.0))
    loaded, diagnostics = await load_market_candles(
        test_session,
        "polymarket",
        since=T0 - timedelta(hours=1),
        until=T3 + timedelta(hours=1),
        resolved={"M1": _venue_market()},
        fallback_schedule=NO_MAKER_FEE,
    )
    assert loaded == []
    assert diagnostics.n_short_history == 1


async def test_a_corrupt_stored_level_is_a_conversion_error_not_a_crash(
    test_session: AsyncSession,
) -> None:
    await _seed(
        test_session,
        _row(ts=T0, volume_lifetime=1000.0),
        _row(ts=T1, bids=[{"price": 5.0, "size": 1.0}], volume_lifetime=1010.0),  # out of [0,1]
        _row(ts=T2, volume_lifetime=1020.0),
        _row(ts=T3, volume_lifetime=1030.0),
    )
    loaded, diagnostics = await load_market_candles(
        test_session,
        "polymarket",
        since=T0 - timedelta(hours=1),
        until=T3 + timedelta(hours=1),
        resolved={"M1": _venue_market()},
        fallback_schedule=NO_MAKER_FEE,
    )
    assert loaded == []
    assert diagnostics.n_conversion_errors == 1


async def test_a_settled_market_with_enough_history_is_loaded_and_dated(
    test_session: AsyncSession,
) -> None:
    await _seed(
        test_session,
        _row(ts=T0, volume_lifetime=1000.0, tick_size=0.005),
        _row(ts=T1, volume_lifetime=1010.0, tick_size=0.005),
        _row(ts=T2, volume_lifetime=1020.0, tick_size=0.005),
        _row(ts=T3, volume_lifetime=1030.0, tick_size=0.005),
    )
    loaded, diagnostics = await load_market_candles(
        test_session,
        "polymarket",
        since=T0 - timedelta(hours=1),
        until=T3 + timedelta(hours=1),
        resolved={"M1": _venue_market(event_id="EV-9")},
        fallback_schedule=NO_MAKER_FEE,
    )
    assert diagnostics.n_replayed_markets == 1
    assert len(loaded) == 1
    market = loaded[0]
    assert market.tick_size == 0.005
    assert market.candles.event == "EV-9"
    assert market.candles.close_ts == int(T3.timestamp())
    assert market.candles.result == "yes"
    assert diagnostics.first_ts == T0
    assert diagnostics.last_ts == T3


async def test_median_observed_dwell_uses_observed_at_not_ts(test_session: AsyncSession) -> None:
    """`ts` gaps here are all 1 hour; `observed_at` gaps are deliberately
    different, so a median computed from the wrong column would not match."""
    await _seed(
        test_session,
        _row(ts=T0, volume_lifetime=1000.0, observed_at=T0),
        _row(ts=T1, volume_lifetime=1010.0, observed_at=T0 + timedelta(seconds=30)),
        _row(ts=T2, volume_lifetime=1020.0, observed_at=T0 + timedelta(seconds=90)),
        _row(ts=T3, volume_lifetime=1030.0, observed_at=T0 + timedelta(seconds=150)),
    )
    _loaded, diagnostics = await load_market_candles(
        test_session,
        "polymarket",
        since=T0 - timedelta(hours=1),
        until=T3 + timedelta(hours=1),
        resolved={"M1": _venue_market()},
        fallback_schedule=NO_MAKER_FEE,
    )
    assert diagnostics.median_observed_dwell_s == 60.0


# ---------------------------------------------------------------------------
# Markout-only mode -- unsettled markets INCLUDED, cash P&L is n/a, terminal
# inventory never enters markout_pnl, per-fill edge is a first-class figure.
# ---------------------------------------------------------------------------


async def test_unsettled_market_is_included_in_markout_only_and_excluded_in_settled(
    test_session: AsyncSession,
) -> None:
    """Same fixture, both modes, asserted both ways (acceptance item 1)."""
    await _seed(
        test_session,
        _row(ts=T0, volume_lifetime=1000.0),
        _row(ts=T1, volume_lifetime=1010.0),
        _row(ts=T2, volume_lifetime=1020.0),
        _row(ts=T3, volume_lifetime=1030.0),
    )
    resolved = {"M1": _venue_market(result=None)}  # open, no settlement yet

    settled_loaded, settled_diag = await load_market_candles(
        test_session,
        "polymarket",
        since=T0 - timedelta(hours=1),
        until=T3 + timedelta(hours=1),
        resolved=resolved,
        fallback_schedule=NO_MAKER_FEE,
    )
    assert settled_loaded == []
    assert settled_diag.n_unsettled == 1
    assert settled_diag.n_replayed_markets == 0

    markout_loaded, markout_diag = await load_market_candles(
        test_session,
        "polymarket",
        since=T0 - timedelta(hours=1),
        until=T3 + timedelta(hours=1),
        resolved=resolved,
        fallback_schedule=NO_MAKER_FEE,
        markout_only=True,
    )
    assert len(markout_loaded) == 1
    assert markout_diag.n_replayed_markets == 1
    assert markout_diag.n_unsettled == 1  # still counted -- just not excluded
    assert markout_diag.n_no_market_metadata == 0
    assert markout_diag.markout_only is True


async def test_markout_only_still_excludes_a_market_with_no_listing_at_all(
    test_session: AsyncSession,
) -> None:
    """Markout-only mode needs `event_id`/`close_time` from SOMEWHERE; a
    market absent from every status is still excluded, and counted under
    the new, more specific reason."""
    await _seed(
        test_session,
        _row(ts=T0, volume_lifetime=1000.0),
        _row(ts=T1, volume_lifetime=1010.0),
        _row(ts=T2, volume_lifetime=1020.0),
        _row(ts=T3, volume_lifetime=1030.0),
    )
    loaded, diagnostics = await load_market_candles(
        test_session,
        "polymarket",
        since=T0 - timedelta(hours=1),
        until=T3 + timedelta(hours=1),
        resolved={},  # M1 not found under ANY status
        fallback_schedule=NO_MAKER_FEE,
        markout_only=True,
    )
    assert loaded == []
    assert diagnostics.n_unsettled == 1
    assert diagnostics.n_no_market_metadata == 1


class _AnyStatusStubAdapter:
    """Just enough for `all_markets`: records the status it was asked for."""

    def __init__(self, markets: list[VenueMarket]) -> None:
        self._markets = markets
        self.last_status: str | None = "not called"

    async def list_markets(self, status: str | None = None) -> list[VenueMarket]:
        self.last_status = status
        return list(self._markets)


async def test_all_markets_asks_for_every_status_and_indexes_by_market_id() -> None:
    adapter = _AnyStatusStubAdapter([_venue_market("A", result=None), _venue_market("B")])
    markets = await all_markets(adapter)
    assert set(markets) == {"A", "B"}
    assert adapter.last_status is None


def test_cash_pnl_is_n_a_in_markout_only_mode_never_zero() -> None:
    """Acceptance item 2: cash P&L is `None`/n-a, never `0.0`, even on a
    market that DID fill (so a defect defaulting to `0.0` would be visible
    rather than accidentally matching a genuinely empty pool)."""
    market = _triple(fill_bid=0.20, fill_ask=0.50)  # fills under both models
    loaded = [LoadedMarket(candles=market, tick_size=0.01)]
    result = replay_snapshots(
        loaded,
        policy=POLICY,
        fill_model="pessimistic",
        fee_model=PolymarketFeeModel(),
        schedule=NO_MAKER_FEE,
        markout_only=True,
    )
    raw = report(result, split="event", seed=1)
    payload = to_markout_only_report(raw)

    assert payload["terminal"] == MARKOUT_ONLY_TERMINAL
    for name in ("overall", "train", "test"):
        block = payload[name]
        assert block["terminal"] == MARKOUT_ONLY_TERMINAL
        assert block["pnl_basis"] == CASH_PNL_UNAVAILABLE
        assert block["total_pnl"] is None
        assert block["mean_pnl_per_trading_market"] is None
        assert block["sd_pnl_per_trading_market"] is None
        assert block["roc"] is None
        assert block["ci95_clustered_by_event"] == [None, None]
        for cell in block["power"].values():
            assert cell["pct5_total_pnl"] is None
            assert cell["p_profit"] is None
    assert payload["verdict"] is None


def test_terminal_inventory_contributes_nothing_to_markout_only_statistic() -> None:
    """Acceptance item 3, direct arithmetic: `_strip_terminal_settlement`
    removes EXACTLY `terminal_inventory * (settle - last_mid)` and nothing
    else -- computed by hand: `10.0 * (1.0 - 0.5) = 5.0`, so `markout_pnl`
    of `3.0` becomes `3.0 - 5.0 = -2.0`."""
    market = _market([], result="yes")  # only `.settle` (1.0) is read below
    row = mm_backtest.MarketRow(
        market_id="M1", event="E1", series="", close_ts=0,
        quote_hours=1, n_fills=2, n_two_sided=1,
        pnl=5.0, markout_pnl=3.0, collateral_mean=1.0,
        terminal_inventory=10.0, held_into_settlement=True,
        settled_short_into_yes=False, rebate_if_paid=0.0, last_mid=0.5,
    )
    stripped = _strip_terminal_settlement(market, row)
    assert stripped.markout_pnl == pytest.approx(-2.0)
    # Every other field is untouched, including `terminal_inventory` itself
    # (kept for the `held_into_settlement` diagnostic) and `pnl`.
    assert stripped.terminal_inventory == 10.0
    assert stripped.pnl == 5.0

    # No terminal inventory, or no fill ever happened (`last_mid is None`):
    # the term `replay()` would have added was already 0.0, so nothing to
    # strip.
    flat_row = replace(row, terminal_inventory=0.0, held_into_settlement=False)
    assert _strip_terminal_settlement(market, flat_row).markout_pnl == 3.0
    never_filled_row = replace(row, last_mid=None)
    assert _strip_terminal_settlement(market, never_filled_row).markout_pnl == 3.0


def test_per_fill_markout_edge_is_total_over_fill_count() -> None:
    """Acceptance item 4: `mean_markout_pnl_per_fill` is `total_markout_pnl
    / n_fills`, reported alongside the existing per-market figure."""
    market = _triple(fill_bid=0.20, fill_ask=0.50)
    loaded = [LoadedMarket(candles=market, tick_size=0.01)]
    result = replay_snapshots(
        loaded,
        policy=POLICY,
        fill_model="pessimistic",
        fee_model=PolymarketFeeModel(),
        schedule=NO_MAKER_FEE,
        markout_only=True,
    )
    raw = report(result, split="event", seed=1)
    payload = to_markout_only_report(raw)
    overall = payload["overall"]
    assert overall["n_fills"] > 0
    assert overall["mean_markout_pnl_per_fill"] == pytest.approx(
        overall["total_markout_pnl"] / overall["n_fills"]
    )


def test_rebate_never_enters_cash_or_markout_in_markout_only_mode() -> None:
    """Acceptance item 5 (GUARDRAILS §2.3), for markout-only mode
    specifically: the rebate changes neither figure, mirroring
    `test_rebate_is_reported_but_never_changes_pnl` above."""
    through_market = _triple(fill_bid=0.20, fill_ask=0.50)
    no_rebate = replace(through_market, fee=NO_MAKER_FEE)
    with_rebate = replace(
        through_market,
        fee=FeeSchedule(taker_rate=0.05, maker_rate=0.0, source="test", maker_rebate_rate=0.30),
    )

    result_no_rebate = replay_snapshots(
        [LoadedMarket(candles=no_rebate, tick_size=0.01)],
        policy=POLICY, fill_model="pessimistic", fee_model=PolymarketFeeModel(),
        schedule=NO_MAKER_FEE, markout_only=True,
    )
    result_with_rebate = replay_snapshots(
        [LoadedMarket(candles=with_rebate, tick_size=0.01)],
        policy=POLICY, fill_model="pessimistic", fee_model=PolymarketFeeModel(),
        schedule=NO_MAKER_FEE, markout_only=True,
    )

    assert result_no_rebate.rows[0].markout_pnl == result_with_rebate.rows[0].markout_pnl
    assert result_no_rebate.rows[0].rebate_if_paid == 0.0
    assert result_with_rebate.rows[0].rebate_if_paid > 0.0

    payload_no_rebate = to_markout_only_report(report(result_no_rebate, split="event", seed=1))
    payload_with_rebate = to_markout_only_report(
        report(result_with_rebate, split="event", seed=1)
    )
    assert (
        payload_no_rebate["overall"]["total_markout_pnl"]
        == payload_with_rebate["overall"]["total_markout_pnl"]
    )
    # Cash P&L is n/a in this mode regardless, so "never enters" holds for
    # it trivially -- but the rebate stays its own separate line either way.
    assert payload_no_rebate["overall"]["total_pnl"] is None
    assert payload_with_rebate["overall"]["total_pnl"] is None


def test_markout_only_terminal_replaces_t2s_settled_label() -> None:
    """`ReplayResult.terminal` and every `terminal` key `report()` writes
    are overwritten with `MARKOUT_ONLY_TERMINAL`, never left at T2's
    `"settled"` (GUARDRAILS.md §2.1)."""
    market = _triple(fill_bid=0.20, fill_ask=0.50)
    result = replay_snapshots(
        [LoadedMarket(candles=market, tick_size=0.01)],
        policy=POLICY, fill_model="pessimistic", fee_model=PolymarketFeeModel(),
        schedule=NO_MAKER_FEE, markout_only=True,
    )
    assert result.terminal == MARKOUT_ONLY_TERMINAL

    payload = to_markout_only_report(report(result, split="temporal", cutoff_ts=int(T3.timestamp()), seed=1))
    assert payload["terminal"] == MARKOUT_ONLY_TERMINAL
    assert payload["split_exclusions"]["terminal"] == MARKOUT_ONLY_TERMINAL
    for name in ("overall", "train", "test"):
        assert payload[name]["terminal"] == MARKOUT_ONLY_TERMINAL


def test_render_header_labels_markout_only_mode() -> None:
    diagnostics = mrs.SnapshotLoadResult(
        venue="polymarket",
        since=T0,
        until=T3,
        n_snapshot_rows=4,
        n_candidate_markets=1,
        n_short_history=0,
        n_unsettled=1,
        n_conversion_errors=0,
        n_unknown_volume_intervals=0,
        n_incomplete_fill_book_intervals=0,
        n_replayed_markets=1,
        first_ts=T0,
        last_ts=T3,
        median_observed_dwell_s=None,
        markout_only=True,
        n_no_market_metadata=0,
    )
    header = render_header(diagnostics)
    assert "MARKOUT-ONLY" in header
    assert "INCLUDED, not excluded" in header


# ---------------------------------------------------------------------------
# Reuse, not reimplementation: identity of the imported callables.
# ---------------------------------------------------------------------------


def test_report_is_byte_identical_to_t2s_report_by_identity() -> None:
    assert mrs.report is mm_backtest.report


def test_replay_is_byte_identical_to_t2s_replay_by_identity() -> None:
    assert mrs.replay is mm_backtest.replay
