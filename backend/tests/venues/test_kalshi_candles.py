"""Tests for `app.venues.kalshi.candles` (mm-proveout T1).

Derived from TASKS.md T1's acceptance lines, not from the implementation:

  1. Parse of a full candle (`yes_bid`/`yes_ask`/`price` all present).
  2. Parse of a ZERO-VOLUME candle: the venue omits the whole `price`
     object when `volume_fp` is `"0.00"` — that must read as
     `px_* is None`, `volume == 0.0`, never as a parse error.
  3. `fetch_candles` sorts its result by `end_ts`, regardless of the
     order the venue returned them in.
  4. `candle_at_or_before` returns the LAST candle with
     `end_ts <= cutoff_ts`, and `None` when every candle ends AFTER the
     cutoff — the no-look-ahead property T2's replay depends on.
  5. `series_for` both branches: derived from `event_ticker`, and the
     `market_id` fallback when `event_ticker` is absent.
  6. An interval outside `{1, 60, 1440}` raises `ValueError`.

The block below "ADVERSARIAL ADDITIONS" was authored by the kit's
test-author role, from the brief and PLAN.md's risk section alone, before
this file's other tests were read: unsorted input to
`candle_at_or_before` (the brief says nothing about `fetch_candles` being
the only sort point, and PLAN.md's tripwire list explicitly worries about
look-ahead), partial/degenerate payloads (one documented field absent
without the whole object being absent), a mixed good/bad batch (does one
malformed candle abort the whole fetch or get silently dropped?), and
more values outside `{1, 60, 1440}` for the "before any network call"
guarantee.

All network access here is `httpx.MockTransport` (GUARDRAILS.md §1.4: no
network to `kalshi.com`/`kalshi.co`, ever, from a test).
"""
import dataclasses
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.config import Settings
from app.venues.base import VenuePayloadError
from app.venues.kalshi.adapter import KalshiAdapter
from app.venues.kalshi.candles import (
    CHUNK_SIZE_PERIODS,
    Candle,
    candle_at_or_before,
    fetch_candles,
    series_for,
)
from app.venues.types import FeeSchedule, VenueMarket

WINDOW_START = datetime(2026, 1, 1, tzinfo=UTC)
WINDOW_END = datetime(2026, 1, 2, tzinfo=UTC)


def _market(market_id: str = "KXFED-26MAR-A", raw: dict | None = None) -> VenueMarket:
    """A minimal `VenueMarket`, matching `tests/venues/test_volume_ranking.py`."""
    return VenueMarket(
        venue="kalshi",
        market_id=market_id,
        event_id=None,
        question="Will it?",
        outcomes=("YES", "NO"),
        outcome_ids={"YES": market_id, "NO": market_id},
        rules_text="",
        resolution_source=None,
        close_time=datetime(2027, 1, 1, tzinfo=UTC),
        expected_settle_time=None,
        status="resolved",
        result="yes",
        tick_size=0.01,
        min_size=1.0,
        fee=FeeSchedule(taker_rate=0.07, maker_rate=0.0, source="settings"),
        raw=raw if raw is not None else {},
    )


def _transport(candlesticks: list[dict]) -> httpx.MockTransport:
    """Serve `candlesticks` at the candlestick path; fail on anything else."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(
            "/series/KXFED/markets/KXFED-26MAR-A/candlesticks"
        ), request.url.path
        assert request.url.params.get("period_interval") == "60"
        return httpx.Response(200, json={"candlesticks": candlesticks})

    return httpx.MockTransport(handler)


def _adapter(candlesticks: list[dict]) -> KalshiAdapter:
    return KalshiAdapter(transport=_transport(candlesticks), settings_obj=Settings())


def _transport_raw_body(body: dict) -> httpx.MockTransport:
    """Serve an arbitrary raw JSON `body` at the candlestick path.

    Unlike `_transport`, this does not wrap its argument under
    `"candlesticks"` -- for tests where the whole envelope shape (not
    just the list contents) is what is malformed, e.g.
    `{"candlesticks": "corrupt"}` or `{"candlesticks": True}`.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(
            "/series/KXFED/markets/KXFED-26MAR-A/candlesticks"
        ), request.url.path
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handler)


def _adapter_raw_body(body: dict) -> KalshiAdapter:
    return KalshiAdapter(transport=_transport_raw_body(body), settings_obj=Settings())


FULL_CANDLE = {
    "end_period_ts": 1_700_003_600,
    "yes_bid": {"close_dollars": "0.42"},
    "yes_ask": {"close_dollars": "0.45"},
    "price": {
        "low_dollars": "0.40",
        "high_dollars": "0.46",
        "close_dollars": "0.44",
    },
    "volume_fp": "120.00",
    "open_interest_fp": "500.00",
}

ZERO_VOLUME_CANDLE = {
    "end_period_ts": 1_700_000_000,
    "yes_bid": {"close_dollars": "0.42"},
    "yes_ask": {"close_dollars": "0.45"},
    "volume_fp": "0.00",
    "open_interest_fp": "500.00",
    # No "price" key at all -- the venue omits it entirely.
}

# ---------------------------------------------------------------------------
# ADVERSARIAL ADDITIONS -- degenerate-payload fixtures (TASKS.md T1 brief:
# a zero-volume candle is not an error; these check the module does not
# over-generalize that into swallowing genuinely partial/malformed data).
# ---------------------------------------------------------------------------

#: `yes_ask` is a documented field but this candle omits the whole key,
#: not just a sub-field of it -- distinct from the zero-volume case,
#: which omits `price` because the venue has nothing to report, not
#: because a field went missing by accident.
YES_ASK_KEY_ABSENT_CANDLE = {
    "end_period_ts": 1_700_007_200,
    "yes_bid": {"close_dollars": "0.40"},
    # "yes_ask" entirely absent (not merely close_dollars missing from it).
    "price": {"low_dollars": "0.39", "high_dollars": "0.41", "close_dollars": "0.40"},
    "volume_fp": "5.00",
    "open_interest_fp": "10.00",
}

#: `price` is PRESENT (this candle traded) but is itself missing one
#: documented sub-field.
PRICE_MISSING_LOW_CANDLE = {
    "end_period_ts": 1_700_010_800,
    "yes_bid": {"close_dollars": "0.40"},
    "yes_ask": {"close_dollars": "0.45"},
    "price": {"high_dollars": "0.44", "close_dollars": "0.42"},  # no low_dollars
    "volume_fp": "3.00",
    "open_interest_fp": "8.00",
}

#: `volume_fp` as a bare JSON number, not the fixed-point string the
#: venue documents. The `Candle.volume` field carries a plain magnitude
#: (no cents/dollars ambiguity the way `*_dollars` fields have), so a
#: typed module should still parse this rather than choke on the type.
VOLUME_AS_NUMBER_CANDLE = {
    "end_period_ts": 1_700_014_400,
    "yes_bid": {"close_dollars": "0.40"},
    "yes_ask": {"close_dollars": "0.45"},
    "price": {"low_dollars": "0.39", "high_dollars": "0.41", "close_dollars": "0.40"},
    "volume_fp": 12.5,
    "open_interest_fp": "20.00",
}


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_full_candle_parses_every_field() -> None:
    adapter = _adapter([FULL_CANDLE])

    candles = await fetch_candles(
        adapter,
        series="KXFED",
        ticker="KXFED-26MAR-A",
        start=WINDOW_START,
        end=WINDOW_END,
        interval_minutes=60,
    )

    assert candles == [
        Candle(
            end_ts=1_700_003_600,
            bid_close=0.42,
            ask_close=0.45,
            px_low=0.40,
            px_high=0.46,
            px_close=0.44,
            volume=120.00,
            open_interest=500.00,
        )
    ]


@pytest.mark.asyncio
async def test_a_zero_volume_candle_has_no_price_and_zero_volume() -> None:
    """`price.*` is ABSENT on a zero-volume candle -- not an error, not a
    fabricated `0.0` (TASKS.md T1 brief)."""
    adapter = _adapter([ZERO_VOLUME_CANDLE])

    candles = await fetch_candles(
        adapter,
        series="KXFED",
        ticker="KXFED-26MAR-A",
        start=WINDOW_START,
        end=WINDOW_END,
        interval_minutes=60,
    )

    assert len(candles) == 1
    candle = candles[0]
    assert candle.px_low is None
    assert candle.px_high is None
    assert candle.px_close is None
    assert candle.volume == 0.0
    # The two-sided quote is still there -- only the trade OHLC vanished.
    assert candle.bid_close == 0.42
    assert candle.ask_close == 0.45


@pytest.mark.asyncio
async def test_missing_end_period_ts_raises_venue_payload_error() -> None:
    bad = {k: v for k, v in FULL_CANDLE.items() if k != "end_period_ts"}
    adapter = _adapter([bad])

    with pytest.raises(VenuePayloadError):
        await fetch_candles(
            adapter,
            series="KXFED",
            ticker="KXFED-26MAR-A",
            start=WINDOW_START,
            end=WINDOW_END,
            interval_minutes=60,
        )


# ---------------------------------------------------------------------------
# ADVERSARIAL ADDITIONS -- partial/degenerate payloads.
#
# The brief only says "price.* is ABSENT when volume_fp is 0.00 -- that is
# a zero-volume candle, not an error." It does NOT say every other kind of
# missing field is equally benign. These pin the distinction: a documented
# key absent for a real, venue-stated reason parses cleanly to `None` on
# just that field; nothing here should be over-generalized into treating
# a genuinely malformed candle -- one whose required timestamp is simply
# gone -- as another harmless omission.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_yes_ask_key_entirely_absent_parses_ask_close_as_none() -> None:
    """`yes_bid` present, `yes_ask` missing outright -- not a KeyError,
    not a dropped candle, only `ask_close is None`."""
    adapter = _adapter([YES_ASK_KEY_ABSENT_CANDLE])

    candles = await fetch_candles(
        adapter,
        series="KXFED",
        ticker="KXFED-26MAR-A",
        start=WINDOW_START,
        end=WINDOW_END,
        interval_minutes=60,
    )

    assert len(candles) == 1
    candle = candles[0]
    assert candle.bid_close == 0.40
    assert candle.ask_close is None
    # The candle DID trade -- price OHLC is present and must not be
    # dropped just because a sibling field (yes_ask) was missing.
    assert candle.px_low == 0.39
    assert candle.px_high == 0.41
    assert candle.px_close == 0.40
    assert candle.volume == 5.0


@pytest.mark.asyncio
async def test_price_present_but_missing_low_dollars_only_nulls_that_field() -> None:
    """A traded candle (`price` present, nonzero volume) missing just
    `low_dollars` must not be treated as the zero-volume case (which
    would null EVERY `px_*` field) nor raise -- only `px_low` is `None`."""
    adapter = _adapter([PRICE_MISSING_LOW_CANDLE])

    candles = await fetch_candles(
        adapter,
        series="KXFED",
        ticker="KXFED-26MAR-A",
        start=WINDOW_START,
        end=WINDOW_END,
        interval_minutes=60,
    )

    assert len(candles) == 1
    candle = candles[0]
    assert candle.px_low is None
    assert candle.px_high == 0.44
    assert candle.px_close == 0.42
    assert candle.bid_close == 0.40
    assert candle.ask_close == 0.45
    assert candle.volume == 3.0


@pytest.mark.asyncio
async def test_volume_fp_as_a_bare_number_still_parses() -> None:
    """`volume_fp` sent as a JSON number (`12.5`) rather than the venue's
    documented fixed-point string (`"12.50"`) is still a magnitude with
    one unambiguous reading -- unlike the cents/dollars fields, there is
    no unit to guess wrong, so this should parse rather than raise."""
    adapter = _adapter([VOLUME_AS_NUMBER_CANDLE])

    candles = await fetch_candles(
        adapter,
        series="KXFED",
        ticker="KXFED-26MAR-A",
        start=WINDOW_START,
        end=WINDOW_END,
        interval_minutes=60,
    )

    assert len(candles) == 1
    assert candles[0].volume == 12.5


@pytest.mark.asyncio
async def test_one_malformed_candle_among_good_ones_does_not_silently_vanish() -> None:
    """A batch of one good candle plus one missing `end_period_ts`.

    `test_missing_end_period_ts_raises_venue_payload_error` above already
    pins that a batch consisting ONLY of a bad candle raises. This checks
    the module does not quietly change its mind when a good candle is
    also present -- the concern being that a per-entry "skip what you
    can't parse" habit (used elsewhere in this venue's adapter for
    positions/fills) could turn a genuinely corrupt candle into a silent
    gap in the time series, which is a worse failure mode for a
    backtest than refusing outright: the caller would never know a
    candle in the middle of its history was dropped, so a raise is the
    expected contract here, matching the single-candle case. If the
    implementation instead drops the bad entry and returns only the good
    one, that is a real behavioural inconsistency with the single-bad-
    candle test above, worth reporting rather than silently accepting
    either way.
    """
    good = {**FULL_CANDLE, "end_period_ts": 1_700_020_000}
    bad = {k: v for k, v in FULL_CANDLE.items() if k != "end_period_ts"}
    adapter = _adapter([good, bad])

    with pytest.raises(VenuePayloadError):
        await fetch_candles(
            adapter,
            series="KXFED",
            ticker="KXFED-26MAR-A",
            start=WINDOW_START,
            end=WINDOW_END,
            interval_minutes=60,
        )


# ---------------------------------------------------------------------------
# Sort order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_candles_sorts_by_end_ts_even_when_the_venue_does_not() -> None:
    later = {**FULL_CANDLE, "end_period_ts": 2_000_000_000}
    earlier = {**FULL_CANDLE, "end_period_ts": 1_000_000_000}
    middle = {**FULL_CANDLE, "end_period_ts": 1_500_000_000}
    # Deliberately out of order on the wire.
    adapter = _adapter([later, earlier, middle])

    candles = await fetch_candles(
        adapter,
        series="KXFED",
        ticker="KXFED-26MAR-A",
        start=WINDOW_START,
        end=WINDOW_END,
        interval_minutes=60,
    )

    assert [c.end_ts for c in candles] == [1_000_000_000, 1_500_000_000, 2_000_000_000]


# ---------------------------------------------------------------------------
# candle_at_or_before -- the no-look-ahead property
# ---------------------------------------------------------------------------


def _candle(end_ts: int) -> Candle:
    return Candle(
        end_ts=end_ts,
        bid_close=0.4,
        ask_close=0.42,
        px_low=0.4,
        px_high=0.42,
        px_close=0.41,
        volume=10.0,
        open_interest=None,
    )


def test_candle_at_or_before_returns_the_last_candle_at_or_before_cutoff() -> None:
    candles = [_candle(100), _candle(200), _candle(300)]

    # Exactly on a boundary: the candle ENDING at the cutoff had already
    # closed at that instant, so it is included, not excluded.
    assert candle_at_or_before(candles, 200) is candles[1]
    # Between two candles: the LAST one that had already closed.
    assert candle_at_or_before(candles, 250) is candles[1]
    # At/after the last candle: that candle.
    assert candle_at_or_before(candles, 999) is candles[2]


def test_candle_at_or_before_returns_none_when_every_candle_ends_after_cutoff() -> None:
    """No-look-ahead in its purest form: nothing had closed yet, so there
    is nothing to return -- never a candle whose period had not finished."""
    candles = [_candle(100), _candle(200)]

    assert candle_at_or_before(candles, 50) is None
    assert candle_at_or_before([], 50) is None


def test_candle_at_or_before_is_correct_on_unsorted_input() -> None:
    """The brief documents `fetch_candles` as the thing that sorts by
    `end_ts`; it says nothing about `candle_at_or_before` itself
    requiring sorted input, and PLAN.md's tripwire list treats
    no-look-ahead as the property this whole module exists to protect.
    A caller (a test, a future script, T2's replay before it composes
    with `fetch_candles`) that hands this function an unsorted list must
    still get "the candle with the largest `end_ts <= cutoff`", not
    whatever a sorted-ascending-and-break-early scan would stop on.

    Deliberately ordered [100, 300, 200] with cutoff=250: the correct
    answer is 200 (the largest end_ts <= 250). A scan that assumes
    ascending order and stops at the first end_ts > cutoff would see 300
    at index 1, conclude the scan is done, and wrongly return 100.
    """
    hundred, three_hundred, two_hundred = _candle(100), _candle(300), _candle(200)
    candles = [hundred, three_hundred, two_hundred]

    assert candle_at_or_before(candles, 250) is two_hundred
    # After every candle, regardless of the order they were passed in.
    assert candle_at_or_before(candles, 999) is three_hundred
    # Before every candle, regardless of order.
    assert candle_at_or_before(candles, 50) is None


def test_candle_at_or_before_never_returns_a_candle_ending_after_cutoff() -> None:
    """Direct statement of the no-look-ahead invariant itself: whatever
    is returned, its period must have already closed at/before `cutoff`."""
    candles = [_candle(50), _candle(150), _candle(250), _candle(350)]
    for cutoff in (0, 49, 50, 51, 150, 249, 250, 251, 349, 350, 351, 10_000):
        result = candle_at_or_before(candles, cutoff)
        if result is not None:
            assert result.end_ts <= cutoff


# ---------------------------------------------------------------------------
# series_for
# ---------------------------------------------------------------------------


def test_series_for_derives_from_event_ticker() -> None:
    market = _market(market_id="KXFED-26MAR-A", raw={"event_ticker": "KXFED-26MAR"})

    assert series_for(market) == "KXFED"


def test_series_for_falls_back_to_market_id_when_event_ticker_is_absent() -> None:
    market = _market(market_id="KXFED-26MAR-A", raw={})

    assert series_for(market) == "KXFED"


# ---------------------------------------------------------------------------
# Invalid interval
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_invalid_interval_raises_value_error_before_any_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected network call: {request.url}")

    adapter = KalshiAdapter(
        transport=httpx.MockTransport(handler), settings_obj=Settings()
    )

    with pytest.raises(ValueError):
        await fetch_candles(
            adapter,
            series="KXFED",
            ticker="KXFED-26MAR-A",
            start=WINDOW_START,
            end=WINDOW_END,
            interval_minutes=5,
        )


def _trap_adapter() -> KalshiAdapter:
    """A `KalshiAdapter` that fails the test on ANY network call.

    Used for every "must validate before touching the network" case
    below -- a typo in `interval_minutes` must never burn a request.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected network call: {request.url}")

    return KalshiAdapter(transport=httpx.MockTransport(handler), settings_obj=Settings())


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_interval", [0, -60, 2, 15, 30, 120, 90, 1441, 10_080])
async def test_every_interval_outside_the_allowed_set_raises_before_any_request(
    bad_interval: int,
) -> None:
    """`interval_minutes` must be exactly one of `{1, 60, 1440}` -- the
    brief names those three and nothing else, so every neighbor of a
    valid value (2, 30, 90, 120 around 60/1440) and every non-positive
    value is equally invalid, not just one arbitrarily chosen typo."""
    with pytest.raises(ValueError):
        await fetch_candles(
            _trap_adapter(),
            series="KXFED",
            ticker="KXFED-26MAR-A",
            start=WINDOW_START,
            end=WINDOW_END,
            interval_minutes=bad_interval,
        )


# ---------------------------------------------------------------------------
# ADVERSARIAL ADDITIONS -- the dataclass contract itself.
# ---------------------------------------------------------------------------


def test_candle_is_frozen() -> None:
    """The brief specifies `@dataclass(frozen=True) Candle` explicitly --
    a value read from a venue should never be mutable in place (the same
    rule `app/venues/types.py` states for every other venue dataclass)."""
    candle = _candle(100)

    with pytest.raises(dataclasses.FrozenInstanceError):
        candle.end_ts = 999  # type: ignore[misc]


# ---------------------------------------------------------------------------
# T1 RETRY -- numeric range validation (GUARDRAILS.md §3.3: a parse that
# cannot honour the payload refuses, it never substitutes a default).
# `bid_close`/`ask_close`/`px_*` are documented as probabilities in
# [0,1]; `volume` is documented as `>= 0`. These pin the four confirmed
# breaks the red-team reproduced directly against `_parse_candle`
# (bid 5.00, ask -1.00, px 999, volume -5), reusing the exact
# `Candle.__post_init__` -> `_check_price`/`_check_size` -> re-raised
# `VenuePayloadError` path, not a weaker ad hoc check.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bid_close_above_one_raises_venue_payload_error() -> None:
    """A wire `yes_bid.close_dollars` of `"5.00"` is a "500%" price --
    it must never become `Candle.bid_close == 5.0`."""
    bad = {**FULL_CANDLE, "yes_bid": {"close_dollars": "5.00"}}
    adapter = _adapter([bad])

    with pytest.raises(VenuePayloadError):
        await fetch_candles(
            adapter,
            series="KXFED",
            ticker="KXFED-26MAR-A",
            start=WINDOW_START,
            end=WINDOW_END,
            interval_minutes=60,
        )


@pytest.mark.asyncio
async def test_ask_close_negative_raises_venue_payload_error() -> None:
    """A negative price is not a probability under any reading."""
    bad = {**FULL_CANDLE, "yes_ask": {"close_dollars": "-1.00"}}
    adapter = _adapter([bad])

    with pytest.raises(VenuePayloadError):
        await fetch_candles(
            adapter,
            series="KXFED",
            ticker="KXFED-26MAR-A",
            start=WINDOW_START,
            end=WINDOW_END,
            interval_minutes=60,
        )


@pytest.mark.asyncio
async def test_px_close_far_above_one_raises_venue_payload_error() -> None:
    bad = {
        **FULL_CANDLE,
        "price": {**FULL_CANDLE["price"], "close_dollars": "999"},
    }
    adapter = _adapter([bad])

    with pytest.raises(VenuePayloadError):
        await fetch_candles(
            adapter,
            series="KXFED",
            ticker="KXFED-26MAR-A",
            start=WINDOW_START,
            end=WINDOW_END,
            interval_minutes=60,
        )


@pytest.mark.asyncio
async def test_negative_volume_raises_venue_payload_error() -> None:
    """A `Candle` carrying `volume=-5.0` corrupts every downstream
    computation that reads volume as a nonnegative magnitude."""
    bad = {**FULL_CANDLE, "volume_fp": "-5.00"}
    adapter = _adapter([bad])

    with pytest.raises(VenuePayloadError):
        await fetch_candles(
            adapter,
            series="KXFED",
            ticker="KXFED-26MAR-A",
            start=WINDOW_START,
            end=WINDOW_END,
            interval_minutes=60,
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("bid_close", 5.0),
        ("ask_close", -1.0),
        ("px_low", 999.0),
        ("px_high", 999.0),
        ("px_close", 999.0),
    ],
)
def test_candle_rejects_a_price_field_outside_zero_one(field: str, value: float) -> None:
    """`Candle.__post_init__` enforces the probability range directly on
    construction (`app.venues.types._check_price`), independent of the
    `fetch_candles` parse path exercised above."""
    with pytest.raises(ValueError):
        dataclasses.replace(_candle(100), **{field: value})


def test_candle_rejects_negative_volume_directly() -> None:
    with pytest.raises(ValueError):
        dataclasses.replace(_candle(100), volume=-5.0)


def test_candle_rejects_negative_open_interest_directly() -> None:
    with pytest.raises(ValueError):
        dataclasses.replace(_candle(100), open_interest=-1.0)


# ---------------------------------------------------------------------------
# T1 RETRY -- envelope hostility (GUARDRAILS.md §3.3): a corrupt
# `"candlesticks"` value must never be indistinguishable from an empty
# window. Before the fix, `body.get("candlesticks") or []` only guarded
# falsy values and the `isinstance(item, dict)` filter silently
# stripped everything else.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_candlesticks_as_a_string_raises_venue_payload_error() -> None:
    """A string is iterable character-by-character, so the old
    `isinstance(item, dict)` filter silently produced 0 candles here --
    corrupt data read as an empty window instead of an error."""
    adapter = _adapter_raw_body({"candlesticks": "corrupt"})

    with pytest.raises(VenuePayloadError):
        await fetch_candles(
            adapter,
            series="KXFED",
            ticker="KXFED-26MAR-A",
            start=WINDOW_START,
            end=WINDOW_END,
            interval_minutes=60,
        )


@pytest.mark.asyncio
async def test_candlesticks_as_a_dict_raises_venue_payload_error() -> None:
    """A dict is iterable over its keys, so the old filter also silently
    produced 0 candles here."""
    adapter = _adapter_raw_body({"candlesticks": {"end_period_ts": 1}})

    with pytest.raises(VenuePayloadError):
        await fetch_candles(
            adapter,
            series="KXFED",
            ticker="KXFED-26MAR-A",
            start=WINDOW_START,
            end=WINDOW_END,
            interval_minutes=60,
        )


@pytest.mark.asyncio
async def test_candlesticks_as_true_raises_venue_payload_error_not_type_error() -> None:
    """`True` is truthy (so the old `or []` never substituted an empty
    list) and not iterable -- the old code raised a bare `TypeError`,
    not the `VenuePayloadError` every other refusal in this module
    raises."""
    adapter = _adapter_raw_body({"candlesticks": True})

    with pytest.raises(VenuePayloadError):
        await fetch_candles(
            adapter,
            series="KXFED",
            ticker="KXFED-26MAR-A",
            start=WINDOW_START,
            end=WINDOW_END,
            interval_minutes=60,
        )


@pytest.mark.asyncio
async def test_a_batch_of_only_garbage_entries_raises_not_an_empty_window() -> None:
    """`[None, "garbage", 42]` has no usable candle in it at all -- the
    old per-entry `isinstance(item, dict)` filter dropped every entry
    and returned `[]`, indistinguishable from a market that genuinely
    had no candles in the requested window."""
    adapter = _adapter([None, "garbage", 42])  # type: ignore[list-item]

    with pytest.raises(VenuePayloadError):
        await fetch_candles(
            adapter,
            series="KXFED",
            ticker="KXFED-26MAR-A",
            start=WINDOW_START,
            end=WINDOW_END,
            interval_minutes=60,
        )


@pytest.mark.parametrize(
    "corrupt_entry",
    [
        pytest.param(
            {k: v for k, v in FULL_CANDLE.items() if k != "end_period_ts"},
            id="dict_missing_end_period_ts",
        ),
        pytest.param(None, id="none_entry"),
        pytest.param("garbage", id="string_entry"),
        pytest.param(42, id="int_entry"),
    ],
)
@pytest.mark.asyncio
async def test_every_degree_of_per_entry_corruption_raises_consistently(
    corrupt_entry: object,
) -> None:
    """A dict-shaped entry missing a required field, and a non-dict
    entry entirely, must be refused with the SAME severity when they
    turn up amid a good candle. Before the T1 retry, only the dict case
    (`test_one_malformed_candle_among_good_ones_does_not_silently_
    vanish` above) raised -- a `None`/string/int entry was silently
    dropped instead. One degree of corruption being fatal while a worse
    one is silent is exactly the inconsistency the kit's red-team
    flagged; this parametrization pins that both are now equally fatal."""
    adapter = _adapter([FULL_CANDLE, corrupt_entry])  # type: ignore[list-item]

    with pytest.raises(VenuePayloadError):
        await fetch_candles(
            adapter,
            series="KXFED",
            ticker="KXFED-26MAR-A",
            start=WINDOW_START,
            end=WINDOW_END,
            interval_minutes=60,
        )


# ---------------------------------------------------------------------------
# T1 RETRY -- `yes_bid`/`yes_ask`/`price` as a wrong-but-truthy type.
# `raw.get(key) or {}` guarded `None` but not a truthy non-dict, so
# `"bad".get("close_dollars")` raised a bare `AttributeError` instead of
# the `VenuePayloadError` every other refusal in this module raises.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_yes_bid_as_a_truthy_string_raises_venue_payload_error() -> None:
    bad = {**FULL_CANDLE, "yes_bid": "bad"}
    adapter = _adapter([bad])

    with pytest.raises(VenuePayloadError):
        await fetch_candles(
            adapter,
            series="KXFED",
            ticker="KXFED-26MAR-A",
            start=WINDOW_START,
            end=WINDOW_END,
            interval_minutes=60,
        )


@pytest.mark.asyncio
async def test_yes_ask_as_a_truthy_non_dict_raises_venue_payload_error() -> None:
    bad = {**FULL_CANDLE, "yes_ask": 1}
    adapter = _adapter([bad])

    with pytest.raises(VenuePayloadError):
        await fetch_candles(
            adapter,
            series="KXFED",
            ticker="KXFED-26MAR-A",
            start=WINDOW_START,
            end=WINDOW_END,
            interval_minutes=60,
        )


@pytest.mark.asyncio
async def test_price_as_a_truthy_non_dict_raises_venue_payload_error() -> None:
    bad = {**FULL_CANDLE, "price": [1, 2, 3]}
    adapter = _adapter([bad])

    with pytest.raises(VenuePayloadError):
        await fetch_candles(
            adapter,
            series="KXFED",
            ticker="KXFED-26MAR-A",
            start=WINDOW_START,
            end=WINDOW_END,
            interval_minutes=60,
        )


# ---------------------------------------------------------------------------
# CHUNKING -- the endpoint's measured 5,000-period cap (module docstring's
# CHUNKING section; NOTES.md "T5 -- 1-MINUTE SUB-STUDY", measured live
# 2026-09-07: a window of exactly 5,000 periods succeeds, 5,040 returns
# HTTP 400). `fetch_candles` splits any window wider than
# `CHUNK_SIZE_PERIODS` (4,800) into multiple requests and concatenates
# them; these tests are derived from this defect's own acceptance list
# (four numbered items), not from the implementation.
# ---------------------------------------------------------------------------


def _scripted_adapter(
    calls: list[tuple[int, int]], responses: list[httpx.Response]
) -> KalshiAdapter:
    """Serve `responses` in order, one per request; record each request's
    `(start_ts, end_ts)` params into `calls` as they arrive.

    Fails the test loudly (`AssertionError`) if `fetch_candles` issues
    more requests than were scripted -- a chunking bug that keeps
    requesting past what a test expected must not go unnoticed.
    """
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(
            "/series/KXFED/markets/KXFED-26MAR-A/candlesticks"
        ), request.url.path
        assert request.url.params.get("period_interval") == "60"
        calls.append(
            (int(request.url.params["start_ts"]), int(request.url.params["end_ts"]))
        )
        n = state["n"]
        state["n"] += 1
        assert n < len(responses), (
            f"fetch_candles issued a {n + 1}th request, only "
            f"{len(responses)} were scripted"
        )
        return responses[n]

    return KalshiAdapter(transport=httpx.MockTransport(handler), settings_obj=Settings())


def _ok(candlesticks: list[dict]) -> httpx.Response:
    return httpx.Response(200, json={"candlesticks": candlesticks})


def _candle_at(end_ts: int) -> dict:
    """A well-formed candlestick dict differing from `FULL_CANDLE` only
    in `end_period_ts` -- for chunking tests, where WHICH candle came
    back (and in what request) matters, not its other fields."""
    return {**FULL_CANDLE, "end_period_ts": end_ts}


#: All chunking tests use `interval_minutes=60`, so one chunk spans
#: `CHUNK_SIZE_PERIODS` hours -- 4,800 * 3,600s = 200 days exactly.
_CHUNK_TEST_START = datetime(2026, 1, 1, tzinfo=UTC)
_CHUNK_SPAN_S = CHUNK_SIZE_PERIODS * 60 * 60


@pytest.mark.asyncio
async def test_a_window_needing_multiple_chunks_issues_multiple_correctly_bounded_requests() -> (
    None
):
    """A window of `CHUNK_SIZE_PERIODS * 2 + 500` hourly periods must
    split into three requests with correct, non-overlapping (touching)
    bounds, and the parsed result must be the concatenation of all
    three, sorted by `end_ts`."""
    start_ts = int(_CHUNK_TEST_START.timestamp())
    bound1 = start_ts + _CHUNK_SPAN_S
    bound2 = bound1 + _CHUNK_SPAN_S
    end_ts = bound2 + 500 * 3600
    end = _CHUNK_TEST_START + timedelta(seconds=end_ts - start_ts)

    calls: list[tuple[int, int]] = []
    adapter = _scripted_adapter(
        calls,
        [
            _ok([_candle_at(start_ts + 3600)]),
            _ok([_candle_at(bound1 + 3600)]),
            _ok([_candle_at(bound2 + 3600)]),
        ],
    )

    candles = await fetch_candles(
        adapter,
        series="KXFED",
        ticker="KXFED-26MAR-A",
        start=_CHUNK_TEST_START,
        end=end,
        interval_minutes=60,
    )

    assert calls == [(start_ts, bound1), (bound1, bound2), (bound2, end_ts)]
    assert [c.end_ts for c in candles] == [
        start_ts + 3600,
        bound1 + 3600,
        bound2 + 3600,
    ]


@pytest.mark.asyncio
async def test_chunk_boundary_has_no_duplicate_and_no_gap() -> None:
    """Two chunks, `CHUNK_SIZE_PERIODS` each. Candles adjacent to the
    boundary on both sides must all survive, in order, with nothing
    duplicated and nothing missing at the seam -- the touching-bounds
    arithmetic (`windows[i][1] == windows[i + 1][0]`) means the boundary
    timestamp itself is chunk 1's `end_ts` and chunk 2's `start_ts`,
    never sent as both or as neither."""
    start_ts = int(_CHUNK_TEST_START.timestamp())
    boundary_ts = start_ts + _CHUNK_SPAN_S
    end_ts = boundary_ts + _CHUNK_SPAN_S
    end = _CHUNK_TEST_START + timedelta(seconds=end_ts - start_ts)

    calls: list[tuple[int, int]] = []
    adapter = _scripted_adapter(
        calls,
        [
            _ok([_candle_at(boundary_ts - 3600), _candle_at(boundary_ts)]),
            _ok([_candle_at(boundary_ts + 3600), _candle_at(boundary_ts + 7200)]),
        ],
    )

    candles = await fetch_candles(
        adapter,
        series="KXFED",
        ticker="KXFED-26MAR-A",
        start=_CHUNK_TEST_START,
        end=end,
        interval_minutes=60,
    )

    assert calls == [(start_ts, boundary_ts), (boundary_ts, end_ts)]
    assert [c.end_ts for c in candles] == [
        boundary_ts - 3600,
        boundary_ts,
        boundary_ts + 3600,
        boundary_ts + 7200,
    ]


@pytest.mark.asyncio
async def test_a_window_of_exactly_chunk_size_issues_one_request() -> None:
    start_ts = int(_CHUNK_TEST_START.timestamp())
    end = _CHUNK_TEST_START + timedelta(seconds=_CHUNK_SPAN_S)

    calls: list[tuple[int, int]] = []
    adapter = _scripted_adapter(calls, [_ok([])])

    await fetch_candles(
        adapter,
        series="KXFED",
        ticker="KXFED-26MAR-A",
        start=_CHUNK_TEST_START,
        end=end,
        interval_minutes=60,
    )

    assert calls == [(start_ts, start_ts + _CHUNK_SPAN_S)]


@pytest.mark.asyncio
async def test_one_period_more_than_chunk_size_issues_two_requests() -> None:
    start_ts = int(_CHUNK_TEST_START.timestamp())
    end = _CHUNK_TEST_START + timedelta(seconds=_CHUNK_SPAN_S + 3600)

    calls: list[tuple[int, int]] = []
    adapter = _scripted_adapter(calls, [_ok([]), _ok([])])

    await fetch_candles(
        adapter,
        series="KXFED",
        ticker="KXFED-26MAR-A",
        start=_CHUNK_TEST_START,
        end=end,
        interval_minutes=60,
    )

    assert calls == [
        (start_ts, start_ts + _CHUNK_SPAN_S),
        (start_ts + _CHUNK_SPAN_S, start_ts + _CHUNK_SPAN_S + 3600),
    ]


@pytest.mark.asyncio
async def test_a_mid_sequence_chunk_transport_error_aborts_the_whole_fetch() -> None:
    """The second of three chunks answers HTTP 400. `fetch_candles` must
    raise -- carrying the SAME `httpx.HTTPStatusError` a single
    unchunked request would have raised -- never return the one good
    candle from chunk 1 as if it were the market's whole (short) history
    (module docstring's PARTIAL FAILURE section: that would be
    indistinguishable from a genuinely short-lived market), and never
    even attempt the third chunk once the second has failed."""
    start_ts = int(_CHUNK_TEST_START.timestamp())
    bound1 = start_ts + _CHUNK_SPAN_S
    bound2 = bound1 + _CHUNK_SPAN_S
    end = _CHUNK_TEST_START + timedelta(seconds=(bound2 - start_ts) + 500 * 3600)

    calls: list[tuple[int, int]] = []
    adapter = _scripted_adapter(
        calls,
        [
            _ok([_candle_at(start_ts + 3600)]),
            httpx.Response(400, json={"error": "bad request"}),
            _ok([_candle_at(bound2 + 3600)]),  # must never be reached
        ],
    )

    with pytest.raises(httpx.HTTPStatusError):
        await fetch_candles(
            adapter,
            series="KXFED",
            ticker="KXFED-26MAR-A",
            start=_CHUNK_TEST_START,
            end=end,
            interval_minutes=60,
        )

    # Fail-fast: the third (would-be-good) chunk was never requested.
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_a_mid_sequence_chunk_payload_error_aborts_the_whole_fetch() -> None:
    """Same shape as the transport-error case above, but the second
    chunk answers 200 with a corrupt candle (missing `end_period_ts`)
    rather than a transport failure -- the OTHER exception
    `fetch_candles` can raise mid-fetch must have the identical
    discard-everything-so-far behaviour, not a weaker one."""
    start_ts = int(_CHUNK_TEST_START.timestamp())
    end = _CHUNK_TEST_START + timedelta(seconds=2 * _CHUNK_SPAN_S)

    bad = {k: v for k, v in FULL_CANDLE.items() if k != "end_period_ts"}
    calls: list[tuple[int, int]] = []
    adapter = _scripted_adapter(
        calls,
        [
            _ok([_candle_at(start_ts + 3600)]),
            _ok([bad]),
        ],
    )

    with pytest.raises(VenuePayloadError):
        await fetch_candles(
            adapter,
            series="KXFED",
            ticker="KXFED-26MAR-A",
            start=_CHUNK_TEST_START,
            end=end,
            interval_minutes=60,
        )

    assert len(calls) == 2
