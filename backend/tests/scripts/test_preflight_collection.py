"""Tests for `app.scripts.preflight --check-collection` (mm-proveout T9,
PLAN.md D8).

`DataCollector.collect_books` (T7/T8) is about to run unattended, on a
60-second beat, for weeks. Every field it depends on --
`quotable_spread`, `venue_volume`, `FeeSchedule.taker_rate`, and
`get_book` returning a two-sided book -- was already exercised against a
HAND-WRITTEN fixture in `tests/venues/fixture_adapter.py`, and PLAN.md's
own tripwire is that a fixture agreeing with the code proves nothing
about whether the live venue has since renamed a field.
`check_collection_for_venue` is the live-payload guard for exactly that;
these tests drive it with an INJECTED `FixtureAdapter`
(GUARDRAILS.md §1.4: no network from a test), never live data.

GUARDRAILS.md §1.2/§1.3: `TRADING_MODE` stays `"paper"` in this test
PROCESS always; the redaction test below builds an explicit
`Settings(...)` with fake credentials and passes it straight to
`build_report`, exactly as `tests/test_preflight.py`'s own redaction
test does, never by mutating `os.environ` or the process-wide
`app.config.settings` singleton.
"""
from pathlib import Path

import pytest

from app.config import Settings
from app.execution.fences import LIVE_TRADING_CONFIRMATION_PHRASE
from app.scripts.preflight import (
    _MIN_TWO_SIDED_FRACTION,
    _MIN_VOLUME_POSITIVE_FRACTION,
    BrokerCheck,
    CollectionCheck,
    DatabaseCheck,
    _collection_group,
    build_report,
    check_collection_for_venue,
)
from app.services.data_collector import select_quotable_markets
from app.venues.types import FeeSchedule, VenueId, quotable_spread, venue_volume
from tests.helpers import make_book
from tests.venues.fixture_adapter import FixtureAdapter, make_venue_market

_OK_DB = DatabaseCheck(
    reachable=True, display_url="postgresql+asyncpg://x:***@localhost/db", current_revision="007"
)
_OK_BROKER = [
    BrokerCheck(label="CELERY_BROKER_URL", display_url="redis://localhost:6379/0", reachable=True)
]


def _quotable_market(market_id: str, venue: VenueId = "polymarket") -> object:
    """A two-sided, liquid, quotable listing -- clears the default
    `book_collection_min_spread` (0.10, spread here is 0.15) and
    `book_collection_min_volume` (100.0, volume here is 500)."""
    if venue == "polymarket":
        raw = {"bestBid": 0.40, "bestAsk": 0.55, "volume24hr": 500.0}
    else:
        raw = {"yes_bid_dollars": "0.40", "yes_ask_dollars": "0.55", "volume_24h_fp": "500.0"}
    return make_venue_market(venue=venue, market_id=market_id, raw=raw)


# ---------------------------------------------------------------------------
# `check_collection_for_venue` -- the live-payload probe itself.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_passes_when_every_sampled_market_and_its_book_check_out() -> None:
    """The ordinary, healthy path: quotable markets whose fields all
    parse, and whose book comes back two-sided."""
    adapter = (
        FixtureAdapter("polymarket")
        .add_market(_quotable_market("PM-1"))
        .add_market(_quotable_market("PM-2"))
        .set_book(
            make_book(bids=[(0.40, 10.0)], asks=[(0.55, 10.0)], market_id="PM-1", outcome="YES")
        )
        .set_book(
            make_book(bids=[(0.40, 10.0)], asks=[(0.55, 10.0)], market_id="PM-2", outcome="YES")
        )
    )

    result = await check_collection_for_venue("polymarket", adapter)

    assert result.error is None
    assert result.failures == []
    assert result.sampled == 2


@pytest.mark.asyncio
async def test_fails_when_a_fake_adapter_has_no_quotable_market_at_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The brief's exact scenario: a fake adapter whose markets are
    missing the spread field entirely (Polymarket without `bestAsk` --
    `app.venues.types.quotable_spread` returns `None` for a one-sided
    listing) has nothing quotable to sample. Reported as `sampled == 0`,
    never as a crash -- and `_collection_group` renders it FAIL, the
    same "quotable == 0 is never healthy" signal
    `app.scripts.probe_quotable` already uses.
    """
    one_sided = make_venue_market(
        venue="polymarket",
        market_id="PM-NO-ASK",
        raw={"bestBid": 0.40, "volume24hr": 500.0},  # bestAsk missing entirely
    )
    adapter = FixtureAdapter("polymarket").add_market(one_sided)

    result = await check_collection_for_venue("polymarket", adapter)

    assert result.error is None
    assert result.sampled == 0

    group = _collection_group([result])
    assert group.status == "fail"
    assert any("no quotable market" in c.message for c in group.checks)


@pytest.mark.asyncio
async def test_fails_when_get_book_returns_a_one_sided_book() -> None:
    """A market that is quotable at the LISTING level (spread/volume both
    fine) but whose actual order book -- a SEPARATE venue endpoint from
    the listing -- has gone one-sided since the listing was read. This
    is the one check that cannot be satisfied merely by the market
    having a good `quotable_spread`/`venue_volume`/`fee`: it is a real,
    independent probe of `get_book` itself, exactly the case PLAN.md D8
    exists for (the listing payload and the book payload can disagree).
    """
    adapter = (
        FixtureAdapter("polymarket")
        .add_market(_quotable_market("PM-STALE-BOOK"))
        .set_book(
            make_book(bids=[], asks=[(0.55, 10.0)], market_id="PM-STALE-BOOK", outcome="YES")
        )
    )

    result = await check_collection_for_venue("polymarket", adapter)

    assert result.error is None
    assert result.sampled == 1
    assert len(result.failures) == 1
    assert "one-sided" in result.failures[0]

    group = _collection_group([result])
    assert group.status == "fail"


@pytest.mark.asyncio
async def test_fails_when_a_quotable_markets_fee_taker_rate_is_not_a_float() -> None:
    """The ONE field check in the loop that is not already guaranteed by
    `select_quotable_markets`'s own filter -- `market.fee.taker_rate is a
    float` -- must actually be exercisable, not merely present in the
    source. `FeeSchedule` is a plain (non-pydantic) `@dataclass`, and its
    `__post_init__` validates `taker_rate` with `math.isfinite(value) and
    value >= 0.0` (`app.venues.types._check_size`) -- a check an `int`
    satisfies exactly as well as a `float` (`math.isfinite(0)` is `True`,
    `isinstance(0, float)` is `False`). So a venue whose fee-parsing path
    returns an `int` (e.g. a JSON payload's `"takerFee": 0` deserializing
    to a Python `int` before the caller remembers to coerce it) builds a
    perfectly valid, listing-quotable `FeeSchedule` that still trips this
    exact check -- proving it is real, not dead code the way the spread/
    volume checks below are (see `test_the_spread_and_volume_field_checks_
    can_never_fail_given_how_sample_is_selected`).
    """
    bad_fee = FeeSchedule(taker_rate=0, maker_rate=0.01, source="test")  # type: ignore[arg-type]
    market = make_venue_market(
        venue="polymarket",
        market_id="PM-INT-FEE",
        raw={"bestBid": 0.40, "bestAsk": 0.55, "volume24hr": 500.0},
        fee=bad_fee,
    )
    assert not isinstance(market.fee.taker_rate, float)  # sanity: the fixture built what it claims
    adapter = (
        FixtureAdapter("polymarket")
        .add_market(market)
        .set_book(
            make_book(bids=[(0.40, 10.0)], asks=[(0.55, 10.0)], market_id="PM-INT-FEE", outcome="YES")
        )
    )

    result = await check_collection_for_venue("polymarket", adapter)

    assert result.error is None
    assert result.sampled == 1
    assert len(result.failures) == 1
    assert "fee.taker_rate" in result.failures[0]

    group = _collection_group([result])
    assert group.status == "fail"


def test_the_spread_and_volume_field_checks_can_never_fail_given_how_sample_is_selected() -> None:
    """mm-proveout T9 red-team finding, NOT a passing/failing behavioral
    bug -- a structural gap in `check_collection_for_venue` (`app.scripts.
    preflight`) itself, true for every possible adapter/fixture/live
    payload, not merely a limitation of this test's fake data.

    `check_collection_for_venue` samples from `quotable`
    (`select_quotable_markets(markets)`'s SECOND return value), then
    separately asserts, per sampled market: `quotable_spread(m) is not
    None` and `venue_volume(m) > 0`. But `select_quotable_markets`
    builds `quotable` as a SUBSET of `two_sided` -- literally `if spread
    is None: continue` before a market is even eligible for `quotable`
    -- and further requires `venue_volume(m) >= settings.
    book_collection_min_volume` (default 100.0) before appending. Both
    predicates are therefore already true of every element BEFORE the
    check loop ever runs, on the exact same (immutable) `VenueMarket`
    object -- `quotable_spread`/`venue_volume` are pure functions of
    `market.raw`, which does not change between selection and the check.

    So: `quotable_spread(m) is None` is UNREACHABLE for m in `quotable`
    unconditionally, for any settings, any adapter, any venue payload --
    proven here directly against `select_quotable_markets`, the exact
    function `check_collection_for_venue` calls, rather than merely
    inferred from reading the source. `venue_volume(m) > 0` is
    unreachable in every configuration this repo actually ships
    (`book_collection_min_volume` defaults to 100.0 and nothing sets it
    to <= 0). A venue that silently renamed its volume/spread field to
    something `quotable_spread`/`venue_volume` cannot parse does not
    trip these two checks at all -- it just shrinks `quotable` (down to
    `sampled == 0` in the worst case, already covered by
    `test_fails_when_a_fake_adapter_has_no_quotable_market_at_all`
    above). The concrete fix: re-derive these two checks from the
    UNFILTERED listing (`markets`, or at loosest `two_sided`) rather than
    from `quotable`, or drop them and rely on `sampled == 0` plus the
    `fee.taker_rate`/`get_book` checks, which are the only ones that can
    actually fire.
    """
    markets = [
        make_venue_market(
            venue="polymarket",
            market_id="PM-QUOTABLE-1",
            raw={"bestBid": 0.40, "bestAsk": 0.55, "volume24hr": 500.0},
        ),
        make_venue_market(
            venue="polymarket",
            market_id="PM-QUOTABLE-BOUNDARY",
            # Exactly at both floors (min_spread=0.10, min_volume=100.0).
            raw={"bestBid": 0.45, "bestAsk": 0.55, "volume24hr": 100.0},
        ),
        make_venue_market(
            venue="polymarket",
            market_id="PM-ONE-SIDED",
            raw={"bestBid": 0.40, "volume24hr": 500.0},  # bestAsk missing
        ),
        make_venue_market(
            venue="polymarket",
            market_id="PM-THIN",
            raw={"bestBid": 0.40, "bestAsk": 0.55, "volume24hr": 1.0},  # below min_volume
        ),
    ]

    _, quotable, _ = select_quotable_markets(markets)

    assert [m.market_id for m in quotable] == ["PM-QUOTABLE-1", "PM-QUOTABLE-BOUNDARY"]
    for market in quotable:
        # These are exactly `check_collection_for_venue`'s two per-market
        # field checks, run here on the SAME objects `sample` would hold
        # -- neither can ever observe a different answer than this.
        assert quotable_spread(market) is not None
        assert venue_volume(market) > 0


@pytest.mark.asyncio
async def test_fails_when_most_of_the_listing_lost_its_spread_field_even_though_a_few_markets_still_parse() -> None:
    """mm-proveout T9 retry, the fix for the test above: a venue that
    renamed its `bestBid`/`bestAsk` fields for MOST of its listing (a
    partial rollout, a schema migration in progress, or simply "most of
    the venue is broken but a couple of markets happen to still carry
    the old field") must produce a `[FAIL]`, even though the handful of
    markets that still parse are perfectly quotable on their own.

    This is exactly the scenario `check_collection_for_venue`'s OLD
    per-sample logic could never catch: `sample = quotable[:5]` can only
    ever contain markets `quotable_spread` already accepted, so a
    `[PASS]` built from that sample says nothing about the other 18 the
    same run silently produced zero snapshots for. This test's markets
    are constructed so `sample` is non-empty (`sampled == 2`, NOT the
    already-covered `sampled == 0` path) and every OLD per-market check
    on that sample would have been satisfied -- the new fraction check,
    computed over the FULL 20-market listing, is the only thing that can
    see the other 18.
    """
    good_1 = _quotable_market("PM-GOOD-1")
    good_2 = _quotable_market("PM-GOOD-2")
    adapter = FixtureAdapter("polymarket").add_market(good_1).add_market(good_2)
    for i in range(18):
        # `bestAsk`/`bestBid` renamed away entirely -- `quotable_spread`
        # returns None for every one of these, but `volume24hr` (500.0,
        # same as the healthy markets) is untouched, so this scenario
        # isolates the SPREAD fraction failure from the volume one.
        adapter.add_market(
            make_venue_market(
                venue="polymarket",
                market_id=f"PM-RENAMED-{i}",
                raw={"bid": 0.40, "ask": 0.55, "volume24hr": 500.0},
            )
        )
    adapter.set_book(
        make_book(bids=[(0.40, 10.0)], asks=[(0.55, 10.0)], market_id="PM-GOOD-1", outcome="YES")
    )

    result = await check_collection_for_venue("polymarket", adapter)

    assert result.error is None
    assert result.listed == 20
    assert result.two_sided == 2  # only the two still-good markets
    assert result.sampled == 2  # NOT the sampled==0 path -- these two are genuinely quotable
    two_sided_fraction = 2 / 20
    assert two_sided_fraction < _MIN_TWO_SIDED_FRACTION  # 10% < the 20% floor
    assert len(result.failures) == 1
    assert "2/20" in result.failures[0]
    assert "quotable_spread" in result.failures[0]

    group = _collection_group([result])
    assert group.status == "fail"


@pytest.mark.asyncio
async def test_fails_when_most_of_the_listing_has_no_readable_volume_even_though_the_sampled_markets_do() -> None:
    """Same shape as the spread test above, for the volume field: most of
    the listing's `volume24hr`/`volume`/etc. keys are gone entirely
    (`venue_volume` falls back to `0.0`, its documented "nothing parsed"
    result -- indistinguishable, from inside `venue_volume` alone, from a
    market that genuinely has zero volume, which is exactly why this is
    a FRACTION check against the full listing rather than a per-market
    one), while the two sampled, genuinely-quotable markets still carry
    real volume. The two-sidedness fraction stays healthy (all 50
    markets have valid bid/ask), isolating this to the volume check.
    """
    good_1 = _quotable_market("PM-GOOD-1")
    good_2 = _quotable_market("PM-GOOD-2")
    adapter = FixtureAdapter("polymarket").add_market(good_1).add_market(good_2)
    for i in range(48):
        # Two-sided (bestBid/bestAsk both present and sane) but NO volume
        # key at all -- venue_volume() falls back to 0.0 for every one.
        adapter.add_market(
            make_venue_market(
                venue="polymarket",
                market_id=f"PM-NO-VOLUME-{i}",
                raw={"bestBid": 0.40, "bestAsk": 0.55},
            )
        )
    adapter.set_book(
        make_book(bids=[(0.40, 10.0)], asks=[(0.55, 10.0)], market_id="PM-GOOD-1", outcome="YES")
    )

    result = await check_collection_for_venue("polymarket", adapter)

    assert result.error is None
    assert result.listed == 50
    assert result.two_sided == 50  # spread parses everywhere -- not the failure here
    assert result.volume_positive == 2  # only the two still-good markets
    assert result.sampled == 2  # NOT the sampled==0 path
    volume_fraction = 2 / 50
    assert volume_fraction < _MIN_VOLUME_POSITIVE_FRACTION  # 4% < the 5% floor
    assert len(result.failures) == 1
    assert "2/50" in result.failures[0]
    assert "venue_volume" in result.failures[0]

    group = _collection_group([result])
    assert group.status == "fail"


@pytest.mark.asyncio
async def test_a_venue_that_cannot_be_listed_is_reported_as_an_error_not_a_crash() -> None:
    """`list_markets` raising must never propagate out of the check --
    matches every other opt-in probe in this file (`--check-venues`)."""
    class _BrokenAdapter(FixtureAdapter):
        async def list_markets(self, status=None, updated_since=None):  # noqa: ANN001, ARG002
            raise RuntimeError("kalshi: 500")

    result = await check_collection_for_venue("kalshi", _BrokenAdapter("kalshi"))

    assert result.sampled == 0
    assert result.error is not None
    assert "RuntimeError" in result.error

    group = _collection_group([result])
    assert group.status == "fail"
    assert any("could not be probed" in c.message for c in group.checks)


# ---------------------------------------------------------------------------
# §1.3 GUARD: no secret value ever appears in the rendered report, even
# with a `--check-collection` group attached (reusing the exact pattern
# `tests/test_preflight.py::test_no_secret_value_ever_appears_in_the_
# rendered_report` already established for `--check-venues`).
# ---------------------------------------------------------------------------


def test_no_secret_value_ever_appears_with_a_collection_group_attached(
    tmp_path: Path,
) -> None:
    fake_private_key = "FAKEPOLYPRIVATEKEYSHOULDNEVERAPPEAR0123456789abcdef"
    fake_kalshi_key_id = "FAKEKALSHIKEYIDSHOULDNEVERAPPEAR"
    fake_pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "FAKEPEMBODYSHOULDNEVERAPPEARINANYOUTPUT1234567890\n"
        "-----END RSA PRIVATE KEY-----\n"
    )
    cfg = Settings(
        TRADING_MODE="live",
        LIVE_TRADING_CONFIRMATION=LIVE_TRADING_CONFIRMATION_PHRASE,
        KILL_SWITCH_PATH=str(tmp_path / "TRADING_KILL_SWITCH"),
        POLYMARKET_PRIVATE_KEY=fake_private_key,
        KALSHI_API_KEY_ID=fake_kalshi_key_id,
        KALSHI_PRIVATE_KEY_PEM=fake_pem,
        DATABASE_URL="postgresql+asyncpg://produser:SUPERSECRETPASSWORD@localhost:5432/db",
    )
    db_check = DatabaseCheck(
        reachable=False,
        display_url="postgresql+asyncpg://produser:***@localhost:5432/db",
        error="timeout",
    )
    collection_checks = [
        CollectionCheck(venue="kalshi", sampled=0, error="RuntimeError: 500"),
        CollectionCheck(
            venue="polymarket",
            sampled=1,
            failures=["PM-STALE-BOOK: get_book returned a one-sided book"],
        ),
    ]

    report = build_report(
        cfg,
        db_check=db_check,
        broker_checks=_OK_BROKER,
        expected_head_revision="007",
        env={},
        collection_checks=collection_checks,
    )
    rendered = report.render()

    assert "Collection field-name/reality check" in rendered
    for secret in (
        fake_private_key,
        fake_kalshi_key_id,
        fake_pem,
        "FAKEPEMBODYSHOULDNEVERAPPEARINANYOUTPUT1234567890",
        "SUPERSECRETPASSWORD",
    ):
        assert secret not in rendered, f"secret value leaked into report: {secret!r}"


def test_collection_checks_none_by_default_leaves_the_group_out(tmp_path: Path) -> None:
    """The default report must keep promising it contacts no venue --
    `--check-collection` is opt-in, same as `--check-venues`."""
    cfg = Settings(KILL_SWITCH_PATH=str(tmp_path / "TRADING_KILL_SWITCH"))

    report = build_report(
        cfg,
        db_check=_OK_DB,
        broker_checks=_OK_BROKER,
        expected_head_revision="007",
        env={},
    )

    assert not any("Collection field-name/reality check" in g.name for g in report.groups)
    assert any(item.startswith("Venue connectivity") for item in report.not_checked)
