"""T22: capital sweep and `EdgeDecayReport`.

Every money number asserted here is computed BY HAND in a comment next to
the assertion (GUARDRAILS.md §5) — never with the code under test.

Two scenarios use a `binary-complement-shaped` fixture strategy
(`_PlantedComplementStrategy`) that buys BOTH legs of a manufactured
YES+NO mispricing and holds to resolution, exactly as
`binary_complement_arbitrage` does (PLAN.md D8) — but with `atomicity=
"best_effort"` and a capital-proportional (not fixed-contract) sizing
rule, because BOTH are required to make a capital sweep's
`pct_intents_downsized` non-trivial (see `sweep.py`'s module docstring
and NOTES.md `### T22`): a fixed contract count never grows with
capital, and `atomicity="all_or_none"` with the default
`partial_tolerance=0.0` either fills 100% or is REJECTED outright (never
"executed but downsized").

The third scenario (`test_bundle_shaped_sweep_is_labeled_unmeasurable`)
is the carry-forward-2 regression: a 3-leg `bundle` intent whose outcomes
are not `"YES"`/`"NO"` can NEVER get a book from a top-of-book-only
snapshot (`Backtester._book_for` — a `MarketSnapshot` carries exactly one
`book` field), so it produces the identical zero-trades-at-every-level
signature as a genuine "no edge at any size" strategy. The two must be
distinguishable through `CapitalRow.zero_trades_cause` /
`EdgeDecayReport.unmeasurable_note`.

**T28: the last three scenarios CHARACTERIZE the capital-cap confound
instead of dodging it.** `pct_intents_downsized` is the union of two
unrelated causes — the engine's own `max_position_pct`/cash cap and the
book running out — and those two run in OPPOSITE directions across a
capital sweep, so the union alone reads backwards. The earlier fixtures
picked `size_fraction=0.18 < max_position_pct=0.20` specifically so the
cap could never bind, which kept the suite green while the shipped
`--synthetic` demo reported 0.0% downsizing at its top two capital
levels. These three drive the cap and the book independently and assert
that the two are separately reported and mutually distinguishable:

- `test_capital_cap_alone_is_reported_as_capital_not_depth`
- `test_book_depth_alone_is_reported_as_depth_not_capital`
- `test_all_or_none_depth_block_is_invisible_to_the_union_metric`
  (the case that produces NO `TradeRecord` at all, so no trade-derived
  metric can see it)
"""
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

from app.services.backtesting import (
    BacktestResult,
    CapitalRow,
    EdgeDecayReport,
    run_sweep,
)
from app.services.backtesting.data_replay import InMemoryDataReplayer, ResolutionEvent
from app.services.backtesting.engine import BacktestConfig, SlippageModel
from app.strategies import STRATEGIES
from app.strategies.base import BaseStrategy, Intent, Leg, MarketSnapshot, Signal
from app.utils.time import utcnow
from tests.helpers import make_snapshot

T0 = utcnow().replace(microsecond=0)


class _PlantedComplementStrategy(BaseStrategy):
    """Buys YES+NO once, sized as a fraction of portfolio equity.

    Config (all optional):
        market_id: Market to act on. Default `"m1"`.
        size_fraction: Fraction of portfolio equity requested per leg's
            USD budget. Default `0.18` — deliberately BELOW
            `BacktestConfig.max_position_pct` (default `0.20`), so the
            engine's own capital/position-size cap never binds and the
            only thing that can shrink a fill below what was requested
            is the fixed-size synthesized book (PLAN.md D6). Held
            constant across a sweep's capital levels, so the REQUESTED
            USD (and therefore contracts, since price is fixed) scales
            linearly with capital while the book's depth does not —
            which is exactly the "fixed depth, growing order size"
            shape a capital sweep exists to surface.

            Set it ABOVE `max_position_pct` to make the capital cap bind
            instead — that is the confound T28 exists to separate, and
            `test_capital_cap_alone_is_reported_as_capital_not_depth`
            drives it deliberately rather than avoiding it.
        atomicity: `"best_effort"` (default) or `"all_or_none"`. Under
            `"all_or_none"` with `BacktestConfig.partial_tolerance = 0.0`
            a short leg kills the WHOLE intent, so a depth failure
            produces no `TradeRecord` at all — see
            `test_all_or_none_depth_block_is_invisible_to_the_union_metric`.
    """

    name = "test_planted_complement"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._emitted = False

    def on_market_data(self, snapshot: MarketSnapshot) -> Intent | None:
        market_id = self.config.get("market_id", "m1")
        if self._emitted or snapshot.market_id != market_id:
            return None
        if snapshot.yes_ask is None or snapshot.no_ask is None:
            return None
        if snapshot.yes_ask + snapshot.no_ask >= 1.0:
            # No complement violation -> no edge -> no signal. This is
            # the ONLY gate: a genuine "no gap" fixture must produce
            # zero intents, not zero fills.
            return None
        self._emitted = True
        return Intent(
            kind="complement",
            legs=[
                Leg(
                    market_id=snapshot.market_id,
                    outcome="YES",
                    side="BUY",
                    limit_price=snapshot.yes_ask,
                    venue=snapshot.venue,
                ),
                Leg(
                    market_id=snapshot.market_id,
                    outcome="NO",
                    side="BUY",
                    limit_price=snapshot.no_ask,
                    venue=snapshot.venue,
                ),
            ],
            hold_to_resolution=True,
            atomicity=self.config.get("atomicity", "best_effort"),
            confidence=0.9,
        )

    def calculate_position_size(
        self,
        signal: Signal,  # noqa: ARG002
        portfolio_value: float,
        positions: dict[str, Any],  # noqa: ARG002
    ) -> float:
        fraction = float(self.config.get("size_fraction", 0.18))
        return portfolio_value * fraction

    def reset(self) -> None:
        super().reset()
        self._emitted = False


class _UnmeasurableBundleStrategy(BaseStrategy):
    """Emits one 3-leg named-outcome bundle intent, once.

    Every leg's outcome ("Trump"/"Biden"/"Other") is neither `"YES"` nor
    `"NO"`, and the fixture attaches no recorded `OrderBook`, so
    `Backtester._book_for` returns `None` for every leg (synthesis is
    undefined for a non-binary outcome — T21 carry-forward 1) and the
    `all_or_none` intent is rejected `"no_eligible_levels"` at every
    capital level (T22 carry-forward 2).
    """

    name = "test_unmeasurable_bundle"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._emitted = False

    def on_market_data(self, snapshot: MarketSnapshot) -> Intent | None:
        market_id = self.config.get("market_id", "m2")
        if self._emitted or snapshot.market_id != market_id:
            return None
        self._emitted = True
        return Intent(
            kind="bundle",
            legs=[
                Leg(
                    market_id=snapshot.market_id,
                    outcome=name,
                    side="BUY",
                    limit_price=0.30,
                    size_contracts=100.0,
                    venue=snapshot.venue,
                )
                for name in ("Trump", "Biden", "Other")
            ],
            hold_to_resolution=True,
            atomicity="all_or_none",
            confidence=0.9,
        )

    def calculate_position_size(
        self,
        signal: Signal,  # noqa: ARG002
        portfolio_value: float,  # noqa: ARG002
        positions: dict[str, Any],  # noqa: ARG002
    ) -> float:
        return 0.0

    def reset(self) -> None:
        super().reset()
        self._emitted = False


def _base_config(**kw: Any) -> BacktestConfig:
    fields: dict[str, Any] = {
        "start_date": T0 - timedelta(minutes=5),
        "end_date": T0 + timedelta(days=366),
        "initial_capital": 10_000.0,
        "slippage_model": SlippageModel.NONE,
        "liquidity_fraction": 0.02,
    }
    fields.update(kw)
    return BacktestConfig(**fields)


def _gap_factory() -> "AsyncIterator[Any]":
    """A planted 4%-plus complement gap on a fixed-depth synthesized book.

    `yes_ask = no_ask = 0.10` -> gross gap = `1 - 0.20 = 0.80` (80%, an
    extreme number chosen purely so the resulting net edge is robust to
    fee-rate assumptions — see the module docstring; this is a synthetic
    unit fixture, not a claim about real market mispricing).

    Depth (T07 formula, `size = liquidity_fraction * volume_24h / price`)
    is IDENTICAL on both sides: `0.02 * 2500 / 0.10 = 500` contracts —
    fixed regardless of capital, which is the whole point of the fixture.

    A third, unrelated-market snapshot 365 days later exists ONLY to
    stretch the equity curve's recorded time span to ~1 year, so
    `PerformanceMetrics.annualized_return` is not distorted by the
    heavy day-count compounding a multi-hour-only equity curve would
    otherwise produce (`calculate_metrics` floors at a minimum of
    `1/365.25` years).
    """
    snap0 = make_snapshot(
        market_id="m1", ts=T0, yes=0.10, no=0.10, spread=0.0, volume_24h=2500.0
    )
    snap1 = make_snapshot(
        market_id="m1",
        ts=T0 + timedelta(minutes=5),
        yes=0.10,
        no=0.10,
        spread=0.0,
        volume_24h=2500.0,
    )
    anchor = make_snapshot(
        market_id="m_anchor",
        ts=T0 + timedelta(days=365),
        yes=0.5,
        no=0.5,
        spread=0.02,
        volume_24h=1_000.0,
    )
    resolution = ResolutionEvent(
        market_id="m1",
        venue="polymarket",
        winning_outcome="YES",
        resolved_at=T0 + timedelta(hours=1),
    )
    return InMemoryDataReplayer(
        snapshots=[snap0, snap1, anchor], resolutions=[resolution]
    )


def _no_gap_factory() -> "AsyncIterator[Any]":
    """`yes_ask + no_ask == 1.0` exactly -> no complement violation, ever."""
    snap0 = make_snapshot(market_id="m1", ts=T0, yes=0.5, no=0.5, spread=0.0)
    snap1 = make_snapshot(
        market_id="m1", ts=T0 + timedelta(minutes=5), yes=0.5, no=0.5, spread=0.0
    )
    return InMemoryDataReplayer(snapshots=[snap0, snap1])


def _bundle_factory() -> "AsyncIterator[Any]":
    snap0 = make_snapshot(market_id="m2", ts=T0, yes=0.5, no=0.5, spread=0.0)
    snap1 = make_snapshot(
        market_id="m2", ts=T0 + timedelta(minutes=5), yes=0.5, no=0.5, spread=0.0
    )
    return InMemoryDataReplayer(snapshots=[snap0, snap1])


async def test_planted_gap_sweep_shows_edge_decay(monkeypatch) -> None:
    """One row per level; net_return non-increasing; downsizing appears at scale.

    Hand computation (fee: category `"test"` is unrecognized ->
    `_POLYMARKET_UNKNOWN_CATEGORY_RATE = 0.05`; `redemption_gas_usd`
    default `0.05`/position):

    - Per-contract-pair economics: gross gap `1 - 0.20 = 0.80`; fee per
      leg `= 0.05 * 0.10 * 0.90 = 0.0045`, so `0.009` for both legs;
      net edge (pre-gas) `= 0.80 - 0.009 = 0.791` per contract pair.
      Resolution gas is FIXED at `2 * 0.05 = 0.10` total, independent of
      size.
    - Book depth (both sides) is fixed at `500` contracts (see
      `_gap_factory`).
    - At `capital=500`: budget `= 500 * 0.18 = 90`; contracts
      `= 90 / 0.20 = 450` (< 500 depth -> FULL fill, no downsizing).
      profit `= 450 * 0.791 - 0.10 = 355.95 - 0.10 = 355.85`.
      `total_return = 355.85 / 500 = 0.7117` (71.17%).
    - At `capital=2_000`: budget `= 360`, contracts `= 1_800` (> 500
      depth -> capped at 500, DOWNSIZED). profit
      `= 500 * 0.791 - 0.10 = 395.40`.
      `total_return = 395.40 / 2_000 = 0.1977` (19.77%).
    - At `capital=10_000`/`50_000`/`250_000`: same capped fill (500
      contracts), so the SAME `395.40` profit divided by a bigger
      denominator: `3.954%`, `0.791%`, `0.158%` respectively — all below
      `settings.min_viable_annualized` (default `0.05`), while `2_000`
      (19.77%) and `500` (71.17%) are comfortably above it. So
      `edge_dies_at` must land on `10_000` (the smallest capped,
      below-threshold level), and returns must be NON-INCREASING in
      capital throughout (flat only if two levels tie, never up).
    - The anchor snapshot stretches the equity curve to ~1 year, so
      `annualized ≈ total_return` (`years ≈ 365/365.25 ≈ 0.9993`,
      negligible compounding distortion) — the hand values above are
      the ones asserted against `annualized`, not `net_return`.
    """
    monkeypatch.setitem(STRATEGIES, "test_planted_complement", _PlantedComplementStrategy)

    report = await run_sweep(
        "test_planted_complement",
        {"market_id": "m1", "size_fraction": 0.18},
        _base_config(),
        _gap_factory,
    )

    assert isinstance(report, EdgeDecayReport)
    assert [row.capital for row in report.rows] == [500.0, 2_000.0, 10_000.0, 50_000.0, 250_000.0]

    for row in report.rows:
        assert isinstance(row, CapitalRow)
        assert row.trades > 0
        assert row.zero_trades_cause is None
        # Every trade here comes from a synthesized (not recorded) book.
        assert row.depth_source == "synthetic"
        assert row.fill_at == "next"

    # net_return non-increasing in capital (T22 acceptance).
    returns = [row.net_return for row in report.rows]
    assert all(a >= b - 1e-9 for a, b in zip(returns, returns[1:]))

    # Hand-computed annualized returns above (years ~= 1, so annualized
    # ~= total_return): alive at 500/2_000, dead from 10_000 onward.
    by_capital = {row.capital: row for row in report.rows}
    assert by_capital[500.0].annualized > 0.05
    assert by_capital[2_000.0].annualized > 0.05
    assert by_capital[10_000.0].annualized < 0.05
    assert by_capital[250_000.0].annualized < 0.05

    # pct_intents_downsized: 0 at $500 (450 requested < 500 depth), and
    # > 0 from $2_000 onward (1_800+ requested > 500 depth).
    assert by_capital[500.0].pct_intents_downsized == 0.0
    assert by_capital[500.0].downsize_trackable_intents == 1
    assert by_capital[250_000.0].pct_intents_downsized > 0.0
    assert by_capital[250_000.0].downsize_trackable_intents == 1

    # T28: the docstring's claim that "the cap never binds here" is now
    # ASSERTED, not merely stated, and the depth term is separated out.
    # Hand computation per level (limit prices 0.10 + 0.10 = 0.20, so
    # notional == contracts * 0.20; SlippageModel.NONE means no pad;
    # cap == min(equity * max_position_pct 0.20, cash * 0.99), and at the
    # first (only) intent equity == cash == capital, so the binding half
    # is always the 0.20 * capital term):
    #     $500:     budget  90 -> 450 contracts, notional    90 <=    100 cap
    #     $2_000:   budget 360 -> 1_800,         notional   360 <=    400 cap
    #     $10_000:  budget 1_800 -> 9_000,       notional 1_800 <=  2_000 cap
    #     $50_000:  budget 9_000 -> 45_000,      notional 9_000 <= 10_000 cap
    #     $250_000: budget 45_000 -> 225_000,    notional 45_000 <= 50_000 cap
    # The cap never binds at any level -> capital-capped share is 0.0
    # everywhere. Book depth is a FIXED 500 contracts per side, so the
    # book binds at exactly the levels where requested > 500: not at
    # $500 (450), and at every level from $2_000 (1_800) up. One intent
    # per level, so each share is 0/1 or 1/1.
    for row in report.rows:
        assert row.sized_intents == 1
        assert row.pct_intents_capital_capped == 0.0
        # best_effort: a short leg is COMMITTED, never blocked.
        assert row.depth_blocked_intents == 0
    assert by_capital[500.0].pct_intents_depth_limited == 0.0
    for capital in (2_000.0, 10_000.0, 50_000.0, 250_000.0):
        assert by_capital[capital].pct_intents_depth_limited == 1.0

    # PLAN.md R4's tripwire, read off the field that actually measures
    # depth: > 0 at the TOP capital level.
    assert report.rows[-1].capital == 250_000.0
    assert report.rows[-1].pct_intents_depth_limited > 0.0

    assert report.edge_dies_at == 10_000.0
    assert "not proof" in report.sweep_ceiling_note.lower() or "ceiling" in report.sweep_ceiling_note.lower()
    # This death is a genuine shrinking edge (real trades, real fills at
    # every level), not a structural non-measurement -- no caveat needed.
    assert report.unmeasurable_note is None


async def test_no_gap_sweep_never_trades(monkeypatch) -> None:
    """`yes_ask + no_ask == 1.0`: the strategy never signals, at any size."""
    monkeypatch.setitem(STRATEGIES, "test_planted_complement", _PlantedComplementStrategy)

    report = await run_sweep(
        "test_planted_complement",
        {"market_id": "m1"},
        _base_config(end_date=T0 + timedelta(days=1)),
        _no_gap_factory,
    )

    assert len(report.rows) == 5
    for row in report.rows:
        assert row.trades == 0
        assert row.pct_intents_downsized == 0.0
        assert row.downsize_trackable_intents == 0
        # Genuine no-edge: the strategy itself never generated an intent.
        assert row.zero_trades_cause == "no_signal"

    # A 0% return at every level trivially clears "below min_viable" ->
    # edge_dies_at is the smallest level tested, and this IS a real "no
    # edge at any size" reading (cause is "no_signal" everywhere), so no
    # unmeasurable caveat is warranted.
    assert report.edge_dies_at == 500.0
    assert report.unmeasurable_note is None


async def test_bundle_shaped_sweep_is_labeled_unmeasurable(monkeypatch) -> None:
    """Zero trades from a STRUCTURAL cause must not read as 'no edge' (carry-forward 2).

    A 3-leg bundle whose outcomes are not YES/NO can never get a book
    from a top-of-book-only snapshot (`MarketSnapshot.book` is a single
    field), so `_atomicity_blocker` rejects the `all_or_none` intent as
    `"no_eligible_levels"` at every capital level — the SAME zero-trades
    signature as `test_no_gap_sweep_never_trades`, but for an entirely
    different, non-edge-related reason. This must be distinguishable.
    """
    monkeypatch.setitem(
        STRATEGIES, "test_unmeasurable_bundle", _UnmeasurableBundleStrategy
    )

    report = await run_sweep(
        "test_unmeasurable_bundle",
        {"market_id": "m2"},
        _base_config(end_date=T0 + timedelta(days=1)),
        _bundle_factory,
        capital_levels=[500.0, 250_000.0],
    )

    for row in report.rows:
        assert row.trades == 0
        assert row.zero_trades_cause is not None
        assert row.zero_trades_cause != "no_signal"
        assert row.zero_trades_cause.startswith("structural:")
        assert "no_eligible_levels" in row.zero_trades_cause
        assert row.rejection_reasons.get("no_eligible_levels", 0) >= 1

    # edge_dies_at still fires mechanically (0% return < threshold), but
    # the report must say this is NOT evidence of "no edge".
    assert report.edge_dies_at == 500.0
    assert report.unmeasurable_note is not None
    assert "not" in report.unmeasurable_note.lower()
    assert "no_eligible_levels" in report.unmeasurable_note


async def _sweep_capturing_results(
    strategy_config: dict[str, Any],
    capital_levels: list[float],
) -> tuple[EdgeDecayReport, dict[float, BacktestResult]]:
    """Run a planted-gap sweep and keep each level's full `BacktestResult`.

    Args:
        strategy_config: `_PlantedComplementStrategy` config.
        capital_levels: Levels to sweep.

    Returns:
        tuple: `(report, {level: BacktestResult})`.
    """
    captured: dict[float, BacktestResult] = {}

    async def capture(level: float, result: BacktestResult) -> None:
        captured[level] = result

    report = await run_sweep(
        "test_planted_complement",
        strategy_config,
        _base_config(),
        _gap_factory,
        capital_levels=capital_levels,
        on_level_result=capture,
    )
    return report, captured


async def test_capital_cap_alone_is_reported_as_capital_not_depth(monkeypatch) -> None:
    """The engine's own cash cap binds; the book does not. (T28)

    This is the confound the earlier fixtures avoided by construction.
    `size_fraction = 0.30` is deliberately ABOVE
    `BacktestConfig.max_position_pct` (0.20), so the engine's own
    `min(portfolio_value * max_position_pct, cash * 0.99)` cap scales the
    order down — while the capital level is chosen small enough that the
    scaled-down order still fits inside the book. `pct_intents_downsized`
    (the union) reports 100% here and cannot say why; the decomposition
    must attribute all of it to CAPITAL and none of it to depth.

    Hand computation at `capital = 400` (`SlippageModel.NONE`, so the
    limit prices are used unpadded; one intent, so every share is 0/1 or
    1/1):

    - budget          = 400 * 0.30                = $120.00
    - total leg price = 0.10 + 0.10               = 0.20
    - REQUESTED       = 120 / 0.20                = 600 contracts per leg
    - notional        = 600 * 0.10 + 600 * 0.10   = $120.00
    - cap             = min(400 * 0.20, 400 * 0.99)
                      = min(80.00, 396.00)        = $80.00
    - 120 > 80 -> scale = 80 / 120 = 2/3
    - ORDERED         = 600 * 2/3                 = 400 contracts per leg
    - capped notional = $80.00 >= settings.min_trade_usd ($10) -> sized
    - book depth      = 0.02 * 2500 / 0.10        = 500 contracts per side
    - 400 <= 500 -> the walk FILLS IN FULL: FILLED = 400 contracts
    """
    monkeypatch.setitem(STRATEGIES, "test_planted_complement", _PlantedComplementStrategy)

    report, results = await _sweep_capturing_results(
        {"market_id": "m1", "size_fraction": 0.30}, [400.0]
    )
    row = report.rows[0]

    assert row.sized_intents == 1
    # The cap bound on the one intent that was sized: 1/1.
    assert row.pct_intents_capital_capped == 1.0
    # The book never bound: 400 ordered <= 500 available.
    assert row.pct_intents_depth_limited == 0.0
    assert row.depth_blocked_intents == 0
    # The union sees the shortfall (400 filled < 600 requested) but
    # attributes nothing: this is exactly why it cannot be read as depth.
    assert row.pct_intents_downsized == 1.0
    assert row.downsize_trackable_intents == 1

    # The same three numbers, per trade, straight off the record.
    buys = [t for t in results[400.0].trades if t.side == "BUY"]
    assert len(buys) == 2  # YES + NO
    for trade in buys:
        assert trade.requested_size == 600.0  # what the strategy asked
        assert trade.ordered_size == 400.0  # what capital allowed
        assert trade.size == 400.0  # what the book delivered


async def test_book_depth_alone_is_reported_as_depth_not_capital(monkeypatch) -> None:
    """The book binds; the engine's cash cap does not. (T28)

    The mirror image of the test above, and the pair is the point: the
    UNION metric reports exactly 100% in both, so it cannot tell them
    apart, while the decomposition reports (100%, 0%) there and
    (0%, 100%) here.

    Hand computation at `capital = 10_000`, `size_fraction = 0.18`:

    - budget    = 10_000 * 0.18                        = $1_800.00
    - REQUESTED = 1_800 / 0.20                          = 9_000 contracts
    - notional  = 9_000 * 0.20                          = $1_800.00
    - cap       = min(10_000 * 0.20, 10_000 * 0.99)
                = min(2_000.00, 9_900.00)               = $2_000.00
    - 1_800 <= 2_000 -> the cap does NOT bind: ORDERED = 9_000 contracts
    - book depth = 0.02 * 2500 / 0.10                   = 500 contracts
    - 500 < 9_000 -> the walk consumes the whole side and stops:
      FILLED = 500 contracts, and `atomicity="best_effort"` COMMITS it.
    """
    monkeypatch.setitem(STRATEGIES, "test_planted_complement", _PlantedComplementStrategy)

    report, results = await _sweep_capturing_results(
        {"market_id": "m1", "size_fraction": 0.18}, [10_000.0]
    )
    row = report.rows[0]

    assert row.sized_intents == 1
    assert row.pct_intents_capital_capped == 0.0
    assert row.pct_intents_depth_limited == 1.0
    # best_effort commits the partial, so nothing was BLOCKED.
    assert row.depth_blocked_intents == 0
    assert row.pct_intents_downsized == 1.0
    assert row.downsize_trackable_intents == 1

    buys = [t for t in results[10_000.0].trades if t.side == "BUY"]
    assert len(buys) == 2
    for trade in buys:
        assert trade.requested_size == 9_000.0
        assert trade.ordered_size == 9_000.0  # capital allowed all of it
        assert trade.size == 500.0  # the book delivered 500

    # The two causes are DISTINGUISHABLE even though the union is
    # identical (1.0) in both scenarios — which is the whole claim.
    cap_report, _ = await _sweep_capturing_results(
        {"market_id": "m1", "size_fraction": 0.30}, [400.0]
    )
    cap_row = cap_report.rows[0]
    assert cap_row.pct_intents_downsized == row.pct_intents_downsized == 1.0
    assert (cap_row.pct_intents_capital_capped, cap_row.pct_intents_depth_limited) == (
        1.0,
        0.0,
    )
    assert (row.pct_intents_capital_capped, row.pct_intents_depth_limited) == (0.0, 1.0)


async def test_all_or_none_depth_block_is_invisible_to_the_union_metric(
    monkeypatch,
) -> None:
    """A depth failure that commits NOTHING must still register. (T28)

    Same sizing as `test_book_depth_alone_is_reported_as_depth_not_capital`
    — 9_000 contracts ordered against a 500-contract book — but with
    `atomicity="all_or_none"`. `BacktestConfig.partial_tolerance`
    defaults to `0.0`, so the required fill is `9_000 * (1 - 0.0)
    = 9_000` and a 500-contract walk misses it: the WHOLE intent is
    rejected `fok_insufficient_depth` and produces no `TradeRecord` at
    all.

    That is the purest depth exhaustion this engine can express, and it
    is structurally invisible to any trade-derived metric — including
    `pct_intents_downsized`, which reads a flat `0.0` here with a
    denominator of zero. It is also the shape the repo's flagship
    `binary_complement_arbitrage` (`all_or_none`) produces for EVERY
    depth failure it can have.

    The overlap with `fill_rate` is deliberate and quantified rather
    than avoided: `fill_rate` is 0/1 = 0.0 because nothing executed, and
    `depth_blocked_intents` is 1 because the reason was depth. The two
    are never summed; the blocked count is what makes the overlap a
    number instead of an inference.
    """
    monkeypatch.setitem(STRATEGIES, "test_planted_complement", _PlantedComplementStrategy)

    report, results = await _sweep_capturing_results(
        {"market_id": "m1", "size_fraction": 0.18, "atomicity": "all_or_none"},
        [10_000.0],
    )
    row = report.rows[0]

    assert row.trades == 0
    # The union metric is blind here: no trade carries a stamp at all.
    assert row.pct_intents_downsized == 0.0
    assert row.downsize_trackable_intents == 0

    # The depth signal is not blind.
    assert row.sized_intents == 1
    assert row.pct_intents_depth_limited == 1.0
    assert row.depth_blocked_intents == 1
    assert row.pct_intents_capital_capped == 0.0

    # The overlap with fill_rate, stated as numbers: 1 intent generated,
    # 0 executed -> 0.0, and the one rejection is named as a depth kill.
    assert row.fill_rate == 0.0
    assert row.rejection_reasons.get("fok_insufficient_depth") == 1
    assert row.zero_trades_cause is not None
    assert row.zero_trades_cause.startswith("structural: fok_insufficient_depth")

    result = results[10_000.0]
    assert result.intents_generated == 1
    assert result.intents_executed == 0
    assert result.intents_sized == 1
    assert result.intents_depth_limited == 1
    assert result.intents_depth_blocked == 1
