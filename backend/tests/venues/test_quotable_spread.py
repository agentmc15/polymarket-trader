"""`quotable_spread` — the listing-payload spread check `collect_books`
selects on (mm-proveout T7, PLAN.md D1).

MEASURED ON LIVE DATA: ranking `DataCollector.collect_books`' candidates
by volume alone (the pre-T7 behaviour) selected 2 Kalshi and 0 Polymarket
markets, out of the top 50 by volume, with spread `>= 0.10` -- at the
time, the exact floor `MarketMaker`'s calibrated `min_spread` refused to
quote inside of. That default has since moved to 0.25 (the 1-minute
Kalshi holdout, `app/strategies/market_making.py`);
`settings.book_collection_min_spread` deliberately stays at 0.10 as a
superset. Volume alone finds the TIGHTEST books; this function is the piece that
lets selection filter on quotability instead, read from the LISTING
payload (`app.services.data_collector.DataCollector.collect_books`
already fetches this via `get_market` for every candidate) so a reject
never costs an extra `get_book` call.

Field names are the venue's own, same rule `test_volume_ranking.py`
pins for `venue_volume`: Kalshi's `/events` listing sends
`yes_bid_dollars`/`yes_ask_dollars` as dollar-STRINGS (e.g. `"0.40"`);
Polymarket's Gamma listing sends `bestBid`/`bestAsk`.
"""
from datetime import UTC, datetime

import pytest

from app.venues.types import FeeSchedule, VenueMarket, quotable_spread


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
    """`yes_bid_dollars`/`yes_ask_dollars`, as dollar-STRINGS -- what
    `/events` actually sends."""
    market = _market(
        "kalshi", {"yes_bid_dollars": "0.40", "yes_ask_dollars": "0.55"}
    )
    assert quotable_spread(market) == pytest.approx(0.15)


def test_polymarkets_own_field_names_are_read() -> None:
    """`bestBid`/`bestAsk` -- what Gamma's listing actually sends."""
    market = _market("polymarket", {"bestBid": 0.30, "bestAsk": 0.45})
    assert quotable_spread(market) == pytest.approx(0.15)


@pytest.mark.parametrize(
    "raw",
    [
        {"yes_bid_dollars": "0.40"},  # ask missing
        {"yes_ask_dollars": "0.55"},  # bid missing
        {},  # neither present
    ],
)
def test_a_one_sided_kalshi_listing_is_unquotable(raw: dict) -> None:
    assert quotable_spread(_market("kalshi", raw)) is None


@pytest.mark.parametrize(
    "raw",
    [
        {"bestBid": 0.30},  # ask missing
        {"bestAsk": 0.45},  # bid missing
        {},  # neither present
    ],
)
def test_a_one_sided_polymarket_listing_is_unquotable(raw: dict) -> None:
    assert quotable_spread(_market("polymarket", raw)) is None


def test_a_crossed_kalshi_book_is_unquotable() -> None:
    """`bid >= ask` — a crossed/locked book is not one this policy could
    quote against either."""
    market = _market(
        "kalshi", {"yes_bid_dollars": "0.60", "yes_ask_dollars": "0.55"}
    )
    assert quotable_spread(market) is None


def test_a_locked_polymarket_book_is_unquotable() -> None:
    """`bid == ask` — the strict inequality `bid < ask` excludes it too."""
    market = _market("polymarket", {"bestBid": 0.50, "bestAsk": 0.50})
    assert quotable_spread(market) is None


@pytest.mark.parametrize(
    "raw",
    [
        {"bestBid": 0.0, "bestAsk": 0.10},  # bid not > 0
        {"bestBid": 0.90, "bestAsk": 1.0},  # ask not < 1
        {"bestBid": -0.05, "bestAsk": 0.10},  # negative bid
    ],
)
def test_a_pair_outside_the_open_probability_range_is_unquotable(raw: dict) -> None:
    assert quotable_spread(_market("polymarket", raw)) is None


@pytest.mark.parametrize("value", ["", "abc", None, [], {}])
def test_a_non_numeric_kalshi_bid_is_unquotable(value) -> None:
    market = _market(
        "kalshi", {"yes_bid_dollars": value, "yes_ask_dollars": "0.55"}
    )
    assert quotable_spread(market) is None


@pytest.mark.parametrize("value", ["", "abc", None, [], {}])
def test_a_non_numeric_polymarket_ask_is_unquotable(value) -> None:
    market = _market("polymarket", {"bestBid": 0.30, "bestAsk": value})
    assert quotable_spread(market) is None


def test_a_boolean_value_is_not_treated_as_numeric() -> None:
    """`isinstance(True, int)` is `True` in Python -- `bool(True) -> 1.0`
    would otherwise sneak a boolean through `float()` as a real price."""
    market = _market("kalshi", {"yes_bid_dollars": True, "yes_ask_dollars": "0.55"})
    assert quotable_spread(market) is None


def test_a_venue_with_no_entry_in_the_field_map_is_unquotable() -> None:
    """Defensive: `VenueId` is exhaustive today, but a caller constructing
    a `VenueMarket` for an unrecognized venue string must not raise here."""
    market = _market("not-a-real-venue", {"bestBid": 0.30, "bestAsk": 0.45})
    assert quotable_spread(market) is None


@pytest.mark.parametrize(
    "raw",
    [
        {"bestBid": float("nan"), "bestAsk": 0.55},
        {"bestBid": 0.30, "bestAsk": float("nan")},
        {"bestBid": float("-inf"), "bestAsk": 0.55},
        {"bestBid": 0.30, "bestAsk": float("inf")},
    ],
)
def test_nan_and_infinite_values_are_unquotable(raw: dict) -> None:
    """`json.loads` accepts the literal tokens `NaN`/`Infinity`/
    `-Infinity` by default (same concern `_check_price`'s own docstring
    raises elsewhere in this module), so a malformed listing payload can
    carry them with no adversary involved. `float()` parses these
    successfully -- they are not caught by the `TypeError`/`ValueError`
    branch -- so the brief's `0 < bid < ask < 1` range check is the only
    thing standing between a NaN/Infinity bid or ask and a spread that
    looks real."""
    assert quotable_spread(_market("polymarket", raw)) is None


def test_the_function_does_not_apply_any_spread_floor_itself() -> None:
    """The brief's own contract for this function is purely structural --
    "missing, non-numeric, or the pair is not `0 < bid < ask < 1`" -- and
    says nothing about `book_collection_min_spread`. A 0.02-wide market is
    a real, valid quote (just one the POLICY refuses), so this must
    return the actual spread, not `None`; thresholding against
    `settings.book_collection_min_spread`/`book_collection_min_volume` is
    `select_quotable_markets`'s job (`app.services.data_collector`), not
    this function's -- conflating the two here would make it impossible
    to log a "two-sided but below floor" market as distinct from an
    actually one-sided/crossed one."""
    market = _market("polymarket", {"bestBid": 0.49, "bestAsk": 0.51})
    assert quotable_spread(market) == pytest.approx(0.02)
