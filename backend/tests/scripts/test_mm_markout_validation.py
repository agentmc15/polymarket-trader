"""`app.scripts.mm_markout_validation`: the arithmetic the Kalshi markout-only
report rests on, on synthetic rows.

The replay itself is the committed harness's and is tested there; what
this file pins is that (a) the settlement term this script reports is
EXACTLY the term `replay()` folds into `markout_pnl` -- so "markout_only"
means what the report says it means -- and (b) the seed-stability and
concentration checks can fail, on inputs built to fail them.
"""
from __future__ import annotations

import pytest

from app.scripts import mm_backtest as mb
from app.scripts import mm_markout_validation as mv


def _candles(result: str) -> mb.MarketCandles:
    return mb.MarketCandles(
        venue="kalshi",
        market_id="KXT-1",
        event="KXT-1",
        series="KXT",
        close_ts=1_787_000_000,
        result=result,
        candles=(),
        fee=None,
        outcome="YES",
    )


def _row(*, markout: float, inventory: float, last_mid: float | None, fills: int = 3) -> mb.MarketRow:
    return mb.MarketRow(
        market_id="KXT-1",
        event="KXT-1",
        series="KXT",
        close_ts=1_787_000_000,
        quote_hours=10.0,
        n_fills=fills,
        n_two_sided=5,
        pnl=0.0,
        markout_pnl=markout,
        collateral_mean=5.0,
        terminal_inventory=inventory,
        held_into_settlement=inventory != 0.0,
        settled_short_into_yes=False,
        rebate_if_paid=0.0,
        last_mid=last_mid,
    )


class TestSettlementTerm:
    def test_equals_what_replay_added_for_terminal_inventory(self):
        # replay(): markout += inventory * (settle - last_mid). YES settles at 1.0.
        row = _row(markout=2.5, inventory=10.0, last_mid=0.40)
        assert mv.settlement_term(_candles("yes"), row) == pytest.approx(10.0 * (1.0 - 0.40))
        # NO settles at 0.0, so a long position loses its whole last mid.
        assert mv.settlement_term(_candles("no"), row) == pytest.approx(10.0 * (0.0 - 0.40))

    def test_zero_when_nothing_was_held(self):
        assert mv.settlement_term(_candles("yes"), _row(markout=1.0, inventory=0.0, last_mid=0.5)) == 0.0
        # Never filled: last_mid is None and replay() adds no term either.
        assert (
            mv.settlement_term(_candles("yes"), _row(markout=0.0, inventory=0.0, last_mid=None, fills=0))
            == 0.0
        )

    def test_markout_only_is_markout_settled_minus_the_term(self):
        candles = _candles("yes")
        row = _row(markout=2.5, inventory=-4.0, last_mid=0.30)
        stripped = mv._strip_terminal_settlement(candles, row)
        assert row.markout_pnl - stripped.markout_pnl == pytest.approx(mv.settlement_term(candles, row))


class TestSeedStability:
    def test_clearly_positive_values_clear_on_every_seed(self):
        events = [f"e{i}" for i in range(40)]
        values = [1.0 + 0.01 * i for i in range(40)]
        block = mv._block(events, values, replicates=200, seeds=3)
        assert block["clears_zero_on_all_seeds"] is True
        assert block["seeds_with_lower_bound_gt_zero"] == 3
        assert block["lower_bound_min"] > 0

    def test_values_centred_on_zero_do_not_clear(self):
        events = [f"e{i}" for i in range(40)]
        values = [(-1.0) ** i * 1.0 for i in range(40)]
        block = mv._block(events, values, replicates=200, seeds=3)
        assert block["clears_zero_on_all_seeds"] is False
        assert block["lower_bound_min"] < 0


class TestConcentration:
    def test_one_event_carrying_most_of_the_total_is_flagged(self):
        events = ["big", "big", "big"] + [f"e{i}" for i in range(30)]
        values = [10.0, 10.0, 10.0] + [0.1] * 30
        conc = mv._concentration(events, values, replicates=200)
        assert conc["top_event"] == "big"
        assert conc["top_event_share"] > mv.TOP_EVENT_SHARE_CEILING
        assert conc["share_under_ceiling"] is False
        # Without it, 30 rows of +0.1 with no variance: the interval is degenerate at +0.1.
        assert conc["clears_zero_without_top_event"] is True

    def test_dispersed_total_is_under_the_ceiling(self):
        events = [f"e{i}" for i in range(50)]
        values = [1.0] * 50
        conc = mv._concentration(events, values, replicates=200)
        assert conc["top_event_share"] == pytest.approx(0.02)
        assert conc["share_under_ceiling"] is True
