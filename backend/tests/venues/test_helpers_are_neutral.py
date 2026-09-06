"""Prove the test helpers (`tests/helpers.py::make_snapshot`, `make_book`)
cannot themselves fabricate an edge.

Every strategy/backtest/paper-trading test in this kit builds its fixtures
through these two helpers. If a helper could silently produce a
YES+NO-complement violation, or a price outside the valid `[0.0, 1.0]`
probability range, every test built on top of it would be unknowingly
testing against a fake arbitrage opportunity that only exists because the
fixture is broken — not because the code under test is. This file is the
guard against that regressing in a later task (per T04's fix to
`make_snapshot`, referenced in `tests/venues/test_types.py`'s module
docstring as "the two `make_snapshot` fixes the P0 reviewer required").

**Discrepancy from the dispatch brief, reported per GUARDRAILS.md §3.4:**
the brief asked for "`make_book(...)` never produces a level outside
[0,1] even for extreme `yes`/`spread` inputs" — but `tests/helpers.py`'s
actual `make_book(bids, asks, venue, market_id, outcome, ts)` signature
has no `yes`/`spread` parameters at all; it takes raw `(price, size)`
tuples directly. The `yes`/`spread` derivation with `[0,1]` clamping
lives entirely in `make_snapshot` (its `yes_bid`/`yes_ask`/`no_bid`/
`no_ask` fields), not in `make_book`. This file tests the clamping where
it actually lives (`make_snapshot`, below) and separately tests
`make_book`'s real neutrality property: it cannot construct an
out-of-range level at all — invalid prices raise via `BookLevel`
validation rather than being silently clamped or fabricated.
"""
import pytest

from tests.helpers import make_book, make_snapshot

# ---------------------------------------------------------------------------
# make_snapshot(): default args and a range of `yes` values are
# arbitrage-neutral (yes + no == 1.0)
# ---------------------------------------------------------------------------


def test_make_snapshot_default_args_are_complementary() -> None:
    """`make_snapshot()` with every default must yield `yes_price +
    no_price == 1.0` — a caller who writes `make_snapshot()` and forgets
    complement math entirely must not be handed a fabricated edge.
    """
    snap = make_snapshot()
    assert snap.yes_price + snap.no_price == pytest.approx(1.0, abs=1e-9)


@pytest.mark.parametrize(
    "yes",
    [
        0.0,
        0.001,
        0.1,
        0.25,
        1 / 3,  # 0.3333333333333333 -- exercises non-terminating binary fraction
        0.5,
        0.6,  # 1.0 - 0.6 in float is 0.4 exactly, but sum must still hold to tolerance
        0.7,
        0.9,
        0.999,
        1.0,
    ],
)
def test_make_snapshot_yes_no_sum_to_one_across_range(yes: float) -> None:
    """For any `yes` in `[0, 1]` with `no` left as the default (`None`),
    `make_snapshot` must derive `no_price = 1.0 - yes` such that
    `yes_price + no_price == 1.0` to float tolerance. A helper that let
    this drift (e.g. by independently rounding each side) would hand
    every complement-arbitrage test a fake few basis points of edge.
    """
    snap = make_snapshot(yes=yes)
    assert snap.yes_price == pytest.approx(yes)
    assert snap.yes_price + snap.no_price == pytest.approx(1.0, abs=1e-9)


def test_make_snapshot_explicit_no_can_deliberately_break_complement() -> None:
    """This is the documented escape hatch, not a neutrality bug: a test
    that WANTS a complement violation (e.g. to prove a strategy correctly
    flags one) passes `no=` explicitly, and `make_snapshot` must honor it
    rather than silently re-deriving `no` from `yes` and hiding the
    violation the test was trying to construct.
    """
    snap = make_snapshot(yes=0.60, no=0.60)  # deliberately yes + no == 1.2
    assert snap.yes_price + snap.no_price == pytest.approx(1.2)


# ---------------------------------------------------------------------------
# make_snapshot(): derived bid/ask fields stay in [0, 1] for extreme inputs
# ---------------------------------------------------------------------------


EXTREME_YES_SPREAD_CASES: list[tuple[float, float]] = [
    (0.99, 0.05),  # the docstring's own example: naive yes_ask = 1.015
    (0.01, 0.05),  # naive yes_bid = -0.015
    (0.0, 1.0),  # naive yes_bid = -0.5
    (1.0, 1.0),  # naive yes_ask = 1.5
    (0.5, 100.0),  # absurd spread, naive bid = -49.5, naive ask = 50.5
    (0.001, 10.0),
    (0.999, 10.0),
]


@pytest.mark.parametrize("yes,spread", EXTREME_YES_SPREAD_CASES)
def test_make_snapshot_derived_prices_never_leave_unit_interval(
    yes: float, spread: float
) -> None:
    """No matter how extreme `yes`/`spread` are, `yes_bid`, `yes_ask`,
    `no_bid`, `no_ask` must all land in `[0.0, 1.0]`. A price outside that
    range is not a valid probability and, if it leaked through to an
    `OrderBook`/`BookLevel`, would raise there — but `make_snapshot`'s
    fields are plain floats with no such guard downstream, so the
    clamping has to happen here, at construction.
    """
    snap = make_snapshot(yes=yes, spread=spread)
    for field_name in ("yes_bid", "yes_ask", "no_bid", "no_ask"):
        value = getattr(snap, field_name)
        assert 0.0 <= value <= 1.0, (
            f"{field_name}={value!r} out of [0,1] for yes={yes}, spread={spread}"
        )


# ---------------------------------------------------------------------------
# make_book(): cannot construct an out-of-range level at all
# ---------------------------------------------------------------------------


def test_make_book_boundary_prices_zero_and_one_are_accepted_unmodified() -> None:
    """0.0 and 1.0 are valid probabilities (inclusive bounds per PLAN.md
    D3) and must pass through `make_book` unchanged -- not clamped to
    some interior value, which would silently move the price the test
    author asked for.
    """
    book = make_book(bids=[(0.0, 10.0)], asks=[(1.0, 10.0)])
    assert book.best_bid() is not None and book.best_bid().price == 0.0
    assert book.best_ask() is not None and book.best_ask().price == 1.0


@pytest.mark.parametrize(
    "bad_price",
    [1.2, -0.1, 1.0000001, -0.0000001, 100.0, -100.0],
)
def test_make_book_never_silently_produces_an_out_of_range_level(
    bad_price: float,
) -> None:
    """`make_book` has no `yes`/`spread` clamping logic of its own -- it
    passes `(price, size)` tuples straight to `BookLevel`. Its neutrality
    property is therefore fail-closed rather than fail-safe: an
    out-of-range price must raise, not be silently accepted and turned
    into an invalid level that a downstream `walk()`/`mid()` computation
    would then treat as a real, tradeable price.
    """
    with pytest.raises(ValueError):
        make_book(bids=[], asks=[(bad_price, 10.0)])
    with pytest.raises(ValueError):
        make_book(bids=[(bad_price, 10.0)], asks=[])
