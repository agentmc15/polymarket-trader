"""T09: resolution settlement, locked capital, and survivorship coverage.

Every money number asserted here is computed BY HAND in a comment next to
the assertion (GUARDRAILS.md §5) — never with the code under test.

Constants used throughout, all sourced not invented:

- Polymarket taker fee `= contracts * rate * price * (1 - price)`
  (PLAN.md §3), `rate = 0.04` for the `"politics"` category
  (`app/venues/fees.py::POLYMARKET_CATEGORY_TAKER_RATES`).
- `redemption_gas_usd = 0.05`, **PER POSITION** (PLAN.md §3 models it as
  the Polygon cost of the redemption transaction, not a per-contract
  cost). `test_complement_pair_gas_is_per_position_not_per_contract`
  proves that unit from the engine's own output rather than trusting it.
"""
from datetime import datetime, timedelta
from typing import Any

import pytest

from app.models.market import Market
from app.models.price_history import PriceHistory
from app.services.backtesting import (
    SETTLE_SIDE,
    SETTLEMENT_KEY,
    BacktestConfig,
    Backtester,
    DataReplayer,
    InMemoryDataReplayer,
    ResolutionEvent,
    SlippageModel,
)
from app.strategies.base import BaseStrategy, Intent, Leg, MarketSnapshot, Signal
from app.utils.time import utcnow

T0 = utcnow().replace(microsecond=0)

#: Per-POSITION redemption cost used by every test here. Pinned on the
#: config rather than read from `Settings` so the hand computations below
#: cannot drift if an operator changes the deployment default.
GAS = 0.05


def _snap(
    *,
    ts: datetime,
    market_id: str = "m1",
    yes_ask: float = 0.45,
    no_ask: float = 0.48,
    volume_24h: float = 100_000.0,
    **kw: Any,
) -> MarketSnapshot:
    """Build a snapshot with explicitly controlled top-of-book quotes.

    Args:
        ts: Aware UTC snapshot timestamp.
        market_id: Market identifier.
        yes_ask: Best YES ask.
        no_ask: Best NO ask.
        volume_24h: 24h notional, which drives synthetic depth.
        **kw: Any other `MarketSnapshot` field override.

    Returns:
        MarketSnapshot: The snapshot.
    """
    fields: dict[str, Any] = {
        "market_id": market_id,
        "token_id": f"{market_id}_yes",
        "timestamp": ts,
        "yes_price": yes_ask,
        "no_price": no_ask,
        "yes_bid": max(0.0, yes_ask - 0.01),
        "yes_ask": yes_ask,
        "no_bid": max(0.0, no_ask - 0.01),
        "no_ask": no_ask,
        "spread": 0.01,
        "volume_24h": volume_24h,
        "question": "Settlement fixture?",
        "category": "politics",
    }
    fields.update(kw)
    return MarketSnapshot(**fields)


def _config(**kw: Any) -> BacktestConfig:
    """Build a `BacktestConfig` with slippage padding OFF and gas pinned.

    Args:
        **kw: Config overrides.

    Returns:
        BacktestConfig: The configuration.
    """
    fields: dict[str, Any] = {
        "start_date": T0 - timedelta(minutes=1),
        "end_date": T0 + timedelta(days=7),
        "initial_capital": 10_000.0,
        "slippage_model": SlippageModel.NONE,
        "liquidity_fraction": 0.02,
        "fill_at": "same",
        "redemption_gas_usd": GAS,
        "settlement_delay_hours": 24.0,
    }
    fields.update(kw)
    return BacktestConfig(**fields)


class _StateObserver:
    """Record portfolio state after every processed stream item.

    `progress_callback` fires once per snapshot AND once per resolution
    event, immediately after the item is applied — which is the only
    place from which the mid-run split between spendable `cash` and
    escrowed `pending_settlements` is observable.

    Attributes:
        timeline: One `(cash, pending_total, position_ids)` entry per
            processed stream item, in order.
    """

    def __init__(self) -> None:
        self._backtester: Backtester | None = None
        self.timeline: list[tuple[float, float, set[str]]] = []

    def bind(self, backtester: Backtester) -> None:
        """Attach the backtester whose state should be captured."""
        self._backtester = backtester

    def __call__(self, progress: float) -> None:  # noqa: ARG002
        """Record the current portfolio split (progress is unused)."""
        assert self._backtester is not None, "bind() before running"
        portfolio = self._backtester.portfolio
        self.timeline.append(
            (
                portfolio.cash,
                portfolio.pending_settlement_total,
                set(portfolio.positions),
            )
        )


class _ComplementOnce(BaseStrategy):
    """Buys YES + NO on one market, once, then never trades again."""

    name = "test_complement_once"

    def __init__(self, *, size_usd: float = 93.0, market_id: str = "m1") -> None:
        super().__init__({})
        self._emitted = False
        self._size_usd = size_usd
        self._market_id = market_id

    def on_market_data(self, snapshot: MarketSnapshot) -> Intent | None:
        """Emit the complement intent once."""
        if self._emitted or snapshot.market_id != self._market_id:
            return None
        self._emitted = True
        return Intent(
            kind="complement",
            legs=[
                Leg(
                    market_id=snapshot.market_id,
                    outcome="YES",
                    side="BUY",
                    limit_price=0.45,
                ),
                Leg(
                    market_id=snapshot.market_id,
                    outcome="NO",
                    side="BUY",
                    limit_price=0.48,
                ),
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
        """Return the fixed USD budget for the intent."""
        return self._size_usd


class _BuyYes(BaseStrategy):
    """Buys YES once on `once_market`, and every time on `repeat_market`."""

    name = "test_buy_yes"

    def __init__(
        self,
        *,
        once_market: str,
        repeat_market: str,
        limit_price: float,
        size_usd: float,
    ) -> None:
        super().__init__({})
        self._once_market = once_market
        self._repeat_market = repeat_market
        self._limit_price = limit_price
        self._size_usd = size_usd
        self._emitted_once = False

    def on_market_data(self, snapshot: MarketSnapshot) -> Intent | None:
        """Emit a one-leg BUY on the configured markets."""
        if snapshot.market_id == self._once_market:
            if self._emitted_once:
                return None
            self._emitted_once = True
        elif snapshot.market_id != self._repeat_market:
            return None
        return Intent(
            kind="single",
            legs=[
                Leg(
                    market_id=snapshot.market_id,
                    outcome="YES",
                    side="BUY",
                    limit_price=self._limit_price,
                )
            ],
            hold_to_resolution=True,
            atomicity="best_effort",
            confidence=0.8,
        )

    def calculate_position_size(
        self,
        signal: Signal,  # noqa: ARG002
        portfolio_value: float,  # noqa: ARG002
        positions: dict[str, Any],  # noqa: ARG002
    ) -> float:
        """Return the fixed USD budget for the intent."""
        return self._size_usd


class _NeverTrades(BaseStrategy):
    """Observes the stream and never emits anything."""

    name = "test_never_trades"

    def on_market_data(self, snapshot: MarketSnapshot) -> None:  # noqa: ARG002
        """Never trade."""
        return None

    def calculate_position_size(
        self,
        signal: Signal,  # noqa: ARG002
        portfolio_value: float,  # noqa: ARG002
        positions: dict[str, Any],  # noqa: ARG002
    ) -> float:
        """Never size anything."""
        return 0.0


async def _run_complement(size_usd: float) -> Any:
    """Buy a YES+NO complement, resolve YES, and return the result.

    Args:
        size_usd: Budget for the complement, which buys
            `size_usd / 0.93` contracts of EACH leg.

    Returns:
        BacktestResult: The completed run.
    """
    strategy = _ComplementOnce(size_usd=size_usd)
    backtester = Backtester(_config(), strategy)
    replayer = InMemoryDataReplayer(
        [_snap(ts=T0)],
        resolutions=[
            ResolutionEvent(
                market_id="m1",
                venue="polymarket",
                winning_outcome="YES",
                resolved_at=T0 + timedelta(hours=1),
            )
        ],
    )
    return await backtester.run(replayer)


# ---------------------------------------------------------------------------
# 1 + 2: a YES position pays $1.00/contract; the NO position on the same
#        event pays $0.00. Both pay redemption gas ONCE, per position.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_yes_position_settles_at_one_dollar_per_contract_minus_gas() -> None:
    """The winning leg is paid $1.00 a contract, less one gas charge.

        contracts    = 93.00 / (0.45 + 0.48) = 100
        entry fee    = 100 * 0.04 * 0.45 * 0.55 = 0.99
        cost basis   = 100 * 0.45 + 0.99       = 45.99
        gross payout = 100 * 1.00              = 100.00
        net proceeds = 100.00 - 0.05           =  99.95
        pnl          =  99.95 - 45.99          =  53.96
    """
    result = await _run_complement(93.0)

    settles = [t for t in result.trades if t.side == SETTLE_SIDE]
    assert len(settles) == 2
    yes_settle = next(t for t in settles if t.outcome == "YES")

    assert yes_settle.price == pytest.approx(1.0)
    assert yes_settle.size == pytest.approx(100.0)
    assert yes_settle.fee == pytest.approx(GAS)
    assert yes_settle.pnl == pytest.approx(53.96, abs=1e-9)
    assert yes_settle.metadata[SETTLEMENT_KEY] is True
    assert yes_settle.metadata["won"] is True
    assert yes_settle.metadata["winning_outcome"] == "YES"
    assert yes_settle.timestamp == T0 + timedelta(hours=1)

    assert result.positions_settled == 2
    # Settled, not marked: nothing is left over to estimate.
    assert result.unrealized_at_end == []
    assert result.unrealized_notional_at_end == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_no_position_settles_at_zero_on_the_same_event() -> None:
    """The losing leg pays $0.00 a contract and still pays its gas.

        contracts    = 100
        entry fee    = 100 * 0.04 * 0.48 * 0.52 =  0.9984
        cost basis   = 100 * 0.48 + 0.9984      = 48.9984
        gross payout = 100 * 0.00               =  0.00
        net proceeds =   0.00 - 0.05            = -0.05
        pnl          =  -0.05 - 48.9984         = -49.0484

    The loser is charged redemption gas too. That is the conservative
    modeling choice, and it is what makes a complement pair's total gas
    exactly `2 x 0.05` no matter which side wins — the property the
    per-contract arithmetic below depends on.
    """
    result = await _run_complement(93.0)

    settles = [t for t in result.trades if t.side == SETTLE_SIDE]
    no_settle = next(t for t in settles if t.outcome == "NO")

    assert no_settle.price == pytest.approx(0.0)
    assert no_settle.size == pytest.approx(100.0)
    assert no_settle.fee == pytest.approx(GAS)
    assert no_settle.pnl == pytest.approx(-49.0484, abs=1e-9)
    assert no_settle.metadata["won"] is False


# ---------------------------------------------------------------------------
# 3: the complement pair — the arbitrage this repo exists to find
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complement_pair_nets_one_minus_cost_minus_fees_minus_two_gas() -> None:
    """Buying YES at 0.45 and NO at 0.48 nets a hand-computed edge.

    Per contract, for a 100-contract pair:

        gross edge   = 1.00 - (0.45 + 0.48)          = 0.070000
        entry fees   = (0.99 + 0.9984) / 100         = 0.019884
        redemption   = (2 * 0.05) / 100              = 0.001000
        net          = 0.070000 - 0.019884 - 0.001   = 0.049116

        total pnl    = 100 * 0.049116                = 4.9116

    Read the second and third lines together before believing the first.
    The gross edge is $0.07 a contract, but the redemption gas is $0.10
    for the PAIR regardless of size. At one contract the same trade nets
    `0.07 - 0.019884 - 0.10 = -0.049884` — a LOSS. Break-even is
    `0.10 / 0.050116 = 1.995`, i.e. two contracts. This "riskless
    arbitrage" is only riskless; it is not automatically profitable, and
    it is the fixed per-position cost, not the spread, that decides.
    """
    result = await _run_complement(93.0)

    total_pnl = result.final_value - result.initial_capital
    assert total_pnl == pytest.approx(4.9116, abs=1e-9)

    settles = [t for t in result.trades if t.side == SETTLE_SIDE]
    assert sum(t.pnl or 0.0 for t in settles) == pytest.approx(4.9116, abs=1e-9)

    # Per contract, to 1e-9.
    assert total_pnl / 100.0 == pytest.approx(0.049116, abs=1e-9)

    # The one-contract case loses money, computed from the same terms.
    one_contract_net = 0.07 - 0.019884 - (2 * GAS)
    assert one_contract_net == pytest.approx(-0.049884, abs=1e-9)
    assert one_contract_net < 0.0


@pytest.mark.asyncio
async def test_complement_pair_gas_is_per_position_not_per_contract() -> None:
    """Two run sizes pin the gas unit, rather than trusting the docstring.

    P&L against contracts must be a straight line whose INTERCEPT is the
    fixed redemption cost:

        pnl(n) = n * 0.050116 - 0.10

        pnl(100) = 100 * 0.050116 - 0.10 = 4.90116 + 0.1104 -> 4.9116
        pnl(20)  =  20 * 0.050116 - 0.10 = 1.00232 - 0.10   -> 0.90232

        slope     = (4.9116 - 0.90232) / (100 - 20) = 0.050116
        intercept = 4.9116 - 100 * 0.050116         = -0.10 = -2 * gas

    If gas were charged PER CONTRACT the 100-contract run would pay $10
    of gas and net `5.0116 - 10 = -4.9884` instead of `+4.9116`, so this
    assertion fails loudly on the wrong unit rather than silently making
    small positions look ruinous and large ones free.
    """
    # 100 contracts: 100 * (0.45 + 0.48) = 93.00 of budget.
    result_100 = await _run_complement(93.0)
    # 20 contracts: 20 * 0.93 = 18.60 of budget.
    result_20 = await _run_complement(18.60)

    pnl_100 = result_100.final_value - result_100.initial_capital
    pnl_20 = result_20.final_value - result_20.initial_capital

    assert pnl_100 == pytest.approx(4.9116, abs=1e-9)
    assert pnl_20 == pytest.approx(0.90232, abs=1e-9)

    slope = (pnl_100 - pnl_20) / (100.0 - 20.0)
    intercept = pnl_100 - 100.0 * slope
    assert slope == pytest.approx(0.050116, abs=1e-9)
    assert intercept == pytest.approx(-2 * GAS, abs=1e-9)


# ---------------------------------------------------------------------------
# 4: settlement proceeds are not spendable until they are released
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_settlement_proceeds_are_locked_until_the_delay_elapses() -> None:
    """Cash from a settled market cannot fund a trade before it clears.

    Timeline, on 60.00 of starting capital:

        T0      buy 100 YES on m1 at 0.50
                  fee  = 100 * 0.04 * 0.50 * 0.50 = 1.00
                  cost = 100 * 0.50 + 1.00        = 51.00
                  cash = 60.00 - 51.00            =  9.00
        T0+1h   m1 resolves YES
                  proceeds = 100 * 1.00 - 0.05    = 99.95, escrowed
                  cash still 9.00; equity unchanged (the money exists)
        T0+2h   m2 prints. The strategy wants 50.00 of notional, but the
                spendable cap is 9.00 * 0.99 = 8.91 — under the engine's
                10.00 minimum — so the intent is REJECTED outright.
        T0+26h  the 24h delay has elapsed; cash = 9.00 + 99.95 = 108.95
                and the same intent now executes.

    Without the lock the T0+2h intent would fill, compounding capital a
    full day before any real account could have moved it — which is
    exactly how a backtest manufactures a return curve no live system can
    reproduce.
    """
    config = _config(
        initial_capital=60.0,
        max_position_pct=1.0,
        settlement_delay_hours=24.0,
    )
    strategy = _BuyYes(
        once_market="m1", repeat_market="m2", limit_price=0.50, size_usd=50.0
    )
    observer = _StateObserver()
    backtester = Backtester(config, strategy, progress_callback=observer)
    observer.bind(backtester)

    replayer = InMemoryDataReplayer(
        [
            _snap(ts=T0, market_id="m1", yes_ask=0.50, no_ask=0.50),
            _snap(ts=T0 + timedelta(hours=2), market_id="m2", yes_ask=0.50, no_ask=0.50),
            _snap(
                ts=T0 + timedelta(hours=26), market_id="m2", yes_ask=0.50, no_ask=0.50
            ),
        ],
        resolutions=[
            ResolutionEvent(
                market_id="m1",
                venue="polymarket",
                winning_outcome="YES",
                resolved_at=T0 + timedelta(hours=1),
            )
        ],
    )

    result = await backtester.run(replayer)

    # Four stream items: m1 snapshot, m1 resolution, two m2 snapshots.
    assert len(observer.timeline) == 4

    cash_after_buy, pending_after_buy, positions_after_buy = observer.timeline[0]
    assert cash_after_buy == pytest.approx(9.0, abs=1e-9)
    assert pending_after_buy == pytest.approx(0.0)
    assert positions_after_buy == {"polymarket:m1:YES"}

    cash_after_resolve, pending_after_resolve, positions_after_resolve = (
        observer.timeline[1]
    )
    # Spendable cash did NOT move; the payout is escrowed.
    assert cash_after_resolve == pytest.approx(9.0, abs=1e-9)
    assert pending_after_resolve == pytest.approx(99.95, abs=1e-9)
    assert positions_after_resolve == set()

    # T0+2h: still locked, so the intent is rejected for want of cash.
    cash_locked, pending_locked, positions_locked = observer.timeline[2]
    assert cash_locked == pytest.approx(9.0, abs=1e-9)
    assert pending_locked == pytest.approx(99.95, abs=1e-9)
    assert positions_locked == set(), "m2 must not have been bought while locked"

    # T0+26h: released, and the same intent now fills.
    cash_released, pending_released, positions_released = observer.timeline[3]
    assert pending_released == pytest.approx(0.0)
    assert positions_released == {"polymarket:m2:YES"}

    assert result.intent_rejections == 1
    assert result.rejection_reasons == {"below_min_size": 1}
    buys_on_m2 = [
        t for t in result.trades if t.market_id == "m2" and t.side == "BUY"
    ]
    assert len(buys_on_m2) == 1
    assert buys_on_m2[0].timestamp == T0 + timedelta(hours=26)


# ---------------------------------------------------------------------------
# 5: survivorship coverage census
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coverage_report_counts_three_markets_one_unresolved() -> None:
    """Coverage counts markets seen, resolved, and left unresolved.

    Fixture: m1 prints twice and resolves, m2 prints three times and
    resolves, m3 prints once and never resolves.

        markets_seen              = 3
        markets_resolved          = 2
        markets_closed_unresolved = 1
        resolution_coverage       = 2 / 3 = 0.6667 -> below 0.8
    """
    strategy = _NeverTrades()
    backtester = Backtester(_config(), strategy)
    replayer = InMemoryDataReplayer(
        [
            _snap(ts=T0, market_id="m1"),
            _snap(ts=T0 + timedelta(minutes=5), market_id="m1"),
            _snap(ts=T0 + timedelta(minutes=1), market_id="m2"),
            _snap(ts=T0 + timedelta(minutes=6), market_id="m2"),
            _snap(ts=T0 + timedelta(minutes=11), market_id="m2"),
            _snap(ts=T0 + timedelta(minutes=2), market_id="m3"),
        ],
        resolutions=[
            ResolutionEvent(
                market_id="m1",
                venue="polymarket",
                winning_outcome="YES",
                resolved_at=T0 + timedelta(hours=1),
            ),
            ResolutionEvent(
                market_id="m2",
                venue="polymarket",
                winning_outcome="NO",
                resolved_at=T0 + timedelta(hours=2),
            ),
        ],
    )

    result = await backtester.run(replayer)
    coverage = result.coverage

    assert coverage.markets_seen == 3
    assert coverage.markets_resolved == 2
    assert coverage.markets_closed_unresolved == 1
    assert coverage.snapshots_per_market == {"m1": 2, "m2": 3, "m3": 1}
    assert coverage.resolution_coverage == pytest.approx(2 / 3)
    assert coverage.low_resolution_coverage is True


# ---------------------------------------------------------------------------
# Look-ahead hygiene: no future-encoding field may reach a snapshot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replayed_snapshot_never_carries_resolution_fields(
    test_session: Any,
) -> None:
    """A resolved market's snapshot must not expose the answer.

    The `Market` row here is fully resolved — `is_resolved=True`,
    `resolution_outcome="YES"`, `resolved_at` set, and `outcome_prices`
    holding the post-resolution 1.00/0.00 — and the replayer is asked for
    a snapshot recorded BEFORE any of that was known.

    The snapshot must carry the market's PUBLISHED criteria
    (`resolution_rules`) and nothing that states the outcome. A strategy
    that could read `is_resolved` off the object it is deciding from is
    reading the answer sheet, and every number the backtest produces
    after that is fiction — so absence is asserted explicitly, not merely
    presence of the good fields.
    """
    snapshot_ts = T0
    market = Market(
        condition_id="0xresolved",
        question="Will it resolve YES?",
        category="politics",
        token_ids={"yes": "0xyes", "no": "0xno"},
        outcomes=["YES", "NO"],
        is_active=False,
        is_resolved=True,
        resolution_outcome="YES",
        end_date=snapshot_ts + timedelta(hours=1),
        resolved_at=snapshot_ts + timedelta(hours=2),
        outcome_prices={"YES": 1.0, "NO": 0.0},
        extra_data={"resolution_rules": "Resolves YES if the official count exceeds 100."},
    )
    test_session.add(market)
    test_session.add(
        PriceHistory(
            market_id="0xresolved",
            timestamp=snapshot_ts,
            yes_price=0.45,
            no_price=0.55,
            yes_bid=0.44,
            yes_ask=0.46,
            volume_24h=10_000.0,
        )
    )
    await test_session.commit()

    replayer = DataReplayer(
        session=test_session,
        start_date=snapshot_ts - timedelta(hours=1),
        end_date=snapshot_ts + timedelta(hours=3),
    )

    items = [item async for item in replayer]
    snapshots = [i for i in items if isinstance(i, MarketSnapshot)]
    assert len(snapshots) == 1
    snapshot = snapshots[0]

    # The published criteria ARE carried — they are knowable up front.
    assert snapshot.resolution_rules == (
        "Resolves YES if the official count exceeds 100."
    )
    assert snapshot.question == "Will it resolve YES?"

    # The answer is NOT, in any form.
    for forbidden in (
        "is_resolved",
        "resolution_outcome",
        "resolved_at",
        "outcome_prices",
    ):
        assert not hasattr(snapshot, forbidden), (
            f"snapshot exposes future-encoding field {forbidden!r}"
        )
    # Nor smuggled through the free-form dicts a snapshot does carry.
    assert "resolution_outcome" not in snapshot.orderbook
    assert "is_resolved" not in snapshot.orderbook

    # Resolution reaches the engine only as a separate, timestamped event.
    events = [i for i in items if isinstance(i, ResolutionEvent)]
    assert len(events) == 1
    assert events[0].winning_outcome == "YES"
    assert events[0].resolved_at == snapshot_ts + timedelta(hours=2)
    # And it arrives AFTER the snapshot that predates it.
    assert items.index(events[0]) > items.index(snapshot)
