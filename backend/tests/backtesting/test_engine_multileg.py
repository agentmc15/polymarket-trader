"""T08: multi-leg intents, next-snapshot fills, and look-ahead invariance.

Every money number asserted here is computed BY HAND in a comment next to
the assertion (GUARDRAILS.md §5) — never with the code under test.

The Polymarket taker fee used throughout is
`fee = contracts * rate * price * (1 - price)` (PLAN.md §3), with
`rate = 0.04` for the `"politics"` category
(`app/venues/fees.py::POLYMARKET_CATEGORY_TAKER_RATES`).
"""
from datetime import datetime, timedelta
from typing import Any

import pytest

from app.services.backtesting import (
    DIAGNOSTIC_SAME_SNAPSHOT_KEY,
    VENUE_MISMATCH_REASON,
    BacktestConfig,
    Backtester,
    InMemoryDataReplayer,
    SlippageModel,
)
from app.services.backtesting.engine import TradeRecord
from app.strategies.base import (
    BaseStrategy,
    Intent,
    Leg,
    MarketSnapshot,
    Signal,
    SignalType,
)
from app.utils.time import utcnow
from app.venues.types import OrderBook
from tests.helpers import make_book

T0 = utcnow().replace(microsecond=0)


def _snap(
    *,
    ts: datetime,
    market_id: str = "m1",
    yes_ask: float = 0.45,
    no_ask: float = 0.48,
    volume_24h: float = 100_000.0,
    book: OrderBook | None = None,
    **kw: Any,
) -> MarketSnapshot:
    """Build a snapshot with explicitly controlled top-of-book quotes.

    `tests.helpers.make_snapshot` derives quotes symmetrically from a
    single mid, which cannot express the complement violation
    (`yes_ask + no_ask < 1`) these tests are about, so the fields are set
    here directly.

    Args:
        ts: Aware UTC snapshot timestamp.
        market_id: Market identifier.
        yes_ask: Best YES ask.
        no_ask: Best NO ask.
        volume_24h: 24h notional, which drives synthetic depth.
        book: Optional RECORDED book to attach.
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
        "question": "Complement arbitrage fixture?",
        "category": "politics",
        "book": book,
    }
    fields.update(kw)
    return MarketSnapshot(**fields)


def _config(**kw: Any) -> BacktestConfig:
    """Build a `BacktestConfig` with slippage padding OFF.

    `SlippageModel.NONE` keeps the limit price exactly what the strategy
    asked for, so the expected fill prices and fees below stay
    hand-computable. The pad itself is a separate concern from multi-leg
    atomicity and next-snapshot timing.

    Args:
        **kw: Config overrides.

    Returns:
        BacktestConfig: The configuration.
    """
    fields: dict[str, Any] = {
        "start_date": T0 - timedelta(minutes=1),
        "end_date": T0 + timedelta(days=1),
        "initial_capital": 10_000.0,
        "slippage_model": SlippageModel.NONE,
        "liquidity_fraction": 0.02,
    }
    fields.update(kw)
    return BacktestConfig(**fields)


class _ComplementStrategy(BaseStrategy):
    """Emits ONE complement intent (YES + NO, same market), then nothing."""

    name = "test_complement"

    def __init__(
        self,
        *,
        yes_limit: float = 0.45,
        no_limit: float = 0.48,
        atomicity: str = "all_or_none",
        size_usd: float = 93.0,
        market_id: str = "m1",
    ) -> None:
        super().__init__({})
        self._emitted = False
        self._yes_limit = yes_limit
        self._no_limit = no_limit
        self._atomicity = atomicity
        self._size_usd = size_usd
        self._market_id = market_id

    def on_market_data(self, snapshot: MarketSnapshot) -> Intent | None:
        """Emit the complement intent once, on the first matching snapshot."""
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
                    limit_price=self._yes_limit,
                ),
                Leg(
                    market_id=snapshot.market_id,
                    outcome="NO",
                    side="BUY",
                    limit_price=self._no_limit,
                ),
            ],
            hold_to_resolution=True,
            atomicity=self._atomicity,  # type: ignore[arg-type]
            confidence=0.9,
        )

    def calculate_position_size(
        self,
        signal: Signal,  # noqa: ARG002
        portfolio_value: float,  # noqa: ARG002
        positions: dict[str, Any],  # noqa: ARG002
    ) -> float:
        """Return a fixed USD budget for the intent."""
        return self._size_usd


class _SingleBuyStrategy(BaseStrategy):
    """Emits a one-leg BUY intent, once or on every snapshot."""

    name = "test_single_buy"

    def __init__(
        self,
        *,
        limit_price: float,
        size_usd: float,
        market_id: str = "m1",
        once: bool = True,
    ) -> None:
        super().__init__({})
        self._limit_price = limit_price
        self._size_usd = size_usd
        self._market_id = market_id
        self._once = once
        self._emitted = False

    def on_market_data(self, snapshot: MarketSnapshot) -> Intent | None:
        """Emit a single-leg BUY intent on this strategy's market."""
        if snapshot.market_id != self._market_id:
            return None
        if self._once and self._emitted:
            return None
        self._emitted = True
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
            confidence=0.75,
        )

    def calculate_position_size(
        self,
        signal: Signal,  # noqa: ARG002
        portfolio_value: float,  # noqa: ARG002
        positions: dict[str, Any],  # noqa: ARG002
    ) -> float:
        """Return a fixed USD budget for the intent."""
        return self._size_usd


class _SingleSignalBuyStrategy(BaseStrategy):
    """Emits a one-leg BUY `Signal` (NOT an `Intent`), once.

    Used to prove Phase-1 remediation FIX 2: a `Signal`-returning
    strategy's leg must be venued from whichever snapshot triggered it,
    not `Leg.venue`'s bare `"polymarket"` default — six of the eight
    shipped strategies return a `Signal`, not an `Intent`.
    """

    name = "test_single_signal_buy"

    def __init__(
        self, *, limit_price: float, size_usd: float, market_id: str = "m1"
    ) -> None:
        super().__init__({})
        self._limit_price = limit_price
        self._size_usd = size_usd
        self._market_id = market_id
        self._emitted = False

    def on_market_data(self, snapshot: MarketSnapshot) -> Signal | None:
        """Emit a single BUY signal on this strategy's market, once."""
        if snapshot.market_id != self._market_id or self._emitted:
            return None
        self._emitted = True
        return Signal(
            type=SignalType.BUY,
            market_id=snapshot.market_id,
            token_id=snapshot.token_id,
            outcome="YES",
            price=self._limit_price,
            size=self._size_usd,
            confidence=0.75,
        )

    def calculate_position_size(
        self,
        signal: Signal,  # noqa: ARG002
        portfolio_value: float,  # noqa: ARG002
        positions: dict[str, Any],  # noqa: ARG002
    ) -> float:
        """Return a fixed USD budget for the intent."""
        return self._size_usd


class _WrongVenueIntentStrategy(BaseStrategy):
    """Emits a one-leg BUY `Intent` whose leg venue is deliberately wrong
    for the market it names — simulates the genuine, structural
    venue-mismatch Phase-1 remediation FIX 2's loud rejection defends
    against (the shipped strategies never do this; this is a stand-in
    for a strategy/engine defect).
    """

    name = "test_wrong_venue_intent"

    def __init__(
        self,
        *,
        limit_price: float,
        size_usd: float,
        market_id: str = "k1",
        leg_venue: str = "polymarket",
    ) -> None:
        super().__init__({})
        self._limit_price = limit_price
        self._size_usd = size_usd
        self._market_id = market_id
        self._leg_venue = leg_venue
        self._emitted = False

    def on_market_data(self, snapshot: MarketSnapshot) -> Intent | None:
        """Emit a single-leg BUY intent, mis-venued, once."""
        if snapshot.market_id != self._market_id or self._emitted:
            return None
        self._emitted = True
        return Intent(
            kind="single",
            legs=[
                Leg(
                    market_id=snapshot.market_id,
                    outcome="YES",
                    side="BUY",
                    limit_price=self._limit_price,
                    venue=self._leg_venue,  # type: ignore[arg-type]
                )
            ],
            hold_to_resolution=False,
            atomicity="best_effort",
            confidence=0.75,
        )

    def calculate_position_size(
        self,
        signal: Signal,  # noqa: ARG002
        portfolio_value: float,  # noqa: ARG002
        positions: dict[str, Any],  # noqa: ARG002
    ) -> float:
        """Return a fixed USD budget for the intent."""
        return self._size_usd


class _StateObserver:
    """Capture portfolio state after every processed snapshot.

    `Backtester.run()` marks all open positions out at `end_date` before
    it returns, so `result.positions_final` and the final cash balance
    cannot show what a mid-run intent actually booked. The public
    `progress_callback` hook fires immediately after each snapshot is
    processed and before that end-of-run sweep, which is exactly the
    observation point these tests need.

    Attributes:
        cash: Cash balance after the most recent snapshot.
        positions: `position_id -> (size, cost_basis)` after the most
            recent snapshot.
    """

    def __init__(self) -> None:
        self._backtester: Backtester | None = None
        self.cash = 0.0
        self.positions: dict[str, tuple[float, float]] = {}

    def bind(self, backtester: Backtester) -> None:
        """Attach the backtester whose state should be captured."""
        self._backtester = backtester

    def __call__(self, progress: float) -> None:  # noqa: ARG002
        """Record the current portfolio snapshot (progress is unused)."""
        assert self._backtester is not None, "bind() before running"
        self.cash = self._backtester.portfolio.cash
        self.positions = {
            pid: (pos.size, pos.cost_basis)
            for pid, pos in self._backtester.portfolio.positions.items()
        }


def _trade_fields(trade: TradeRecord) -> tuple[Any, ...]:
    """Flatten a `TradeRecord` into a comparable tuple of every field.

    Used by the look-ahead test: comparing counts (or even prices alone)
    would miss a future snapshot leaking into a trade's size, fee,
    slippage or metadata.

    Args:
        trade: The trade record.

    Returns:
        tuple: Every field of the record, metadata included.
    """
    return (
        trade.timestamp,
        trade.venue,
        trade.market_id,
        trade.outcome,
        trade.token_id,
        trade.side,
        trade.price,
        trade.size,
        trade.fee,
        trade.slippage,
        trade.pnl,
        trade.signal_confidence,
        trade.intent_id,
        tuple(sorted(trade.metadata.items())),
    )


# ---------------------------------------------------------------------------
# (a) complement intent, fill_at="same"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complement_fills_both_legs_equal_contracts_same_snapshot() -> None:
    """A complement intent buys the SAME CONTRACT COUNT of YES and NO.

    Asks 0.45 (YES) and 0.48 (NO), budget $93.00:

        contracts = 93.00 / (0.45 + 0.48) = 93.00 / 0.93 = 100.00 each

    Equal CONTRACTS is the only split that hedges. Equal DOLLARS would
    buy 46.50/0.45 = 103.33 YES against 46.50/0.48 = 96.875 NO, leaving a
    6.46-contract naked directional residual on a position the strategy
    believes is riskless.

    Cash:
        YES notional  = 100 * 0.45 = 45.0000
        YES fee       = 100 * 0.04 * 0.45 * 0.55 = 0.9900
        NO  notional  = 100 * 0.48 = 48.0000
        NO  fee       = 100 * 0.04 * 0.48 * 0.52 = 0.9984
        total         = 94.9884
        cash          = 10000.00 - 94.9884 = 9905.0116
    """
    config = _config(fill_at="same")
    strategy = _ComplementStrategy()
    # `run()` marks every open position out at `end_date` before returning
    # (PLAN.md D6; T09 replaces that with resolution settlement), so the
    # portfolio is observed through the public progress hook, which fires
    # immediately after each snapshot is processed.
    observed = _StateObserver()
    backtester = Backtester(config, strategy, progress_callback=observed)
    observed.bind(backtester)
    replayer = InMemoryDataReplayer([_snap(ts=T0)])

    result = await backtester.run(replayer)

    assert result.intents_generated == 1
    assert result.intents_executed == 1
    assert result.intent_rejections == 0
    assert result.fill_at == "same"

    # Two positions, one per leg, keyed `venue:market_id:outcome`.
    positions = observed.positions
    assert set(positions) == {"polymarket:m1:YES", "polymarket:m1:NO"}
    yes_size, yes_basis = positions["polymarket:m1:YES"]
    no_size, no_basis = positions["polymarket:m1:NO"]
    assert yes_size == pytest.approx(100.0)
    assert no_size == pytest.approx(100.0)
    assert yes_size == pytest.approx(no_size)

    # Cost basis is PER LEG, fees included.
    assert yes_basis == pytest.approx(45.0 + 0.99)
    assert no_basis == pytest.approx(48.0 + 0.9984)

    assert observed.cash == pytest.approx(9905.0116)

    # Two BUY trades, both stamped as diagnostic same-snapshot fills.
    buys = [t for t in backtester.trades if t.side == "BUY"]
    assert len(buys) == 2
    for trade in buys:
        assert trade.metadata[DIAGNOSTIC_SAME_SNAPSHOT_KEY] is True
        assert trade.timestamp == T0
    by_outcome = {t.outcome: t for t in buys}
    assert by_outcome["YES"].price == pytest.approx(0.45)
    assert by_outcome["YES"].fee == pytest.approx(0.99)
    assert by_outcome["NO"].price == pytest.approx(0.48)
    assert by_outcome["NO"].fee == pytest.approx(0.9984)

    # Depth was invented from volume, and the result says so.
    assert result.depth_source == "synthetic"
    # No `VenueMarket` is passed during replay, so no fill was tick-checked.
    assert result.tick_unvalidated_fills == 2
    assert result.undeclared_zero_fee_markets == ()
    assert result.crossed_book_skips == 0


# ---------------------------------------------------------------------------
# (b) all_or_none rejects when one leg's book is too thin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_or_none_rejects_when_no_book_too_thin() -> None:
    """A thin NO book must reject the WHOLE complement, not book the YES leg.

    The YES leg's synthetic book is deep (0.02 * 100000 / 0.45 = 4444.4
    contracts) and would fill all 100 contracts on its own. The NO leg's
    RECORDED book holds only 5 contracts at 0.48, so it fills 5 of 100 —
    5% of the requested size, far under `partial_tolerance = 0.0`.

    Committing the YES leg alone would turn a hedge into a $45 naked YES
    bet the strategy never asked for, so NOTHING executes: zero positions,
    cash untouched at exactly the initial 10000.00, one rejection.
    """
    thin_no_book = make_book(
        bids=[(0.47, 500.0)],
        asks=[(0.48, 5.0)],
        market_id="m1",
        outcome="NO",
        ts=T0,
    )
    config = _config(fill_at="same")
    strategy = _ComplementStrategy()
    backtester = Backtester(config, strategy)
    replayer = InMemoryDataReplayer([_snap(ts=T0, book=thin_no_book)])

    result = await backtester.run(replayer)

    assert result.intents_generated == 1
    assert result.intents_executed == 0
    assert result.intent_rejections == 1
    assert result.rejection_reasons == {"fok_insufficient_depth": 1}

    assert backtester.portfolio.positions == {}
    assert backtester.trades == []
    assert backtester.portfolio.cash == 10_000.0
    assert result.final_value == pytest.approx(10_000.0)

    # One synthetic (YES) book and one recorded (NO) book were walked.
    assert result.depth_source == "mixed"


# ---------------------------------------------------------------------------
# (c) fill_at="next" fills against N+1's book, never N's
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fill_at_next_uses_the_next_snapshots_ask() -> None:
    """A signal computed on N must fill against N+1's ask, not N's.

    N quotes YES at 0.60 and N+1 quotes it at 0.55. The intent's limit is
    0.60 (what the strategy saw on N), so BOTH prices are reachable — the
    test therefore distinguishes the two engines rather than merely
    proving that a stale limit fails to fill.

        contracts = 60.00 / 0.60 = 100.00
        fill      = 100 @ 0.55 (N+1's ask)
        fee       = 100 * 0.04 * 0.55 * 0.45 = 0.99

    Filling at N's 0.60 would be look-ahead: the strategy acted on a price
    in the same instant it observed it, which no live system can do.
    """
    t1 = T0 + timedelta(minutes=5)
    config = _config(fill_at="next")
    strategy = _SingleBuyStrategy(limit_price=0.60, size_usd=60.0)
    observed = _StateObserver()
    backtester = Backtester(config, strategy, progress_callback=observed)
    observed.bind(backtester)
    replayer = InMemoryDataReplayer(
        [
            _snap(ts=T0, yes_ask=0.60, no_ask=0.42),
            _snap(ts=t1, yes_ask=0.55, no_ask=0.47),
        ]
    )

    result = await backtester.run(replayer)

    assert result.fill_at == "next"
    assert result.intents_generated == 1
    assert result.intents_executed == 1
    assert result.intent_expirations == 0

    buys = [t for t in backtester.trades if t.side == "BUY"]
    assert len(buys) == 1
    trade = buys[0]
    assert trade.price == pytest.approx(0.55)  # N+1's ask
    assert trade.price != pytest.approx(0.60)  # NOT N's ask
    assert trade.timestamp == t1
    assert trade.size == pytest.approx(100.0)
    assert trade.fee == pytest.approx(0.99)
    # Never stamped as a diagnostic: this is the look-ahead-free path.
    assert DIAGNOSTIC_SAME_SNAPSHOT_KEY not in trade.metadata

    # The position exists after N+1 was processed (observed before the
    # end-of-run mark-out), holding the contracts bought at N+1's ask.
    size, cost_basis = observed.positions["polymarket:m1:YES"]
    assert size == pytest.approx(100.0)
    # 100 * 0.55 + 0.99 = 55.99
    assert cost_basis == pytest.approx(55.99)


# ---------------------------------------------------------------------------
# (d) a pending intent whose market goes quiet expires
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_intent_expires_when_its_market_goes_quiet() -> None:
    """No next snapshot within `pending_ttl` means the intent expires.

    The intent is generated on m1 at T0 with a one-hour TTL. m1 then goes
    silent; only m2 keeps printing. When m2's T0+2h snapshot advances the
    replay clock past the TTL the intent is dropped, so m1's eventual
    T0+3h snapshot fills nothing.

    Expiring rather than filling three hours later is the point: a signal
    computed off a three-hour-old price is not the signal the strategy
    made, and filling it would credit the backtest with a trade no live
    system would have placed.
    """
    config = _config(fill_at="next", pending_ttl=timedelta(hours=1))
    strategy = _SingleBuyStrategy(limit_price=0.60, size_usd=60.0, market_id="m1")
    backtester = Backtester(config, strategy)
    replayer = InMemoryDataReplayer(
        [
            _snap(ts=T0, market_id="m1", yes_ask=0.60),
            _snap(ts=T0 + timedelta(hours=2), market_id="m2", yes_ask=0.50),
            _snap(ts=T0 + timedelta(hours=3), market_id="m1", yes_ask=0.55),
        ]
    )

    result = await backtester.run(replayer)

    assert result.intents_generated == 1
    assert result.intent_expirations == 1
    assert result.intents_executed == 0
    assert backtester.trades == []
    assert backtester.portfolio.positions == {}
    assert backtester.portfolio.cash == 10_000.0


@pytest.mark.asyncio
async def test_pending_intent_expires_when_the_stream_ends() -> None:
    """An intent still queued when the replay ends never filled, and says so.

    There is no second snapshot at all, so the TTL clock never advances —
    the end-of-run sweep must still account for the intent rather than
    letting it vanish and leaving `intents_generated` unexplained.
    """
    config = _config(fill_at="next")
    strategy = _SingleBuyStrategy(limit_price=0.60, size_usd=60.0)
    backtester = Backtester(config, strategy)
    replayer = InMemoryDataReplayer([_snap(ts=T0, yes_ask=0.60)])

    result = await backtester.run(replayer)

    assert result.intents_generated == 1
    assert result.intent_expirations == 1
    assert result.intents_executed == 0
    assert result.trades == []


# ---------------------------------------------------------------------------
# (e) look-ahead invariance — the most important test in the kit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trades_through_n_plus_1_are_invariant_to_what_happens_at_n_plus_2() -> (
    None
):
    """Two streams identical through N+1 must produce identical trades there.

    Run A and run B see byte-identical snapshots at T0 and T0+5m. At
    T0+10m run B diverges as violently as a prediction market can:

    - the YES price gaps from 0.50 to 0.99 (the market effectively
      resolving YES),
    - 24h volume collapses from 100,000 to 0, so the synthesized book has
      NO depth at all,
    - the quoted spread blows out and the market's end date moves.

    Every trade dated at or before T0+5m must be identical — the same
    price, size, fee, slippage, confidence, intent id and metadata, not
    merely the same count. If a later snapshot can change an earlier
    trade, the engine is reading the future and every number it has ever
    produced is fiction.
    """
    t1 = T0 + timedelta(minutes=5)
    t2 = T0 + timedelta(minutes=10)

    shared = [
        _snap(ts=T0, yes_ask=0.50, no_ask=0.52),
        _snap(ts=t1, yes_ask=0.51, no_ask=0.51),
    ]
    calm_future = _snap(ts=t2, yes_ask=0.52, no_ask=0.50)
    violent_future = _snap(
        ts=t2,
        yes_ask=0.99,
        no_ask=0.01,
        volume_24h=0.0,
        spread=0.40,
        end_date=t2 + timedelta(days=365),
        question="Market resolved YES",
    )

    async def _run(tail: list[MarketSnapshot]) -> Any:
        strategy = _SingleBuyStrategy(limit_price=0.99, size_usd=50.0, once=False)
        backtester = Backtester(_config(fill_at="next"), strategy)
        return await backtester.run(InMemoryDataReplayer([*shared, *tail]))

    result_a = await _run([calm_future])
    result_b = await _run([violent_future])
    # A third stream that simply STOPS after N+1: if an earlier trade
    # depends on later data even EXISTING, this catches it.
    result_c = await _run([])

    def _prefix(result: Any) -> list[tuple[Any, ...]]:
        return [_trade_fields(t) for t in result.trades if t.timestamp <= t1]

    prefix_a = _prefix(result_a)

    # There must actually BE trades in the prefix, or the test proves nothing.
    assert prefix_a, "no trades on or before N+1 — the invariance check is vacuous"
    assert prefix_a == _prefix(result_b)
    assert prefix_a == _prefix(result_c)

    # The trade in the prefix priced off N+1's ask (0.51), not off the
    # violent 0.99 at N+2 — the canonical look-ahead failure is a
    # backtest that buffers the stream and prices against a later row.
    assert prefix_a[0][6] == pytest.approx(0.51)

    # The equity curve through N+1 is likewise a function of the past only.
    assert result_a.equity_curve[:2] == result_b.equity_curve[:2]
    assert result_a.equity_curve[:2] == result_c.equity_curve[:2]

    # And the divergence really is enormous, so the checks above had
    # something to catch: B's collapsed book fills nothing at T0+10m
    # while A's fills normally.
    assert [t.timestamp for t in result_a.trades if t.timestamp == t2] == [t2]
    assert [t.timestamp for t in result_b.trades if t.timestamp == t2] == []


# ---------------------------------------------------------------------------
# Phase-1 remediation FIX 2: a `Signal`-returning strategy's leg is venued
# from the triggering snapshot; a leg whose venue is provably wrong is
# rejected LOUDLY, never as a market-condition count.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_signal_strategy_venue_flows_from_triggering_snapshot() -> None:
    """A `Signal`-returning strategy fed a Kalshi snapshot must produce a
    Kalshi-venued position, not silently default to `"polymarket"`
    (`Leg.venue`'s bare default) and then fail to fill against a book
    that was never even looked up on the right venue.
    """
    config = _config(fill_at="same")
    strategy = _SingleSignalBuyStrategy(limit_price=0.45, size_usd=50.0, market_id="k1")
    backtester = Backtester(config, strategy)
    replayer = InMemoryDataReplayer(
        [_snap(ts=T0, market_id="k1", yes_ask=0.45, no_ask=0.48, venue="kalshi")]
    )

    result = await backtester.run(replayer)

    assert result.intents_generated == 1
    assert result.intents_executed == 1
    assert result.intent_rejections == 0

    buys = [t for t in backtester.trades if t.side == "BUY"]
    assert len(buys) == 1
    assert buys[0].venue == "kalshi"
    assert set(backtester.portfolio.positions) == {"kalshi:k1:YES"}


@pytest.mark.asyncio
async def test_intent_leg_venue_mismatch_is_rejected_loudly_not_as_market_condition() -> None:
    """A leg whose venue matches none of the venues ever observed for its
    market_id — though that market_id HAS been seen, just on another
    venue — is STRUCTURAL, not a market condition: retrying it against a
    later snapshot reproduces the identical mismatch. It must be
    rejected under the distinct `VENUE_MISMATCH_REASON`, never counted
    as `"no_eligible_levels"` (which reads as "no liquidity") or bled out
    as a silent `pending_ttl` expiry.
    """
    config = _config(fill_at="same")
    strategy = _WrongVenueIntentStrategy(
        limit_price=0.45, size_usd=50.0, market_id="k1", leg_venue="polymarket"
    )
    backtester = Backtester(config, strategy)
    replayer = InMemoryDataReplayer(
        [_snap(ts=T0, market_id="k1", yes_ask=0.45, no_ask=0.48, venue="kalshi")]
    )

    result = await backtester.run(replayer)

    assert result.intents_generated == 1
    assert result.intents_executed == 0
    assert result.intent_rejections == 1
    assert result.rejection_reasons == {VENUE_MISMATCH_REASON: 1}
    assert result.intent_expirations == 0
    assert backtester.portfolio.positions == {}
    assert backtester.trades == []
    assert result.errors == []
