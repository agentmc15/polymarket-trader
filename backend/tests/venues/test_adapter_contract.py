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

T44 added a second half to the contract, about what happens when a real
payload DISAGREES with the shape each adapter was written against. These
are cross-venue promises because the failure they prevent is cross-venue:
an adapter that answers a divergent payload with a plausible-looking
wrong number, in silence, is the same defect whichever venue sent it.

  * a market payload whose values the domain types reject raises a
    `VenueError`, never a bare `ValueError` (which is not in
    `app.services.scanner.VENUE_READ_FAULTS`, so it would abort a scan
    pass instead of skipping that venue).
  * a balance payload carrying no amount raises, rather than reporting a
    funded account as `$0.00`.
  * a page of orders NONE of which parse raises, rather than reporting
    "nothing is resting" -- which `app.execution.reconcile` acts on.
  * an unknown extra key is TOLERATED, on markets and on books. Venues
    add fields; that must never be an outage, and this half of the
    contract is what stops a later hardening pass from making it one.
"""
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

from app.venues.base import FeeModel, VenueError, VenuePayloadError
from app.venues.polymarket.adapter import PolymarketAdapter
from app.venues.types import OrderBook, VenueMarket
from tests.venues import test_kalshi_adapter as kalshi_tests
from tests.venues import test_polymarket_adapter as polymarket_tests
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


@dataclass(frozen=True)
class DivergenceCase:
    """One venue's builders for the T44 half of the contract.

    Each builder answers the SAME question in that venue's own payload
    vocabulary -- Polymarket's minimum order size arrives on the CLOB
    market payload and Kalshi's on the market itself, so the shared test
    asks for "an adapter whose market payload the domain type rejects"
    rather than for a literal field. `monkeypatch` is accepted by every
    builder because Polymarket's credentialed reads go through
    `py_clob_client` and are stubbed out (no network, no real key -- see
    `tests.venues.test_polymarket_adapter.credentialed_adapter`);
    Kalshi's go over its `httpx.MockTransport` and ignore it.

    Attributes:
        market_id: A market id present in that venue's happy fixtures.
        outcome: An outcome name valid on that market.
        bad_market: Adapter whose market payload carries a value
            `VenueMarket` rejects.
        bad_balance: Adapter whose balance payload carries no amount.
        unparseable_orders: Adapter whose open orders none of them parse.
        extra_keys: Adapter whose market and book payloads carry unknown
            extra fields.
    """

    market_id: str
    outcome: str
    bad_market: Callable[[pytest.MonkeyPatch], Any]
    bad_balance: Callable[[pytest.MonkeyPatch], Any]
    unparseable_orders: Callable[[pytest.MonkeyPatch], Any]
    extra_keys: Callable[[pytest.MonkeyPatch], Any]


DIVERGENCE_CASES = [
    pytest.param(
        DivergenceCase(
            market_id=MARKET_A001,
            outcome="Yes",
            bad_market=lambda _mp: (
                polymarket_tests.adapter_with_a_market_the_domain_type_rejects()
            ),
            bad_balance=(
                polymarket_tests.adapter_with_a_balance_payload_missing_its_amount
            ),
            unparseable_orders=polymarket_tests.adapter_with_orders_that_never_parse,
            extra_keys=lambda _mp: polymarket_tests.adapter_with_extra_unknown_keys(),
        ),
        id="polymarket",
    ),
    pytest.param(
        DivergenceCase(
            market_id=KALSHI_ALPHA,
            outcome="YES",
            bad_market=lambda _mp: (
                kalshi_tests.adapter_with_a_market_the_domain_type_rejects()
            ),
            bad_balance=lambda _mp: (
                kalshi_tests.adapter_with_a_balance_payload_missing_its_amount()
            ),
            unparseable_orders=lambda _mp: (
                kalshi_tests.adapter_with_orders_that_never_parse()
            ),
            extra_keys=lambda _mp: kalshi_tests.adapter_with_extra_unknown_keys(),
        ),
        id="kalshi",
    ),
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


# ---------------------------------------------------------------------------
# T44: the same promises about DIVERGENT payloads, on both venues.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("case", DIVERGENCE_CASES)
async def test_a_market_the_domain_types_reject_is_a_typed_venue_error(
    case: DivergenceCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An out-of-domain market is EXCLUDED from a listing, not fatal to it.

    `VenueMarket`'s validators name the offending field but raise a plain
    `ValueError`, which is not in `app.services.scanner.VENUE_READ_FAULTS`
    and would abort a whole pass as if it were a programming error. So it
    is still wrapped as a `VenuePayloadError` -- that half is unchanged.

    What changed is the BLAST RADIUS. This test used to assert that
    `list_markets()` raises, i.e. that one bad market costs the caller
    every other market from that venue. Running against the live Gamma
    API showed what that means in practice: 182 of ~2100 real markets
    carry no `endDate`, so a single one of them blanked the entire
    Polymarket listing on every scan. The listing now skips the market it
    cannot build, counts it, logs it, and returns the rest -- the same
    "one bad market must not blank the venue" property T38 gave book
    fetching.

    `get_market()` still raises, and that asymmetry is deliberate: asking
    for ONE market and receiving silence is the quiet failure this repo
    keeps finding, while asking "what is listed" and being told
    "everything that parsed, and here is the count that did not" is an
    honest answer.
    """
    adapter = case.bad_market(monkeypatch)

    markets = await adapter.list_markets()

    assert all(m.market_id != case.market_id for m in markets), (
        "the market whose payload the domain type rejects must be excluded"
    )

    with pytest.raises(VenueError) as excinfo:
        await adapter.get_market(case.market_id)

    assert isinstance(excinfo.value, VenuePayloadError)
    # The message must still say WHICH field was wrong -- the whole point
    # of the wrap is legibility, not just the type.
    assert "size" in str(excinfo.value) or "tick" in str(excinfo.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", DIVERGENCE_CASES)
async def test_a_balance_payload_with_no_amount_is_refused(
    case: DivergenceCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No amount field is an ERROR, never `$0.00`.

    A zero balance and a balance we could not read are the same number to
    every caller, and that number decides how much capital exists
    (GUARDRAILS.md §1.6: capital is per venue and never guessed).
    """
    adapter = case.bad_balance(monkeypatch)

    with pytest.raises(VenuePayloadError):
        await adapter.get_balance()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", DIVERGENCE_CASES)
async def test_a_page_of_orders_that_none_parse_is_not_reported_as_empty(
    case: DivergenceCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every row failing is a schema disagreement, not "nothing is resting".

    `app.execution.reconcile` treats a read that RAISED as "we do not
    know" and refuses to conclude anything from it, but treats a
    successful empty read as "the venue holds nothing" and acts on it.
    Silently dropping every unparseable order collapses the first case
    into the second, and reports every live order as gone.
    """
    adapter = case.unparseable_orders(monkeypatch)

    with pytest.raises(VenuePayloadError):
        await adapter.get_open_orders()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", DIVERGENCE_CASES)
async def test_an_unknown_extra_key_is_tolerated(
    case: DivergenceCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TOLERANCE PIN, on both venues, for markets AND books.

    This is the half of the contract that protects against overcorrection:
    venues add fields all the time, and an adapter that refuses a payload
    carrying one it has never heard of would fail on the first harmless
    addition -- an outage of its own, caused by the fix rather than the
    divergence.
    """
    adapter = case.extra_keys(monkeypatch)

    markets = await adapter.list_markets()
    book = await adapter.get_book(case.market_id, case.outcome)

    assert markets
    assert book.bids or book.asks
