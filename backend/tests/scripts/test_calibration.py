"""The calibration study's arithmetic, pinned without touching the network.

Three properties decide whether the study answers the question or a
different one, and each corresponds to a trap that produces a confident
WRONG answer:

* P&L executes at the TOUCH, not the mid. On the live sample the mean
  quoted spread was 16 cents against calibration deviations of 3-9
  cents, so a study priced at the mid finds an edge that a trader cannot
  reach.
* Fees come from the model, never a literal (GUARDRAILS.md §1.5), and
  Kalshi's whole-cent floor is most of the cost at the extremes — where
  the favorite-longshot claim actually lives.
* Intervals resample whole EVENTS. The candidates in a race share an
  outcome, and resampling markets independently would report an interval
  narrower than the evidence supports.
"""
import math

import pytest

from app.scripts.calibration import (
    BUCKET_EDGES,
    Calibration,
    bucket_of,
    cluster_bootstrap,
)


def _row(bid: float, ask: float, outcome: float, event: str = "E1") -> dict:
    return {"bid": bid, "ask": ask, "outcome": outcome, "event": event,
            "mid": (bid + ask) / 2, "spread": ask - bid}


def test_buckets_tile_the_unit_interval_without_gaps() -> None:
    assert BUCKET_EDGES[0] == 0.0
    assert BUCKET_EDGES[-1] == 1.0
    assert list(BUCKET_EDGES) == sorted(BUCKET_EDGES)
    for price in (0.0, 0.049, 0.05, 0.5, 0.899, 0.95, 0.9999):
        i = bucket_of(price)
        assert BUCKET_EDGES[i] <= price < BUCKET_EDGES[i + 1] or price >= BUCKET_EDGES[-2]


def test_buying_pays_the_ask_and_selling_receives_the_bid() -> None:
    """The spread must be a cost in BOTH directions, never a subsidy."""
    study = Calibration()
    # A market quoted 0.40/0.60 that settles YES.
    row = _row(bid=0.40, ask=0.60, outcome=1.0)

    buy, sell = study.net_buy(row), study.net_sell(row)

    # Buyer paid 0.60 plus fee for a dollar; seller received 0.40 minus
    # fee and paid a dollar. Neither may be scored against the 0.50 mid.
    assert buy < 1.0 - 0.60
    assert sell < 0.40 - 1.0 + 1e-9
    # And the two sides cannot both profit on one trade.
    assert buy + sell < 0.0


def test_the_fee_is_charged_and_comes_from_the_model() -> None:
    """A zero-fee study would report an edge the venue takes away."""
    study = Calibration()
    row = _row(bid=0.50, ask=0.50, outcome=1.0)

    # Gross of fees this is exactly +0.50; the reported number must be less.
    assert study.net_buy(row) < 0.50
    assert study.schedule.taker_rate > 0.0


def test_a_certain_favorite_is_not_profitable_at_the_fee_floor() -> None:
    """The measured result, reproduced as arithmetic.

    Kalshi's fee is ceiled to a whole cent per fill for non-direct
    members, so buying at 0.99 to collect 1.00 cannot pay even when the
    contract ALWAYS settles yes. This is why the live 0.95-1.00 bucket
    showed realized ~1.0 and still lost money — the apparent
    "underpricing" of favorites is the transaction-cost floor, not an
    inefficiency.
    """
    study = Calibration()

    assert study.net_buy(_row(bid=0.98, ask=0.99, outcome=1.0)) <= 0.0


def test_the_interval_widens_when_outcomes_share_an_event() -> None:
    """Clustered vs naive is the difference between honest and flattering."""
    # 40 markets, all in ONE event, all settling the same way: there is
    # really only one observation here.
    one_event = [_row(0.5, 0.5, 1.0, event="E1") for _ in range(40)]
    lo, hi = cluster_bootstrap(one_event, lambda z: sum(r["outcome"] for r in z) / len(z))

    # Too few distinct events to resample: the study must refuse to
    # report an interval rather than invent a tight one.
    assert math.isnan(lo) and math.isnan(hi)


def test_independent_events_do_produce_an_interval() -> None:
    rows = [_row(0.5, 0.5, float(i % 2), event=f"E{i}") for i in range(60)]

    lo, hi = cluster_bootstrap(rows, lambda z: sum(r["outcome"] for r in z) / len(z))

    assert not math.isnan(lo)
    assert lo < 0.5 < hi


def test_the_venues_own_result_spelling_is_accepted() -> None:
    """A row scored `"yes"`/`"no"` must convert, not vanish."""
    rows = [
        {"bid_24h": 0.4, "ask_24h": 0.5, "result": "yes", "event": "E1"},
        {"bid_24h": 0.4, "ask_24h": 0.5, "result": "no", "event": "E2"},
    ]

    kept = Calibration.observations(rows, 24)

    assert [r["outcome"] for r in kept] == [1.0, 0.0]


def test_a_row_with_no_outcome_at_all_is_dropped_not_scored_as_a_loss() -> None:
    """Defaulting a missing outcome to 0.0 would bias every bucket down."""
    rows = [{"bid_24h": 0.4, "ask_24h": 0.5, "event": "E1"},
            {"bid_24h": 0.4, "ask_24h": 0.5, "result": "scalar", "event": "E2"}]

    assert Calibration.observations(rows, 24) == []


def test_observations_drop_rows_with_no_quote_on_both_sides() -> None:
    """A one-sided book has no mid, and inventing one would fabricate data."""
    rows = [
        {"bid_24h": 0.4, "ask_24h": 0.5, "outcome": 1.0, "event": "E1"},
        {"bid_24h": None, "ask_24h": 0.5, "outcome": 1.0, "event": "E2"},
        {"bid_24h": 0.0, "ask_24h": 0.0, "outcome": 0.0, "event": "E3"},
    ]

    kept = Calibration.observations(rows, 24)

    assert len(kept) == 1
    assert kept[0]["mid"] == pytest.approx(0.45)
