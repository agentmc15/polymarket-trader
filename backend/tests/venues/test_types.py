"""Tests for `app.venues.types` (T04) and the two `make_snapshot` fixes
the P0 reviewer required alongside it (NOTES.md P0 finding #10).

Derived from TASKS.md T04's acceptance lines, not from the implementation:
  1. `OrderBook`/`BookLevel` validation rejects price 1.2 and naive `ts`.
  2. `walk()` consumes across levels and returns a partial fill when the
     book runs dry, without raising and without over-filling.
  3. `best_ask()` on an empty `asks` list is `None`.
"""
from datetime import UTC, datetime

import pytest

from app.venues.base import FeeModel, VenueAdapter, VenuePayloadError, VenueRateLimited
from app.venues.types import (
    Balance,
    BookLevel,
    FeeSchedule,
    Fill,
    OrderAck,
    OrderBook,
    OrderRequest,
    Position,
    VenueMarket,
)
from tests.helpers import make_book, make_snapshot

AWARE_TS = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
NAIVE_TS = datetime(2026, 1, 1, 12, 0, 0)


# ---------------------------------------------------------------------------
# Validation: prices in [0.0, 1.0]
# ---------------------------------------------------------------------------


def test_book_level_rejects_price_above_one() -> None:
    """A `BookLevel` price of 1.2 is not a probability and must be rejected."""
    with pytest.raises(ValueError, match=r"price"):
        BookLevel(price=1.2, size=100.0)


def test_book_level_rejects_negative_price() -> None:
    """A negative price is not a probability either."""
    with pytest.raises(ValueError, match=r"price"):
        BookLevel(price=-0.1, size=100.0)


def test_book_level_rejects_negative_size() -> None:
    """Sizes (contracts) must be >= 0."""
    with pytest.raises(ValueError, match=r"size"):
        BookLevel(price=0.5, size=-1.0)


def test_order_request_rejects_price_above_one() -> None:
    """`OrderRequest.price` shares the same [0.0, 1.0] validation."""
    with pytest.raises(ValueError, match=r"price"):
        OrderRequest(
            venue="polymarket",
            market_id="m1",
            outcome="YES",
            side="BUY",
            price=1.2,
            size=10.0,
            tif="GTC",
            client_order_id="c1",
        )


def test_fill_rejects_price_above_one() -> None:
    """`Fill.price` shares the same [0.0, 1.0] validation."""
    with pytest.raises(ValueError, match=r"price"):
        Fill(
            venue="polymarket",
            order_id="o1",
            price=1.2,
            size=10.0,
            fee=0.0,
            ts=AWARE_TS,
            liquidity="taker",
        )


def test_position_rejects_price_above_one() -> None:
    """`Position.avg_price` shares the same [0.0, 1.0] validation."""
    with pytest.raises(ValueError, match=r"price"):
        Position(venue="kalshi", market_id="m1", outcome="YES", size=10.0, avg_price=1.2)


# ---------------------------------------------------------------------------
# Validation: timestamps must be aware
# ---------------------------------------------------------------------------


def test_order_book_rejects_naive_timestamp() -> None:
    """A naive `ts` on `OrderBook` must raise, per `ensure_aware`."""
    with pytest.raises(ValueError, match=r"naive"):
        OrderBook(
            venue="polymarket",
            market_id="m1",
            outcome="YES",
            bids=[],
            asks=[],
            ts=NAIVE_TS,
        )


def test_fill_rejects_naive_timestamp() -> None:
    """A naive `ts` on `Fill` must raise, per `ensure_aware`."""
    with pytest.raises(ValueError, match=r"naive"):
        Fill(
            venue="polymarket",
            order_id="o1",
            price=0.5,
            size=10.0,
            fee=0.0,
            ts=NAIVE_TS,
            liquidity="taker",
        )


def test_venue_market_rejects_naive_close_time() -> None:
    """A naive `close_time` on `VenueMarket` must raise, per `ensure_aware`."""
    with pytest.raises(ValueError, match=r"naive"):
        _make_market(close_time=NAIVE_TS)


def test_order_ack_rejects_naive_timestamp() -> None:
    """A naive `ts` on `OrderAck` must raise, per `ensure_aware`."""
    with pytest.raises(ValueError, match=r"naive"):
        OrderAck(
            venue="polymarket",
            order_id="o1",
            client_order_id="c1",
            status="open",
            filled_size=0.0,
            remaining_size=10.0,
            avg_fill_price=None,
            ts=NAIVE_TS,
        )


# ---------------------------------------------------------------------------
# walk(): best-price-first, cross-level, partial-fill-on-dry-book
# ---------------------------------------------------------------------------


def test_walk_consumes_across_levels() -> None:
    """`walk("buy", 150)` on asks [(0.40,100),(0.41,100)] takes the whole
    first level (100) then 50 of the second (150 - 100 = 50), best price
    first — never touching the second level's remaining 50.
    """
    book = make_book(bids=[], asks=[(0.40, 100.0), (0.41, 100.0)])
    result = book.walk("buy", 150.0)
    assert result == [(0.40, 100.0), (0.41, 50.0)]
    # total size taken: 100 + 50 = 150 == requested size exactly
    assert sum(size for _, size in result) == 150.0


def test_walk_returns_partial_when_book_runs_dry() -> None:
    """`walk("buy", 150)` on a single-level ask book [(0.40,100)] can only
    return 100 total — it must NOT raise and must NOT fabricate size.
    """
    book = make_book(bids=[], asks=[(0.40, 100.0)])
    result = book.walk("buy", 150.0)
    assert result == [(0.40, 100.0)]
    assert sum(size for _, size in result) == 100.0  # dry after 100, not 150


def test_walk_sell_side_walks_bids() -> None:
    """`walk("sell", ...)` consumes `bids`, best (highest) price first."""
    book = make_book(bids=[(0.60, 50.0), (0.59, 100.0)], asks=[])
    result = book.walk("sell", 80.0)
    # 50 from the best bid (0.60), then 30 of the next (80 - 50 = 30)
    assert result == [(0.60, 50.0), (0.59, 30.0)]


def test_walk_never_returns_more_than_requested() -> None:
    """A book with more depth than requested returns exactly `size`, not
    the whole book.
    """
    book = make_book(bids=[], asks=[(0.40, 1000.0)])
    result = book.walk("buy", 10.0)
    assert result == [(0.40, 10.0)]


def test_walk_zero_size_returns_empty() -> None:
    """Requesting zero size walks nothing."""
    book = make_book(bids=[], asks=[(0.40, 100.0)])
    assert book.walk("buy", 0.0) == []


def test_walk_rejects_negative_size() -> None:
    """A negative size is nonsensical and must raise."""
    book = make_book(bids=[], asks=[(0.40, 100.0)])
    with pytest.raises(ValueError, match=r"size"):
        book.walk("buy", -1.0)


def test_walk_rejects_bad_side() -> None:
    """An invalid `side` must raise, not silently do nothing."""
    book = make_book(bids=[(0.4, 10.0)], asks=[(0.5, 10.0)])
    with pytest.raises(ValueError, match=r"side"):
        book.walk("hold", 10.0)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# best_bid / best_ask / mid / depth_at
# ---------------------------------------------------------------------------


def test_best_ask_of_empty_asks_is_none() -> None:
    """`best_ask()` on a book with no asks must be `None`, not raise or
    return a sentinel.
    """
    book = make_book(bids=[(0.4, 10.0)], asks=[])
    assert book.best_ask() is None


def test_best_bid_of_empty_bids_is_none() -> None:
    """`best_bid()` on a book with no bids must be `None`."""
    book = make_book(bids=[], asks=[(0.6, 10.0)])
    assert book.best_bid() is None


def test_best_bid_and_ask_return_top_of_book() -> None:
    """`best_bid`/`best_ask` return the FIRST (best) level, not e.g. the
    last or an average.
    """
    book = make_book(bids=[(0.45, 10.0), (0.44, 20.0)], asks=[(0.55, 5.0), (0.56, 30.0)])
    assert book.best_bid() == BookLevel(price=0.45, size=10.0)
    assert book.best_ask() == BookLevel(price=0.55, size=5.0)


def test_mid_is_none_when_one_side_empty() -> None:
    """A midpoint needs both sides; if either is empty, `mid()` is `None`."""
    assert make_book(bids=[], asks=[(0.5, 10.0)]).mid() is None
    assert make_book(bids=[(0.5, 10.0)], asks=[]).mid() is None


def test_mid_averages_best_bid_and_ask() -> None:
    """`mid()` is the simple average of the best bid and best ask prices."""
    book = make_book(bids=[(0.48, 10.0)], asks=[(0.52, 10.0)])
    # (0.48 + 0.52) / 2 = 0.50
    assert book.mid() == pytest.approx(0.50)


def test_depth_at_sums_marketable_ask_levels() -> None:
    """`depth_at(price, "buy")` sums all ask levels priced at or below
    `price` — the total a buyer willing to pay up to `price` could fill.
    """
    book = make_book(bids=[], asks=[(0.40, 100.0), (0.41, 50.0), (0.45, 20.0)])
    # levels at 0.40 and 0.41 are <= 0.41: 100 + 50 = 150
    assert book.depth_at(0.41, "buy") == 150.0


def test_depth_at_sums_marketable_bid_levels() -> None:
    """`depth_at(price, "sell")` sums all bid levels priced at or above
    `price` — the total a seller willing to accept down to `price` could
    fill.
    """
    book = make_book(bids=[(0.60, 50.0), (0.58, 40.0), (0.50, 10.0)], asks=[])
    # levels at 0.60 and 0.58 are >= 0.58: 50 + 40 = 90
    assert book.depth_at(0.58, "sell") == 90.0


# ---------------------------------------------------------------------------
# make_book returns a typed OrderBook
# ---------------------------------------------------------------------------


def test_make_book_returns_order_book_of_book_levels() -> None:
    """`make_book` must build a real `OrderBook`, not a plain dict, and its
    levels must be `BookLevel` instances (so `.price`/`.size` attribute
    access works, not `["price"]` dict indexing).
    """
    book = make_book(bids=[(0.4, 10.0)], asks=[(0.6, 20.0)])
    assert isinstance(book, OrderBook)
    assert isinstance(book.bids[0], BookLevel)
    assert book.bids[0].price == 0.4
    assert book.ts.tzinfo is not None


# ---------------------------------------------------------------------------
# Other T04 types construct and validate (imports, errors, protocol)
# ---------------------------------------------------------------------------


def _make_market(close_time: datetime = AWARE_TS, min_size: float = 1.0) -> VenueMarket:
    return VenueMarket(
        venue="polymarket",
        market_id="m1",
        event_id=None,
        question="Will it rain?",
        outcomes=["YES", "NO"],
        outcome_ids={"YES": "tok-yes", "NO": "tok-no"},
        rules_text="Resolves YES if it rains.",
        resolution_source=None,
        close_time=close_time,
        expected_settle_time=None,
        status="open",
        result=None,
        tick_size=0.01,
        min_size=min_size,
        fee=FeeSchedule(taker_rate=0.05, maker_rate=0.0, source="category_table"),
        raw={},
    )


def test_venue_market_constructs_with_valid_fields() -> None:
    """A well-formed `VenueMarket` constructs without raising."""
    market = _make_market()
    assert market.status == "open"
    assert market.fee.taker_rate == 0.05


def test_venue_market_rejects_zero_tick_size() -> None:
    """`tick_size` must be in (0.0, 1.0], not zero."""
    with pytest.raises(ValueError, match=r"tick_size"):
        VenueMarket(
            venue="kalshi",
            market_id="m1",
            event_id=None,
            question="q",
            outcomes=["YES", "NO"],
            outcome_ids={},
            rules_text="",
            resolution_source=None,
            close_time=AWARE_TS,
            expected_settle_time=None,
            status="open",
            result=None,
            tick_size=0.0,
            min_size=1.0,
            fee=FeeSchedule(taker_rate=0.07, maker_rate=0.0, source="settings_default"),
            raw={},
        )


def test_fee_schedule_rejects_negative_rate() -> None:
    """`FeeSchedule.taker_rate` must be >= 0."""
    with pytest.raises(ValueError, match=r"taker_rate"):
        FeeSchedule(taker_rate=-0.01, maker_rate=0.0, source="settings_default")


def test_fee_schedule_rejects_blank_source() -> None:
    """`FeeSchedule.source` must be non-empty (callers need provenance)."""
    with pytest.raises(ValueError, match=r"source"):
        FeeSchedule(taker_rate=0.05, maker_rate=0.0, source="")


def test_balance_rejects_negative_available() -> None:
    """`Balance.available` must be >= 0."""
    with pytest.raises(ValueError, match=r"available"):
        Balance(venue="polymarket", available=-1.0, locked=0.0)


def test_order_request_rejects_blank_client_order_id() -> None:
    """`client_order_id` is the idempotency key; it must be non-empty."""
    with pytest.raises(ValueError, match=r"client_order_id"):
        OrderRequest(
            venue="polymarket",
            market_id="m1",
            outcome="YES",
            side="BUY",
            price=0.5,
            size=10.0,
            tif="GTC",
            client_order_id="",
        )


def test_venue_errors_carry_their_documented_attributes() -> None:
    """`VenuePayloadError`/`VenueRateLimited` carry the extra attributes
    T04's brief names, on top of being normal exceptions.
    """
    payload_err = VenuePayloadError("bad book payload", raw={"asks": "not-a-list"})
    assert payload_err.raw == {"asks": "not-a-list"}

    rate_limited = VenueRateLimited("slow down", retry_after_s=1.5)
    assert rate_limited.retry_after_s == 1.5

    rate_limited_no_hint = VenueRateLimited("slow down")
    assert rate_limited_no_hint.retry_after_s is None


def test_import_line_from_acceptance_criterion_1() -> None:
    """Mirrors TASKS.md T04 acceptance line 1 as an in-process import
    check (the shell form is also run directly in the verify command).
    """
    assert OrderBook is not None
    assert BookLevel is not None
    assert VenueMarket is not None
    assert OrderRequest is not None
    assert Fill is not None
    assert VenueAdapter is not None
    assert FeeModel is not None


# ---------------------------------------------------------------------------
# make_snapshot: the two P0-reviewer-required trap fixes (NOTES.md #10)
# ---------------------------------------------------------------------------


def test_make_snapshot_default_is_arbitrage_neutral() -> None:
    """`make_snapshot(yes=0.6)` must NOT fabricate a complement edge: `no`
    now defaults to `1.0 - yes`, so `yes + no == 1.0` by default. Before
    this fix, `no` defaulted to a flat 0.5 regardless of `yes`, so
    `make_snapshot(yes=0.6)` produced `yes + no = 1.10` — a phantom 10%
    arbitrage edge that binary-complement tests (T06-T08) would have
    silently consumed as real.
    """
    snapshot = make_snapshot(yes=0.6)
    # 0.6 + (1.0 - 0.6) = 1.0 exactly, not 0.6 + 0.5 = 1.10
    assert snapshot.yes_price + snapshot.no_price == pytest.approx(1.0)

    default_snapshot = make_snapshot()
    assert default_snapshot.yes_price + default_snapshot.no_price == pytest.approx(1.0)


def test_make_snapshot_explicit_no_still_allows_complement_violation() -> None:
    """A test that wants a deliberate complement violation can still pass
    `no=` explicitly — the arbitrage-neutral default must not block that.
    """
    snapshot = make_snapshot(yes=0.6, no=0.5)
    # 0.6 + 0.5 = 1.10 - a deliberate 10% violation, opted into explicitly
    assert snapshot.yes_price + snapshot.no_price == pytest.approx(1.10)


def test_make_snapshot_extreme_yes_and_spread_stays_in_unit_interval() -> None:
    """`make_snapshot(yes=0.99, spread=0.05)` used to produce
    `yes_ask = 0.99 + 0.025 = 1.015`, a price `BookLevel`/`OrderBook`'s own
    validator would reject. `yes_ask`/`no_bid` etc. must now clamp to
    `[0.0, 1.0]`.
    """
    snapshot = make_snapshot(yes=0.99, spread=0.05)
    assert snapshot.yes_ask is not None
    assert snapshot.yes_bid is not None
    assert snapshot.no_ask is not None
    assert snapshot.no_bid is not None
    assert 0.0 <= snapshot.yes_ask <= 1.0
    assert 0.0 <= snapshot.yes_bid <= 1.0
    assert 0.0 <= snapshot.no_ask <= 1.0
    assert 0.0 <= snapshot.no_bid <= 1.0
    # yes_ask would have been 1.015 unclamped; clamping caps it at 1.0
    assert snapshot.yes_ask == pytest.approx(1.0)

    low_snapshot = make_snapshot(yes=0.01, spread=0.05)
    assert low_snapshot.yes_bid is not None
    # yes_bid would have been -0.015 unclamped; clamping floors it at 0.0
    assert low_snapshot.yes_bid == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# RETRY DEFECT 1: frozen is shallow -- mutating a collection field in place
# (not rebinding it) must fail, because a value validated once should never
# be able to grow depth/outcomes/ids afterward.
# ---------------------------------------------------------------------------


def test_order_book_bids_cannot_be_mutated_in_place() -> None:
    """`book.venue = ...` already raises (frozen dataclass), but
    `book.bids.append(...)` is a DIFFERENT bug: it mutates the list a
    field points to without rebinding the field at all. `bids` must be a
    genuinely immutable `tuple`, which has no `.append`.
    """
    book = make_book(bids=[(0.10, 5.0)], asks=[(0.20, 5.0)])
    assert isinstance(book.bids, tuple)
    with pytest.raises(AttributeError):
        book.bids.append(BookLevel(price=0.01, size=1e9))  # type: ignore[attr-defined]


def test_order_book_asks_cannot_be_mutated_in_place() -> None:
    """Same property as `bids`, mirrored for `asks`."""
    book = make_book(bids=[(0.10, 5.0)], asks=[(0.20, 5.0)])
    assert isinstance(book.asks, tuple)
    with pytest.raises(AttributeError):
        book.asks.append(BookLevel(price=0.01, size=1e9))  # type: ignore[attr-defined]


def test_venue_market_outcomes_cannot_be_mutated_in_place() -> None:
    """`outcomes` must be an immutable `tuple`, not a `list` a caller
    could `.append()`/`.pop()` after the market was validated.
    """
    market = _make_market()
    assert isinstance(market.outcomes, tuple)
    with pytest.raises(AttributeError):
        market.outcomes.append("MAYBE")  # type: ignore[attr-defined]


def test_venue_market_outcome_ids_cannot_be_mutated_in_place() -> None:
    """Red-team demonstrated mutating a token id in place
    (`market.outcome_ids["YES"] = "evil-token"`) after validation. This
    must raise: `outcome_ids` is a read-only `MappingProxyType`.
    """
    market = _make_market()
    with pytest.raises(TypeError):
        market.outcome_ids["YES"] = "evil-token"  # type: ignore[index]


def test_venue_market_outcome_ids_is_immune_to_source_dict_mutation() -> None:
    """`outcome_ids` is coerced from a PRIVATE COPY of whatever was passed
    in, so mutating the original dict the caller still holds a reference
    to must NOT leak through to the validated market.
    """
    source = {"YES": "tok-yes", "NO": "tok-no"}
    market = VenueMarket(
        venue="polymarket",
        market_id="m1",
        event_id=None,
        question="q",
        outcomes=["YES", "NO"],
        outcome_ids=source,
        rules_text="",
        resolution_source=None,
        close_time=AWARE_TS,
        expected_settle_time=None,
        status="open",
        result=None,
        tick_size=0.01,
        min_size=1.0,
        fee=FeeSchedule(taker_rate=0.05, maker_rate=0.0, source="settings_default"),
        raw={},
    )
    source["YES"] = "evil-token"
    assert market.outcome_ids["YES"] == "tok-yes"


def test_venue_market_raw_cannot_be_mutated_in_place() -> None:
    """`raw` must also be a read-only `MappingProxyType`."""
    market = _make_market()
    with pytest.raises(TypeError):
        market.raw["injected"] = "value"  # type: ignore[index]


# ---------------------------------------------------------------------------
# RETRY DEFECT 2: NaN and +/-Infinity must be rejected everywhere a size or
# price is validated -- `json.loads` accepts these tokens by default, so a
# malformed venue payload reaches these fields with no adversary needed.
# ---------------------------------------------------------------------------

NAN = float("nan")
POS_INF = float("inf")
NEG_INF = float("-inf")
NON_FINITE = [NAN, POS_INF, NEG_INF]


@pytest.mark.parametrize("bad", NON_FINITE)
def test_book_level_price_rejects_non_finite(bad: float) -> None:
    with pytest.raises(ValueError, match=r"price"):
        BookLevel(price=bad, size=1.0)


@pytest.mark.parametrize("bad", NON_FINITE)
def test_book_level_size_rejects_non_finite(bad: float) -> None:
    with pytest.raises(ValueError, match=r"size"):
        BookLevel(price=0.5, size=bad)


@pytest.mark.parametrize("bad", NON_FINITE)
def test_order_request_price_rejects_non_finite(bad: float) -> None:
    with pytest.raises(ValueError, match=r"price"):
        OrderRequest(
            venue="polymarket",
            market_id="m1",
            outcome="YES",
            side="BUY",
            price=bad,
            size=10.0,
            tif="GTC",
            client_order_id="c1",
        )


@pytest.mark.parametrize("bad", NON_FINITE)
def test_order_request_size_rejects_non_finite(bad: float) -> None:
    with pytest.raises(ValueError, match=r"size"):
        OrderRequest(
            venue="polymarket",
            market_id="m1",
            outcome="YES",
            side="BUY",
            price=0.5,
            size=bad,
            tif="GTC",
            client_order_id="c1",
        )


@pytest.mark.parametrize("bad", NON_FINITE)
def test_order_ack_filled_size_rejects_non_finite(bad: float) -> None:
    with pytest.raises(ValueError, match=r"filled_size"):
        OrderAck(
            venue="polymarket",
            order_id="o1",
            client_order_id="c1",
            status="open",
            filled_size=bad,
            remaining_size=10.0,
            avg_fill_price=None,
            ts=AWARE_TS,
        )


@pytest.mark.parametrize("bad", NON_FINITE)
def test_order_ack_remaining_size_rejects_non_finite(bad: float) -> None:
    with pytest.raises(ValueError, match=r"remaining_size"):
        OrderAck(
            venue="polymarket",
            order_id="o1",
            client_order_id="c1",
            status="open",
            filled_size=0.0,
            remaining_size=bad,
            avg_fill_price=None,
            ts=AWARE_TS,
        )


@pytest.mark.parametrize("bad", NON_FINITE)
def test_fill_size_rejects_non_finite(bad: float) -> None:
    with pytest.raises(ValueError, match=r"size"):
        Fill(
            venue="polymarket",
            order_id="o1",
            price=0.5,
            size=bad,
            fee=0.0,
            ts=AWARE_TS,
            liquidity="taker",
        )


@pytest.mark.parametrize("bad", NON_FINITE)
def test_fill_fee_rejects_non_finite(bad: float) -> None:
    with pytest.raises(ValueError, match=r"fee"):
        Fill(
            venue="polymarket",
            order_id="o1",
            price=0.5,
            size=10.0,
            fee=bad,
            ts=AWARE_TS,
            liquidity="taker",
        )


@pytest.mark.parametrize("bad", NON_FINITE)
def test_venue_market_min_size_rejects_non_finite(bad: float) -> None:
    with pytest.raises(ValueError, match=r"min_size"):
        _make_market(min_size=bad)


@pytest.mark.parametrize("bad", NON_FINITE)
def test_position_size_rejects_non_finite(bad: float) -> None:
    with pytest.raises(ValueError, match=r"size"):
        Position(venue="kalshi", market_id="m1", outcome="YES", size=bad, avg_price=0.5)


@pytest.mark.parametrize("bad", NON_FINITE)
def test_balance_available_rejects_non_finite(bad: float) -> None:
    with pytest.raises(ValueError, match=r"available"):
        Balance(venue="polymarket", available=bad, locked=0.0)


@pytest.mark.parametrize("bad", NON_FINITE)
def test_balance_locked_rejects_non_finite(bad: float) -> None:
    with pytest.raises(ValueError, match=r"locked"):
        Balance(venue="polymarket", available=0.0, locked=bad)


@pytest.mark.parametrize("bad", NON_FINITE)
def test_fee_schedule_taker_rate_rejects_non_finite(bad: float) -> None:
    with pytest.raises(ValueError, match=r"taker_rate"):
        FeeSchedule(taker_rate=bad, maker_rate=0.0, source="settings_default")


@pytest.mark.parametrize("bad", NON_FINITE)
def test_fee_schedule_maker_rate_rejects_non_finite(bad: float) -> None:
    with pytest.raises(ValueError, match=r"maker_rate"):
        FeeSchedule(taker_rate=0.0, maker_rate=bad, source="settings_default")


# ---------------------------------------------------------------------------
# RETRY DEFECT 4: walk() must validate its own `size` argument -- a NaN
# size must not silently drain the entire book (`size < 0` doesn't catch
# NaN, and `min(level.size, nan)` always returns `level.size`).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", NON_FINITE)
def test_walk_rejects_non_finite_size(bad: float) -> None:
    book = make_book(bids=[], asks=[(0.40, 100.0)])
    with pytest.raises(ValueError, match=r"size"):
        book.walk("buy", bad)


def test_walk_with_nan_size_does_not_drain_the_book() -> None:
    """Belt-and-suspenders on top of the raise above: document the exact
    failure mode a missing NaN guard would have produced, so a regression
    here is unambiguous. Before the fix, `walk("buy", nan)` returned the
    ENTIRE book (300 contracts across all three levels) instead of
    raising or filling nothing.
    """
    book = make_book(bids=[], asks=[(0.10, 100.0), (0.20, 100.0), (0.30, 100.0)])
    with pytest.raises(ValueError):
        book.walk("buy", NAN)


# ---------------------------------------------------------------------------
# HARDENING 5: OrderBook normalizes level ordering on construction, so
# walk()/best_bid()/best_ask()/depth_at() hold regardless of input order.
# ---------------------------------------------------------------------------


def test_order_book_sorts_unsorted_bids_and_asks_on_construction() -> None:
    """An out-of-order input book comes back sorted: bids descending,
    asks ascending, regardless of how they were supplied.
    """
    book = make_book(
        bids=[(0.10, 40.0), (0.60, 10.0), (0.30, 20.0)],
        asks=[(0.90, 100.0), (0.10, 100.0), (0.50, 100.0)],
    )
    assert [lvl.price for lvl in book.bids] == [0.60, 0.30, 0.10]
    assert [lvl.price for lvl in book.asks] == [0.10, 0.50, 0.90]


def test_unsorted_book_walk_matches_sorted_equivalent() -> None:
    """Red-team's exact demonstration: `asks=[(0.90,100),(0.10,100)]`
    walking 50 must come from the CHEAPER 0.10 level, not the first-listed
    0.90 level -- a book that ignored input order used to return
    `[(0.90, 50)]` (too expensive), silently manufacturing an
    over-expensive fill (and, mirrored on the bid side, a too-cheap one --
    fabricated profit that backtests well and loses money live).
    """
    unsorted_book = make_book(bids=[], asks=[(0.90, 100.0), (0.10, 100.0)])
    sorted_book = make_book(bids=[], asks=[(0.10, 100.0), (0.90, 100.0)])

    unsorted_result = unsorted_book.walk("buy", 50.0)
    sorted_result = sorted_book.walk("buy", 50.0)

    assert unsorted_result == sorted_result == [(0.10, 50.0)]


def test_unsorted_book_walk_matches_sorted_equivalent_on_bid_side() -> None:
    """Mirror case on `bids`: selling into an unsorted bid book must still
    hit the highest (best) bid first, not the first-listed one.
    """
    unsorted_book = make_book(bids=[(0.10, 100.0), (0.90, 100.0)], asks=[])
    sorted_book = make_book(bids=[(0.90, 100.0), (0.10, 100.0)], asks=[])

    unsorted_result = unsorted_book.walk("sell", 50.0)
    sorted_result = sorted_book.walk("sell", 50.0)

    assert unsorted_result == sorted_result == [(0.90, 50.0)]


# ---------------------------------------------------------------------------
# Provenance metadata on `OrderBook`/`Fill` (added in T07 so a
# synthetic-depth fill stays labeled — GUARDRAILS.md §1.7)
# ---------------------------------------------------------------------------


def test_order_book_defaults_depth_source_to_recorded() -> None:
    """A book built with no metadata is a real, observed book."""
    book = make_book(bids=[], asks=[(0.5, 10.0)])

    assert book.metadata["depth_source"] == "recorded"
    assert book.depth_source == "recorded"


def test_order_book_keeps_an_explicit_synthetic_tag() -> None:
    """`synthesize_book` (T07) sets this; nothing may quietly overwrite it."""
    book = OrderBook(
        venue="polymarket",
        market_id="m1",
        outcome="YES",
        bids=(),
        asks=(),
        ts=AWARE_TS,
        metadata={"depth_source": "synthetic"},
    )

    assert book.depth_source == "synthetic"


def test_order_book_rejects_an_unknown_depth_source() -> None:
    """A typo ("syntetic") would silently mislabel fabricated depth as real."""
    with pytest.raises(ValueError, match=r"depth_source"):
        OrderBook(
            venue="polymarket",
            market_id="m1",
            outcome="YES",
            bids=(),
            asks=(),
            ts=AWARE_TS,
            metadata={"depth_source": "syntetic"},
        )


def test_order_book_metadata_is_immutable() -> None:
    """A book cannot be re-labeled `"recorded"` after construction."""
    book = make_book(bids=[], asks=[(0.5, 10.0)])

    with pytest.raises(TypeError):
        book.metadata["depth_source"] = "synthetic"  # type: ignore[index]


def test_fill_metadata_defaults_empty_and_is_immutable() -> None:
    """`Fill.metadata` follows the same immutability rule as every mapping here."""
    fill = Fill(
        venue="polymarket",
        order_id="o1",
        price=0.5,
        size=10.0,
        fee=0.0,
        ts=AWARE_TS,
        liquidity="taker",
    )

    assert dict(fill.metadata) == {}
    with pytest.raises(TypeError):
        fill.metadata["latency_ms"] = 5  # type: ignore[index]
