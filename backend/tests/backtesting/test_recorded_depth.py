"""T21: recorded book depth — `BookSnapshot`, `DataCollector.collect_books`,
and the engine's use of a RECORDED book over a synthesized one.

Every money number asserted here is computed BY HAND in a comment next to
the assertion (GUARDRAILS.md §5) — never with the code under test. The
Polymarket taker fee used throughout is
`fee = contracts * rate * price * (1 - price)` (PLAN.md §3), with
`rate = 0.04` for the `"politics"` category
(`app/venues/fees.py::POLYMARKET_CATEGORY_TAKER_RATES`), charged PER
FILL (`app/execution/fill_engine.py`'s module docstring).

Carry-forwards covered here (see NOTES.md's `### T21` section):

1. Multi-outcome bundles are dead without a recorded book: `Backtester
   ._book_for` cannot synthesize depth for any outcome other than
   `"YES"`/`"NO"` (T07/T10), so a bundle leg (an arbitrary outcome label
   like `"TRUMP"`) can ONLY ever fill against a recorded book.
   `test_bundle_outcome_recorded_book_fills_with_real_levels` and
   `test_bundle_outcome_without_recorded_book_is_an_explicit_skip` prove
   both halves of that: it fills correctly when a recorded book exists,
   and it is an explicit, labeled skip (never a silent YES/NO guess)
   when one does not.
2. Per-outcome price MARKING: a bundle leg's `_current_prices` cache key
   used to be reachable only via the two literal `"YES"`/`"NO"` writes,
   so it stayed marked at its entry price forever.
   `test_bundle_outcome_price_marking_uses_latest_snapshot_not_entry_price`
   and `test_price_for_outcome_resolves_book_and_orderbook_payload_sources`
   cover the fix.
"""
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.book_snapshot import BookSnapshot
from app.models.price_history import PriceHistory
from app.services.backtesting import (
    BacktestConfig,
    Backtester,
    DataReplayer,
    InMemoryDataReplayer,
)
from app.services.backtesting.engine import SlippageModel
from app.services.data_collector import DataCollector
from app.strategies.base import BaseStrategy, Intent, Leg, MarketSnapshot, Signal
from app.utils.time import utcnow
from app.venues.types import VenueId
from tests.helpers import make_book, make_snapshot
from tests.venues.fixture_adapter import FixtureAdapter, make_venue_market

T0 = utcnow().replace(microsecond=0)


class _RecordedDepthBuyStrategy(BaseStrategy):
    """Emits one fixed-size BUY intent per configured `(market, outcome)`, once.

    `plans` maps `market_id -> (outcome, limit_price, size_contracts)`.
    Every leg declares `size_contracts` explicitly, so `_leg_sizes`
    (`app/services/backtesting/engine.py`) honors it VERBATIM rather
    than consulting `calculate_position_size` — the exact contract count
    every test in this file hand-computes a fill against is therefore
    never at the mercy of portfolio-value-dependent sizing.
    """

    name = "test_recorded_depth_buy"

    def __init__(self, plans: dict[str, tuple[str, float, float]]) -> None:
        super().__init__({})
        self._plans = dict(plans)
        self._emitted: set[str] = set()

    def on_market_data(self, snapshot: MarketSnapshot) -> Intent | None:
        """Emit this market's configured BUY intent once, on its first snapshot."""
        plan = self._plans.get(snapshot.market_id)
        if plan is None or snapshot.market_id in self._emitted:
            return None
        self._emitted.add(snapshot.market_id)
        outcome, limit_price, size_contracts = plan
        return Intent(
            kind="single",
            legs=[
                Leg(
                    market_id=snapshot.market_id,
                    outcome=outcome,
                    side="BUY",
                    limit_price=limit_price,
                    size_contracts=size_contracts,
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
        """Unused: every leg above declares `size_contracts` explicitly."""
        return 0.0


def _config(**kw: Any) -> BacktestConfig:
    """Build a `BacktestConfig` with slippage padding OFF and `fill_at='next'`."""
    fields: dict[str, Any] = {
        "start_date": T0 - timedelta(minutes=1),
        "end_date": T0 + timedelta(days=1),
        "initial_capital": 10_000.0,
        "slippage_model": SlippageModel.NONE,
        "fill_at": "next",
        "liquidity_fraction": 0.02,
    }
    fields.update(kw)
    return BacktestConfig(**fields)


# ---------------------------------------------------------------------------
# Brief acceptance: recorded + synthetic mix to "mixed"; recorded fill
# walks real levels.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mixed_recorded_and_synthetic_depth_source_and_recorded_avg_price() -> (
    None
):
    """Two markets, one with a RECORDED book, one synthesized -> `"mixed"`.

    `m1` fills against a recorded two-level ask book: `50 @ 0.40` then
    `50 @ 0.45`. `m2` has no recorded book at all, so `_book_for` falls
    back to `synthesize_book` (T07) from its top-of-book quotes.

        m1 avg_price = (50 * 0.40 + 50 * 0.45) / 100
                     = (20.0 + 22.5) / 100 = 0.425

    `depth_source` aggregates BOTH books this run walked
    (`Backtester._aggregate_depth_source`): one `"recorded"`, one
    `"synthetic"` -> `"mixed"`.
    """
    t1 = T0 + timedelta(minutes=5)

    recorded_book = make_book(
        bids=[(0.39, 500.0)],
        asks=[(0.40, 50.0), (0.45, 50.0)],
        market_id="m1",
        outcome="YES",
        ts=t1,
    )

    config = _config()
    strategy = _RecordedDepthBuyStrategy(
        {"m1": ("YES", 0.50, 100.0), "m2": ("YES", 0.60, 100.0)}
    )
    backtester = Backtester(config, strategy)
    replayer = InMemoryDataReplayer(
        [
            make_snapshot(market_id="m1", ts=T0, yes=0.50, category="politics"),
            make_snapshot(market_id="m2", ts=T0, yes=0.55, category="politics"),
            make_snapshot(
                market_id="m1",
                ts=t1,
                yes=0.50,
                category="politics",
                book=recorded_book,
            ),
            make_snapshot(
                market_id="m2",
                ts=t1,
                yes=0.55,
                category="politics",
                volume_24h=100_000.0,
            ),
        ]
    )

    result = await backtester.run(replayer)

    assert result.depth_source == "mixed"

    m1_buys = [t for t in backtester.trades if t.market_id == "m1" and t.side == "BUY"]
    assert len(m1_buys) == 1
    trade = m1_buys[0]
    assert trade.price == pytest.approx(0.425)
    assert trade.size == pytest.approx(100.0)

    m2_buys = [t for t in backtester.trades if t.market_id == "m2" and t.side == "BUY"]
    assert len(m2_buys) == 1


# ---------------------------------------------------------------------------
# Brief acceptance: `collect_books` on `FixtureAdapter`s, idempotent re-run.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_collect_books_writes_rows_and_is_idempotent_on_rerun(
    test_session: AsyncSession,
) -> None:
    """`collect_books` writes one `BookSnapshot` row per outcome, and is
    idempotent: `FixtureAdapter.get_book` is deliberately static (same
    book, same `ts`, on every call), so a re-run over the identical
    market must write ZERO new rows rather than duplicating them (the
    unique constraint on `(venue, market_id, outcome, ts)`, enforced
    here via check-then-insert — see `DataCollector
    ._upsert_book_snapshot`'s docstring for why not a database-native
    `ON CONFLICT` clause).
    """
    market = make_venue_market(
        venue="polymarket",
        market_id="PM-1",
        outcomes=("YES", "NO"),
        raw={"volume": 50_000.0},
    )
    yes_book = make_book(
        bids=[(0.44, 100.0)], asks=[(0.46, 100.0)], market_id="PM-1", outcome="YES"
    )
    no_book = make_book(
        bids=[(0.53, 100.0)], asks=[(0.55, 100.0)], market_id="PM-1", outcome="NO"
    )
    adapter = (
        FixtureAdapter("polymarket")
        .add_market(market)
        .set_book(yes_book)
        .set_book(no_book)
    )
    adapters = {"polymarket": adapter}
    market_ids_per_venue: dict[VenueId, list[str]] = {"polymarket": ["PM-1"]}

    collector = DataCollector(test_session)

    written_first = await collector.collect_books(
        adapters, market_ids_per_venue, test_session
    )
    assert written_first == 2  # one row per outcome: YES, NO

    rows = (await test_session.execute(select(BookSnapshot))).scalars().all()
    assert len(rows) == 2
    assert {row.outcome for row in rows} == {"YES", "NO"}
    assert {row.depth_source for row in rows} == {"recorded"}

    written_second = await collector.collect_books(
        adapters, market_ids_per_venue, test_session
    )
    assert written_second == 0  # idempotent: identical (venue, market, outcome, ts)

    rows_after = (await test_session.execute(select(BookSnapshot))).scalars().all()
    assert len(rows_after) == 2  # no duplicates


# ---------------------------------------------------------------------------
# `DataReplayer` (the DB-backed replayer): the actual `book_match_window_s`
# matching logic, against a real (test) session — `InMemoryDataReplayer`
# above never touches this code path at all, since its snapshots carry a
# caller-supplied `.book` directly.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_data_replayer_attaches_recorded_book_within_window_only(
    test_session: AsyncSession,
) -> None:
    """`DataReplayer` attaches a `BookSnapshot` iff it is within
    `settings.book_match_window_s` seconds BEFORE the price row's `ts`
    (never after — a future book would be look-ahead, PLAN.md D6).

    Three markets, three outcomes for `_get_recorded_book`'s window:

    - `m-within`: book observed `window / 2` before the price row -> attached.
    - `m-outside`: book observed `window * 2` before the price row -> NOT
      attached (too stale to stand in for "the book at this instant").
    - `m-future`: book observed 10s AFTER the price row -> NOT attached,
      regardless of how close in time — attaching it would be look-ahead.
    """
    window = timedelta(seconds=settings.book_match_window_s)
    row_ts = T0

    test_session.add_all(
        [
            PriceHistory(
                market_id="m-within",
                timestamp=row_ts,
                yes_price=0.50,
                no_price=0.50,
                volume_24h=10_000.0,
            ),
            PriceHistory(
                market_id="m-outside",
                timestamp=row_ts,
                yes_price=0.50,
                no_price=0.50,
                volume_24h=10_000.0,
            ),
            PriceHistory(
                market_id="m-future",
                timestamp=row_ts,
                yes_price=0.50,
                no_price=0.50,
                volume_24h=10_000.0,
            ),
            BookSnapshot(
                venue="polymarket",
                market_id="m-within",
                outcome="YES",
                ts=row_ts - window / 2,
                bids=[{"price": 0.44, "size": 10.0}],
                asks=[{"price": 0.46, "size": 10.0}],
                tick_size=0.01,
                min_size=1.0,
            ),
            BookSnapshot(
                venue="polymarket",
                market_id="m-outside",
                outcome="YES",
                ts=row_ts - window * 2,
                bids=[{"price": 0.44, "size": 10.0}],
                asks=[{"price": 0.46, "size": 10.0}],
                tick_size=0.01,
                min_size=1.0,
            ),
            BookSnapshot(
                venue="polymarket",
                market_id="m-future",
                outcome="YES",
                ts=row_ts + timedelta(seconds=10),
                bids=[{"price": 0.44, "size": 10.0}],
                asks=[{"price": 0.46, "size": 10.0}],
                tick_size=0.01,
                min_size=1.0,
            ),
        ]
    )
    await test_session.commit()

    replayer = DataReplayer(
        session=test_session,
        start_date=row_ts - timedelta(hours=1),
        end_date=row_ts + timedelta(hours=1),
    )
    snapshots = {
        item.market_id: item
        async for item in replayer
        if isinstance(item, MarketSnapshot)
    }
    assert set(snapshots) == {"m-within", "m-outside", "m-future"}

    within_book = snapshots["m-within"].book
    assert within_book is not None
    assert within_book.depth_source == "recorded"
    assert within_book.outcome == "YES"

    assert snapshots["m-outside"].book is None
    assert snapshots["m-future"].book is None


# ---------------------------------------------------------------------------
# Carry-forward 1: an arbitrary (non-YES/NO) outcome label, end to end.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bundle_outcome_recorded_book_fills_with_real_levels() -> None:
    """A recorded book for a named bundle outcome (`"TRUMP"`) fills correctly.

    `Backtester._book_for` cannot SYNTHESIZE depth for a non-YES/NO
    outcome (T07/T10) — a multi-outcome bundle leg can only ever fill
    against a RECORDED book. This is that book, walked:

        avg_price = (25 * 0.20 + 25 * 0.22) / 50
                  = (5.0 + 5.5) / 50 = 0.21
    """
    t1 = T0 + timedelta(minutes=5)
    recorded_book = make_book(
        bids=[(0.18, 500.0)],
        asks=[(0.20, 25.0), (0.22, 25.0)],
        market_id="m9",
        outcome="TRUMP",
        ts=t1,
    )

    config = _config()
    strategy = _RecordedDepthBuyStrategy({"m9": ("TRUMP", 0.25, 50.0)})
    backtester = Backtester(config, strategy)
    replayer = InMemoryDataReplayer(
        [
            make_snapshot(market_id="m9", ts=T0, yes=0.50, category="politics"),
            make_snapshot(
                market_id="m9",
                ts=t1,
                yes=0.50,
                category="politics",
                book=recorded_book,
            ),
        ]
    )

    result = await backtester.run(replayer)

    assert result.depth_source == "recorded"
    buys = [t for t in backtester.trades if t.side == "BUY"]
    assert len(buys) == 1
    trade = buys[0]
    assert trade.outcome == "TRUMP"
    assert trade.price == pytest.approx(0.21)
    assert trade.size == pytest.approx(50.0)


@pytest.mark.asyncio
async def test_bundle_outcome_without_recorded_book_is_an_explicit_skip() -> None:
    """No recorded book for a non-binary outcome -> a labeled rejection,
    NEVER a silent YES/NO guess.

    Same intent as the test above, but `m9`'s next snapshot carries no
    book at all. `_book_for` returns `None` for `"TRUMP"` (it is neither
    `"YES"` nor `"NO"`, so `synthesize_book` is never even attempted —
    see its own debug log), so the leg has nothing to fill against and
    the intent is rejected `"no_eligible_levels"` rather than filling
    against some fabricated or wrong-outcome price.
    """
    t1 = T0 + timedelta(minutes=5)

    config = _config()
    strategy = _RecordedDepthBuyStrategy({"m9": ("TRUMP", 0.25, 50.0)})
    backtester = Backtester(config, strategy)
    replayer = InMemoryDataReplayer(
        [
            make_snapshot(market_id="m9", ts=T0, yes=0.50, category="politics"),
            make_snapshot(market_id="m9", ts=t1, yes=0.50, category="politics"),
        ]
    )

    result = await backtester.run(replayer)

    assert result.intents_generated == 1
    assert result.intents_executed == 0
    assert result.intent_rejections == 1
    assert result.rejection_reasons == {"no_eligible_levels": 1}
    assert backtester.trades == []


# ---------------------------------------------------------------------------
# Carry-forward 2: per-outcome price MARKING for an arbitrary outcome.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bundle_outcome_price_marking_uses_latest_snapshot_not_entry_price() -> (
    None
):
    """A bundle position must mark at the LATEST price seen, not its entry.

    Before T21, `_process_snapshot` wrote `_current_prices` under only
    the two literal keys `f"{venue}:{market_id}:YES"`/`"...:NO"`, so a
    position keyed `polymarket:m9:TRUMP` could never find its own cache
    entry and `Portfolio.total_equity` fell back to `pos.entry_price`
    forever (see `_price_for_outcome`'s docstring). This proves the fix
    end to end via `BacktestResult.final_value`, the one PUBLIC number
    that reflects the mark (positions are never liquidated at
    `end_date`, only marked — PLAN.md D6).

    Fill (t1, recorded book, same numbers as the test above):
        avg_price = (25 * 0.20 + 25 * 0.22) / 50 = 0.21
        fee       = 25 * 0.04 * 0.20 * 0.80 + 25 * 0.04 * 0.22 * 0.78
                  = 0.16 + 0.1716 = 0.3316
        cost      = 50 * 0.21 + 0.3316 = 10.5 + 0.3316 = 10.8316
        cash      = 10_000.00 - 10.8316 = 9_989.1684

    A LATER snapshot (t2) prices `"TRUMP"` at `0.30` via
    `orderbook["outcomes"]` (no book this time — proving the OTHER
    resolution path in `_price_for_outcome` too). The position is never
    sold, so at `end_date`:

        mark_at_end = 50 * 0.30 = 15.0
        final_value = 9_989.1684 + 15.0 = 10_004.1684

    If the pre-T21 bug were still present, the position would instead
    mark at its entry price (0.21): `9_989.1684 + 50 * 0.21 = 10_000.00`
    — a materially different, and wrong, number.
    """
    t1 = T0 + timedelta(minutes=5)
    t2 = t1 + timedelta(minutes=5)
    recorded_book = make_book(
        bids=[(0.18, 500.0)],
        asks=[(0.20, 25.0), (0.22, 25.0)],
        market_id="m9",
        outcome="TRUMP",
        ts=t1,
    )

    config = _config(end_date=t2 + timedelta(days=1))
    strategy = _RecordedDepthBuyStrategy({"m9": ("TRUMP", 0.25, 50.0)})
    backtester = Backtester(config, strategy)
    replayer = InMemoryDataReplayer(
        [
            make_snapshot(market_id="m9", ts=T0, yes=0.50, category="politics"),
            make_snapshot(
                market_id="m9",
                ts=t1,
                yes=0.50,
                category="politics",
                book=recorded_book,
            ),
            make_snapshot(
                market_id="m9",
                ts=t2,
                yes=0.50,
                category="politics",
                orderbook={"outcomes": {"TRUMP": 0.30}},
            ),
        ]
    )

    result = await backtester.run(replayer)

    buys = [t for t in backtester.trades if t.side == "BUY"]
    assert len(buys) == 1
    trade = buys[0]
    assert trade.price == pytest.approx(0.21)
    assert trade.fee == pytest.approx(0.3316)

    assert result.final_value == pytest.approx(10_004.1684)
    assert result.final_value != pytest.approx(10_000.00)  # the pre-T21 bug's number


def test_price_for_outcome_resolves_book_and_orderbook_payload_sources() -> None:
    """Direct coverage of `_price_for_outcome`'s non-YES/NO resolution paths.

    Both sources a bundle outcome can be priced from: a `snapshot.book`
    matching that outcome (via `.mid()`), and
    `snapshot.orderbook["outcomes"]` (the same payload shape
    `multi_outcome_bundle_arbitrage` reads prices from). An outcome with
    NEITHER resolves to `None` — never a guessed/fabricated price.
    """
    config = _config()
    backtester = Backtester(config, _RecordedDepthBuyStrategy({}))

    book_snapshot = make_snapshot(
        market_id="m9",
        yes=0.50,
        book=make_book(
            bids=[(0.19, 10.0)], asks=[(0.21, 10.0)], market_id="m9", outcome="TRUMP"
        ),
    )
    # mid = (0.19 + 0.21) / 2 = 0.20
    assert backtester._price_for_outcome(book_snapshot, "TRUMP") == pytest.approx(0.20)
    # Case-insensitive: the position's own outcome may be differently cased.
    assert backtester._price_for_outcome(book_snapshot, "trump") == pytest.approx(0.20)

    payload_snapshot = make_snapshot(
        market_id="m9", yes=0.50, orderbook={"outcomes": {"BIDEN": 0.35}}
    )
    assert backtester._price_for_outcome(payload_snapshot, "BIDEN") == pytest.approx(
        0.35
    )

    unresolvable_snapshot = make_snapshot(market_id="m9", yes=0.50)
    assert backtester._price_for_outcome(unresolvable_snapshot, "NOBODY") is None

    # YES/NO is unchanged, regardless of casing.
    assert backtester._price_for_outcome(unresolvable_snapshot, "yes") == pytest.approx(
        0.50
    )
