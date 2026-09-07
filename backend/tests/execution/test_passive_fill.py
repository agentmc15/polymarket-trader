"""Resting-order fills, and the queue assumption that decides the result.

The gap between the two fill models is not a tuning detail — measured on
34,137 hourly Kalshi candles, the same passive strategy earns +0.0051 per
fill optimistically and LOSES 0.0095 pessimistically. So the model is an
explicit input, it is recorded on every fill, and the default is the
pessimistic one.
"""
import pytest

from app.config import Settings
from app.execution.passive_fill import (
    PassiveFillEngine,
    TradeRange,
    mark_to_market,
)
from app.strategies.market_making import Quote, QuotePair
from app.venues.fees import KalshiFeeModel
from app.venues.types import FeeSchedule


def _schedule() -> FeeSchedule:
    s = Settings()
    return FeeSchedule(taker_rate=s.kalshi_taker_fee_rate,
                       maker_rate=s.kalshi_maker_fee_rate, source="settings")


def _engine(model="pessimistic") -> PassiveFillEngine:
    return PassiveFillEngine(KalshiFeeModel(), _schedule(), fill_model=model)


def _pair(bid: float | None = 0.45, ask: float | None = 0.55) -> QuotePair:
    return QuotePair(
        venue="kalshi", market_id="KXTEST-1", outcome="YES",
        bid=None if bid is None else Quote("buy", bid, 10.0),
        ask=None if ask is None else Quote("sell", ask, 10.0),
        reason="quoting_two_sided",
    )


# -- the queue assumption --------------------------------------------


def test_a_print_at_your_price_fills_you_only_optimistically() -> None:
    """The whole disagreement between the two models, in one case."""
    trades = TradeRange(low=0.45, high=0.45, volume=100.0)

    assert _engine("optimistic").fills(_pair(), trades) != []
    assert _engine("pessimistic").fills(_pair(), trades) == []


def test_a_print_through_your_price_fills_you_under_both() -> None:
    trades = TradeRange(low=0.44, high=0.44, volume=100.0)

    for model in ("optimistic", "pessimistic"):
        fills = _engine(model).fills(_pair(), trades)
        assert [f.side for f in fills] == ["buy"]


def test_the_default_is_pessimistic() -> None:
    """Assuming queue priority you have not earned is what turns a losing
    strategy into a winning backtest."""
    engine = PassiveFillEngine(KalshiFeeModel(), _schedule())

    assert engine.fill_model == "pessimistic"
    assert engine.fills(_pair(), TradeRange(0.45, 0.45, 100.0)) == []


def test_every_fill_records_the_model_that_produced_it() -> None:
    """A P&L number must never be readable without its assumption."""
    fills = _engine("optimistic").fills(_pair(), TradeRange(0.45, 0.55, 100.0))

    assert fills and all(f.fill_model == "optimistic" for f in fills)


# -- which side fills -------------------------------------------------


def test_a_buy_rests_below_and_fills_on_prints_reaching_down() -> None:
    fills = _engine().fills(_pair(), TradeRange(low=0.40, high=0.50, volume=10.0))

    assert [f.side for f in fills] == ["buy"]
    assert fills[0].price == pytest.approx(0.45)


def test_a_sell_rests_above_and_fills_on_prints_reaching_up() -> None:
    fills = _engine().fills(_pair(), TradeRange(low=0.50, high=0.60, volume=10.0))

    assert [f.side for f in fills] == ["sell"]


def test_both_sides_can_fill_in_one_interval() -> None:
    """The round trip the strategy exists to earn."""
    fills = _engine().fills(_pair(), TradeRange(low=0.40, high=0.60, volume=10.0))

    assert {f.side for f in fills} == {"buy", "sell"}


def test_no_volume_means_no_fill_however_wide_the_range() -> None:
    assert _engine("optimistic").fills(_pair(), TradeRange(0.0, 1.0, 0.0)) == []


def test_a_withdrawn_side_cannot_fill() -> None:
    fills = _engine().fills(_pair(bid=None), TradeRange(0.0, 1.0, 100.0))

    assert [f.side for f in fills] == ["sell"]


def test_a_maker_fills_at_its_own_price_not_the_markets() -> None:
    """The point of being a maker: the print was far through the quote,
    and the fill is still at the quote."""
    fills = _engine().fills(_pair(), TradeRange(low=0.10, high=0.10, volume=10.0))

    assert fills[0].price == pytest.approx(0.45)


# -- P&L --------------------------------------------------------------


def test_a_buy_profits_when_the_mark_rises_and_loses_when_it_falls() -> None:
    fill = _engine().fills(_pair(), TradeRange(0.40, 0.40, 10.0))[0]

    assert mark_to_market(fill, 0.50) > 0
    assert mark_to_market(fill, 0.40) < 0


def test_a_sell_profits_when_the_mark_falls() -> None:
    fill = _engine().fills(_pair(), TradeRange(0.60, 0.60, 10.0))[0]

    assert mark_to_market(fill, 0.50) > 0
    assert mark_to_market(fill, 0.60) < 0


def test_the_fee_comes_from_the_model_and_is_charged() -> None:
    """Kalshi's maker rate is the economic case for this whole strategy,
    so it is sourced from the fee model rather than assumed to be zero."""
    fill = _engine().fills(_pair(), TradeRange(0.40, 0.40, 10.0))[0]
    schedule = _schedule()

    assert fill.fee == KalshiFeeModel().fee(0.45, 10.0, "maker", schedule)
    # Marking flat must return exactly minus the fee, never a profit.
    assert mark_to_market(fill, 0.45) == pytest.approx(-fill.fee)


def test_an_out_of_range_mark_is_rejected() -> None:
    fill = _engine().fills(_pair(), TradeRange(0.40, 0.40, 10.0))[0]

    with pytest.raises(ValueError, match="probability"):
        mark_to_market(fill, 1.5)


# -- input validation -------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [{"low": 0.6, "high": 0.4, "volume": 1.0},
     {"low": -0.1, "high": 0.4, "volume": 1.0},
     {"low": 0.1, "high": 1.4, "volume": 1.0},
     {"low": 0.1, "high": 0.4, "volume": -1.0}],
)
def test_an_impossible_trade_range_is_rejected(kwargs) -> None:
    with pytest.raises(ValueError):
        TradeRange(**kwargs)
