"""The venue fee rates, pinned once, with their provenance.

MAKERS PAY ON KALSHI. This repo carried `kalshi_maker_fee_rate = 0.0`,
and that is the costliest direction to be wrong in: a zero maker rate
makes passive quoting look free, and the entire market-making thesis in
this kit was built on exactly that premise.

Confirmed 2026-09-06 against Kalshi's help centre, which states it
plainly — "Maker fees are charged for orders placed that are not
immediately matched and are instead left as resting orders on the
orderbook" — and against the published fee schedule, which puts the
maker rate at 1.75% x p x (1-p), a quarter of the 7% taker rate.

WHY THIS FILE EXISTS RATHER THAN A COMMENT. The API publishes no fee
data at all: no fee field on any market payload (checked across 400 live
markets) and no fee endpoint. So these constants cannot be sourced at
runtime, cannot be validated against the venue by any automated check,
and can only ever go stale SILENTLY — while sitting underneath every
profitability number this repo produces. Pinning them in one place with
the date and the source is the most that can be done: when they next
change, this test is what makes someone go and look.
"""
import pytest

from app.config import Settings

#: Standard-market rates as published, 2026-09-06.
KALSHI_TAKER_RATE = 0.07
KALSHI_MAKER_RATE = 0.0175


def test_kalshi_rates_are_the_published_ones() -> None:
    settings = Settings()

    assert settings.kalshi_taker_fee_rate == pytest.approx(KALSHI_TAKER_RATE)
    assert settings.kalshi_maker_fee_rate == pytest.approx(KALSHI_MAKER_RATE)


def test_the_maker_rate_is_not_zero() -> None:
    """Named separately because zero is the specific wrong value that was
    here, and the one that silently turns a losing passive strategy into
    a winning backtest."""
    assert Settings().kalshi_maker_fee_rate > 0.0


def test_the_maker_rate_is_a_quarter_of_the_taker_rate() -> None:
    """The published relationship, pinned so a partial edit is caught."""
    settings = Settings()

    assert settings.kalshi_maker_fee_rate == pytest.approx(
        settings.kalshi_taker_fee_rate * 0.25
    )
