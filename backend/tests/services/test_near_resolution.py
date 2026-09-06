"""T20 -- `app.services.scanner.near_resolution_pass` (PLAN.md D10(a)-(d)).

Derived from the T20 brief/acceptance in `.claude/kits/market-edge/TASKS.md`,
not by reading `app/services/scanner.py` and mirroring it back. Every
money/percentage figure is computed BY HAND in a comment before it is
asserted (GUARDRAILS.md §5).

No network, no live mode, no real order (GUARDRAILS.md §1.1/§1.2/§1.4):
market data comes from `tests.venues.fixture_adapter.FixtureAdapter`,
never a real adapter; the bucket-cap test at the bottom routes through
the SAME `OrderRouter` the live path uses, with a `PaperVenueAdapter`
over a `FixtureAdapter` for its last hop (same pattern as
`tests/execution/test_router.py`).
"""
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import app.tasks.scanner as scanner_task
from app.config import Settings
from app.config import settings as app_settings
from app.execution.ledger import CapitalLedger
from app.execution.router import OrderRouter
from app.models.intent import IntentRecord
from app.models.price_history import PriceHistory
from app.services.scanner import near_resolution_pass
from app.tasks import celery_app
from app.strategies.base import Intent, Leg
from app.utils.time import utcnow
from app.venues.paper import PaperVenueAdapter
from tests.helpers import make_book
from tests.venues.fixture_adapter import FixtureAdapter, make_venue_market

KX_MARKET = "KXTEST-1"
PM_MARKET = "PM-DETERMINED"

#: Long enough to clear `app.services.scoring`'s 200-char `rules_text`
#: penalty, and a named source clears the "no resolution_source"
#: penalty -- isolating `resolution_risk` to its `0.15` base plus
#: whatever THIS test deliberately adds (mirrors
#: `tests/services/test_scoring.py::_open_market`'s own convention).
LONG_RULES_TEXT = "This market resolves according to the stated rules. " * 5  # 260 chars


@pytest_asyncio.fixture
async def sessions(test_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """A session factory over the shared in-memory `test_engine`."""
    return async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


def _kalshi_adapter(*, close_time, expected_settle_time, status="closed") -> FixtureAdapter:
    """A Kalshi `FixtureAdapter` with one near-certain YES market.

    `status="closed"` by default -- a real Kalshi market past its close
    but not yet determined moves to `status="closed"`
    (`app/venues/kalshi/adapter.py::_MARKET_STATUS_FROM_PAYLOAD`), which
    `app.services.scanner.scan()`'s `status="open"` filter would miss
    entirely. Using it here (rather than `"open"`) proves
    `near_resolution_pass`'s `list_markets(status=None)` picks the
    market up regardless.
    """
    market = make_venue_market(
        "kalshi",
        KX_MARKET,
        status=status,
        close_time=close_time,
        expected_settle_time=expected_settle_time,
        rules_text=LONG_RULES_TEXT,
        resolution_source="Official Source",
    )
    adapter = FixtureAdapter("kalshi")
    adapter.add_market(market)
    adapter.set_book(
        make_book(
            bids=[(0.96, 500.0)], asks=[(0.98, 500.0)],
            venue="kalshi", market_id=KX_MARKET, outcome="YES",
        )
    )
    adapter.set_book(
        make_book(
            bids=[(0.01, 500.0)], asks=[(0.03, 500.0)],
            venue="kalshi", market_id=KX_MARKET, outcome="NO",
        )
    )
    return adapter


def _polymarket_adapter(*, close_time) -> FixtureAdapter:
    """A Polymarket `FixtureAdapter` whose market stayed `status="open"`
    past its own `close_time` -- the UMA-challenge-window quirk this
    module's docstring describes (`expected_settle_time` is always
    `None` for Polymarket; there is nothing venue-specific to override).
    """
    market = make_venue_market(
        "polymarket",
        PM_MARKET,
        status="open",
        close_time=close_time,
        rules_text=LONG_RULES_TEXT,
        resolution_source="Official Source",
    )
    adapter = FixtureAdapter("polymarket")
    adapter.add_market(market)
    adapter.set_book(
        make_book(
            bids=[(0.96, 500.0)], asks=[(0.98, 500.0)],
            venue="polymarket", market_id=PM_MARKET, outcome="YES",
        )
    )
    adapter.set_book(
        make_book(
            bids=[(0.01, 500.0)], asks=[(0.03, 500.0)],
            venue="polymarket", market_id=PM_MARKET, outcome="NO",
        )
    )
    return adapter


async def test_kalshi_outcome_determined_produces_one_scored_intent_at_baseline_risk(
    test_session: AsyncSession,
) -> None:
    """Baseline: past close, NOT yet in its own dispute window, no
    liquidity history on record.

    close_time is 1h behind `now` (outcome_determined's requirement);
    `expected_settle_time` is 30h AHEAD of `now`, so Kalshi's own
    dispute-window reference has not arrived yet -- `in_dispute_window`
    must be `False` even though `close_time` has passed. No
    `PriceHistory` rows exist for this market, so `liquidity_collapse`
    is `False` with `unavailable=True` (never read as "measured fine").

    ask=0.98, Kalshi's default 0.07 rate, ONE deep ask level (500
    contracts, so the 100 requested fill in a SINGLE fill -- see
    `tests/strategies/test_settlement_edge.py`'s identical
    "one_deep_fill" case for the same fee/gas/residual arithmetic):
    fee = ceil_cents(100*0.07*0.98*0.02) = ceil_cents($0.1372) = $0.14
    total, $0.0014/contract. gas=$0.0005/contract. residual =
    1-0.98-0.0014-0.0005 = $0.0181/contract, fraction =
    0.0181/0.98 = 0.01846938775510206. hours_to_resolution=30 (the
    stated `expected_settle_time`), floor_hours=max(30,
    settlement_delay_hours=24)=30, annualized =
    0.01846938775510206 * (8760/30) = 5.393061224489802.
    resolution_risk = 0.15 base ONLY (no dispute, no liquidity penalty).
    fill_confidence = 1.0 (500 contracts resting at exactly the limit,
    100 requested). composite = 5.393061224489802 * 1.0 * 0.85 =
    4.584102040816331.
    """
    now = utcnow()
    adapter = _kalshi_adapter(
        close_time=now - timedelta(hours=1),
        expected_settle_time=now + timedelta(hours=30),
    )

    result = await near_resolution_pass({"kalshi": adapter}, test_session)

    assert len(result) == 1
    scored = result[0]
    assert scored.strategy == "settlement_edge"
    assert scored.intent.metadata["bucket"] == "near_resolution"
    assert scored.intent.metadata["liquidity_collapse"] is False
    assert scored.intent.metadata["liquidity_collapse_unavailable"] is True
    assert scored.score.hours_to_resolution == pytest.approx(30.0)
    assert scored.score.annualized_return == pytest.approx(5.393061224489802)
    assert scored.score.resolution_risk == pytest.approx(0.15)
    assert scored.score.fill_confidence == pytest.approx(1.0)
    assert scored.score.composite == pytest.approx(4.584102040816331)

    # And it was actually persisted, tagged for the router's bucket cap.
    persisted = await test_session.get(IntentRecord, scored.intent_record_id)
    assert persisted is not None
    assert persisted.extra_data["bucket"] == "near_resolution"


async def test_liquidity_collapse_flagged_from_a_synthetic_24h_spread_series(
    test_session: AsyncSession,
) -> None:
    """Same baseline market, but a trailing 24h `PriceHistory` series
    whose spread is far tighter than the current one.

    spread_now = yes_ask(0.98) - yes_bid(0.96) = 0.02. Five
    `PriceHistory` rows over the trailing 24h all record spread=0.002
    (median=0.002, an odd count so no averaging is needed) ->
    ratio = 0.02 / 0.002 = 10 > 3 -> `liquidity_collapse=True`.
    resolution_risk = 0.15 base + 0.2 liquidity = 0.35. The
    fee/gas/residual/annualized arithmetic is UNCHANGED from the
    baseline test (liquidity has no bearing on the strategy's own
    pricing, and the book is the same one deep 500-contract level) ->
    annualized_return is still 5.393061224489802; composite =
    5.393061224489802 * 1.0 * (1 - 0.35) = 3.5054897959183715.
    """
    now = utcnow()
    adapter = _kalshi_adapter(
        close_time=now - timedelta(hours=1),
        expected_settle_time=now + timedelta(hours=30),
    )
    for hours_ago in (1, 5, 10, 15, 20):
        test_session.add(
            PriceHistory(
                market_id=KX_MARKET,
                timestamp=now - timedelta(hours=hours_ago),
                yes_price=0.98,
                no_price=0.02,
                spread=0.002,
            )
        )
    await test_session.commit()

    result = await near_resolution_pass({"kalshi": adapter}, test_session)

    assert len(result) == 1
    scored = result[0]
    assert scored.intent.metadata["liquidity_collapse"] is True
    assert scored.intent.metadata["liquidity_collapse_unavailable"] is False
    assert scored.score.annualized_return == pytest.approx(5.393061224489802)
    assert scored.score.resolution_risk == pytest.approx(0.35)
    assert scored.score.composite == pytest.approx(3.5054897959183715)


async def test_dispute_window_blocks_by_default_and_is_scored_when_allowed(
    test_session: AsyncSession,
) -> None:
    """Polymarket, `close_time` passed 30 minutes ago, `status` still
    `"open"` (the UMA-challenge-window quirk) -- `in_dispute_window` is
    `True` for Polymarket the instant `close_time` passes, distinct from
    Kalshi's own, later `expected_settle_time` reference.

    Default `allow_dispute_window=False` -> `settlement_edge` itself
    refuses to emit anything -> the pass returns nothing at all (not a
    dropped/filtered row -- literally zero intents, proving the block is
    the STRATEGY's, not a scoring artifact).

    With `strategy_config={"allow_dispute_window": True}`, the SAME
    market signals. `expected_settle_time` is `None` for Polymarket, so
    `_expected_settlement_ts` falls back to `now + settlement_delay_hours
    = now + 24h` -> hours_to_resolution=24, floor_hours=max(24,24)=24.
    ask=0.98 at Polymarket's unknown-category 0.05 rate: fee =
    1*0.05*0.98*0.02 = 0.00098 (no venue floor, unlike Kalshi), gas=
    $0.0005, residual = 1 - 0.98 - 0.00098 - 0.0005 = 0.01852, fraction =
    0.01852/0.98 = 0.018897959183673485, annualized =
    0.018897959183673485 * (8760/24) = 6.897755102040822.
    resolution_risk = 0.15 base + 0.3 dispute = 0.45 (no liquidity
    history for this market either, but that is not being asserted
    here). composite = 6.897755102040822 * 1.0 * 0.55 =
    3.7937653061224523.

    T21c NOTE (traced, not assumed): this fixture's YES book has BOTH a
    bid (0.96) and an ask (0.98), so `_snapshot_from_market`'s
    `yes_price = yes_book.mid()` is `0.97` -- AT the 0.97 threshold --
    and `outcome_determined` fires for the **YES** side, whose book
    (`snapshot.book`, always the YES book for a binary market -- see
    that function's docstring) is the SAME book `_priced_fills` needs.
    That is `outcome != book.outcome` NEVER true here, so this
    particular fixture never walks into the wrong-book defect T21c
    fixed (`app.strategies.settlement_edge._priced_fills`'s
    `outcome_books` param / `app.services.scanner.near_resolution_pass`'s
    per-outcome plumbing) -- confirmed by re-running this exact scenario
    against the fixed code and observing an IDENTICAL
    `annualized_return`/`composite`. `test_no_side_prices_against_its_own_book_not_yes`
    below is the fixture that DOES exercise the fixed path (YES near-
    worthless, NO the determined/bought side, so `snapshot.book` and the
    evaluated `outcome` genuinely differ) and asserts the honest,
    depth-limited number.
    """
    now = utcnow()
    adapter = _polymarket_adapter(close_time=now - timedelta(minutes=30))

    blocked = await near_resolution_pass({"polymarket": adapter}, test_session)
    assert blocked == []

    allowed = await near_resolution_pass(
        {"polymarket": adapter},
        test_session,
        strategy_config={"allow_dispute_window": True},
    )

    assert len(allowed) == 1
    scored = allowed[0]
    assert scored.intent.metadata["in_dispute_window"] is True
    assert scored.score.hours_to_resolution == pytest.approx(24.0)
    assert scored.score.annualized_return == pytest.approx(6.897755102040822)
    assert scored.score.resolution_risk == pytest.approx(0.45)
    assert scored.score.composite == pytest.approx(3.7937653061224523)


# ---------------------------------------------------------------------------
# T21c regressions: the scanner must hand `settlement_edge` the REAL book for
# the side it is actually evaluating, and an observed-EMPTY book must yield
# NO fill, never the optimistic top-of-book fallback.
# ---------------------------------------------------------------------------

NO_DETERMINED_MARKET = "PM-NO-DETERMINED"


def _polymarket_no_determined_adapter(
    *, close_time: datetime, no_asks: list[tuple[float, float]]
) -> FixtureAdapter:
    """A Polymarket market where **NO**, not YES, is the near-certain side.

    YES's own book has both a bid (0.015) and ask (0.025) -- unlike
    `_polymarket_adapter` above, whose YES book straddles 0.97 -- so
    `yes_price = yes_book.mid() = (0.015+0.025)/2 = 0.02 <= 1 - 0.97 =
    0.03`, and `outcome_determined` fires for **NO**. `MarketSnapshot.book`
    (`app.services.scanner._snapshot_from_market`) is STILL the YES book
    for a binary market -- that mismatch (`book.outcome="YES" !=
    outcome="NO"`) is exactly the shape `_priced_fills`'s `outcome_books`
    plumbing exists to route around: the caller must reach for the REAL
    NO book, never fall back to pricing the NO fill off the YES book (or
    a synthesized complement of it).
    """
    market = make_venue_market(
        "polymarket",
        NO_DETERMINED_MARKET,
        status="open",
        close_time=close_time,
        rules_text=LONG_RULES_TEXT,
        resolution_source="Official Source",
    )
    adapter = FixtureAdapter("polymarket")
    adapter.add_market(market)
    adapter.set_book(
        make_book(
            bids=[(0.015, 100.0)], asks=[(0.025, 100.0)],
            venue="polymarket", market_id=NO_DETERMINED_MARKET, outcome="YES",
        )
    )
    adapter.set_book(
        make_book(
            bids=[(0.975, 100.0)], asks=no_asks,
            venue="polymarket", market_id=NO_DETERMINED_MARKET, outcome="NO",
        )
    )
    return adapter


async def test_no_side_prices_against_its_own_book_not_yes(
    test_session: AsyncSession,
) -> None:
    """T21c (Defect 1): the NO side must be priced against the REAL NO
    book's actual depth, not the YES book (mismatched) collapsed to a
    single top-of-book fill.

    The NO book carries genuine, THIN depth at the top: `asks=[(0.98,
    3.0), (0.995, 500.0)]` -- only 3 contracts at the best price, then
    500 more a click worse. `min_position_size=100` walks BOTH levels:
    fills = `[(0.98, 3.0), (0.995, 97.0)]`.

    ask (size-weighted): `(0.98*3 + 0.995*97) / 100 = (2.94 + 96.515) /
    100 = 0.99455`.
    fee (Polymarket, unknown-category 0.05 rate, no per-fill floor --
    unlike Kalshi, summed over the two real fills):
        fill 1: `3 * 0.05 * 0.98 * 0.02   = 0.00294`
        fill 2: `97 * 0.05 * 0.995 * 0.005 = 0.02413375`
        total = `0.02707375`, per-contract = `0.0002707375`.
    gas: `0.05 / 100 = 0.0005`/contract (unchanged -- one redemption
    over the same 100 filled contracts).
    residual: `1 - 0.99455 - 0.0002707375 - 0.0005 = 0.0046792625`/contract.
    fraction: `0.0046792625 / 0.99455 = 0.0047049545020361625`.
    hours_to_resolution: Polymarket has no `expected_settle_time`, so
    `_expected_settlement_ts` falls back to `now + settlement_delay_hours
    = 24h`; floor_hours = `max(24, 24) = 24`.
    annualized: `0.0047049545020361625 * (8760/24) = 1.7173083932431994`
    (171.73%) -- clears `min_viable_annualized` (0.05) -> SIGNAL.

    THE BUG THIS GUARDS AGAINST: before T21c, `snapshot.book` was always
    the YES book, so `_priced_fills`'s outcome check
    (`book.outcome.casefold() != outcome.casefold()`) failed for this
    NO-side evaluation and fell into the optimistic single-fill
    fallback -- ALL 100 contracts priced at `top_of_book=0.98` (the
    NO book's OWN best ask, correctly read for the threshold check, but
    then never actually walked) in ONE phantom fill, instead of the real
    3-then-97 split. That produces `annualized_return =
    6.897755102040822` (verified by re-running this exact fixture
    against the pre-fix code) -- a 4.0x overstatement of the honest
    1.7173083932431994 this test asserts, because it prices 100
    contracts at a level that, in reality, holds only 3.
    """
    now = utcnow()
    adapter = _polymarket_no_determined_adapter(
        close_time=now - timedelta(hours=1),
        no_asks=[(0.98, 3.0), (0.995, 500.0)],
    )

    result = await near_resolution_pass(
        {"polymarket": adapter},
        test_session,
        strategy_config={"allow_dispute_window": True},
    )

    assert len(result) == 1
    scored = result[0]
    leg = scored.intent.legs[0]
    assert leg.outcome == "NO"
    assert scored.intent.metadata["ask"] == pytest.approx(0.9945499999999999)
    assert scored.intent.metadata["fill_count"] == 2
    assert scored.intent.metadata["filled_size"] == pytest.approx(100.0)
    assert scored.score.hours_to_resolution == pytest.approx(24.0)
    assert scored.score.annualized_return == pytest.approx(1.7173083932431994)


async def test_observed_empty_no_book_produces_no_intent(
    test_session: AsyncSession,
) -> None:
    """T21c (Defect 2): a POSITIVELY OBSERVED, empty NO book (`asks=()`)
    must produce NO fill and therefore NO intent -- never the optimistic
    top-of-book fallback that made an empty book read as maximum
    liquidity.

    Same NO-determined market as the test above, but the NO book's
    `asks` is empty (`()`) -- a `depth_source="recorded"` book that
    positively asserts zero resting offers, exactly the normal state of
    a near-resolution market whose asks have been withdrawn. `bids`
    still rest at `0.90` so this is not "no book was fetched at all",
    it is "a book was fetched and it has nothing to sell at any price".

    `_priced_fills` resolves `outcome_books["NO"]` (a real, matching
    book), so it is NOT case 1 (no book observed) -- it walks the book,
    `OrderBook.walk` returns `[]` for empty `asks`, `filled_size` is
    `0.0`, and `SettlementEdgeStrategy.evaluate` returns `None` before
    ever computing an ask/fee/annualized figure. The pass therefore
    returns literally zero scored intents for this market.

    THE BUG THIS GUARDS AGAINST: before T21c, `_priced_fills` treated
    "book present, matches, but `walk()` returned no fills" the SAME as
    "no book at all" and fell back to `[(top_of_book, size)]` -- pricing
    the full 100 requested contracts at `top_of_book` (the NO book's own
    best BID-derived top, since `no_ask` is `None` when `asks` is empty)
    as if 100 contracts of real depth existed, when the honest fill is
    zero.
    """
    now = utcnow()
    adapter = _polymarket_no_determined_adapter(
        close_time=now - timedelta(hours=1),
        no_asks=[],
    )

    result = await near_resolution_pass(
        {"polymarket": adapter},
        test_session,
        strategy_config={"allow_dispute_window": True},
    )

    assert result == []


async def test_observed_zero_size_ask_level_produces_no_intent(
    test_session: AsyncSession,
) -> None:
    """T21c (Defect 2), the other repro shape from the same defect: a
    single ask LEVEL present but with `size=0.0` is exactly as
    "no liquidity" as an empty `asks` tuple, and must be treated
    identically -- `OrderBook.walk` skips a `take <= 0` level rather
    than emitting a phantom zero-size fill, so `filled_size` is `0.0`
    here too and the pass emits no intent.
    """
    now = utcnow()
    adapter = _polymarket_no_determined_adapter(
        close_time=now - timedelta(hours=1),
        no_asks=[(0.98, 0.0)],
    )

    result = await near_resolution_pass(
        {"polymarket": adapter},
        test_session,
        strategy_config={"allow_dispute_window": True},
    )

    assert result == []


# ---------------------------------------------------------------------------
# The bucket cap (PLAN.md D10(d)) -- a router-level fence, not a scanner one.
# ---------------------------------------------------------------------------


def _bucketed_intent(*, size: float, price: float) -> Intent:
    """A single-leg, near-resolution-bucketed `Intent` on the fixture market."""
    return Intent(
        kind="single",
        legs=[
            Leg(
                market_id="PM-BUCKET",
                outcome="YES",
                side="BUY",
                limit_price=price,
                size_contracts=size,
                venue="polymarket",
            )
        ],
        hold_to_resolution=True,
        atomicity="best_effort",
        confidence=0.9,
        metadata={"bucket": "near_resolution"},
    )


async def test_bucket_cap_rejects_the_second_intent_once_the_first_fills_it(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """`max_near_resolution_notional_usd=500` (the default). A first
    intent of exactly $500 (1000 contracts * 0.50) passes -- the check is
    a strict "exceeds", not "reaches" (`app.execution.fences
    .check_order_limits`'s own documented convention). Once it fills, ANY
    further near-resolution intent -- even a tiny $5 one -- pushes the
    bucket's aggregate to $505 > $500 and must be rejected, proving the
    cap aggregates ACROSS submissions rather than checking one intent's
    own notional in isolation.
    """
    settings_obj = Settings(
        trading_mode="paper",
        paper_starting_balances={"polymarket": 10_000.0, "kalshi": 1000.0},
        max_order_notional_usd=10_000.0,
        max_open_notional_usd=10_000.0,
        max_daily_loss_usd=10_000.0,
        max_near_resolution_notional_usd=500.0,
    )
    ledger = CapitalLedger.paper(settings_obj)
    inner = FixtureAdapter("polymarket")
    inner.add_market(make_venue_market("polymarket", "PM-BUCKET"))
    inner.set_book(
        make_book(
            bids=[(0.50, 2000.0)], asks=[(0.50, 2000.0)],
            venue="polymarket", market_id="PM-BUCKET", outcome="YES",
        )
    )
    adapter = PaperVenueAdapter(inner, None, ledger)
    router = OrderRouter({"polymarket": adapter}, ledger, sessions, settings_obj)

    first = await router.submit(_bucketed_intent(size=1000.0, price=0.50), "unit-test")
    assert first.status == "executed"
    assert first.legs[0].filled_size == pytest.approx(1000.0)

    second = await router.submit(_bucketed_intent(size=10.0, price=0.50), "unit-test")
    assert second.status == "rejected"
    assert second.reason == "risk_limit"

    async with sessions() as session:
        record = await session.get(IntentRecord, second.intent_id)
    assert record is not None
    assert "near_resolution" in record.extra_data["rejected_detail"]


# ---------------------------------------------------------------------------
# T25 -- the pass's PRODUCTION callers (it had none before this)
# ---------------------------------------------------------------------------


def test_the_celery_beat_schedules_the_near_resolution_pass_on_its_own_interval() -> None:
    """The periodic caller `near_resolution_pass()` never had.

    `app/tasks/__init__.py` used to schedule only `scan_opportunities`,
    which runs `scan()` over `ARBITRAGE_STRATEGIES` — and
    `settlement_edge` is in the `"edge"` category, so no beat could ever
    reach this pass. Everything keyed on the `metadata["bucket"] =
    "near_resolution"` tag it alone produces was therefore guarding a
    path nothing walked.

    The interval must be its OWN setting, not a reuse of
    `scan_interval_s`: the two passes look for different things (a
    transient two-book mispricing vs a capital-lockup trade held until
    the venue settles) and cost different amounts to run (`scan_top_n`
    markets per venue vs every market inside `near_resolution_hours`).
    Asserting the two entries differ is what pins that.
    """
    schedule = celery_app.conf.beat_schedule
    tasks = {name: entry["task"] for name, entry in schedule.items()}
    assert "app.tasks.scanner.scan_near_resolution" in tasks.values()

    entry = next(
        item
        for item in schedule.values()
        if item["task"] == "app.tasks.scanner.scan_near_resolution"
    )
    arbitrage = next(
        item
        for item in schedule.values()
        if item["task"] == "app.tasks.scanner.scan_opportunities"
    )
    assert entry["schedule"] == app_settings.near_resolution_scan_interval_s
    assert arbitrage["schedule"] == app_settings.scan_interval_s
    assert entry["task"] != arbitrage["task"]
    # Its own knob, not an alias of the arbitrage one.
    assert (
        Settings.model_fields["near_resolution_scan_interval_s"].alias
        == "NEAR_RESOLUTION_SCAN_INTERVAL_S"
    )


async def test_the_beat_task_body_persists_a_near_resolution_intent(
    test_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`run_near_resolution_scan()` — the exact coroutine the beat runs.

    This is the production entry point, not `near_resolution_pass()`
    directly: the two module-level dependencies the Celery process
    supplies (`read_adapters()` for the venue adapters,
    `async_session_factory` for the database) are the only things
    substituted, so everything between them is the shipped code path.

    Both substitutes are inert with respect to GUARDRAILS.md §1.4:
    `read_adapters()` is replaced BEFORE it can construct a real adapter,
    so no venue is ever contacted.

    ask=0.98 on the Kalshi fixture, `expected_settle_time` 30h ahead, no
    dispute window — the same market the baseline test at the top of this
    module scores, so exactly one intent is expected.
    """
    sessions = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    now = utcnow()
    adapter = _kalshi_adapter(
        close_time=now - timedelta(hours=1),
        expected_settle_time=now + timedelta(hours=30),
    )
    monkeypatch.setattr(
        scanner_task, "read_adapters", lambda: {"kalshi": adapter}
    )
    monkeypatch.setattr(scanner_task, "async_session_factory", sessions)

    summary = await scanner_task.run_near_resolution_scan()

    assert summary["mode"] == "paper"
    assert summary["scanned_venues"] == ["kalshi"]
    assert summary["opportunities_found"] == 1

    async with sessions() as session:
        rows = list((await session.execute(select(IntentRecord))).scalars().all())
    assert len(rows) == 1
    row = rows[0]
    assert row.strategy == "settlement_edge"
    assert row.status == "pending"
    assert row.extra_data["bucket"] == "near_resolution"
    # The label that lets this pass's rows coexist with `scan()`'s on
    # `/arbitrage/opportunities` instead of replacing them.
    assert row.extra_data["scan_pass"] == "near_resolution"
    assert row.extra_data["scan_id"] is not None
