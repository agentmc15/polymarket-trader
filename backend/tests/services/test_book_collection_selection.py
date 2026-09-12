"""Book-collection selection by quotability, not raw volume (mm-proveout
T7, PLAN.md D1).

MEASURED ON LIVE DATA: ranking `DataCollector.collect_books`' candidates
by volume alone (the pre-T7 behaviour) selected 2 Kalshi and 0 Polymarket
markets, out of the top 50 by volume, with spread `>= 0.10` -- at the
time, exactly `MarketMaker`'s calibrated `min_spread`, the floor it
refused to quote inside of. That default has since moved to 0.25 (the
1-minute Kalshi holdout, `app/strategies/market_making.py`);
`settings.book_collection_min_spread` deliberately stays at 0.10 as a
superset (see the tests below at the `0.10` floor for why this file's
own numbers are unaffected). Volume alone finds the tightest books, not
the ones the policy would actually quote.

`select_quotable_markets` (`app.services.data_collector`) is the shared
partition both `DataCollector.collect_books` and
`app.scripts.probe_quotable` use, so these tests exercise it directly
AND through `collect_books` on a `FixtureAdapter` -- the latter proves a
market that fails the filter never even reaches `get_book`, which is the
"no book fetch spent on rejects" half of the brief.
"""
import math
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import app.services.data_collector as dc
from app.config import Settings
from app.models.book_snapshot import BookSnapshot
from app.models.selection_membership import SelectionMembership
from app.services.data_collector import DataCollector, select_quotable_markets
from app.venues.types import FeeSchedule, OrderBook, VenueId, VenueMarket, venue_volume
from tests.helpers import make_book
from tests.venues.fixture_adapter import FixtureAdapter, make_venue_market


def _market(market_id: str, venue: VenueId = "polymarket", **raw: Any) -> VenueMarket:
    """Build a `VenueMarket` with `raw` as its listing payload."""
    return make_venue_market(venue=venue, market_id=market_id, raw=raw)


# ---------------------------------------------------------------------------
# `select_quotable_markets` — the partition itself.
# ---------------------------------------------------------------------------


def test_only_two_sided_markets_above_both_floors_are_quotable() -> None:
    """Four candidates, one genuinely quotable: two-sided, spread and
    volume both clear the default floors (`min_spread=0.10`,
    `min_volume=100.0`)."""
    good = _market("good", bestBid=0.40, bestAsk=0.55, volume24hr=500.0)  # spread 0.15
    one_sided = _market("one-sided", bestBid=0.40, volume24hr=500.0)  # no ask at all
    too_tight = _market("too-tight", bestBid=0.49, bestAsk=0.51, volume24hr=500.0)  # spread 0.02
    too_thin = _market("too-thin", bestBid=0.40, bestAsk=0.55, volume24hr=10.0)  # volume 10

    two_sided, quotable, selected = select_quotable_markets(
        [good, one_sided, too_tight, too_thin]
    )

    assert {m.market_id for m in two_sided} == {"good", "too-tight", "too-thin"}
    assert [m.market_id for m in quotable] == ["good"]
    assert [m.market_id for m in selected] == ["good"]


def test_selected_is_ordered_by_volume_within_the_quotable_set() -> None:
    """All three clear both floors; `selected` must be volume-descending,
    not the input order (which is ascending here)."""
    low = _market("low", bestBid=0.40, bestAsk=0.55, volume24hr=150.0)
    high = _market("high", bestBid=0.40, bestAsk=0.55, volume24hr=9_000.0)
    mid = _market("mid", bestBid=0.40, bestAsk=0.55, volume24hr=1_200.0)

    _, quotable, selected = select_quotable_markets([low, high, mid])

    assert {m.market_id for m in quotable} == {"low", "high", "mid"}
    assert [m.market_id for m in selected] == ["high", "mid", "low"]


def test_top_n_caps_selected_after_the_quotable_filter_not_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`book_collection_top_n` bounds `selected`, applied AFTER (not
    instead of) the two-sided/spread/volume filter."""
    monkeypatch.setattr(dc.settings, "book_collection_top_n", 2)
    markets = [
        _market(f"m{i}", bestBid=0.40, bestAsk=0.55, volume24hr=100.0 + i)
        for i in range(1, 6)
    ]

    _, quotable, selected = select_quotable_markets(markets)

    assert len(quotable) == 5  # all five clear the quotability floor
    assert [m.market_id for m in selected] == ["m5", "m4"]  # top 2 by volume


@pytest.mark.parametrize("bad_top_n", [-500, -1, 0])
def test_top_n_negative_slice_hazard_is_rejected_at_settings_construction(
    bad_top_n: int,
) -> None:
    """`book_collection_top_n <= 0` must not reach `select_quotable_markets`.

    Named for the hazard, not just the bound: `select_quotable_markets`
    does `sorted(quotable, ...)[: settings.book_collection_top_n]`, and
    Python's slice semantics treat a negative stop as a legal index
    counted from the END of the list rather than an invalid count.
    Reproduced directly against the real function with five quotable
    markets, before this fix existed:

        top_n=  500 -> selected=['m5','m4','m3','m2','m1']  (all 5)
        top_n=    2 -> selected=['m5','m4']                  (capped, expected)
        top_n=    0 -> selected=[]                           (collects nothing)
        top_n=   -1 -> selected=['m5','m4','m3','m2']        (drops the LAST market)
        top_n=  -10 -> selected=[]                           (collects NOTHING)

    `top_n=-10` alongside `top_n=500` shows the trap precisely: two
    configuration values that differ only by a minus sign and a digit
    produce, respectively, "keep everything" and "keep nothing" -- with
    no exception raised at the point of the typo. This test proves the
    fix is at `Settings` construction (`app.config.Settings`), not in
    `select_quotable_markets` itself, so a bad value can never reach the
    slice in the first place -- for every non-positive value, not just
    the boundary.
    """
    with pytest.raises(ValidationError, match="book_collection_top_n"):
        Settings(book_collection_top_n=bad_top_n)


def test_top_n_positive_values_are_still_accepted() -> None:
    """The rejection is `<= 0` only -- it must not overreach onto `>= 1`."""
    assert Settings(book_collection_top_n=1).book_collection_top_n == 1
    assert Settings(book_collection_top_n=500).book_collection_top_n == 500


def test_min_spread_is_read_from_settings_not_a_literal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 0.05-wide market is excluded at the default 0.10 floor and
    included once `book_collection_min_spread` is lowered — proving the
    threshold is read from `settings`, not hardcoded."""
    market = _market("tight", bestBid=0.475, bestAsk=0.525, volume24hr=500.0)  # spread 0.05

    _, quotable_before, _ = select_quotable_markets([market])
    assert quotable_before == []

    monkeypatch.setattr(dc.settings, "book_collection_min_spread", 0.03)
    _, quotable_after, _ = select_quotable_markets([market])
    assert [m.market_id for m in quotable_after] == ["tight"]


def test_min_spread_boundary_is_inclusive_not_exclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The brief states the comparison as `ask - bid >= min_spread` --
    `>=`, not `>`. This pins that at the bit level rather than at a
    "nice" decimal like 0.10, because `0.50 - 0.40 == 0.09999999999999998`
    in IEEE-754 double precision (verified: `(0.5 - 0.4) < 0.1` is `True`
    in Python) -- a boundary test written as `bestBid=0.40, bestAsk=0.50`
    against a literal `min_spread=0.10` would silently test the wrong
    side of the line depending on which float happens to round up. Using
    the SAME float the code will compute (`ask - bid`) as the threshold
    itself removes that ambiguity: a market whose computed spread exactly
    equals `book_collection_min_spread` must be `quotable` (`>=`
    includes equality), and one whose spread is the very next
    representable float below the threshold must not (`>=` excludes
    anything strictly less)."""
    bid, ask = 0.40, 0.50
    spread = ask - bid  # 0.09999999999999998 -- NOT 0.1, by construction
    market = _market("boundary", venue="polymarket", bestBid=bid, bestAsk=ask, volume24hr=500.0)

    monkeypatch.setattr(dc.settings, "book_collection_min_spread", spread)
    _, at_boundary, _ = select_quotable_markets([market])
    assert [m.market_id for m in at_boundary] == ["boundary"]  # equal -> included

    monkeypatch.setattr(
        dc.settings, "book_collection_min_spread", math.nextafter(spread, math.inf)
    )
    _, just_above, _ = select_quotable_markets([market])
    assert just_above == []  # one ULP over the market's spread -> excluded


def test_min_volume_boundary_is_inclusive_not_exclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same `>=`-not-`>` claim as the spread boundary above, for
    `venue_volume(m) >= settings.book_collection_min_volume`."""
    volume = 100.0
    market = _market("boundary-vol", venue="polymarket", bestBid=0.40, bestAsk=0.55, volume24hr=volume)

    monkeypatch.setattr(dc.settings, "book_collection_min_volume", volume)
    _, at_boundary, _ = select_quotable_markets([market])
    assert [m.market_id for m in at_boundary] == ["boundary-vol"]  # equal -> included

    monkeypatch.setattr(
        dc.settings, "book_collection_min_volume", math.nextafter(volume, math.inf)
    )
    _, just_above, _ = select_quotable_markets([market])
    assert just_above == []  # one ULP over the market's volume -> excluded


def test_min_volume_is_read_from_settings_not_a_literal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A volume-50 market is excluded at the default 100.0 floor and
    included once `book_collection_min_volume` is lowered."""
    market = _market("thin", bestBid=0.40, bestAsk=0.55, volume24hr=50.0)

    _, quotable_before, _ = select_quotable_markets([market])
    assert quotable_before == []

    monkeypatch.setattr(dc.settings, "book_collection_min_volume", 10.0)
    _, quotable_after, _ = select_quotable_markets([market])
    assert [m.market_id for m in quotable_after] == ["thin"]


# ---------------------------------------------------------------------------
# `DataCollector.collect_books` — the filter actually gates `get_book`.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_collect_books_never_fetches_a_book_for_a_rejected_market(
    test_session: AsyncSession,
) -> None:
    """A market that fails quotability must not cost a `get_book` call at
    all ("no book fetch is spent on rejects") and must write no
    `BookSnapshot` row."""
    quoted = make_venue_market(
        venue="polymarket",
        market_id="PM-QUOTED",
        outcomes=("YES",),
        raw={"bestBid": 0.40, "bestAsk": 0.55, "volume24hr": 500.0},
    )
    rejected = make_venue_market(
        venue="polymarket",
        market_id="PM-REJECTED",
        outcomes=("YES",),
        # Two-sided but spread (0.02) is well under the default floor.
        raw={"bestBid": 0.49, "bestAsk": 0.51, "volume24hr": 500.0},
    )
    from tests.helpers import make_book

    adapter = (
        FixtureAdapter("polymarket")
        .add_market(quoted)
        .add_market(rejected)
        .set_book(make_book(bids=[(0.40, 10.0)], asks=[(0.55, 10.0)], market_id="PM-QUOTED", outcome="YES"))
        .set_book(make_book(bids=[(0.49, 10.0)], asks=[(0.51, 10.0)], market_id="PM-REJECTED", outcome="YES"))
    )
    market_ids_per_venue: dict[VenueId, list[str]] = {
        "polymarket": ["PM-QUOTED", "PM-REJECTED"]
    }

    collector = DataCollector(test_session)
    written = await collector.collect_books(
        {"polymarket": adapter}, market_ids_per_venue, test_session
    )

    assert written == 1
    assert adapter.get_book_calls == 1  # PM-REJECTED never asked for a book

    rows = (await test_session.execute(select(BookSnapshot))).scalars().all()
    assert [row.market_id for row in rows] == ["PM-QUOTED"]


@pytest.mark.asyncio
async def test_snapshot_written_carries_listing_volume_and_fee_in_force(
    test_session: AsyncSession,
) -> None:
    """mm-proveout T8 (PLAN.md D9): the `BookSnapshot` row `collect_books`
    writes must carry the LISTING's `venue_volume` and the market's
    `fee.taker_rate`/`fee.maker_rebate_rate` -- never leave them `None`
    for a market collected today. Uses Kalshi (rather than the
    Polymarket-default fixture above) so this also proves the volume
    read is `venue_volume`, which prefers `volume_24h_fp` on Kalshi, not
    a bare `raw["volume"]` Kalshi never sends."""
    fee = FeeSchedule(
        taker_rate=0.07, maker_rate=0.0, source="settings", maker_rebate_rate=0.0175
    )
    market = make_venue_market(
        venue="kalshi",
        market_id="KALSHI-QUOTED",
        outcomes=("YES",),
        raw={
            "yes_bid_dollars": "0.40",
            "yes_ask_dollars": "0.55",
            "volume_24h_fp": "777.0",
        },
        fee=fee,
    )
    adapter = (
        FixtureAdapter("kalshi")
        .add_market(market)
        .set_book(
            make_book(
                bids=[(0.40, 10.0)],
                asks=[(0.55, 10.0)],
                venue="kalshi",
                market_id="KALSHI-QUOTED",
                outcome="YES",
            )
        )
    )
    market_ids_per_venue: dict[VenueId, list[str]] = {"kalshi": ["KALSHI-QUOTED"]}

    collector = DataCollector(test_session)
    written = await collector.collect_books(
        {"kalshi": adapter}, market_ids_per_venue, test_session
    )
    assert written == 1

    row = (
        await test_session.execute(
            select(BookSnapshot).where(BookSnapshot.market_id == "KALSHI-QUOTED")
        )
    ).scalar_one()
    assert row.volume == venue_volume(market) == 777.0
    assert row.taker_fee_rate == fee.taker_rate == 0.07
    assert row.maker_rebate_rate == fee.maker_rebate_rate == 0.0175


@pytest.mark.asyncio
async def test_snapshot_fee_carries_the_markets_own_schedule_not_a_venue_default(
    test_session: AsyncSession,
) -> None:
    """mm-proveout T8, adversarial: the test above stores `0.07`/`0.0175`
    -- exactly Kalshi's own `Settings` defaults -- so it cannot tell
    "read `market.fee`" apart from "fell back to the venue default and
    got lucky". Two Polymarket markets collected in the SAME
    `collect_books` call: one left on `DEFAULT_SCHEDULES["polymarket"]`
    (`taker_rate=0.04`, `source="category_table"`), the other given a
    per-market override shaped like Polymarket's own `feeSchedule`
    (`source="venue_schedule"`, `taker_rate=0.13`, `maker_rebate_rate=
    0.31` -- chosen far from 0.04/0.0 and from each other so no
    coincidental match is possible). Each row must carry ITS OWN
    market's numbers, proving the write is per-market, not one constant
    broadcast to every row in the run.
    """
    default_fee_market = make_venue_market(
        venue="polymarket",
        market_id="PM-DEFAULT-FEE",
        outcomes=("YES",),
        raw={"bestBid": 0.40, "bestAsk": 0.55, "volume24hr": 500.0},
        # fee omitted -> DEFAULT_SCHEDULES["polymarket"]: taker_rate=0.04
    )
    overridden_fee = FeeSchedule(
        taker_rate=0.13, maker_rate=0.0, source="venue_schedule", maker_rebate_rate=0.31
    )
    overridden_fee_market = make_venue_market(
        venue="polymarket",
        market_id="PM-OVERRIDE-FEE",
        outcomes=("YES",),
        raw={"bestBid": 0.30, "bestAsk": 0.45, "volume24hr": 600.0},
        fee=overridden_fee,
    )
    adapter = (
        FixtureAdapter("polymarket")
        .add_market(default_fee_market)
        .add_market(overridden_fee_market)
        .set_book(
            make_book(bids=[(0.40, 10.0)], asks=[(0.55, 10.0)], market_id="PM-DEFAULT-FEE", outcome="YES")
        )
        .set_book(
            make_book(bids=[(0.30, 10.0)], asks=[(0.45, 10.0)], market_id="PM-OVERRIDE-FEE", outcome="YES")
        )
    )
    market_ids_per_venue: dict[VenueId, list[str]] = {
        "polymarket": ["PM-DEFAULT-FEE", "PM-OVERRIDE-FEE"]
    }

    collector = DataCollector(test_session)
    written = await collector.collect_books(
        {"polymarket": adapter}, market_ids_per_venue, test_session
    )
    assert written == 2

    default_row = (
        await test_session.execute(
            select(BookSnapshot).where(BookSnapshot.market_id == "PM-DEFAULT-FEE")
        )
    ).scalar_one()
    override_row = (
        await test_session.execute(
            select(BookSnapshot).where(BookSnapshot.market_id == "PM-OVERRIDE-FEE")
        )
    ).scalar_one()

    # The overridden market's rate is its OWN market.fee -- a hardcoded
    # fallback to the venue default (0.04 / 0.0) would fail these two.
    assert override_row.taker_fee_rate == 0.13
    assert override_row.maker_rebate_rate == 0.31
    assert override_row.taker_fee_rate != default_fee_market.fee.taker_rate

    # The default-schedule market still carries its own, different rate
    # -- proving the two rows are independently sourced per market, not
    # one constant applied to both in the same run.
    assert default_row.taker_fee_rate == default_fee_market.fee.taker_rate == 0.04
    assert default_row.maker_rebate_rate == 0.0


@pytest.mark.asyncio
async def test_maker_rebate_rate_and_taker_fee_rate_never_contaminate_each_other(
    test_session: AsyncSession,
) -> None:
    """GUARDRAILS.md §2.3/D6: `maker_rebate_rate` is carried as data and
    must never be folded into, swapped with, or otherwise leak into
    `taker_fee_rate`. Two markets share the IDENTICAL `taker_rate`
    (0.05) and differ ONLY in `maker_rebate_rate` (0.0 vs `0.25`, the
    exact value the brief names): if `collect_books` ever swapped the
    two columns, summed them, or let one influence the other, the two
    rows' `taker_fee_rate` would stop matching even though their
    markets' taker rates are identical.
    """
    same_taker_no_rebate = FeeSchedule(
        taker_rate=0.05, maker_rate=0.0, source="venue_schedule", maker_rebate_rate=0.0
    )
    same_taker_with_rebate = FeeSchedule(
        taker_rate=0.05, maker_rate=0.0, source="venue_schedule", maker_rebate_rate=0.25
    )
    no_rebate_market = make_venue_market(
        venue="polymarket",
        market_id="PM-NO-REBATE",
        outcomes=("YES",),
        raw={"bestBid": 0.40, "bestAsk": 0.55, "volume24hr": 500.0},
        fee=same_taker_no_rebate,
    )
    with_rebate_market = make_venue_market(
        venue="polymarket",
        market_id="PM-WITH-REBATE",
        outcomes=("YES",),
        raw={"bestBid": 0.40, "bestAsk": 0.55, "volume24hr": 500.0},
        fee=same_taker_with_rebate,
    )
    adapter = (
        FixtureAdapter("polymarket")
        .add_market(no_rebate_market)
        .add_market(with_rebate_market)
        .set_book(
            make_book(bids=[(0.40, 10.0)], asks=[(0.55, 10.0)], market_id="PM-NO-REBATE", outcome="YES")
        )
        .set_book(
            make_book(bids=[(0.40, 10.0)], asks=[(0.55, 10.0)], market_id="PM-WITH-REBATE", outcome="YES")
        )
    )
    market_ids_per_venue: dict[VenueId, list[str]] = {
        "polymarket": ["PM-NO-REBATE", "PM-WITH-REBATE"]
    }

    collector = DataCollector(test_session)
    written = await collector.collect_books(
        {"polymarket": adapter}, market_ids_per_venue, test_session
    )
    assert written == 2

    no_rebate_row = (
        await test_session.execute(
            select(BookSnapshot).where(BookSnapshot.market_id == "PM-NO-REBATE")
        )
    ).scalar_one()
    with_rebate_row = (
        await test_session.execute(
            select(BookSnapshot).where(BookSnapshot.market_id == "PM-WITH-REBATE")
        )
    ).scalar_one()

    assert no_rebate_row.taker_fee_rate == 0.05
    assert with_rebate_row.taker_fee_rate == 0.05  # unaffected by the rebate
    assert no_rebate_row.maker_rebate_rate == 0.0
    assert with_rebate_row.maker_rebate_rate == 0.25  # its own value, not merged into taker


@pytest.mark.asyncio
async def test_snapshot_volume_is_the_listing_volume_not_the_books_size_or_level_count(
    test_session: AsyncSession,
) -> None:
    """The brief: `volume` must be the LISTING's `venue_volume(market)` --
    not the book's size, not a count of levels, not zero. The book here
    has a total resting size (3.0 + 7.0 = 10.0 contracts) and a level
    count (2, one per side) that both differ sharply from the listing's
    own volume figure (4321.0), so a wrong implementation that summed
    book depth or counted levels instead of reading the listing would
    produce a number this test can tell apart from the correct one.
    """
    market = make_venue_market(
        venue="polymarket",
        market_id="PM-VOLUME-CHECK",
        outcomes=("YES",),
        raw={"bestBid": 0.40, "bestAsk": 0.55, "volume24hr": 4321.0},
    )
    adapter = (
        FixtureAdapter("polymarket")
        .add_market(market)
        .set_book(
            make_book(bids=[(0.40, 3.0)], asks=[(0.55, 7.0)], market_id="PM-VOLUME-CHECK", outcome="YES")
        )
    )
    market_ids_per_venue: dict[VenueId, list[str]] = {"polymarket": ["PM-VOLUME-CHECK"]}

    collector = DataCollector(test_session)
    written = await collector.collect_books(
        {"polymarket": adapter}, market_ids_per_venue, test_session
    )
    assert written == 1

    row = (
        await test_session.execute(
            select(BookSnapshot).where(BookSnapshot.market_id == "PM-VOLUME-CHECK")
        )
    ).scalar_one()

    assert row.volume == venue_volume(market) == 4321.0
    total_book_size = 3.0 + 7.0
    assert row.volume != total_book_size  # not the book's total resting size
    assert row.volume != 2  # not a count of levels (one bid level, one ask level)
    assert row.volume != 0.0  # not zero


@pytest.mark.asyncio
async def test_no_rejected_market_of_any_kind_ever_reaches_get_book(
    test_session: AsyncSession,
) -> None:
    """Stronger version of the test above: covers every distinct
    rejection reason the brief names (one-sided, crossed, non-numeric,
    below-volume) in one call, and -- unlike the count-based assertion
    above -- registers NO book at all for any rejected market. If
    `collect_books` fetched a book for even one of them,
    `FixtureAdapter.get_book` would raise `KeyError` (it is not caught by
    `collect_books`, which only catches `VenueError`), so this fails
    LOUDLY on a violation rather than merely mismatching a count."""
    quoted = make_venue_market(
        venue="polymarket",
        market_id="PM-QUOTED",
        outcomes=("YES",),
        raw={"bestBid": 0.40, "bestAsk": 0.55, "volume24hr": 500.0},
    )
    one_sided = make_venue_market(
        venue="polymarket",
        market_id="PM-ONE-SIDED",
        outcomes=("YES",),
        raw={"bestBid": 0.40, "volume24hr": 500.0},  # no bestAsk at all
    )
    crossed = make_venue_market(
        venue="polymarket",
        market_id="PM-CROSSED",
        outcomes=("YES",),
        raw={"bestBid": 0.60, "bestAsk": 0.50, "volume24hr": 500.0},  # bid > ask
    )
    non_numeric = make_venue_market(
        venue="polymarket",
        market_id="PM-NON-NUMERIC",
        outcomes=("YES",),
        raw={"bestBid": "n/a", "bestAsk": 0.55, "volume24hr": 500.0},
    )
    too_thin = make_venue_market(
        venue="polymarket",
        market_id="PM-TOO-THIN",
        outcomes=("YES",),
        raw={"bestBid": 0.40, "bestAsk": 0.55, "volume24hr": 1.0},  # below default floor
    )

    adapter = (
        FixtureAdapter("polymarket")
        .add_market(quoted)
        .add_market(one_sided)
        .add_market(crossed)
        .add_market(non_numeric)
        .add_market(too_thin)
        # Deliberately NO `.set_book(...)` for any rejected market.
        .set_book(make_book(bids=[(0.40, 10.0)], asks=[(0.55, 10.0)], market_id="PM-QUOTED", outcome="YES"))
    )
    market_ids_per_venue: dict[VenueId, list[str]] = {
        "polymarket": [
            "PM-QUOTED",
            "PM-ONE-SIDED",
            "PM-CROSSED",
            "PM-NON-NUMERIC",
            "PM-TOO-THIN",
        ]
    }

    collector = DataCollector(test_session)
    written = await collector.collect_books(
        {"polymarket": adapter}, market_ids_per_venue, test_session
    )

    assert written == 1
    assert adapter.get_book_calls == 1

    rows = (await test_session.execute(select(BookSnapshot))).scalars().all()
    assert [row.market_id for row in rows] == ["PM-QUOTED"]


# ---------------------------------------------------------------------------
# T8 red-team retry: "the fee in force" on a same-`ts` re-poll of a quiet book.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_fee_and_volume_are_refreshed_on_a_same_ts_repoll(
    test_session: AsyncSession,
) -> None:
    """Chosen semantics (decision (a), `DataCollector._upsert_book_snapshot`
    docstring): identical `ts` is NOT treated as "nothing changed" for
    `volume`/`taker_fee_rate`/`maker_rebate_rate`. `ts` is the book's own
    identity (when it last moved) -- `PolymarketAdapter._parse_book` sets
    `OrderBook.ts` from the CLOB payload's own `timestamp`, not poll time
    -- so a quiet Polymarket market can report the IDENTICAL `ts` across
    many `collect_books` polls while its `feeSchedule` and cumulative
    volume move underneath. This test reproduces the red-team's exact
    numbers across two `collect_books` passes over an UNCHANGED book
    (same `ts`, never re-registered) and pins that the row's
    `volume`/`taker_fee_rate`/`maker_rebate_rate` follow the market's
    CURRENT numbers rather than freezing at whatever they were on first
    sight:

        [poll 1] written=1 volume=500.0  taker_fee_rate=0.07 maker_rebate_rate=0.0175
        [poll 2] written=0 volume=9999.0 taker_fee_rate=0.02 maker_rebate_rate=0.0
                 (pre-fix, poll 2 wrongly kept volume=500.0/taker_fee_rate=0.07 -- STALE)

    "The fee in force" therefore means the fee as of the MOST RECENT
    poll that observed this book, which is what `mm_replay_snapshots`
    (T11) must compute P&L from -- not the fee at first sight.
    """
    ts = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)
    quiet_book = make_book(
        bids=[(0.40, 10.0)], asks=[(0.55, 10.0)],
        market_id="PM-QUIET", outcome="YES", ts=ts,
    )
    stale_fee = FeeSchedule(
        taker_rate=0.07, maker_rate=0.0, source="venue_schedule", maker_rebate_rate=0.0175
    )
    market_poll_1 = make_venue_market(
        venue="polymarket",
        market_id="PM-QUIET",
        outcomes=("YES",),
        raw={"bestBid": 0.40, "bestAsk": 0.55, "volume24hr": 500.0},
        fee=stale_fee,
    )
    # `set_book` is called exactly once: the book itself never moves
    # between the two polls below, so its `ts` (and every other field)
    # stays identical -- exactly the "quiet Polymarket market" case.
    adapter = FixtureAdapter("polymarket").add_market(market_poll_1).set_book(quiet_book)
    market_ids_per_venue: dict[VenueId, list[str]] = {"polymarket": ["PM-QUIET"]}

    collector = DataCollector(test_session)

    written_1 = await collector.collect_books(
        {"polymarket": adapter}, market_ids_per_venue, test_session
    )
    assert written_1 == 1

    row = (
        await test_session.execute(
            select(BookSnapshot).where(BookSnapshot.market_id == "PM-QUIET")
        )
    ).scalar_one()
    row_id = row.id
    # SQLite drops tzinfo on round trip (a pre-existing, orthogonal
    # quirk of `DateTime(timezone=True)` on this dialect); compare the
    # wall-clock value, not `tzinfo` identity.
    assert row.ts.replace(tzinfo=UTC) == ts
    assert row.volume == 500.0
    assert row.taker_fee_rate == 0.07
    assert row.maker_rebate_rate == 0.0175

    # Poll 2: the venue's fee schedule and cumulative volume have both
    # changed, but the book itself has not moved -- `adapter` still
    # answers `get_book` with the SAME `OrderBook` (same `ts`), the way
    # `PolymarketAdapter._parse_book` would on a quiet market.
    fresh_fee = FeeSchedule(
        taker_rate=0.02, maker_rate=0.0, source="venue_schedule", maker_rebate_rate=0.0
    )
    market_poll_2 = make_venue_market(
        venue="polymarket",
        market_id="PM-QUIET",
        outcomes=("YES",),
        raw={"bestBid": 0.40, "bestAsk": 0.55, "volume24hr": 9999.0},
        fee=fresh_fee,
    )
    adapter.add_market(market_poll_2)

    written_2 = await collector.collect_books(
        {"polymarket": adapter}, market_ids_per_venue, test_session
    )
    assert written_2 == 0  # same natural key -- no new row

    rows_after = (
        (await test_session.execute(select(BookSnapshot))).scalars().all()
    )
    assert len(rows_after) == 1  # still exactly one row -- refreshed, not duplicated

    row_after = rows_after[0]
    assert row_after.id == row_id  # the SAME row, updated in place
    assert row_after.ts.replace(tzinfo=UTC) == ts  # the book's identity is unchanged
    assert row_after.volume == 9999.0  # not stuck at 500.0
    assert row_after.taker_fee_rate == 0.02  # not stuck at 0.07
    assert row_after.maker_rebate_rate == 0.0  # not stuck at 0.0175


@pytest.mark.asyncio
async def test_an_unchanged_repoll_does_not_rewrite_identical_values(
    test_session: AsyncSession,
) -> None:
    """Companion to the refresh test above: when a re-poll's `volume`/
    `fee` genuinely match the stored row (the ordinary idempotent case
    `test_collect_books_writes_rows_and_is_idempotent_on_rerun` in
    `tests/backtesting/test_recorded_depth.py` already covers), the
    refresh is a same-value no-op -- `written` stays `0` and the row's
    id and values are unchanged. Proves the fix does not turn every
    re-poll into a write; only a genuinely different volume/fee triggers
    one.
    """
    ts = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)
    book = make_book(
        bids=[(0.40, 10.0)], asks=[(0.55, 10.0)],
        market_id="PM-STEADY", outcome="YES", ts=ts,
    )
    fee = FeeSchedule(
        taker_rate=0.07, maker_rate=0.0, source="venue_schedule", maker_rebate_rate=0.0175
    )
    market = make_venue_market(
        venue="polymarket",
        market_id="PM-STEADY",
        outcomes=("YES",),
        raw={"bestBid": 0.40, "bestAsk": 0.55, "volume24hr": 500.0},
        fee=fee,
    )
    adapter = FixtureAdapter("polymarket").add_market(market).set_book(book)
    market_ids_per_venue: dict[VenueId, list[str]] = {"polymarket": ["PM-STEADY"]}

    collector = DataCollector(test_session)
    assert (
        await collector.collect_books(
            {"polymarket": adapter}, market_ids_per_venue, test_session
        )
        == 1
    )
    written_2 = await collector.collect_books(
        {"polymarket": adapter}, market_ids_per_venue, test_session
    )
    assert written_2 == 0

    rows = (await test_session.execute(select(BookSnapshot))).scalars().all()
    assert len(rows) == 1
    assert rows[0].volume == 500.0
    assert rows[0].taker_fee_rate == 0.07
    assert rows[0].maker_rebate_rate == 0.0175


# ---------------------------------------------------------------------------
# mm-proveout T9 red-team, confirmed live: `collect_books`'s per-venue
# isolation held only for `VenueError` raised inside the narrow
# `get_market`/`get_book` try/excepts. A plain, unwrapped exception (a
# payload bug, an `IntegrityError`) raised while processing one venue
# used to propagate out of the WHOLE call -- so an untried second venue
# was never reached, and (because the old code committed once at the
# very end) an already-processed first venue's rows were lost too. T9
# fixed this with a per-venue `try/except Exception` + per-venue commit;
# these two tests reproduce the exact defect shape in both venue orders
# and prove it no longer happens.
# ---------------------------------------------------------------------------


class _UnwrappedPayloadBugAdapter(FixtureAdapter):
    """A read adapter whose `get_market` always raises a PLAIN
    `RuntimeError` -- simulating "an unwrapped payload bug", the T9
    red-team's confirmed non-`VenueError` failure mode, as opposed to the
    `VenueError` `collect_books` already handled per-market before this
    fix. The exception is raised from deep inside `collect_books`'s own
    per-venue body (the `for market_id in market_ids` loop), never inside
    the narrow `except VenueError` around that same call -- so it can
    only be caught by the new per-venue `except Exception` this test
    exists to pin.
    """

    async def get_market(self, market_id: str) -> VenueMarket:
        raise RuntimeError(f"unwrapped payload bug for {market_id!r}")


def _healthy_polymarket_adapter(market_id: str = "PM-HEALTHY") -> FixtureAdapter:
    """A normal, two-sided, quotable Polymarket fixture adapter."""
    market = make_venue_market(
        venue="polymarket",
        market_id=market_id,
        outcomes=("YES",),
        raw={"bestBid": 0.40, "bestAsk": 0.55, "volume24hr": 500.0},
    )
    return (
        FixtureAdapter("polymarket")
        .add_market(market)
        .set_book(
            make_book(
                bids=[(0.40, 10.0)], asks=[(0.55, 10.0)],
                market_id=market_id, outcome="YES",
            )
        )
    )


@pytest.mark.asyncio
async def test_a_non_venue_error_exception_in_the_first_venue_does_not_block_the_second(
    test_session: AsyncSession,
) -> None:
    """Kalshi (broken) is listed FIRST in `market_ids_per_venue` -- dict
    iteration is insertion order, so this reproduces the exact defect:
    before the fix, Kalshi's `RuntimeError` would have propagated out of
    `collect_books` entirely and Polymarket, listed second, would never
    have been attempted at all.
    """
    broken = _UnwrappedPayloadBugAdapter("kalshi")
    healthy = _healthy_polymarket_adapter()

    market_ids_per_venue: dict[VenueId, list[str]] = {
        "kalshi": ["KALSHI-BROKEN"],
        "polymarket": ["PM-HEALTHY"],
    }
    collector = DataCollector(test_session)

    written = await collector.collect_books(
        {"kalshi": broken, "polymarket": healthy}, market_ids_per_venue, test_session
    )

    assert written == 1
    rows = (await test_session.execute(select(BookSnapshot))).scalars().all()
    assert [row.market_id for row in rows] == ["PM-HEALTHY"]


@pytest.mark.asyncio
async def test_a_non_venue_error_exception_in_the_second_venue_does_not_undo_the_firsts_commit(
    test_session: AsyncSession,
) -> None:
    """The other half of the same defect: the FIRST venue's rows used to
    be lost too, because the pre-fix implementation committed once at the
    very end of the whole call -- an exception raised while processing
    the SECOND venue rolled everything back before the FIRST venue's
    `session.add()`-ed rows were ever made durable. Polymarket (healthy)
    is listed FIRST here; Kalshi (broken) second -- proving the fix is
    "per-venue commit", not merely "keep going after an error".
    """
    healthy = _healthy_polymarket_adapter()
    broken = _UnwrappedPayloadBugAdapter("kalshi")

    market_ids_per_venue: dict[VenueId, list[str]] = {
        "polymarket": ["PM-HEALTHY"],
        "kalshi": ["KALSHI-BROKEN"],
    }
    collector = DataCollector(test_session)

    written = await collector.collect_books(
        {"polymarket": healthy, "kalshi": broken}, market_ids_per_venue, test_session
    )

    assert written == 1
    rows = (await test_session.execute(select(BookSnapshot))).scalars().all()
    assert [row.market_id for row in rows] == ["PM-HEALTHY"]


@pytest.mark.asyncio
async def test_the_first_venues_commit_survives_the_seconds_failure_seen_from_a_fresh_session(
    test_engine: AsyncEngine,
) -> None:
    """Same order and defect as the test above (Polymarket healthy and
    FIRST, Kalshi broken and SECOND), but read back through a session
    this test never wrote through -- a distinct `AsyncSession` instance
    from a distinct `async_sessionmaker`, with its own empty identity
    map, bound to the SAME underlying `test_engine`/SQLite connection.

    This closes a gap the test above leaves open: querying via the same
    `test_session` object that performed the writes cannot distinguish
    "the row is durably committed to the database" from "the row is
    merely still sitting in that one session's Python-side cache" (its
    identity map, or a connection that never actually released its
    transaction). `session.commit()` inside `collect_books` is a real
    commit against the shared SQLite connection (`test_engine` uses
    `StaticPool`, so every session shares the one physical connection),
    so a fresh session querying afterwards must see the row too if the
    fix is real -- and would NOT see it if the "fix" only ever looked
    correct because the test that pinned it queried its own writer.
    """
    healthy = _healthy_polymarket_adapter()
    broken = _UnwrappedPayloadBugAdapter("kalshi")

    market_ids_per_venue: dict[VenueId, list[str]] = {
        "polymarket": ["PM-HEALTHY"],
        "kalshi": ["KALSHI-BROKEN"],
    }

    write_sessions = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with write_sessions() as write_session:
        collector = DataCollector(write_session)
        written = await collector.collect_books(
            {"polymarket": healthy, "kalshi": broken}, market_ids_per_venue, write_session
        )
        assert written == 1

    # A brand-new session/identity map, never used for the write above.
    read_sessions = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with read_sessions() as fresh_session:
        rows = (await fresh_session.execute(select(BookSnapshot))).scalars().all()
    assert [row.market_id for row in rows] == ["PM-HEALTHY"]


# ---------------------------------------------------------------------------
# mm-proveout T15 (Phase 2 review, findings F1/F4/F5): `volume_lifetime`,
# `observed_at`, `fee_source`, `maker_fee_rate` -- the columns migration
# `008` cannot add later because collection has not started yet.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_carries_lifetime_volume_and_fee_provenance_distinct_from_volume(
    test_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row `collect_books` writes must carry the LIFETIME volume
    counter (`volume_fp` on Kalshi) -- never `venue_volume`'s 24-hour-
    first reading, which is `volume` above (unchanged, still correct for
    ranking) -- plus the poll's own wall clock and the fee's provenance
    and maker rate. `volume_24h_fp` (999.0) and `volume_fp` (777.0) are
    deliberately different so a wrong implementation that read the same
    key twice, or fell back to `venue_volume`, is caught rather than
    coincidentally matching.
    """
    fixed_now = datetime(2026, 9, 7, 12, 30, 0, tzinfo=UTC)
    monkeypatch.setattr(dc, "utcnow", lambda: fixed_now)

    fee = FeeSchedule(
        taker_rate=0.07, maker_rate=0.0175, source="settings", maker_rebate_rate=0.0
    )
    market = make_venue_market(
        venue="kalshi",
        market_id="KALSHI-LIFETIME",
        outcomes=("YES",),
        raw={
            "yes_bid_dollars": "0.40",
            "yes_ask_dollars": "0.55",
            "volume_24h_fp": "999.0",
            "volume_fp": "777.0",
        },
        fee=fee,
    )
    adapter = (
        FixtureAdapter("kalshi")
        .add_market(market)
        .set_book(
            make_book(
                bids=[(0.40, 10.0)], asks=[(0.55, 10.0)],
                venue="kalshi", market_id="KALSHI-LIFETIME", outcome="YES",
            )
        )
    )
    market_ids_per_venue: dict[VenueId, list[str]] = {"kalshi": ["KALSHI-LIFETIME"]}

    collector = DataCollector(test_session)
    written = await collector.collect_books(
        {"kalshi": adapter}, market_ids_per_venue, test_session
    )
    assert written == 1

    row = (
        await test_session.execute(
            select(BookSnapshot).where(BookSnapshot.market_id == "KALSHI-LIFETIME")
        )
    ).scalar_one()
    assert row.volume == 999.0  # unaffected -- still the 24h ranking figure
    assert row.volume_lifetime == 777.0  # the LIFETIME counter, a distinct key
    assert row.volume_lifetime != row.volume
    assert row.observed_at.replace(tzinfo=UTC) == fixed_now
    assert row.fee_source == "settings"
    assert row.maker_fee_rate == 0.0175
    assert row.maker_fee_rate != row.taker_fee_rate


@pytest.mark.asyncio
async def test_decreasing_lifetime_volume_on_a_same_ts_refresh_yields_none(
    test_session: AsyncSession,
) -> None:
    """Migration `008`'s monotonicity guard. Measured live: Gamma's
    (Polymarket's) lifetime `volumeNum` decreased for 29 of 255 markets
    over ~28 minutes -- the counter itself gets restated, not "goes
    backwards". `_upsert_book_snapshot` must never write the smaller
    reading, and never `0.0` or a negative in its place --
    `passive_fill.py:142` treats `0.0` as a hard "no trading" gate and
    `:97` raises `ValueError` on a negative -- so a restated counter can
    only be recorded as `None`.
    """
    ts = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)
    quiet_book = make_book(
        bids=[(0.40, 10.0)], asks=[(0.55, 10.0)],
        market_id="PM-RESTATED", outcome="YES", ts=ts,
    )
    fee = FeeSchedule(taker_rate=0.04, maker_rate=0.0, source="venue_schedule")
    market_poll_1 = make_venue_market(
        venue="polymarket",
        market_id="PM-RESTATED",
        outcomes=("YES",),
        raw={"bestBid": 0.40, "bestAsk": 0.55, "volume24hr": 500.0, "volumeNum": 9000.0},
        fee=fee,
    )
    # `set_book` is called exactly once: the book itself never moves
    # between the two polls below, the "quiet Polymarket market" case
    # where a same-`ts` refresh (not a new row) is what happens.
    adapter = FixtureAdapter("polymarket").add_market(market_poll_1).set_book(quiet_book)
    market_ids_per_venue: dict[VenueId, list[str]] = {"polymarket": ["PM-RESTATED"]}

    collector = DataCollector(test_session)
    written_1 = await collector.collect_books(
        {"polymarket": adapter}, market_ids_per_venue, test_session
    )
    assert written_1 == 1

    row = (
        await test_session.execute(
            select(BookSnapshot).where(BookSnapshot.market_id == "PM-RESTATED")
        )
    ).scalar_one()
    assert row.volume_lifetime == 9000.0

    # Poll 2: SAME ts (the book has not moved) but the venue's lifetime
    # counter is now LOWER than what was already recorded -- a restatement.
    market_poll_2 = make_venue_market(
        venue="polymarket",
        market_id="PM-RESTATED",
        outcomes=("YES",),
        raw={"bestBid": 0.40, "bestAsk": 0.55, "volume24hr": 500.0, "volumeNum": 100.0},
        fee=fee,
    )
    adapter.add_market(market_poll_2)

    written_2 = await collector.collect_books(
        {"polymarket": adapter}, market_ids_per_venue, test_session
    )
    assert written_2 == 0  # same natural key -- refreshed, not a new row

    row_after = (
        await test_session.execute(
            select(BookSnapshot).where(BookSnapshot.market_id == "PM-RESTATED")
        )
    ).scalar_one()
    assert row_after.id == row.id
    # Unknown, never the smaller reading, never 0.0, never negative.
    assert row_after.volume_lifetime is None


@pytest.mark.asyncio
async def test_decreasing_lifetime_volume_across_separate_polls_also_yields_none(
    test_session: AsyncSession,
) -> None:
    """Companion to the same-`ts` test above: `KalshiAdapter.get_book`
    stamps `ts=utcnow()` on every poll, so a Kalshi market practically
    ALWAYS takes the INSERT branch, never a same-`ts` refresh -- a guard
    that only compared a row against its own prior value would never
    fire for Kalshi at all, which is the venue finding F1's 24h-vs-
    lifetime measurement was made on. This proves the guard also holds
    across two separate ROWS for the same `(venue, market_id, outcome)`,
    via `_last_known_lifetime_volume`.
    """
    ts_1 = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)
    ts_2 = datetime(2026, 9, 7, 12, 1, 0, tzinfo=UTC)
    fee = FeeSchedule(taker_rate=0.07, maker_rate=0.0175, source="settings")
    market_poll_1 = make_venue_market(
        venue="kalshi",
        market_id="KALSHI-RESTATED",
        outcomes=("YES",),
        raw={
            "yes_bid_dollars": "0.40",
            "yes_ask_dollars": "0.55",
            "volume_24h_fp": "200.0",
            "volume_fp": "5000.0",
        },
        fee=fee,
    )
    adapter = (
        FixtureAdapter("kalshi")
        .add_market(market_poll_1)
        .set_book(
            make_book(
                bids=[(0.40, 10.0)], asks=[(0.55, 10.0)],
                venue="kalshi", market_id="KALSHI-RESTATED", outcome="YES", ts=ts_1,
            )
        )
    )
    market_ids_per_venue: dict[VenueId, list[str]] = {"kalshi": ["KALSHI-RESTATED"]}
    collector = DataCollector(test_session)

    assert (
        await collector.collect_books(
            {"kalshi": adapter}, market_ids_per_venue, test_session
        )
        == 1
    )

    # Second poll: a NEW ts (as every real Kalshi poll produces), with a
    # LOWER lifetime counter than the first row's -- a restatement.
    market_poll_2 = make_venue_market(
        venue="kalshi",
        market_id="KALSHI-RESTATED",
        outcomes=("YES",),
        raw={
            "yes_bid_dollars": "0.41",
            "yes_ask_dollars": "0.56",
            "volume_24h_fp": "200.0",
            "volume_fp": "10.0",
        },
        fee=fee,
    )
    adapter.add_market(market_poll_2).set_book(
        make_book(
            bids=[(0.41, 10.0)], asks=[(0.56, 10.0)],
            venue="kalshi", market_id="KALSHI-RESTATED", outcome="YES", ts=ts_2,
        )
    )
    assert (
        await collector.collect_books(
            {"kalshi": adapter}, market_ids_per_venue, test_session
        )
        == 1
    )

    rows = (
        (
            await test_session.execute(
                select(BookSnapshot)
                .where(BookSnapshot.market_id == "KALSHI-RESTATED")
                .order_by(BookSnapshot.ts)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2
    assert rows[0].volume_lifetime == 5000.0
    assert rows[1].volume_lifetime is None  # restated -- never 10.0, never negative


@pytest.mark.asyncio
async def test_observed_at_is_written_on_a_same_ts_refresh_not_only_on_insert(
    test_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`observed_at` must reflect THIS poll's wall clock even when
    nothing else about the row changed -- its whole job is answering
    "when did we last confirm this row is still current", which a
    content-gated write cannot answer for a market that is genuinely
    unchanged (same volume, same fee, same book) across many consecutive
    polls, the "quiet Polymarket market" case `_upsert_book_snapshot`'s
    docstring names.
    """
    ts = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)
    poll_1_at = datetime(2026, 9, 7, 12, 0, 5, tzinfo=UTC)
    poll_2_at = datetime(2026, 9, 7, 12, 1, 5, tzinfo=UTC)
    quiet_book = make_book(
        bids=[(0.40, 10.0)], asks=[(0.55, 10.0)],
        market_id="PM-QUIET-OBSERVED", outcome="YES", ts=ts,
    )
    fee = FeeSchedule(taker_rate=0.04, maker_rate=0.0, source="venue_schedule")
    market = make_venue_market(
        venue="polymarket",
        market_id="PM-QUIET-OBSERVED",
        outcomes=("YES",),
        raw={"bestBid": 0.40, "bestAsk": 0.55, "volume24hr": 500.0},
        fee=fee,
    )
    adapter = FixtureAdapter("polymarket").add_market(market).set_book(quiet_book)
    market_ids_per_venue: dict[VenueId, list[str]] = {"polymarket": ["PM-QUIET-OBSERVED"]}
    collector = DataCollector(test_session)

    monkeypatch.setattr(dc, "utcnow", lambda: poll_1_at)
    assert (
        await collector.collect_books(
            {"polymarket": adapter}, market_ids_per_venue, test_session
        )
        == 1
    )
    row = (
        await test_session.execute(
            select(BookSnapshot).where(BookSnapshot.market_id == "PM-QUIET-OBSERVED")
        )
    ).scalar_one()
    assert row.observed_at.replace(tzinfo=UTC) == poll_1_at

    # Poll 2: the SAME ts, SAME volume, SAME fee -- a genuinely quiet
    # book, exactly the case a content-gated write would leave
    # `observed_at` stuck at poll 1 forever.
    monkeypatch.setattr(dc, "utcnow", lambda: poll_2_at)
    written_2 = await collector.collect_books(
        {"polymarket": adapter}, market_ids_per_venue, test_session
    )
    assert written_2 == 0  # same natural key -- refreshed, not a new row

    row_after = (
        await test_session.execute(
            select(BookSnapshot).where(BookSnapshot.market_id == "PM-QUIET-OBSERVED")
        )
    ).scalar_one()
    assert row_after.id == row.id  # the SAME row, updated in place
    assert row_after.observed_at.replace(tzinfo=UTC) == poll_2_at  # NOT stuck at poll_1_at
    assert row_after.observed_at.replace(tzinfo=UTC) != poll_1_at
    assert row_after.volume == 500.0  # content genuinely unchanged
    assert row_after.taker_fee_rate == 0.04  # content genuinely unchanged


# ---------------------------------------------------------------------------
# mm-proveout T16 part (a) (Phase 2 review): `collect_books` used to
# reduce every candidate to a bare market id and re-fetch it with
# `get_market`, even when the caller (the beat, `app.tasks.collection.
# run_collect_books`) already had a fully-built `VenueMarket` in hand
# from its own `list_markets` walk. Measured live 2026-09-07: Kalshi's
# listing walk returned 99,588 open markets in 10.46s while `get_market`
# averaged 0.096s/call -- re-fetching every candidate cost ~2.66 HOURS
# per tick against a 60-second beat. `collect_books` now accepts EITHER
# a bare id (resolved via `get_market`, kept for backward compatibility)
# or an already-built `VenueMarket` (used as-is, zero `get_market` calls)
# in the same candidate sequence.
# ---------------------------------------------------------------------------


class _CountingGetMarketAdapter(FixtureAdapter):
    """`FixtureAdapter` that counts `get_market` calls, so a test can
    prove `collect_books` made ZERO of them for a pre-fetched candidate."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.get_market_calls = 0

    async def get_market(self, market_id: str) -> VenueMarket:
        self.get_market_calls += 1
        return await super().get_market(market_id)


@pytest.mark.asyncio
async def test_collect_books_makes_no_get_market_call_for_an_already_fetched_candidate(
    test_session: AsyncSession,
) -> None:
    """A candidate that IS ALREADY a `VenueMarket` (what the beat passes,
    having paid for the listing walk already) costs zero `get_market`
    calls -- it is used as-is, not re-fetched."""
    market = make_venue_market(
        venue="polymarket",
        market_id="PM-PREFETCHED",
        outcomes=("YES",),
        raw={"bestBid": 0.40, "bestAsk": 0.55, "volume24hr": 500.0},
    )
    adapter = (
        _CountingGetMarketAdapter("polymarket")
        .add_market(market)
        .set_book(
            make_book(
                bids=[(0.40, 10.0)], asks=[(0.55, 10.0)],
                market_id="PM-PREFETCHED", outcome="YES",
            )
        )
    )
    collector = DataCollector(test_session)

    written = await collector.collect_books(
        {"polymarket": adapter}, {"polymarket": [market]}, test_session
    )

    assert written == 1
    assert adapter.get_market_calls == 0
    rows = (await test_session.execute(select(BookSnapshot))).scalars().all()
    assert [row.market_id for row in rows] == ["PM-PREFETCHED"]


@pytest.mark.asyncio
async def test_collect_books_still_resolves_a_bare_string_id_via_get_market(
    test_session: AsyncSession,
) -> None:
    """The backward-compatible half of the same change: a bare market id
    (what `app.scripts.collect_prices.collect_books_once`, the manual CLI
    path, and every other test in this file pass) still costs exactly
    ONE `get_market` call -- the id-based path is not removed, only no
    longer what the beat uses."""
    market = make_venue_market(
        venue="polymarket",
        market_id="PM-BY-ID",
        outcomes=("YES",),
        raw={"bestBid": 0.40, "bestAsk": 0.55, "volume24hr": 500.0},
    )
    adapter = (
        _CountingGetMarketAdapter("polymarket")
        .add_market(market)
        .set_book(
            make_book(
                bids=[(0.40, 10.0)], asks=[(0.55, 10.0)],
                market_id="PM-BY-ID", outcome="YES",
            )
        )
    )
    collector = DataCollector(test_session)

    written = await collector.collect_books(
        {"polymarket": adapter}, {"polymarket": ["PM-BY-ID"]}, test_session
    )

    assert written == 1
    assert adapter.get_market_calls == 1


# ---------------------------------------------------------------------------
# mm-proveout T16 part (b) (Phase 2 review): a routine 404 must cost ONE
# candidate, not the whole venue. Confirmed live 2026-09-07: 3 of 5 real
# tickers sampled from Kalshi's own open listing returned 404 on
# `GET /markets/{ticker}`. `app.venues.kalshi.adapter.
# raise_for_venue_error` deliberately lets a 404 (and every status but
# 429/401/403) fall through to `httpx.Response.raise_for_status()`, which
# raises `httpx.HTTPStatusError` -- NOT a `VenueError` subclass, per that
# function's own docstring, and that mapping is explicitly not this
# task's to change. Before this fix `collect_books`'s two per-candidate
# catches were `except VenueError` only, so this fault escaped to the
# venue-level bare `except Exception` several lines down and cost the
# ENTIRE venue's tick -- every other candidate, and every already-
# selected market's book, lost along with the one delisted ticker.
# ---------------------------------------------------------------------------

#: A hand-built request/response pair -- the `httpx` exception raised
#: below is the REAL exception type with a REAL status attached, without
#: any transport, socket, or venue being touched (GUARDRAILS.md §1.4).
_FAKE_REQUEST = httpx.Request("GET", "https://kalshi.example.test/markets/x")


def _a_404() -> httpx.HTTPStatusError:
    """The exception `raise_for_venue_error` raises for a 404 -- the SAME
    exception type `httpx.Response.raise_for_status()` raises for any
    status besides 429/401/403, which is what that function deliberately
    lets through."""
    response = httpx.Response(404, request=_FAKE_REQUEST, json={"error": "not_found"})
    return httpx.HTTPStatusError(
        "Client error '404 Not Found'", request=_FAKE_REQUEST, response=response
    )


class _FailOnOneMarketAdapter(FixtureAdapter):
    """`get_market` raises `httpx.HTTPStatusError` for exactly ONE market
    id (simulating a delisted/settled ticker); every other id resolves
    normally."""

    def __init__(self, *args: Any, fail_on: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._fail_on = fail_on

    async def get_market(self, market_id: str) -> VenueMarket:
        if market_id == self._fail_on:
            raise _a_404()
        return await super().get_market(market_id)


class _FailOnOneBookAdapter(FixtureAdapter):
    """`get_book` raises `httpx.HTTPStatusError` for exactly ONE
    `(market_id, outcome)`; every other pair resolves normally."""

    def __init__(self, *args: Any, fail_on: tuple[str, str], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._fail_on = fail_on

    async def get_book(self, market_id: str, outcome: str) -> OrderBook:
        if (market_id, outcome) == self._fail_on:
            raise _a_404()
        return await super().get_book(market_id, outcome)


@pytest.mark.asyncio
async def test_an_http_status_error_resolving_one_candidate_costs_only_that_candidate(
    test_session: AsyncSession,
) -> None:
    """The candidate that 404s on `get_market` is skipped; the other two
    candidates in the SAME venue are still resolved, selected, and
    written."""
    ok_1 = make_venue_market(
        venue="kalshi",
        market_id="KALSHI-OK-1",
        outcomes=("YES",),
        raw={"yes_bid_dollars": "0.40", "yes_ask_dollars": "0.55", "volume_24h_fp": "500"},
    )
    ok_2 = make_venue_market(
        venue="kalshi",
        market_id="KALSHI-OK-2",
        outcomes=("YES",),
        raw={"yes_bid_dollars": "0.40", "yes_ask_dollars": "0.55", "volume_24h_fp": "500"},
    )
    adapter = (
        _FailOnOneMarketAdapter("kalshi", fail_on="KALSHI-404")
        .add_market(ok_1)
        .add_market(ok_2)
        .set_book(make_book(bids=[(0.40, 10.0)], asks=[(0.55, 10.0)], venue="kalshi", market_id="KALSHI-OK-1", outcome="YES"))
        .set_book(make_book(bids=[(0.40, 10.0)], asks=[(0.55, 10.0)], venue="kalshi", market_id="KALSHI-OK-2", outcome="YES"))
    )
    collector = DataCollector(test_session)

    written = await collector.collect_books(
        {"kalshi": adapter},
        {"kalshi": ["KALSHI-404", "KALSHI-OK-1", "KALSHI-OK-2"]},
        test_session,
    )

    assert written == 2
    rows = (await test_session.execute(select(BookSnapshot))).scalars().all()
    assert {row.market_id for row in rows} == {"KALSHI-OK-1", "KALSHI-OK-2"}


@pytest.mark.asyncio
async def test_an_http_status_error_fetching_one_book_costs_only_that_book(
    test_session: AsyncSession,
) -> None:
    """Same fault, at the OTHER per-candidate call site: a market that
    resolves and is selected fine, but whose `get_book` call 404s (e.g.
    delisted between the listing walk and this tick's book fetch)."""
    ok = make_venue_market(
        venue="kalshi",
        market_id="KALSHI-BOOK-OK",
        outcomes=("YES",),
        raw={"yes_bid_dollars": "0.40", "yes_ask_dollars": "0.55", "volume_24h_fp": "500"},
    )
    fails = make_venue_market(
        venue="kalshi",
        market_id="KALSHI-BOOK-404",
        outcomes=("YES",),
        raw={"yes_bid_dollars": "0.40", "yes_ask_dollars": "0.55", "volume_24h_fp": "600"},
    )
    adapter = (
        _FailOnOneBookAdapter("kalshi", fail_on=("KALSHI-BOOK-404", "YES"))
        .add_market(ok)
        .add_market(fails)
        .set_book(make_book(bids=[(0.40, 10.0)], asks=[(0.55, 10.0)], venue="kalshi", market_id="KALSHI-BOOK-OK", outcome="YES"))
    )
    collector = DataCollector(test_session)

    written = await collector.collect_books(
        {"kalshi": adapter},
        {"kalshi": [ok, fails]},
        test_session,
    )

    assert written == 1
    rows = (await test_session.execute(select(BookSnapshot))).scalars().all()
    assert [row.market_id for row in rows] == ["KALSHI-BOOK-OK"]


# ---------------------------------------------------------------------------
# mm-proveout T16 part (c) (Phase 2 review): persist which markets were
# SELECTED per tick, so a later reader (T11) can tell "we stopped
# selecting this market" (a `SelectionMembership` row now shows
# `selected=False`) from "the collector broke" (no such transition is
# recorded). Measured on Kalshi over 5 minutes: `quotable` held at 684,
# `selected` held at 500, but 20 markets (4.0%) left the selected set and
# 20 entered -- every departure was a lost quotability, none were pushed
# out by the `book_collection_top_n` cap.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_collect_books_records_selected_markets_as_selected(
    test_session: AsyncSession,
) -> None:
    """Every market in this tick's `selected` set gets a `SelectionMembership`
    row with `selected=True` and `last_selected_at` set; a market that
    failed quotability (never reached `selected`) gets none at all."""
    quoted = make_venue_market(
        venue="polymarket",
        market_id="PM-SEL-QUOTED",
        outcomes=("YES",),
        raw={"bestBid": 0.40, "bestAsk": 0.55, "volume24hr": 500.0},
    )
    rejected = make_venue_market(
        venue="polymarket",
        market_id="PM-SEL-REJECTED",
        outcomes=("YES",),
        raw={"bestBid": 0.49, "bestAsk": 0.51, "volume24hr": 500.0},  # spread 0.02
    )
    adapter = (
        FixtureAdapter("polymarket")
        .add_market(quoted)
        .add_market(rejected)
        .set_book(make_book(bids=[(0.40, 10.0)], asks=[(0.55, 10.0)], market_id="PM-SEL-QUOTED", outcome="YES"))
    )
    collector = DataCollector(test_session)

    await collector.collect_books(
        {"polymarket": adapter}, {"polymarket": [quoted, rejected]}, test_session
    )

    rows = (await test_session.execute(select(SelectionMembership))).scalars().all()
    assert [(row.venue, row.market_id, row.selected) for row in rows] == [
        ("polymarket", "PM-SEL-QUOTED", True)
    ]
    row = rows[0]
    assert row.last_selected_at is not None
    assert row.last_deselected_at is None


@pytest.mark.asyncio
async def test_a_market_that_leaves_the_selected_set_is_marked_deselected(
    test_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A market selected on tick 1 and NOT a candidate on tick 2 (it lost
    quotability, exactly the measured Kalshi finding) transitions to
    `selected=False` with `last_deselected_at` set on tick 2 -- while a
    market selected on BOTH ticks stays `selected=True` and its
    `last_selected_at` advances to the later tick."""
    stays = make_venue_market(
        venue="polymarket",
        market_id="PM-STAYS-SELECTED",
        outcomes=("YES",),
        raw={"bestBid": 0.40, "bestAsk": 0.55, "volume24hr": 500.0},
    )
    leaves = make_venue_market(
        venue="polymarket",
        market_id="PM-LEAVES-SELECTED",
        outcomes=("YES",),
        raw={"bestBid": 0.40, "bestAsk": 0.55, "volume24hr": 400.0},
    )
    adapter = (
        FixtureAdapter("polymarket")
        .add_market(stays)
        .add_market(leaves)
        .set_book(make_book(bids=[(0.40, 10.0)], asks=[(0.55, 10.0)], market_id="PM-STAYS-SELECTED", outcome="YES"))
        .set_book(make_book(bids=[(0.40, 10.0)], asks=[(0.55, 10.0)], market_id="PM-LEAVES-SELECTED", outcome="YES"))
    )
    collector = DataCollector(test_session)

    tick_1_at = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)
    tick_2_at = datetime(2026, 9, 7, 12, 1, 0, tzinfo=UTC)

    monkeypatch.setattr(dc, "utcnow", lambda: tick_1_at)
    await collector.collect_books(
        {"polymarket": adapter}, {"polymarket": [stays, leaves]}, test_session
    )

    # Tick 2: `leaves` is no longer even a candidate (it lost quotability
    # upstream and the caller's listing no longer includes it) -- the
    # same shape as the measured Kalshi finding, where a departure was a
    # lost quotability, never a candidate `collect_books` still saw and
    # rejected.
    monkeypatch.setattr(dc, "utcnow", lambda: tick_2_at)
    await collector.collect_books(
        {"polymarket": adapter}, {"polymarket": [stays]}, test_session
    )

    rows = {
        row.market_id: row
        for row in (
            await test_session.execute(select(SelectionMembership))
        ).scalars().all()
    }
    assert set(rows) == {"PM-STAYS-SELECTED", "PM-LEAVES-SELECTED"}

    stays_row = rows["PM-STAYS-SELECTED"]
    assert stays_row.selected is True
    assert stays_row.last_selected_at.replace(tzinfo=UTC) == tick_2_at
    assert stays_row.last_deselected_at is None

    leaves_row = rows["PM-LEAVES-SELECTED"]
    assert leaves_row.selected is False
    assert leaves_row.last_selected_at.replace(tzinfo=UTC) == tick_1_at
    assert leaves_row.last_deselected_at.replace(tzinfo=UTC) == tick_2_at


@pytest.mark.asyncio
async def test_selection_membership_round_trips_through_a_fresh_session(
    test_engine: AsyncEngine, test_session: AsyncSession
) -> None:
    """The persistence primitive itself: a `SelectionMembership` row
    written by one session is readable, with every field intact, from a
    completely separate session/identity map bound to the same engine --
    the same durability bar `BookSnapshot`'s own round-trip tests hold
    themselves to."""
    now = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)
    test_session.add(
        SelectionMembership(
            venue="kalshi",
            market_id="KALSHI-ROUNDTRIP",
            selected=True,
            last_selected_at=now,
            last_deselected_at=None,
        )
    )
    await test_session.commit()

    fresh_sessions = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with fresh_sessions() as fresh_session:
        row = (
            await fresh_session.execute(
                select(SelectionMembership).where(
                    SelectionMembership.market_id == "KALSHI-ROUNDTRIP"
                )
            )
        ).scalar_one()

    assert row.venue == "kalshi"
    assert row.market_id == "KALSHI-ROUNDTRIP"
    assert row.selected is True
    assert row.last_selected_at.replace(tzinfo=UTC) == now
    assert row.last_deselected_at is None
