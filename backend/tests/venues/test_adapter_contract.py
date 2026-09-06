"""The shared `VenueAdapter` contract, run against BOTH venues (T12).

PLAN.md §1 "Done is checkable", item 4: `app/venues/` contains
`VenueAdapter` + a Polymarket and a Kalshi adapter, and "both pass the
shared adapter contract test suite on recorded fixtures". This file is
that suite.

Each case is parametrized over both adapters, each wired to its own
`httpx.MockTransport` over its own hand-written fixtures (GUARDRAILS.md
§1.4: no network to a venue, ever, from a test). The transports and
fixtures are reused from each venue's own test module rather than
duplicated, so the contract is checked against exactly the payloads that
venue's tests describe.

What "the contract" means here -- the venue-agnostic promises every
strategy and the execution router are entitled to rely on, independent
of which venue answered:

  * `list_markets` returns `VenueMarket`s whose `close_time` is an AWARE
    UTC datetime (a naive one is a defect, not an environment quirk --
    GUARDRAILS.md §4; time-to-resolution is in every score, PLAN.md §6).
  * every market carries a NON-EMPTY `FeeSchedule.source`. Provenance is
    behavioural downstream: T07's fill engine decides whether a zero fee
    was declared on purpose by reading exactly this string.
  * `get_book` returns a sorted, validated, `"recorded"`-tagged book,
    with every price a probability in `[0,1]` and every size >= 0.
  * an uncrossed venue book stays uncrossed after normalization
    (`best_bid <= best_ask`). For Kalshi that is a real conversion check
    -- its asks are DERIVED from the opposite side's bids -- and it
    matters because T07 silently DECLINES a crossed book rather than
    raising.
  * `stream_books` raises `NotImplementedError` rather than faking a
    stream (PLAN.md §2: REST polling only in this kit).
"""
from typing import Any

import pytest

from app.venues.base import FeeModel
from app.venues.polymarket.adapter import PolymarketAdapter
from app.venues.types import OrderBook, VenueMarket
from tests.venues.test_kalshi_adapter import ALPHA as KALSHI_ALPHA
from tests.venues.test_kalshi_adapter import make_adapter as make_kalshi_adapter
from tests.venues.test_polymarket_adapter import MARKET_A001, _make_transport


def _polymarket_case() -> tuple[Any, str, str]:
    return PolymarketAdapter(transport=_make_transport()), MARKET_A001, "Yes"


def _kalshi_case() -> tuple[Any, str, str]:
    return make_kalshi_adapter(), KALSHI_ALPHA, "YES"


#: `(id, factory)` -- each factory returns
#: `(adapter, market_id, outcome)` on fixture data.
VENUE_CASES = [
    pytest.param(_polymarket_case, id="polymarket"),
    pytest.param(_kalshi_case, id="kalshi"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", VENUE_CASES)
async def test_list_markets_returns_venue_markets_with_aware_close_times(
    case: Any,
) -> None:
    """Every listed market is a `VenueMarket` with an aware UTC `close_time`."""
    adapter, _, _ = case()

    markets = await adapter.list_markets()

    assert markets, "fixture set must contain at least one market"
    for market in markets:
        assert isinstance(market, VenueMarket)
        assert market.venue == adapter.venue
        assert market.close_time.tzinfo is not None
        assert market.close_time.utcoffset() is not None
        if market.expected_settle_time is not None:
            assert market.expected_settle_time.utcoffset() is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("case", VENUE_CASES)
async def test_every_market_declares_where_its_fee_came_from(case: Any) -> None:
    """`FeeSchedule.source` is non-empty on every market, on every venue."""
    adapter, _, _ = case()

    markets = await adapter.list_markets()

    for market in markets:
        assert market.fee.source
        assert market.fee.taker_rate >= 0.0
        assert market.fee.maker_rate >= 0.0


@pytest.mark.asyncio
@pytest.mark.parametrize("case", VENUE_CASES)
async def test_market_constraints_are_usable_for_sizing(case: Any) -> None:
    """`tick_size` is a real increment and `min_size` is non-negative.

    T07's fill engine reads both off `VenueMarket` (they are deliberately
    NOT on `OrderBook`), so a zero or nonsensical tick would break tick
    validation for every order on that venue.
    """
    adapter, _, _ = case()

    for market in await adapter.list_markets():
        assert 0.0 < market.tick_size <= 1.0
        assert market.min_size >= 0.0
        assert market.outcomes
        assert market.status in ("open", "closed", "resolved")


@pytest.mark.asyncio
@pytest.mark.parametrize("case", VENUE_CASES)
async def test_get_book_returns_a_sorted_validated_recorded_book(case: Any) -> None:
    """Books are normalized identically no matter which venue produced them."""
    adapter, market_id, outcome = case()

    book = await adapter.get_book(market_id, outcome)

    assert isinstance(book, OrderBook)
    assert book.venue == adapter.venue
    assert book.market_id == market_id
    assert book.ts.utcoffset() is not None
    # A book parsed from a real venue payload is observed depth, and must
    # stay labeled that way (GUARDRAILS.md §1.7).
    assert book.depth_source == "recorded"
    bid_prices = [lvl.price for lvl in book.bids]
    ask_prices = [lvl.price for lvl in book.asks]
    assert bid_prices == sorted(bid_prices, reverse=True)
    assert ask_prices == sorted(ask_prices)
    for level in (*book.bids, *book.asks):
        assert 0.0 <= level.price <= 1.0
        assert level.size >= 0.0


@pytest.mark.asyncio
@pytest.mark.parametrize("case", VENUE_CASES)
async def test_a_normal_book_is_not_crossed(case: Any) -> None:
    """`best_bid <= best_ask` on an uncrossed fixture, on both venues.

    Polymarket sends both sides of the book; Kalshi sends BIDS ONLY and
    the asks are derived as `1 - opposite_bid`. Inverting that derivation
    would cross every Kalshi book and fabricate an arbitrage against
    Polymarket on every quote -- and would fail QUIETLY, because T07's
    fill engine declines a crossed book with `reason="crossed_book"`
    rather than raising.
    """
    adapter, market_id, outcome = case()

    book = await adapter.get_book(market_id, outcome)

    best_bid, best_ask = book.best_bid(), book.best_ask()
    assert best_bid is not None and best_ask is not None
    assert best_bid.price <= best_ask.price


@pytest.mark.asyncio
@pytest.mark.parametrize("case", VENUE_CASES)
async def test_walking_the_book_never_over_fills(case: Any) -> None:
    """`walk` returns at most the requested size, on both venues."""
    adapter, market_id, outcome = case()

    book = await adapter.get_book(market_id, outcome)
    taken = book.walk("buy", 50.0)

    assert sum(size for _, size in taken) <= 50.0 + 1e-9


@pytest.mark.parametrize("case", VENUE_CASES)
def test_fee_model_is_a_fee_model(case: Any) -> None:
    """Every adapter answers with a real `FeeModel` producing a sane fee."""
    adapter, _, _ = case()

    model = adapter.fee_model()

    assert isinstance(model, FeeModel)
    from app.venues.types import FeeSchedule

    fee = model.fee(
        price=0.5,
        size_contracts=10.0,
        liquidity="taker",
        schedule=FeeSchedule(taker_rate=0.05, maker_rate=0.0, source="contract_test"),
    )
    assert fee >= 0.0


@pytest.mark.parametrize("case", VENUE_CASES)
def test_stream_books_is_not_faked(case: Any) -> None:
    """PLAN.md §2: no WebSocket in this kit -- the slot raises, it does not lie."""
    adapter, market_id, outcome = case()

    with pytest.raises(NotImplementedError):
        adapter.stream_books([(market_id, outcome)])
