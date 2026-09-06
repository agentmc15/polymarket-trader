"""Adversarial property tests for `OrderBook.walk()` (T04 venue seam).

Derived independently from TASKS.md T04's acceptance lines and PLAN.md D3 —
NOT from reading `app/venues/types.py`. The contract this file holds T04
to:

  1. Prices are probabilities in [0, 1] on both venues; sizes are contracts
     (1 contract pays $1.00 at resolution) (PLAN.md D3).
  2. `walk("buy", 150)` on asks `[(0.40,100),(0.41,100)]` returns
     `[(0.40,100),(0.41,50)]`; on `[(0.40,100)]` returns `[(0.40,100)]`
     (T04 acceptance line 2).
  3. `OrderBook` exposes `best_bid()`, `best_ask()`, `mid()`,
     `depth_at(price, side)`, `walk(side, size)` (T04 brief).

`walk()` is what T07's fill engine will call to price every simulated and
paper fill. A bug here does not raise — it silently manufactures fills
(size, price, or count) that never existed in the book, which is worse
than a crash because it looks like a working backtest. Every property
below is chosen because a plausible off-by-one, sign error, or float
sloppiness in a hand-rolled `walk()` would violate exactly it while still
passing the two acceptance examples verbatim.
"""
from datetime import UTC, datetime

import pytest

from tests.helpers import make_book

AWARE_TS = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _total(levels: list[tuple[float, float]]) -> float:
    """Sum of sizes across a raw `(price, size)` level list."""
    return sum(size for _, size in levels)


# ---------------------------------------------------------------------------
# Conservation: sum(returned sizes) == min(requested, total depth on that side)
# ---------------------------------------------------------------------------

CONSERVATION_CASES: list[tuple[str, list[tuple[float, float]], list[tuple[float, float]], float]] = [
    # (side, bids, asks, requested_size)
    # size 0 -> nothing filled regardless of depth
    ("buy", [], [(0.40, 100.0)], 0.0),
    ("sell", [(0.60, 100.0)], [], 0.0),
    # single-level book, exact fill
    ("buy", [], [(0.40, 100.0)], 100.0),
    # single-level book, requested size EXCEEDS total depth (must not overfill)
    ("buy", [], [(0.40, 100.0)], 500.0),
    # multi-level book, partial fill inside the book
    ("buy", [], [(0.10, 30.0), (0.20, 70.0), (0.30, 1000.0)], 50.0),
    # multi-level book, requested size vastly exceeds total depth
    ("buy", [], [(0.10, 30.0), (0.20, 70.0), (0.30, 1000.0)], 1_000_000.0),
    # sell side, multi-level, exact total depth requested
    ("sell", [(0.60, 50.0), (0.59, 100.0), (0.58, 25.0)], [], 175.0),
    # sell side, requested size vastly exceeds total depth
    ("sell", [(0.60, 50.0), (0.59, 100.0), (0.58, 25.0)], [], 999_999.0),
]


@pytest.mark.parametrize("side,bids,asks,requested", CONSERVATION_CASES)
def test_walk_conserves_size(
    side: str,
    bids: list[tuple[float, float]],
    asks: list[tuple[float, float]],
    requested: float,
) -> None:
    """For any book and any requested size, the total size `walk()` returns
    equals `min(requested, total depth on the walked side)` — never more
    (fabricated liquidity) and never less (silently dropped fillable size).
    """
    book = make_book(bids=bids, asks=asks)
    levels = asks if side == "buy" else bids
    expected_filled = min(requested, _total(levels))

    result = book.walk(side, requested)  # type: ignore[arg-type]

    assert sum(size for _, size in result) == pytest.approx(expected_filled, abs=1e-9)


# ---------------------------------------------------------------------------
# Monotonic price consumption: best price first, in order
# ---------------------------------------------------------------------------


def test_walk_buy_returns_non_decreasing_prices() -> None:
    """Buying walks the ASKS best-price-first: cheapest fills come first,
    so consecutive returned prices must be non-decreasing. A `walk` that
    returned levels out of order would understate the true cost of the
    marginal contracts.
    """
    book = make_book(
        bids=[],
        asks=[(0.10, 10.0), (0.20, 10.0), (0.30, 10.0), (0.40, 10.0)],
    )
    result = book.walk("buy", 35.0)
    prices = [price for price, _ in result]
    assert prices == sorted(prices)
    assert all(p2 >= p1 for p1, p2 in zip(prices, prices[1:]))


def test_walk_sell_returns_non_increasing_prices() -> None:
    """Selling walks the BIDS best-price-first: the highest bid is hit
    first, so consecutive returned prices must be non-increasing.
    """
    book = make_book(
        bids=[(0.60, 10.0), (0.50, 10.0), (0.40, 10.0), (0.30, 10.0)],
        asks=[],
    )
    result = book.walk("sell", 35.0)
    prices = [price for price, _ in result]
    assert prices == sorted(prices, reverse=True)
    assert all(p2 <= p1 for p1, p2 in zip(prices, prices[1:]))


# ---------------------------------------------------------------------------
# Never over-fills: no level exceeds its book size; only the LAST level partial
# ---------------------------------------------------------------------------


def test_walk_only_the_last_returned_level_is_partial() -> None:
    """Every fully-consumed level must come back at its full book size;
    only the final (deepest-touched) level may come back short — and no
    level may EVER come back larger than what the book actually held
    there, which would manufacture liquidity that never existed.
    """
    asks = [(0.10, 10.0), (0.20, 20.0), (0.30, 30.0), (0.40, 40.0)]
    book = make_book(bids=[], asks=asks)
    # 10 (full L1) + 20 (full L2) + 15 (partial L3, out of 30) = 45; L4 untouched.
    result = book.walk("buy", 45.0)

    assert result == [(0.10, 10.0), (0.20, 20.0), (0.30, 15.0)]

    book_size_by_price = dict(asks)
    for i, (price, size) in enumerate(result):
        assert size <= book_size_by_price[price] + 1e-12, "level exceeds book depth"
        if i < len(result) - 1:
            assert size == pytest.approx(book_size_by_price[price]), (
                "a non-final level came back partial — only the last "
                "touched level may be partial"
            )
    # the deepest level touched (0.30) is the only partial one
    last_price, last_size = result[-1]
    assert last_size < book_size_by_price[last_price]
    # the level never reached (0.40) must not appear at all
    assert 0.40 not in [price for price, _ in result]


def test_walk_never_returns_more_than_requested_even_with_ample_depth() -> None:
    """A book with far more depth than requested must return exactly the
    requested size, never the whole book.
    """
    book = make_book(bids=[], asks=[(0.40, 1_000_000.0)])
    result = book.walk("buy", 10.0)
    assert result == [(0.40, 10.0)]
    assert sum(size for _, size in result) == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# Side correctness: buy => asks, sell => bids. Not the reverse.
# ---------------------------------------------------------------------------


def test_walk_buy_consumes_asks_not_bids() -> None:
    """An asymmetric book where the two sides give clearly different
    answers: a tiny, cheap bid side and a large, expensive ask side.
    `walk("buy", ...)` must come back priced at the ASK (0.90), and must
    reflect the ASK side's abundant depth — not the bid side's scarce 40
    contracts. Getting this backwards is a sign error that would make
    every arbitrage calculation look profitable on paper.
    """
    book = make_book(bids=[(0.10, 40.0)], asks=[(0.90, 999.0)])
    result = book.walk("buy", 50.0)
    assert result == [(0.90, 50.0)]  # fully fillable from the deep ask side
    prices = [p for p, _ in result]
    assert 0.10 not in prices


def test_walk_sell_consumes_bids_not_asks() -> None:
    """Same asymmetric book, opposite side: `walk("sell", ...)` must be
    priced at the BID (0.10) and be capped by the bid side's scarce 40
    contracts (it must NOT fill 50 from the ask side's depth).
    """
    book = make_book(bids=[(0.10, 40.0)], asks=[(0.90, 999.0)])
    result = book.walk("sell", 50.0)
    assert result == [(0.10, 40.0)]  # only 40 available on the bid side
    prices = [p for p, _ in result]
    assert 0.90 not in prices


# ---------------------------------------------------------------------------
# Empty book
# ---------------------------------------------------------------------------


def test_walk_on_fully_empty_book_returns_empty_list_both_sides() -> None:
    """A book with no bids and no asks must not raise on either side."""
    book = make_book(bids=[], asks=[])
    assert book.walk("buy", 100.0) == []
    assert book.walk("sell", 100.0) == []


def test_walk_on_empty_relevant_side_returns_empty_list() -> None:
    """Walking a side that has no levels (even if the OTHER side has
    plenty) must return `[]`, not raise and not borrow from the other
    side.
    """
    book = make_book(bids=[(0.50, 100.0)], asks=[])
    assert book.walk("buy", 10.0) == []  # no asks to buy from

    book2 = make_book(bids=[], asks=[(0.50, 100.0)])
    assert book2.walk("sell", 10.0) == []  # no bids to sell into


# ---------------------------------------------------------------------------
# Float behavior: remainder arithmetic must not fabricate a dust level
# ---------------------------------------------------------------------------


def test_walk_does_not_leave_a_phantom_dust_level() -> None:
    """Two ask levels of size 0.1 and 0.2 fully satisfy a request of
    `0.1 + 0.2` in real arithmetic (0.3 == 0.3). In IEEE-754 float,
    `0.1 + 0.2 == 0.30000000000000004`, four ULPs above 0.3. If `walk()`
    tracks `remaining -= level.size` naively across levels without
    tolerance, the float remainder after consuming both real levels can
    land on a tiny positive residual (empirically ~4.44e-17 here) instead
    of exactly 0.0 — and if a third level exists behind them, a naive
    "while remaining > 0" loop will take a dust-sized bite out of it and
    return a THIRD, near-zero-size level that was never really requested.
    That phantom level would later be booked as a real, unfillable
    position by anything downstream (the fill engine, a position ledger)
    that trusts `walk()`'s output at face value.
    """
    book = make_book(bids=[], asks=[(0.10, 0.1), (0.20, 0.2), (0.30, 5.0)])
    requested = 0.1 + 0.2  # == 0.30000000000000004, not the mathematical 0.3

    result = book.walk("buy", requested)

    # Only the two real levels may appear; the 5.0-deep third level must
    # be completely untouched -- not even a dust-sized sliver of it.
    assert len(result) == 2, (
        f"expected exactly 2 levels (0.10 and 0.20), got {result!r} -- "
        "a third entry here is a phantom dust level from float remainder"
    )
    prices = [p for p, _ in result]
    assert 0.30 not in prices
    assert result[0] == pytest.approx((0.10, 0.1))
    assert result[1] == pytest.approx((0.20, 0.2))
    # total filled is (to float tolerance) exactly what was asked
    assert sum(size for _, size in result) == pytest.approx(requested, abs=1e-9)


# ---------------------------------------------------------------------------
# depth_at() and mid(): document actual behavior, don't assume it silently
# ---------------------------------------------------------------------------


def test_depth_at_generous_price_equals_total_depth_on_that_side() -> None:
    """`depth_at(price, side)` at a price so generous it would marketably
    fill against every level on that side (1.0 for buying — the maximum
    possible probability price; 0.0 for selling — the minimum) must equal
    the FULL depth of that side, not just the touch. This holds under any
    reasonable reading of "depth at price" without needing to guess the
    exact inequality direction the implementation uses.
    """
    asks = [(0.10, 30.0), (0.20, 70.0), (0.30, 1000.0)]
    bids = [(0.60, 50.0), (0.59, 100.0), (0.58, 25.0)]
    book = make_book(bids=bids, asks=asks)

    assert book.depth_at(1.0, "buy") == pytest.approx(_total(asks))
    assert book.depth_at(0.0, "sell") == pytest.approx(_total(bids))


def test_depth_at_best_price_only_equals_touch_size() -> None:
    """`depth_at` evaluated at exactly the best ask/bid price must equal
    ONLY that top-of-book level's size, not the whole side's depth --
    otherwise `depth_at` could never distinguish a thin touch from a deep
    book, which downstream edge-sizing logic depends on.
    """
    asks = [(0.10, 30.0), (0.20, 70.0), (0.30, 1000.0)]
    bids = [(0.60, 50.0), (0.59, 100.0), (0.58, 25.0)]
    book = make_book(bids=bids, asks=asks)

    assert book.depth_at(0.10, "buy") == pytest.approx(30.0)
    assert book.depth_at(0.60, "sell") == pytest.approx(50.0)


def test_mid_on_ask_only_book() -> None:
    """A one-sided book (asks only, no bids) has no true bid-ask midpoint
    to average. `mid()` must not fabricate a number from the one side it
    has (that would be indistinguishable downstream from a real two-sided
    mid and silently corrupt any edge calculation reading it) — the only
    honest answers are `None` or a raise. This test locks in `None` as
    the contract; if the real implementation instead raises or returns a
    single-side number, this test fails and that mismatch is the finding
    (see report), not a reason to weaken the assertion.
    """
    book = make_book(bids=[], asks=[(0.50, 10.0)])
    assert book.mid() is None


def test_mid_on_bid_only_book() -> None:
    """Symmetric case: bids only, no asks. Same reasoning as
    `test_mid_on_ask_only_book` — `None`, not a fabricated one-sided mid.
    """
    book = make_book(bids=[(0.50, 10.0)], asks=[])
    assert book.mid() is None


def test_mid_on_two_sided_book_is_average_of_touch_prices() -> None:
    """Sanity check for the two-sided case so the one-sided tests above
    are read against a known-good baseline, not in isolation.
    """
    book = make_book(bids=[(0.48, 10.0)], asks=[(0.52, 10.0)])
    # (0.48 + 0.52) / 2 = 0.50
    assert book.mid() == pytest.approx(0.50)
