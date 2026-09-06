"""Tests for the T10 arbitrage strategy refactor (PLAN.md §2, GUARDRAILS.md
§1.5): `binary_complement_arbitrage` and `multi_outcome_bundle_arbitrage`
must emit real multi-leg `Intent`s, price fees from `FeeModel` (never a
literal), and never fall back to a degenerate binary bundle.

Derived from the T10 brief/acceptance criteria in
`.claude/kits/market-edge/TASKS.md`, not from reading the strategy
modules and mirroring their implementation.
"""
from datetime import timedelta

import pytest

from app.config import settings
from app.services.backtesting import BacktestConfig, Backtester, InMemoryDataReplayer
from app.strategies import STRATEGIES
from app.strategies.base import Intent
from app.strategies.binary_complement_arbitrage import (
    BinaryComplementArbitrageStrategy,
    _sizing_ask,
)
from app.strategies.multi_outcome_bundle_arbitrage import (
    MultiOutcomeBundleArbitrageStrategy,
)
from app.utils.time import utcnow
from tests.helpers import make_book, make_snapshot

# ---------------------------------------------------------------------------
# binary_complement_arbitrage: hand-computed edge, two legs, equal contracts.
# ---------------------------------------------------------------------------


def test_complement_emits_two_legs_equal_contracts_and_hand_computed_edge() -> None:
    """YES ask 0.45 / NO ask 0.48, category unknown (-> 0.05 taker rate),
    `min_position_size=10` (explicit -- NOT the shipped default, which is
    100 as of the T10 retry; see
    `binary_complement_arbitrage`'s module docstring for why -- passed
    explicitly here only so this test keeps reproducing the T10 brief's
    own worked numbers verbatim) -> a signal, with the exact edge from
    the T10 brief's own worked example.

    Hand computation (matches the brief/orchestrator numbers exactly):
        fee(0.45) = 1 * 0.05 * 0.45 * 0.55 = 0.012375
        fee(0.48) = 1 * 0.05 * 0.48 * 0.52 = 0.012480
        sum fees  = 0.024855   (== "$2.4855 at 100 contracts" / 100)
        gross edge = 1 - (0.45 + 0.48) = 0.07
        gas/contract = 2 * 0.05 / 10 = 0.01   (see module docstring: the
            fixed $0.10 per-position redemption gas is amortized over
            `min_position_size` contracts)
        edge = 0.07 - 0.024855 - 0.01 = 0.035145 (3.51%)
    0.035145 >= the default `min_profit_margin` (0.02) -> signal.

    (The shipped-default arithmetic -- `min_position_size=100` -> gas/
    contract = 0.001 -> edge = 0.044145 -- is exercised by
    `test_backtest_binary_complement_arbitrage_bare_defaults_trades_both_outcomes`
    below, which uses `STRATEGIES[...]()` with no config override at all.)
    """
    strategy = BinaryComplementArbitrageStrategy(config={"min_position_size": 10.0})
    snapshot = make_snapshot(
        market_id="m1",
        yes=0.45,
        no=0.48,
        spread=0.0,  # clean asks: yes_ask == 0.45, no_ask == 0.48
        category=None,  # -> category_rate(None) == 0.05
        volume_24h=50_000.0,
        end_date=utcnow() + timedelta(days=1),
    )

    result = strategy.on_market_data(snapshot)

    assert isinstance(result, Intent)
    assert result.kind == "complement"
    assert len(result.legs) == 2

    legs_by_outcome = {leg.outcome: leg for leg in result.legs}
    assert set(legs_by_outcome) == {"YES", "NO"}
    assert legs_by_outcome["YES"].limit_price == pytest.approx(0.45)
    assert legs_by_outcome["NO"].limit_price == pytest.approx(0.48)
    assert legs_by_outcome["YES"].side == "BUY"
    assert legs_by_outcome["NO"].side == "BUY"

    # Equal CONTRACT counts on both legs (never equal dollars — see
    # module docstring: equal dollars of unequal-priced YES/NO would
    # leave a naked directional residual on the cheaper side).
    assert legs_by_outcome["YES"].size_contracts == legs_by_outcome["NO"].size_contracts
    assert legs_by_outcome["YES"].size_contracts == pytest.approx(10.0)

    assert result.metadata["edge"] == pytest.approx(0.035145, abs=1e-9)
    assert result.metadata["gross_edge"] == pytest.approx(0.07, abs=1e-9)
    assert result.metadata["yes_fee"] == pytest.approx(0.012375, abs=1e-9)
    assert result.metadata["no_fee"] == pytest.approx(0.012480, abs=1e-9)
    assert result.metadata["gas_per_contract"] == pytest.approx(0.01, abs=1e-9)


def test_no_intent_when_fees_eat_the_gross_edge() -> None:
    """Asks 0.49/0.49, category crypto (0.07 taker rate): gross edge is
    0.02 and fees alone are already 0.034986 per contract -- fees exceed
    the gross edge before gas is even considered, so this must return
    `None` regardless of `min_position_size`/gas amortization.

    Hand computation (pinned exactly by the T10 brief):
        gross edge = 1 - (0.49 + 0.49) = 0.02
        fee(0.49) = 1 * 0.07 * 0.49 * 0.51 = 0.017493
        sum fees  = 0.034986
        0.02 - 0.034986 = -0.014986 < 0  -> already negative before gas
    """
    strategy = BinaryComplementArbitrageStrategy()
    snapshot = make_snapshot(
        market_id="m1",
        yes=0.49,
        no=0.49,
        spread=0.0,
        category="crypto",
        volume_24h=50_000.0,
    )

    assert strategy.on_market_data(snapshot) is None


@pytest.mark.parametrize(
    ("min_position_size", "expect_signal"),
    [
        # gas/contract = 2*0.05/10 = 0.01 -> edge 0.035145 >= 0.02 -> signal
        (10.0, True),
        # gas/contract = 2*0.05/2  = 0.05 -> edge = 0.07-0.024855-0.05
        #    = -0.004855 < 0.02 -> no signal: the SAME price gap is
        #    unprofitable at a small assumed size because the fixed
        #    redemption gas dominates a small trade (pins the
        #    "gas is per-intent, amortized over min_position_size"
        #    design choice documented in the module docstring).
        (2.0, False),
    ],
)
def test_gas_amortization_uses_min_position_size_as_the_divisor(
    min_position_size: float, expect_signal: bool
) -> None:
    """The fixed `2 * redemption_gas_usd` cost is divided by
    `config["min_position_size"]`, not folded in per-contract directly
    and not applied as a flat, size-independent gate -- so the SAME
    0.45/0.48 price gap signals at `min_position_size=10` and does not
    at `min_position_size=2`.
    """
    strategy = BinaryComplementArbitrageStrategy(
        config={"min_position_size": min_position_size}
    )
    snapshot = make_snapshot(
        market_id="m1",
        yes=0.45,
        no=0.48,
        spread=0.0,
        category=None,
        volume_24h=50_000.0,
    )

    result = strategy.on_market_data(snapshot)

    if expect_signal:
        assert isinstance(result, Intent)
    else:
        assert result is None


def test_sizing_ask_walks_book_instead_of_top_of_book() -> None:
    """`_sizing_ask` returns the size-weighted average price over
    `snapshot.book`'s levels, not the top-of-book quote, once a book is
    present for the requested outcome.

    Book: asks [(0.40, 5), (0.44, 20)]. Walking 10 contracts consumes
    the full first level (5 @ 0.40) then 5 more from the second level
    (5 @ 0.44): VWAP = (5*0.40 + 5*0.44) / 10 = 0.42 -- neither the
    top-of-book price (0.40) nor the fallback quote (0.99, deliberately
    far off, to prove it was NOT used).
    """
    book = make_book(
        bids=[(0.38, 50)],
        asks=[(0.40, 5.0), (0.44, 20.0)],
        market_id="m1",
        outcome="YES",
    )
    snapshot = make_snapshot(market_id="m1", yes=0.40, no=0.55, book=book)

    ask = _sizing_ask(snapshot, outcome="YES", top_of_book=0.99, size=10.0)

    assert ask == pytest.approx(0.42)


def test_sizing_ask_falls_back_to_top_of_book_without_a_matching_book() -> None:
    """No book at all -> `_sizing_ask` returns the supplied top-of-book
    quote unchanged."""
    snapshot = make_snapshot(market_id="m1", yes=0.40, no=0.55)
    assert snapshot.book is None

    ask = _sizing_ask(snapshot, outcome="YES", top_of_book=0.41, size=10.0)

    assert ask == 0.41


def test_sizing_ask_falls_back_when_book_outcome_does_not_match() -> None:
    """A book recorded for the NO side must not be used to price the
    YES leg -- falls back to the supplied top-of-book quote."""
    book = make_book(
        bids=[(0.50, 50)], asks=[(0.55, 50.0)], market_id="m1", outcome="NO"
    )
    snapshot = make_snapshot(market_id="m1", yes=0.40, no=0.55, book=book)

    ask = _sizing_ask(snapshot, outcome="YES", top_of_book=0.41, size=10.0)

    assert ask == 0.41


# ---------------------------------------------------------------------------
# multi_outcome_bundle_arbitrage: N legs, per-leg fees, no binary fallback.
# ---------------------------------------------------------------------------


def test_bundle_emits_one_leg_per_outcome() -> None:
    """A 4-outcome market with a cheap-enough sum of asks signals a
    `kind="bundle"` `Intent` with one BUY leg per outcome, all sized
    equal in contracts.

    Hand computation (category unknown -> 0.05 taker rate):
        total_cost = 0.20 + 0.20 + 0.20 + 0.15 = 0.75
        fees: 3 * (0.05*0.20*0.80) + 1 * (0.05*0.15*0.85)
            = 3*0.008 + 0.006375 = 0.030375
        profit_margin = 1 - 0.75 - 0.030375 = 0.219625 >= 0.03 (default)
    """
    strategy = MultiOutcomeBundleArbitrageStrategy()
    snapshot = make_snapshot(
        market_id="m2",
        orderbook={"outcomes": {"A": 0.20, "B": 0.20, "C": 0.20, "D": 0.15}},
        end_date=utcnow() + timedelta(days=1),
    )

    result = strategy.on_market_data(snapshot)

    assert isinstance(result, Intent)
    assert result.kind == "bundle"
    assert len(result.legs) == 4
    assert {leg.outcome for leg in result.legs} == {"A", "B", "C", "D"}
    assert all(leg.side == "BUY" for leg in result.legs)

    sizes = {leg.size_contracts for leg in result.legs}
    assert len(sizes) == 1  # every leg sized equal in contracts

    assert result.metadata["profit_margin"] == pytest.approx(0.219625, abs=1e-9)


def test_bundle_two_outcome_market_returns_none() -> None:
    """A market with only 2 priced outcomes returns `None` outright --
    no fallback to treating it as a degenerate binary bundle (T10's
    explicit fix: the pre-T10 version silently accepted this)."""
    strategy = MultiOutcomeBundleArbitrageStrategy()
    snapshot = make_snapshot(
        market_id="m2",
        orderbook={"outcomes": {"YES": 0.40, "NO": 0.40}},
    )

    assert strategy.on_market_data(snapshot) is None


def test_bundle_missing_outcome_ask_returns_none() -> None:
    """One outcome missing both `"ask"` and `"price"` makes the whole
    market unpriceable this tick -- `None`, not a partial 2-leg bundle."""
    strategy = MultiOutcomeBundleArbitrageStrategy()
    snapshot = make_snapshot(
        market_id="m2",
        orderbook={
            "outcomes": {
                "A": {"price": 0.30, "liquidity": 600.0},
                "B": {"liquidity": 600.0},  # no ask, no price
                "C": {"ask": 0.30, "liquidity": 600.0},
            }
        },
    )

    assert strategy.on_market_data(snapshot) is None


def test_bundle_insufficient_liquidity_returns_none() -> None:
    """An outcome below `min_liquidity_per_outcome` blocks the whole
    bundle."""
    strategy = MultiOutcomeBundleArbitrageStrategy()
    snapshot = make_snapshot(
        market_id="m2",
        orderbook={
            "outcomes": {
                "A": {"ask": 0.20, "liquidity": 600.0},
                "B": {"ask": 0.20, "liquidity": 600.0},
                "C": {"ask": 0.20, "liquidity": 10.0},  # too thin
            }
        },
    )

    assert strategy.on_market_data(snapshot) is None


# ---------------------------------------------------------------------------
# Acceptance criterion 3: a full backtest produces trades on BOTH outcomes.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backtest_complement_arbitrage_trades_both_outcomes() -> None:
    """A persistent YES+NO ask gap produces real fills on BOTH outcomes
    through the actual backtest engine (next-snapshot fills, real
    multi-leg execution) -- not just a strategy-level signal.

    `min_position_size=20` here is an explicit, non-default size
    (smaller than the shipped default of 100 -- see
    `test_backtest_binary_complement_arbitrage_bare_defaults_trades_both_outcomes`
    below for that) chosen only to show the engine's multi-leg fill
    mechanics do not depend on any one particular size: 20 contracts *
    ~0.40 * 2 legs ~= $16, clear of `settings.min_trade_usd` ($10).
    """
    start = utcnow()
    end = start + timedelta(hours=6)

    snapshots = [
        make_snapshot(
            market_id="m3",
            ts=start + timedelta(minutes=15 * i),
            yes=0.40,
            no=0.40,
            spread=0.0,
            category="test",
            volume_24h=50_000.0,
            end_date=end,
        )
        for i in range(4)
    ]

    config = BacktestConfig(start_date=start, end_date=end, initial_capital=10_000.0)
    strategy = BinaryComplementArbitrageStrategy(config={"min_position_size": 20.0})
    backtester = Backtester(config, strategy)
    replayer = InMemoryDataReplayer(snapshots)

    result = await backtester.run(replayer)

    outcomes_traded = {t.outcome for t in result.trades}
    assert {"YES", "NO"} <= outcomes_traded
    assert result.total_trades > 0


@pytest.mark.asyncio
async def test_backtest_binary_complement_arbitrage_bare_defaults_trades_both_outcomes() -> (
    None
):
    """T10 retry acceptance criterion 1: `STRATEGIES["binary_complement_arbitrage"]()`
    -- the SHIPPED default, no config override at all -- must be able to
    execute a real trade through a full backtest.

    This is the defect the retry exists for: the previous shipped
    default (`min_position_size=10.0`) produced an intent notional
    (`min_position_size * (yes_ask + no_ask)`) below the engine's
    `settings.min_trade_usd` ($10) floor at EVERY price this strategy
    would ever actually signal at, because this strategy only signals
    when `yes_ask + no_ask` is low (that is the edge) -- a bigger edge
    means a SMALLER notional per contract, not a larger one. The
    previous test suite hid this by overriding `min_position_size` in
    every fixture instead of testing the real default (see
    `test_backtest_complement_arbitrage_trades_both_outcomes` above,
    which still uses an explicit override).

    Fixture: a persistent YES=0.40/NO=0.41 (sum 0.81) gap -- the exact
    reproduction used to find this defect. With the current default
    (`min_position_size=100.0`), notional = 100 * 0.81 = $81, clear of
    the $10 floor, so both legs must actually fill.
    """
    start = utcnow()
    end = start + timedelta(hours=6)

    snapshots = [
        make_snapshot(
            market_id="m6",
            ts=start + timedelta(minutes=15 * i),
            yes=0.40,
            no=0.41,
            spread=0.0,
            category="test",
            volume_24h=50_000.0,
            end_date=end,
        )
        for i in range(4)
    ]

    config = BacktestConfig(start_date=start, end_date=end, initial_capital=10_000.0)
    strategy = STRATEGIES["binary_complement_arbitrage"]()
    assert strategy.config["min_position_size"] == pytest.approx(100.0)

    backtester = Backtester(config, strategy)
    replayer = InMemoryDataReplayer(snapshots)

    result = await backtester.run(replayer)

    assert result.intents_generated > 0
    assert result.rejection_reasons.get("below_min_size", 0) == 0
    outcomes_traded = {t.outcome for t in result.trades}
    assert {"YES", "NO"} <= outcomes_traded
    assert result.total_trades > 0


@pytest.mark.asyncio
async def test_backtest_multi_outcome_bundle_arbitrage_bare_defaults_clear_notional_floor() -> (
    None
):
    """T10 retry acceptance criterion 2: does `multi_outcome_bundle_arbitrage`
    share `binary_complement_arbitrage`'s notional-floor defect? Yes --
    with `STRATEGIES["multi_outcome_bundle_arbitrage"]()` (the
    previously shipped default, `min_position_size=10.0`) on the exact
    fixture `test_bundle_emits_one_leg_per_outcome` above already uses
    (asks summing to 0.75), the intent's notional was `10 * 0.75 =
    $7.50` -- below the $10 floor -- and the engine rejected every
    intent `"below_min_size"`, exactly the same shape of bug. Fixed the
    same way: the default is now 100 (see
    `multi_outcome_bundle_arbitrage`'s module docstring), so
    `100 * 0.75 = $75` clears the floor.

    This test proves THAT SPECIFIC defect is gone: with bare defaults,
    the engine no longer rejects the intent `"below_min_size"`.

    It deliberately does NOT assert `result.total_trades > 0` the way
    the binary-strategy test above does. Unlike a binary complement
    (whose YES/NO legs price off `yes_ask`/`no_ask`, scalar fields
    present on every snapshot, so `Backtester._book_for` can synthesize
    a book for either leg from that one snapshot), a bundle's legs are
    named outcomes (e.g. "A"/"B"/"C"/"D") priced from
    `snapshot.orderbook["outcomes"]`, and `Backtester._book_for` has no
    synthesis path for any outcome other than "YES"/"NO" (see its own
    docstring: "a multi-outcome bundle leg on a venue whose snapshot
    carries only YES/NO quotes" returns `None`) -- a separate,
    pre-existing limitation tied to PLAN.md D10 (real per-outcome
    `book_snapshots` are not collected yet), not to `min_position_size`,
    and out of scope here per GUARDRAILS.md §6 ("no rewrite of the
    backtest engine's public surface"). That gap means a >=3-leg bundle
    intent cannot fill end-to-end through `Backtester` today regardless
    of sizing, and is reported separately rather than silently worked
    around.
    """
    start = utcnow()
    end = start + timedelta(hours=6)

    snapshots = [
        make_snapshot(
            market_id="m7",
            ts=start + timedelta(minutes=15 * i),
            orderbook={"outcomes": {"A": 0.20, "B": 0.20, "C": 0.20, "D": 0.15}},
            volume_24h=50_000.0,
            end_date=end,
        )
        for i in range(4)
    ]

    config = BacktestConfig(start_date=start, end_date=end, initial_capital=10_000.0)
    strategy = STRATEGIES["multi_outcome_bundle_arbitrage"]()
    assert strategy.config["min_position_size"] == pytest.approx(100.0)

    backtester = Backtester(config, strategy)
    replayer = InMemoryDataReplayer(snapshots)

    result = await backtester.run(replayer)

    assert result.intents_generated > 0
    assert result.rejection_reasons.get("below_min_size", 0) == 0


def test_redemption_gas_usd_settings_default_matches_module_assumption() -> None:
    """Sanity check that this test file's hand-computed numbers (gas =
    2 * 0.05 = 0.10 per pair) match the actual configured
    `settings.redemption_gas_usd` -- if an operator ever changes that
    default, this test (not just the strategy) will fail loudly rather
    than silently drifting from the numbers above.
    """
    assert settings.redemption_gas_usd == pytest.approx(0.05)
