"""The volume used for ranking must exist on the payload being ranked.

MEASURED ON LIVE DATA: `_volume()` returned **0.0 for all 96,478 open
Kalshi markets**. It read `raw["volume"]`, and Kalshi's event payloads
carry `volume_fp` and `volume_24h_fp` — no bare `volume` key at all.
Polymarket was fine (97.7% non-zero) because its Gamma payload does carry
`volume`.

WHY THAT IS WORSE THAN IT SOUNDS. This is the sort key for
`scan_top_n`, the "top N markets by volume" every scan is restricted to,
and for `collect_books`' identical ranking. With every Kalshi market
scoring 0.0 the sort is a tie across the entire venue, so "the 200
highest-volume Kalshi markets" was really 200 arbitrary ones in whatever
order the listing arrived. Live, the top 5 it chose had volumes of 296,
565, 1,010 and 1,510 while a market with 118,515 sat at the same rank.

The listing is now ~96,000 markets after the event-pagination fix, so
an arbitrary 200 of them is very nearly a random sample — the selection
step that is supposed to concentrate the scan on tradeable markets was
instead diluting it.

The fixture that hid this is named in the old docstring: it claimed
"both real venues' raw payloads carry a `volume` key
(tests/fixtures/kalshi/markets.json)". That fixture does. The live
`/events` payload the adapter actually reads does not — the same
fixture-versus-reality gap that hid `tick_size` vs `minimum_tick_size`.
"""
from datetime import UTC, datetime

import pytest

from app.venues.types import FeeSchedule, VenueMarket, venue_volume


def _market(venue: str, raw: dict) -> VenueMarket:
    return VenueMarket(
        venue=venue,  # type: ignore[arg-type]
        market_id="M1",
        event_id=None,
        question="Will it?",
        outcomes=("YES", "NO"),
        outcome_ids={"YES": "y", "NO": "n"},
        rules_text="",
        resolution_source=None,
        close_time=datetime(2027, 1, 1, tzinfo=UTC),
        expected_settle_time=None,
        status="open",
        result=None,
        tick_size=0.01,
        min_size=1.0,
        fee=FeeSchedule(taker_rate=0.07, maker_rate=0.0, source="settings"),
        raw=raw,
    )


def test_kalshis_own_field_names_are_read() -> None:
    """`volume_fp`/`volume_24h_fp` — what `/events` actually sends."""
    assert venue_volume(_market("kalshi", {"volume_fp": "118515.21"})) == pytest.approx(
        118515.21
    )


def test_the_24_hour_field_wins_because_that_is_what_the_ranking_means() -> None:
    """Both scanner and collector document this as a 24h-volume proxy, so
    a lifetime total must not outrank a recent one."""
    market = _market("kalshi", {"volume_fp": "999999", "volume_24h_fp": "50"})

    assert venue_volume(market) == pytest.approx(50.0)


def test_polymarkets_field_names_are_read() -> None:
    assert venue_volume(_market("polymarket", {"volume24hr": 1234.5})) == pytest.approx(
        1234.5
    )


def test_polymarket_falls_back_to_lifetime_volume() -> None:
    """316 of ~500 sampled live markets carry `volume24hr`; the rest carry
    only `volume`/`volumeNum`, and ranking those at 0.0 would drop them."""
    assert venue_volume(_market("polymarket", {"volume": "777"})) == pytest.approx(777.0)
    assert venue_volume(
        _market("polymarket", {"volumeNum": 888.0})
    ) == pytest.approx(888.0)


def test_a_market_with_no_volume_field_ranks_zero_not_an_error() -> None:
    """Absent volume is a ranking of last, never a crash mid-scan."""
    assert venue_volume(_market("kalshi", {})) == 0.0


@pytest.mark.parametrize("value", ["", "abc", None, [], {}])
def test_an_unparseable_volume_ranks_zero(value) -> None:
    assert venue_volume(_market("kalshi", {"volume_24h_fp": value})) == 0.0


def test_a_negative_volume_is_clamped_rather_than_sorting_last() -> None:
    """No venue should send one; if one does, it must not outrank a real
    market by sorting below zero."""
    assert venue_volume(_market("kalshi", {"volume_fp": "-5"})) == 0.0


def test_the_scanner_and_collector_share_this_one_implementation() -> None:
    """They were two copies documented as mirroring each other, and they
    drifted from reality together. One function now, imported by both."""
    from app.services.data_collector import _book_collection_volume
    from app.services.scanner import _volume

    market = _market("kalshi", {"volume_24h_fp": "42"})

    assert _volume(market) == pytest.approx(42.0)
    assert _book_collection_volume(market) == pytest.approx(42.0)
