"""`OrderRouter` / `CapitalLedger` / `PaperVenueAdapter` / reconcile (T14).

Every test here runs the SAME `OrderRouter` the live path will use
(PLAN.md D4); only the last hop is a `PaperVenueAdapter` over a
`FixtureAdapter` instead of a venue. No network, no live mode, no real
order — GUARDRAILS.md §1.1/§1.2/§1.4.

ONE FAMILY OF TESTS BELOW IS DELIBERATELY *NOT* PAPER-SHAPED. The
remediation section at the end routes through
`tests.venues.live_shaped_adapter.LiveShapedAdapter`, whose
acknowledgements and fills carry the shape the REAL adapters produce —
`Fill.order_id` is the venue's own order id and `Fill.metadata` never
contains a `client_order_id`. `PaperVenueAdapter` stamps that key on
every fill it simulates, so a suite built only on it cannot see whether
the router can match a live fill to its order at all. It could not, and
552 green tests said nothing about it.

Every money figure below is computed BY HAND in a comment before it is
asserted (GUARDRAILS.md §5). The fee models are the production ones
(`PolymarketFeeModel`, via `FixtureAdapter`), so a hand-computed number
that matches is evidence about the real formula, not about a stub.

The shared fixture book is LOCKED at 0.50/0.50 (best bid == best ask),
which `SimulatedFillEngine` explicitly permits — a locked book is legal,
only a CROSSED one is refused. It is chosen because it makes the unwind
arithmetic exact: buying and selling at the same price isolates the fees
as the entire cost of the round trip, which is precisely the quantity
the unwind test is about.
"""
from datetime import timedelta

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.execution.fences import KillSwitchEngaged, assert_placement_allowed
from app.execution.ledger import CapitalLedger, InsufficientCapital
from app.execution.reconcile import NO_ACK_REASON, reconcile
from app.execution.router import (
    OrderRouter,
    RoutedIntent,
    _fill_matches,
    snap_to_tick,
    token_id_for,
)
from app.models.intent import IntentRecord
from app.models.market import Market
from app.models.position import Position as PositionRow
from app.models.trade import Order as OrderRow
from app.models.trade import OrderSide, OrderStatus, OrderType
from app.models.trade import Trade as TradeRow
from app.strategies.base import Intent, Leg
from app.utils.time import utcnow
from app.venues.paper import PaperVenueAdapter
from app.venues.registry import get_read_adapter
from app.venues.types import Fill, OrderRequest
from tests.helpers import make_book
from tests.venues.fixture_adapter import FixtureAdapter, make_venue_market
from tests.venues.live_shaped_adapter import LiveShapedAdapter

PM_MARKET = "PM-1"
KALSHI_MARKET = "KXTEST-26"


def build_settings(**overrides: object) -> Settings:
    """Build an explicit paper-mode `Settings` for a router test.

    GUARDRAILS.md §1.2: fence-adjacent tests construct `Settings`
    objects and pass them in, rather than touching the environment. The
    starting balances are stated here so every ledger assertion below
    has a visible baseline.
    """
    fields: dict[str, object] = {
        "trading_mode": "paper",
        "paper_starting_balances": {"polymarket": 1000.0, "kalshi": 1000.0},
        "max_order_notional_usd": 250.0,
        "max_open_notional_usd": 1000.0,
        "max_daily_loss_usd": 100.0,
        "max_near_resolution_notional_usd": 500.0,
        "all_or_none_fill_tolerance": 0.995,
        "unwind_slippage_ticks": 5,
        "reconcile_grace_s": 120.0,
    }
    fields.update(overrides)
    return Settings(**fields)  # type: ignore[arg-type]


class CountingPaperAdapter(PaperVenueAdapter):
    """A `PaperVenueAdapter` that counts placement attempts.

    Used to prove a NEGATIVE: that a risk-limit rejection happens before
    anything is placed. Asserting "no order rows were written" would not
    prove that (the router could place first and fail to persist); only
    counting the calls does.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.place_order_calls = 0

    async def place_order(self, order):  # type: ignore[no-untyped-def]
        self.place_order_calls += 1
        return await super().place_order(order)


def polymarket_inner(
    *,
    yes_bids: list[tuple[float, float]] | None = None,
    yes_asks: list[tuple[float, float]] | None = None,
    no_bids: list[tuple[float, float]] | None = None,
    no_asks: list[tuple[float, float]] | None = None,
) -> FixtureAdapter:
    """Build the Polymarket read adapter with a locked 0.50/0.50 YES book."""
    inner = FixtureAdapter("polymarket")
    inner.add_market(make_venue_market("polymarket", PM_MARKET))
    inner.set_book(
        make_book(
            bids=yes_bids if yes_bids is not None else [(0.50, 500.0)],
            asks=yes_asks if yes_asks is not None else [(0.50, 500.0)],
            venue="polymarket",
            market_id=PM_MARKET,
            outcome="YES",
        )
    )
    inner.set_book(
        make_book(
            bids=no_bids if no_bids is not None else [(0.50, 500.0)],
            asks=no_asks if no_asks is not None else [(0.50, 500.0)],
            venue="polymarket",
            market_id=PM_MARKET,
            outcome="NO",
        )
    )
    return inner


def kalshi_inner() -> FixtureAdapter:
    """Build the Kalshi read adapter with a locked 0.50/0.50 YES book."""
    inner = FixtureAdapter("kalshi")
    inner.add_market(make_venue_market("kalshi", KALSHI_MARKET))
    inner.set_book(
        make_book(
            bids=[(0.50, 500.0)],
            asks=[(0.50, 500.0)],
            venue="kalshi",
            market_id=KALSHI_MARKET,
            outcome="YES",
        )
    )
    return inner


@pytest_asyncio.fixture
async def sessions(test_engine) -> async_sessionmaker[AsyncSession]:
    """Return a session factory over the in-memory test database."""
    return async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


def single_leg_intent(
    size: float = 10.0, price: float = 0.50, intent_id: str | None = None
) -> Intent:
    """Build a one-leg BUY intent on the Polymarket fixture market."""
    metadata = {"intent_id": intent_id} if intent_id else {}
    return Intent(
        kind="single",
        legs=[
            Leg(
                market_id=PM_MARKET,
                outcome="YES",
                side="BUY",
                limit_price=price,
                size_contracts=size,
                venue="polymarket",
            )
        ],
        hold_to_resolution=False,
        atomicity="best_effort",
        confidence=0.9,
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# 1. A single-leg fill persists Order + Trade + Position
# ---------------------------------------------------------------------------


async def test_single_leg_fill_persists_order_trade_and_position(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """One filled leg must leave three consistent rows and a debited ledger.

    Hand-computed, Polymarket taker formula
    `fee = size * rate * p * (1 - p)` at the Politics category rate 0.04:
        fee   = 10 * 0.04 * 0.50 * 0.50 = 0.10
        cash  = 10 * 0.50 + 0.10        = 5.10
        free  = 1000.00 - 5.10          = 994.90
    """
    settings = build_settings()
    ledger = CapitalLedger.paper(settings)
    adapter = PaperVenueAdapter(polymarket_inner(), None, ledger)
    router = OrderRouter({"polymarket": adapter}, ledger, sessions, settings)

    routed = await router.submit(single_leg_intent(), "unit-test")

    assert routed.status == "executed"
    assert len(routed.legs) == 1
    leg = routed.legs[0]
    assert leg.status == "filled"
    assert leg.filled_size == pytest.approx(10.0)
    assert leg.avg_price == pytest.approx(0.50)
    assert leg.fee == pytest.approx(0.10)
    assert leg.client_order_id == f"{routed.intent_id}:0:0"
    assert routed.naked_legs == ()

    async with sessions() as session:
        order = await session.scalar(
            select(OrderRow).where(OrderRow.client_order_id == leg.client_order_id)
        )
        assert order is not None
        assert order.status is OrderStatus.FILLED
        assert order.mode == "paper"
        assert order.venue == "polymarket"
        assert order.filled_size == pytest.approx(10.0)
        assert order.remaining_size == pytest.approx(0.0)
        # Polymarket DOES have a per-outcome CLOB token id, so it is used.
        assert order.token_id == f"{PM_MARKET}-yes"
        assert order.extra_data["tif"] == "GTC"

        trades = list(
            (await session.execute(select(TradeRow).where(TradeRow.mode == "paper")))
            .scalars()
            .all()
        )
        assert len(trades) == 1
        assert trades[0].side is OrderSide.BUY
        assert trades[0].price == pytest.approx(0.50)
        assert trades[0].size == pytest.approx(10.0)
        assert trades[0].fee == pytest.approx(0.10)
        assert trades[0].order_id == order.id
        assert trades[0].extra_data["unwind"] is False

        position = await session.scalar(
            select(PositionRow).where(PositionRow.mode == "paper")
        )
        assert position is not None
        assert position.size == pytest.approx(10.0)
        assert position.avg_entry_price == pytest.approx(0.50)
        # total_cost carries the fee: 10 * 0.50 + 0.10 = 5.10
        assert position.total_cost == pytest.approx(5.10)
        assert position.venue == "polymarket"
        assert position.closed_at is None

        record = await session.get(IntentRecord, routed.intent_id)
        assert record is not None
        assert record.status == "executed"
        assert record.mode == "paper"
        assert record.strategy == "unit-test"

    assert ledger.available("polymarket") == pytest.approx(994.90)
    assert ledger.locked("polymarket") == pytest.approx(0.0)
    # The OTHER venue is untouched: no reservation, no settlement.
    assert ledger.available("kalshi") == pytest.approx(1000.0)


# ---------------------------------------------------------------------------
# 2. A duplicate client_order_id returns the ORIGINAL ack, books nothing new
# ---------------------------------------------------------------------------


async def test_duplicate_client_order_id_returns_original_ack_and_no_second_trade(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Re-submitting the same intent must be idempotent end to end.

    The venue returns the ORIGINAL acknowledgement (so the caller sees
    the fill that really happened, not a second one), the UNIQUE
    constraint on `client_order_id` surfaces as a catchable
    `IntegrityError` the router interprets as a replay, and the freshly
    reserved capital is released rather than settled — so the ledger
    still reads `1000.00 - 5.10 = 994.90`, charged exactly once.
    """
    settings = build_settings()
    ledger = CapitalLedger.paper(settings)
    adapter = PaperVenueAdapter(polymarket_inner(), None, ledger)
    router = OrderRouter({"polymarket": adapter}, ledger, sessions, settings)

    intent_id = "fixed-intent-0001"
    first = await router.submit(single_leg_intent(intent_id=intent_id), "unit-test")
    after_first = ledger.available("polymarket")
    second = await router.submit(single_leg_intent(intent_id=intent_id), "unit-test")

    assert first.intent_id == second.intent_id == intent_id
    assert first.legs[0].status == "filled"
    assert second.legs[0].status == "replayed"
    # The ORIGINAL ack is what came back: the same 10 contracts, not 20.
    assert second.legs[0].filled_size == pytest.approx(10.0)

    async with sessions() as session:
        orders = (
            (await session.execute(select(func.count()).select_from(OrderRow)))
        ).scalar_one()
        trades = (
            (await session.execute(select(func.count()).select_from(TradeRow)))
        ).scalar_one()
        positions = list(
            (await session.execute(select(PositionRow))).scalars().all()
        )
    assert orders == 1, "the duplicate must not create a second order row"
    assert trades == 1, "the duplicate must not create a second Trade"
    assert len(positions) == 1
    assert positions[0].size == pytest.approx(10.0), "position must not double"

    assert after_first == pytest.approx(994.90)
    assert ledger.available("polymarket") == pytest.approx(994.90)
    assert ledger.locked("polymarket") == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 3. all_or_none + a thin NO book -> the YES leg is unwound
# ---------------------------------------------------------------------------


async def test_all_or_none_unwinds_the_filled_leg_when_the_other_is_thin(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A half-filled `all_or_none` intent must not leave a naked leg.

    The NO book has NO asks at all, so the NO leg cannot fill; the YES
    leg fills in full. Holding YES alone is a directional bet the
    strategy never asked for, and there is no cross-venue (or even
    cross-order) atomicity to undo the first leg for free — so the
    router sells it back, and that round trip costs two taker fees.

    Hand-computed (Polymarket, rate 0.04, locked 0.50/0.50 YES book):
        buy fee   = 10 * 0.04 * 0.50 * 0.50 = 0.10
        cash out  = 10 * 0.50 + 0.10        = 5.10
        sell fee  = 10 * 0.04 * 0.50 * 0.50 = 0.10
        cash in   = 10 * 0.50 - 0.10        = 4.90
        ledger    = 1000.00 - 5.10 + 4.90   = 999.80  (= 1000 - 0.20 fees)
        realized unwind cost = 5.10 - 4.90  = 0.20
    """
    settings = build_settings()
    ledger = CapitalLedger.paper(settings)
    # NO has bids but NO asks: nothing to buy, so the NO leg gets nothing.
    inner = polymarket_inner(no_asks=[], no_bids=[(0.49, 500.0)])
    adapter = PaperVenueAdapter(inner, None, ledger)
    router = OrderRouter({"polymarket": adapter}, ledger, sessions, settings)

    intent = Intent(
        kind="complement",
        legs=[
            Leg(
                market_id=PM_MARKET,
                outcome="YES",
                side="BUY",
                limit_price=0.50,
                size_contracts=10.0,
                venue="polymarket",
            ),
            Leg(
                market_id=PM_MARKET,
                outcome="NO",
                side="BUY",
                limit_price=0.50,
                size_contracts=10.0,
                venue="polymarket",
            ),
        ],
        hold_to_resolution=True,
        atomicity="all_or_none",
        confidence=0.9,
    )

    routed = await router.submit(intent, "complement-arb")

    assert routed.legs[0].filled_size == pytest.approx(10.0)
    assert routed.legs[1].filled_size == pytest.approx(0.0)
    assert len(routed.unwinds) == 1
    unwind = routed.unwinds[0]
    assert unwind.side == "SELL"
    assert unwind.unwind is True
    assert unwind.filled_size == pytest.approx(10.0)
    assert unwind.avg_price == pytest.approx(0.50)
    assert unwind.fee == pytest.approx(0.10)
    # The unwind's limit is placed 5 ticks below the 0.50 bid, but the
    # fill still happens AT the book's own level price.
    assert unwind.limit_price == pytest.approx(0.45)
    # No exposure was left behind: the sale completed.
    assert routed.naked_legs == ()
    assert routed.unwind_cost_usd == pytest.approx(0.20)

    # The whole round trip cost exactly the two taker fees.
    assert ledger.available("polymarket") == pytest.approx(999.80)
    assert ledger.locked("polymarket") == pytest.approx(0.0)

    async with sessions() as session:
        # `all_or_none` places with tif=IOC. The `ordertype` enum in
        # migration 004 has no IOC member, so the row records FOK (the
        # other NON-RESTING type) and the authoritative time-in-force
        # lives in extra_data. A follow-up migration adding IOC should
        # backfill from there.
        entry = await session.scalar(
            select(OrderRow).where(OrderRow.outcome == "YES")
        )
        assert entry is not None
        assert entry.extra_data["tif"] == "IOC"
        assert entry.order_type is OrderType.FOK

        trades = list(
            (
                await session.execute(
                    select(TradeRow).order_by(TradeRow.id)
                )
            )
            .scalars()
            .all()
        )
        assert [t.side for t in trades] == [OrderSide.BUY, OrderSide.SELL]
        assert trades[1].extra_data["unwind"] is True

        position = await session.scalar(select(PositionRow))
        assert position is not None
        assert position.size == pytest.approx(0.0)
        assert position.closed_at is not None
        # THE TWO NUMBERS FOR THIS EVENT MUST AGREE. Bought and sold at
        # 0.50, so the round trip cost exactly the two taker fees:
        #     basis    = 10 * 0.50 + 0.10 (entry fee) = 5.10
        #     proceeds = 10 * 0.50 - 0.10 (exit fee)  = 4.90
        #     realized = 4.90 - 5.10                  = -0.20
        # which is minus the router's own `unwind_cost_usd` (0.20) and
        # matches the ledger's 1000.00 -> 999.80 above. A fee-EXCLUSIVE
        # basis would say -0.10 and understate the loss by exactly one
        # taker fee.
        assert position.realized_pnl == pytest.approx(-0.20)
        assert position.realized_pnl == pytest.approx(-routed.unwind_cost_usd)
        # A fully closed position holds nothing, so it costs nothing —
        # the entry fee is realized, not stranded on `total_cost`.
        assert position.total_cost == pytest.approx(0.0)

        record = await session.get(IntentRecord, routed.intent_id)
        assert record is not None
        assert record.extra_data["unwind"]["attempted"] == 1
        assert record.extra_data["unwind"]["completed"] == 1
        assert record.extra_data["unwind"]["realized_cost_usd"] == pytest.approx(0.20)
        assert record.extra_data["unwind"]["naked_legs"] == []


async def test_failed_unwind_leaves_a_loud_persisted_naked_leg(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """An unwind with NO BID to sell into must never fail quietly.

    This is the worst state the system can reach: a directional position
    the strategy never wanted, that could not be closed. It has to
    surface on the result AND on the persisted intent row, not only in a
    log line.
    """
    settings = build_settings()
    ledger = CapitalLedger.paper(settings)
    # YES can be bought (asks exist) but has NO bid to sell back into,
    # and NO cannot be bought at all.
    inner = polymarket_inner(
        yes_bids=[], yes_asks=[(0.50, 500.0)], no_asks=[], no_bids=[]
    )
    adapter = PaperVenueAdapter(inner, None, ledger)
    router = OrderRouter({"polymarket": adapter}, ledger, sessions, settings)

    intent = Intent(
        kind="complement",
        legs=[
            Leg(
                market_id=PM_MARKET,
                outcome="YES",
                side="BUY",
                limit_price=0.50,
                size_contracts=10.0,
                venue="polymarket",
            ),
            Leg(
                market_id=PM_MARKET,
                outcome="NO",
                side="BUY",
                limit_price=0.50,
                size_contracts=10.0,
                venue="polymarket",
            ),
        ],
        hold_to_resolution=True,
        atomicity="all_or_none",
        confidence=0.9,
    )

    routed = await router.submit(intent, "complement-arb")

    assert routed.has_naked_exposure is True
    assert len(routed.naked_legs) == 1
    naked = routed.naked_legs[0]
    assert naked.reason == "no_bid"
    assert naked.size == pytest.approx(10.0)
    assert naked.avg_price == pytest.approx(0.50)
    assert naked.venue == "polymarket"

    async with sessions() as session:
        record = await session.get(IntentRecord, routed.intent_id)
        assert record is not None
        assert record.extra_data["naked_exposure"] is True
        assert record.extra_data["unwind"]["naked_legs"][0]["reason"] == "no_bid"
        # The exposure is REAL and stays on the books until someone acts.
        position = await session.scalar(select(PositionRow))
        assert position is not None
        assert position.size == pytest.approx(10.0)
        assert position.closed_at is None


# ---------------------------------------------------------------------------
# 4. Capital is per venue (GUARDRAILS.md §1.6 / PLAN.md R6)
# ---------------------------------------------------------------------------


def test_ledger_never_lets_kalshi_draw_on_the_polymarket_balance() -> None:
    """A Kalshi reservation must fail on Kalshi's own balance alone.

    Kalshi dollars sit in a CFTC-regulated FCM account whose ACH/wire
    settlement is measured in days, so a "combined" $1,005 is not
    capital that could reach a Kalshi order. The cross-venue bound is
    `min`, never a total.
    """
    ledger = CapitalLedger({"polymarket": 1000.0, "kalshi": 5.0})

    with pytest.raises(InsufficientCapital) as excinfo:
        ledger.reserve("kalshi", 100.0)

    assert excinfo.value.venue == "kalshi"
    assert excinfo.value.available == pytest.approx(5.0)
    assert ledger.available("polymarket") == pytest.approx(1000.0)
    assert ledger.available("kalshi") == pytest.approx(5.0)
    assert ledger.locked("kalshi") == pytest.approx(0.0)
    # The correct cross-venue bound: the poorer venue, not 1005.0.
    assert ledger.min_available(["polymarket", "kalshi"]) == pytest.approx(5.0)


async def test_router_rejects_a_kalshi_leg_the_kalshi_balance_cannot_cover(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The same rule, through the router: no leg, no rows, no borrowing.

    Kalshi is seeded with $5 and Polymarket with $1,000; a 100-contract
    Kalshi BUY at 0.50 needs ~$50 on Kalshi. It must be rejected outright
    rather than funded from the other venue.
    """
    settings = build_settings(
        paper_starting_balances={"polymarket": 1000.0, "kalshi": 5.0}
    )
    ledger = CapitalLedger.paper(settings)
    kalshi_adapter = CountingPaperAdapter(kalshi_inner(), None, ledger)
    router = OrderRouter({"kalshi": kalshi_adapter}, ledger, sessions, settings)

    intent = Intent(
        kind="single",
        legs=[
            Leg(
                market_id=KALSHI_MARKET,
                outcome="YES",
                side="BUY",
                limit_price=0.50,
                size_contracts=100.0,
                venue="kalshi",
            )
        ],
        hold_to_resolution=False,
        atomicity="best_effort",
        confidence=0.9,
    )

    routed = await router.submit(intent, "unit-test")

    assert routed.status == "rejected"
    assert routed.reason == "insufficient_capital"
    assert kalshi_adapter.place_order_calls == 0
    assert ledger.available("polymarket") == pytest.approx(1000.0)
    assert ledger.available("kalshi") == pytest.approx(5.0)
    assert ledger.locked("kalshi") == pytest.approx(0.0)

    async with sessions() as session:
        orders = (
            await session.execute(select(func.count()).select_from(OrderRow))
        ).scalar_one()
        record = await session.get(IntentRecord, routed.intent_id)
    assert orders == 0
    assert record is not None
    assert record.status == "rejected"
    assert record.extra_data["rejected_reason"] == "insufficient_capital"


# ---------------------------------------------------------------------------
# 5. reconcile marks a stale PENDING order FAILED with reason no_ack
# ---------------------------------------------------------------------------


async def test_reconcile_marks_a_stale_pending_order_failed(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A `PENDING` row the venue has never heard of must not stay live.

    `OrderRouter` commits the row BEFORE calling the venue, so "no venue
    record" is the expected state while a request is in flight. Past the
    grace window that is no longer plausible, and the row must stop
    claiming exposure. A row still inside the window is left alone in
    the same pass — proving the grace window is real and not a rounding
    accident.
    """
    settings = build_settings(reconcile_grace_s=120.0)
    now = utcnow()
    async with sessions() as session:
        market = Market(venue="polymarket", condition_id=PM_MARKET, question="Q?")
        session.add(market)
        await session.flush()
        for suffix, age in (("stale", timedelta(minutes=10)), ("fresh", timedelta(seconds=5))):
            session.add(
                OrderRow(
                    market_id=market.id,
                    venue="polymarket",
                    client_order_id=f"{suffix}:0:0",
                    intent_id=suffix,
                    token_id="",
                    outcome="YES",
                    side=OrderSide.BUY,
                    status=OrderStatus.PENDING,
                    mode="paper",
                    price=0.50,
                    size=10.0,
                    filled_size=0.0,
                    remaining_size=10.0,
                    created_at=now - age,
                    updated_at=now - age,
                )
            )
        await session.commit()

    ledger = CapitalLedger.paper(settings)
    adapter = PaperVenueAdapter(polymarket_inner(), None, ledger)

    report = await reconcile(
        "polymarket",
        adapter,
        sessions,
        mode="paper",
        now=now,
        settings_obj=settings,
    )

    assert report.checked == 2
    assert report.marked_failed == 1
    assert report.unresolved == 1

    async with sessions() as session:
        stale = await session.scalar(
            select(OrderRow).where(OrderRow.client_order_id == "stale:0:0")
        )
        fresh = await session.scalar(
            select(OrderRow).where(OrderRow.client_order_id == "fresh:0:0")
        )
    assert stale is not None and fresh is not None
    assert stale.status is OrderStatus.FAILED
    assert stale.error_message == NO_ACK_REASON
    assert stale.remaining_size == pytest.approx(0.0)
    assert fresh.status is OrderStatus.PENDING, "the grace window must be honoured"


# ---------------------------------------------------------------------------
# 6. A risk limit rejects before anything is placed
# ---------------------------------------------------------------------------


async def test_risk_limit_rejects_an_oversized_leg_before_any_placement(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """An oversized leg must be stopped BEFORE it reaches the adapter.

    `max_order_notional_usd` is $100 here and the leg is
    `1000 * 0.50 = $500` of notional. The proof that nothing was placed
    is the adapter's own call count, not the absence of rows: a router
    that placed first and failed to persist would leave no rows either.
    """
    settings = build_settings(max_order_notional_usd=100.0)
    ledger = CapitalLedger.paper(settings)
    adapter = CountingPaperAdapter(polymarket_inner(), None, ledger)
    router = OrderRouter({"polymarket": adapter}, ledger, sessions, settings)

    routed = await router.submit(single_leg_intent(size=1000.0), "unit-test")

    assert routed.status == "rejected"
    assert routed.reason == "risk_limit"
    assert adapter.place_order_calls == 0
    # Nothing was reserved either: the fences run before the ledger.
    assert ledger.available("polymarket") == pytest.approx(1000.0)
    assert ledger.locked("polymarket") == pytest.approx(0.0)

    async with sessions() as session:
        orders = (
            await session.execute(select(func.count()).select_from(OrderRow))
        ).scalar_one()
        trades = (
            await session.execute(select(func.count()).select_from(TradeRow))
        ).scalar_one()
        record = await session.get(IntentRecord, routed.intent_id)
    assert orders == 0
    assert trades == 0
    assert record is not None
    assert record.status == "rejected"
    assert record.extra_data["rejected_reason"] == "risk_limit"


async def test_reconcile_changes_nothing_when_the_venue_cannot_be_read(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A venue outage must not be mistaken for a venue with nothing on it.

    If `get_open_orders()` raises, an empty result is "we do not know",
    not "your orders are gone". Concluding the latter would mark live,
    working orders `FAILED` en masse during a bad minute at the venue —
    turning a transient outage into a corrupted view of real exposure.
    """
    settings = build_settings(reconcile_grace_s=1.0)
    now = utcnow()
    async with sessions() as session:
        market = Market(venue="polymarket", condition_id=PM_MARKET, question="Q?")
        session.add(market)
        await session.flush()
        session.add(
            OrderRow(
                market_id=market.id,
                venue="polymarket",
                client_order_id="outage:0:0",
                intent_id="outage",
                token_id="",
                outcome="YES",
                side=OrderSide.BUY,
                status=OrderStatus.PENDING,
                mode="paper",
                price=0.50,
                size=10.0,
                filled_size=0.0,
                remaining_size=10.0,
                created_at=now - timedelta(hours=1),
                updated_at=now - timedelta(hours=1),
            )
        )
        await session.commit()

    class UnreadableAdapter(PaperVenueAdapter):
        """Simulates a venue that is up enough to reject, not to answer."""

        async def get_open_orders(self):  # type: ignore[no-untyped-def]
            raise RuntimeError("429 Too Many Requests")

    ledger = CapitalLedger.paper(settings)
    adapter = UnreadableAdapter(polymarket_inner(), None, ledger)

    report = await reconcile(
        "polymarket", adapter, sessions, mode="paper", now=now, settings_obj=settings
    )

    assert report.checked == 1
    assert report.unresolved == 1
    assert report.marked_failed == 0
    assert report.marked_cancelled == 0

    async with sessions() as session:
        row = await session.scalar(
            select(OrderRow).where(OrderRow.client_order_id == "outage:0:0")
        )
    assert row is not None
    # An hour past a 1-second grace window, and STILL untouched, purely
    # because the venue could not be read.
    assert row.status is OrderStatus.PENDING
    assert row.error_message is None


async def test_reconcile_records_discovered_fills_and_marks_the_order_filled(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A `PENDING` row whose order DID fill must be repaired, not failed.

    This is the crash the `PENDING`-before-placement design exists for:
    the venue took the order and filled it, but the process died before
    the acknowledgement was written. Reconciliation has to find the fill
    and book it — including the `Trade` row the router never got to
    write — rather than declare the order lost.
    """
    settings = build_settings()
    ledger = CapitalLedger.paper(settings)
    adapter = PaperVenueAdapter(polymarket_inner(), None, ledger)
    client_order_id = "recon:0:0"

    now = utcnow()
    async with sessions() as session:
        market = Market(venue="polymarket", condition_id=PM_MARKET, question="Q?")
        session.add(market)
        await session.flush()
        session.add(
            OrderRow(
                market_id=market.id,
                venue="polymarket",
                client_order_id=client_order_id,
                intent_id="recon",
                token_id=f"{PM_MARKET}-yes",
                outcome="YES",
                side=OrderSide.BUY,
                status=OrderStatus.PENDING,
                mode="paper",
                price=0.50,
                size=10.0,
                filled_size=0.0,
                remaining_size=10.0,
                created_at=now - timedelta(minutes=10),
                updated_at=now - timedelta(minutes=10),
            )
        )
        await session.commit()

    # The venue really did take and fill it; only the ack was lost.
    ack = await adapter.place_order(
        OrderRequest(
            venue="polymarket",
            market_id=PM_MARKET,
            outcome="YES",
            side="BUY",
            price=0.50,
            size=10.0,
            tif="IOC",
            client_order_id=client_order_id,
        )
    )
    assert ack.status == "filled"

    report = await reconcile(
        "polymarket", adapter, sessions, mode="paper", settings_obj=settings
    )

    assert report.marked_filled == 1
    assert report.marked_failed == 0
    assert report.trades_recorded == 1

    async with sessions() as session:
        row = await session.scalar(
            select(OrderRow).where(OrderRow.client_order_id == client_order_id)
        )
        trade = await session.scalar(
            select(TradeRow).where(TradeRow.trade_id == f"{client_order_id}#0")
        )
    assert row is not None
    assert row.status is OrderStatus.FILLED
    assert row.filled_size == pytest.approx(10.0)
    assert row.remaining_size == pytest.approx(0.0)
    assert trade is not None
    assert trade.mode == "paper"
    assert trade.price == pytest.approx(0.50)
    # 10 * 0.04 * 0.50 * 0.50 = 0.10
    assert trade.fee == pytest.approx(0.10)
    assert trade.extra_data["reconciled"] is True

    # Running the pass again must change nothing: the trade id is
    # deterministic, so a repeat cannot double-book the same fill.
    again = await reconcile(
        "polymarket", adapter, sessions, mode="paper", settings_obj=settings
    )
    assert again.checked == 0
    async with sessions() as session:
        trades = (
            await session.execute(select(func.count()).select_from(TradeRow))
        ).scalar_one()
    assert trades == 1


# ---------------------------------------------------------------------------
# PaperVenueAdapter mechanics: resting GTC residuals, poll, cancel
# ---------------------------------------------------------------------------


def _yes_book(asks: list[tuple[float, float]]):
    """Build a YES book on the fixture market with the given ask side."""
    return make_book(
        bids=[(0.49, 500.0)],
        asks=asks,
        venue="polymarket",
        market_id=PM_MARKET,
        outcome="YES",
    )


async def test_gtc_residual_rests_and_completes_on_poll() -> None:
    """A `"GTC"` residual rests, and fills only when the book actually moves.

    `SimulatedFillEngine` reports a residual but never rests it — that is
    the adapter's job. The residual must not fill out of thin air: each
    fill below happens only because the ask side moved to a price the
    order's limit permits, which is a genuine market event.
    """
    settings = build_settings()
    ledger = CapitalLedger.paper(settings)
    inner = polymarket_inner(yes_asks=[(0.60, 500.0)])
    adapter = PaperVenueAdapter(inner, None, ledger)

    ack = await adapter.place_order(
        OrderRequest(
            venue="polymarket",
            market_id=PM_MARKET,
            outcome="YES",
            side="BUY",
            price=0.50,
            size=20.0,
            tif="GTC",
            client_order_id="rest:0:0",
        )
    )
    # The whole order is above the limit, so nothing fills and all 20
    # contracts rest — the ONE status the brief's four-value vocabulary
    # reserves for "accepted, working, nothing done yet".
    assert ack.status == "open"
    assert ack.filled_size == pytest.approx(0.0)
    assert ack.remaining_size == pytest.approx(20.0)
    assert len(await adapter.get_open_orders()) == 1

    # Book unchanged: polling must NOT invent a fill.
    assert await adapter.poll() == []
    assert len(await adapter.get_open_orders()) == 1

    # The ask comes down to the limit, but only 5 deep.
    inner.set_book(_yes_book([(0.50, 5.0), (0.60, 500.0)]))
    changed = await adapter.poll()
    assert len(changed) == 1
    assert changed[0].status == "partially_filled"
    assert changed[0].filled_size == pytest.approx(5.0)
    assert changed[0].remaining_size == pytest.approx(15.0)
    assert len(await adapter.get_open_orders()) == 1

    # Now there is real depth at the limit and the residual completes.
    inner.set_book(_yes_book([(0.50, 500.0)]))
    changed = await adapter.poll()
    assert len(changed) == 1
    assert changed[0].status == "filled"
    assert changed[0].filled_size == pytest.approx(20.0)
    assert await adapter.get_open_orders() == []
    positions = await adapter.get_positions()
    assert positions[0].size == pytest.approx(20.0)


async def test_cancel_order_removes_a_resting_order() -> None:
    """Cancelling a rested order clears its residual, and only once.

    A second cancellation raises rather than succeeding silently: a
    venue refuses to cancel an order it no longer has, and pretending
    otherwise would let a caller believe it had removed exposure it
    still holds.
    """
    settings = build_settings()
    ledger = CapitalLedger.paper(settings)
    adapter = PaperVenueAdapter(polymarket_inner(yes_asks=[(0.50, 5.0)]), None, ledger)

    ack = await adapter.place_order(
        OrderRequest(
            venue="polymarket",
            market_id=PM_MARKET,
            outcome="YES",
            side="BUY",
            price=0.50,
            size=20.0,
            tif="GTC",
            client_order_id="cancel-me:0:0",
        )
    )
    assert len(await adapter.get_open_orders()) == 1

    await adapter.cancel_order(ack.order_id)

    assert await adapter.get_open_orders() == []
    with pytest.raises(KeyError):
        await adapter.cancel_order(ack.order_id)


async def test_paper_adapter_returns_the_original_ack_for_a_duplicate_key() -> None:
    """Idempotency at the adapter itself: no second simulation, no second fill.

    The router's replay handling sits on top of this, but the property
    has to hold at the venue boundary too — that is where a real venue
    enforces it.
    """
    settings = build_settings()
    ledger = CapitalLedger.paper(settings)
    adapter = PaperVenueAdapter(polymarket_inner(), None, ledger)
    request = OrderRequest(
        venue="polymarket",
        market_id=PM_MARKET,
        outcome="YES",
        side="BUY",
        price=0.50,
        size=10.0,
        tif="IOC",
        client_order_id="dupe:0:0",
    )

    first = await adapter.place_order(request)
    second = await adapter.place_order(request)

    assert first.order_id == second.order_id
    assert second.filled_size == pytest.approx(10.0), "not 20: nothing re-filled"
    positions = await adapter.get_positions()
    assert len(positions) == 1
    assert positions[0].size == pytest.approx(10.0)
    assert len(await adapter.get_fills(utcnow() - timedelta(minutes=1))) == 1


# ---------------------------------------------------------------------------
# Supporting units: tick snapping and the Kalshi token_id decision
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("price", "side", "expected"),
    [
        # A BUY rounds DOWN: snapping must never reach a level the
        # strategy did not authorize paying for.
        (0.4567, "BUY", 0.45),
        # A SELL rounds UP: same rule, mirrored.
        (0.4567, "SELL", 0.46),
        # Already on the grid: unchanged in both directions.
        (0.46, "BUY", 0.46),
        (0.46, "SELL", 0.46),
        # Arrived via arithmetic (0.40 + 0.01 is 0.41000000000000003 in
        # binary floating point) and must still read as 0.41.
        (0.40 + 0.01, "BUY", 0.41),
    ],
)
def test_snap_to_tick_is_conservative(price: float, side: str, expected: float) -> None:
    """Tick snapping only ever makes an order LESS aggressive."""
    assert snap_to_tick(price, 0.01, side) == pytest.approx(expected)  # type: ignore[arg-type]


def test_token_id_is_empty_for_kalshi_and_real_for_polymarket() -> None:
    """Kalshi has no per-outcome token id, so `token_id` is `""`.

    Kalshi's adapter maps BOTH outcomes to the market ticker, which is
    not a token id and is not unique across outcomes — writing it into
    `token_id` would silently merge a market's YES and NO sides for
    anything keyed on that column. Polymarket does mint real per-outcome
    CLOB token ids, and those are used.
    """
    kalshi = make_venue_market("kalshi", KALSHI_MARKET)
    polymarket = make_venue_market("polymarket", PM_MARKET)

    assert kalshi.outcome_ids["YES"] == KALSHI_MARKET
    assert token_id_for(kalshi, "YES") == ""
    assert token_id_for(kalshi, "NO") == ""
    assert token_id_for(polymarket, "YES") == f"{PM_MARKET}-yes"
    assert token_id_for(polymarket, "NO") == f"{PM_MARKET}-no"


# ---------------------------------------------------------------------------
# T14 REMEDIATION 1. The live seam: fills that name the VENUE's order id
#
# Every test above this line routes through a `PaperVenueAdapter`, which
# stamps `metadata["client_order_id"]` on every simulated fill. Neither
# real adapter does, and neither venue's fills endpoint echoes a client
# order id at all. A paper-only suite is therefore structurally incapable
# of noticing that the router could not match a live fill to its order —
# which is why 552 green tests coexisted with a live path that booked no
# `Trade`, no `Position`, a zero fee, and released the whole capital
# reservation on a FULLY FILLED real order.
# ---------------------------------------------------------------------------


async def test_live_shaped_fills_are_matched_booked_and_settled(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A venue-shaped fill must book a trade, a position, a fee and a SETTLE.

    `LiveShapedAdapter` reproduces both real adapters' fill shape
    exactly: `Fill.order_id` is the VENUE's order id (unrelated to the
    client key) and `Fill.metadata` carries only the venue's own keys
    (`market`/`asset_id` on Polymarket). Nothing here names the
    `client_order_id`.

    Hand-computed (10 contracts at 0.50, the adapter's flat 0.01/contract
    fee):
        fee      = 10 * 0.01            = 0.10
        spent    = 10 * 0.50 + 0.10     = 5.10
        reserved = 10 * 0.50 + worst-case fee 0.10 + headroom 0.10 = 5.20
        free     = 1000.00 - 5.10       = 994.90

    The ledger figure is the load-bearing one. Matching by the client key
    alone found NO fills, and `_resolve_reservation`'s "no fills" branch
    then RELEASED all 5.20 — leaving 1000.00, as though a filled order
    had cost nothing at all.
    """
    settings = build_settings()
    ledger = CapitalLedger.paper(settings)
    adapter = LiveShapedAdapter(polymarket_inner(), fee_per_contract=0.01)
    router = OrderRouter({"polymarket": adapter}, ledger, sessions, settings)

    routed = await router.submit(single_leg_intent(), "live-shaped")

    assert routed.status == "executed"
    leg = routed.legs[0]
    assert leg.status == "filled"
    assert leg.filled_size == pytest.approx(10.0)
    # A zero fee here is the signature of the defect: it means no fill
    # was matched to this order.
    assert leg.fee == pytest.approx(0.10)
    assert leg.venue_order_id == "polymarket-venue-order-1"
    assert leg.venue_order_id != leg.client_order_id

    # The reservation was SETTLED at what was spent, not released.
    assert ledger.available("polymarket") == pytest.approx(994.90)
    assert ledger.locked("polymarket") == pytest.approx(0.0)

    async with sessions() as session:
        trades = list((await session.execute(select(TradeRow))).scalars().all())
        assert len(trades) == 1
        assert trades[0].side is OrderSide.BUY
        assert trades[0].price == pytest.approx(0.50)
        assert trades[0].size == pytest.approx(10.0)
        assert trades[0].fee == pytest.approx(0.10)

        position = await session.scalar(select(PositionRow))
        assert position is not None
        assert position.size == pytest.approx(10.0)
        assert position.avg_entry_price == pytest.approx(0.50)
        assert position.total_cost == pytest.approx(5.10)

        order = await session.scalar(select(OrderRow))
        assert order is not None
        assert order.status is OrderStatus.FILLED
        assert order.order_id == "polymarket-venue-order-1"


async def test_a_fill_labelled_with_another_orders_client_key_is_not_stolen(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A LABELLED fill belongs to the order it names, and to no other.

    The venue-order-id arm of the matching rule must never override an
    explicit `metadata["client_order_id"]`, or a paper fill could be
    attributed to a second order that happened to share an id.
    """
    now = utcnow()
    mine = Fill(
        venue="polymarket",
        order_id="venue-1",
        price=0.50,
        size=1.0,
        fee=0.01,
        ts=now,
        liquidity="taker",
        metadata={"client_order_id": "intent:0:0"},
    )
    someone_elses = Fill(
        venue="polymarket",
        order_id="venue-1",
        price=0.50,
        size=1.0,
        fee=0.01,
        ts=now,
        liquidity="taker",
        metadata={"client_order_id": "other:0:0"},
    )
    unlabelled = Fill(
        venue="polymarket",
        order_id="venue-1",
        price=0.50,
        size=1.0,
        fee=0.01,
        ts=now,
        liquidity="taker",
        metadata={"market": "PM-1", "asset_id": "PM-1-asset"},
    )

    assert _fill_matches(mine, "intent:0:0", "venue-1") is True
    assert _fill_matches(someone_elses, "intent:0:0", "venue-1") is False
    assert _fill_matches(unlabelled, "intent:0:0", "venue-1") is True
    # An unidentifiable fill must not be attributed to every order at once.
    assert _fill_matches(unlabelled, "intent:0:0", "") is False


# ---------------------------------------------------------------------------
# T14 REMEDIATION 2. The kill switch halts PLACEMENT (paper and live),
# and reconciliation keeps running while it is engaged.
# ---------------------------------------------------------------------------


async def test_kill_switch_halts_placement_on_an_already_built_router(
    sessions: async_sessionmaker[AsyncSession], tmp_path
) -> None:
    """An engaged switch must stop the NEXT order, not just the first.

    `app/api/deps.py` caches one `OrderRouter` and its adapters for the
    life of the process, so a check that only ran in a live adapter's
    CONSTRUCTOR ran once, at the first order, and an operator throwing
    the switch afterwards changed nothing at all. This test builds the
    router ONCE and toggles the switch under it, in paper mode — a halt
    that could not be exercised in paper is a halt nobody has tested.
    """
    switch = tmp_path / "HALT_TRADING"
    settings = build_settings(kill_switch_path=str(switch))
    ledger = CapitalLedger.paper(settings)
    adapter = CountingPaperAdapter(polymarket_inner(), None, ledger)
    router = OrderRouter({"polymarket": adapter}, ledger, sessions, settings)

    # 1. Switch engaged: nothing is planned, reserved or placed.
    switch.write_text("")
    halted = await router.submit(single_leg_intent(intent_id="halted"), "unit-test")

    assert halted.status == "rejected"
    assert halted.reason == "kill_switch"
    assert adapter.place_order_calls == 0
    assert ledger.available("polymarket") == pytest.approx(1000.0)
    assert ledger.locked("polymarket") == pytest.approx(0.0)

    async with sessions() as session:
        record = await session.get(IntentRecord, "halted")
        assert record is not None
        assert record.status == "rejected"
        assert record.extra_data["rejected_reason"] == "kill_switch"
        assert await session.scalar(select(func.count()).select_from(OrderRow)) == 0

    # 2. Same router, same adapters, switch withdrawn: it places again.
    switch.unlink()
    allowed = await router.submit(single_leg_intent(intent_id="allowed"), "unit-test")

    assert allowed.status == "executed"
    assert adapter.place_order_calls == 1


async def test_reconciliation_keeps_running_while_the_kill_switch_is_engaged(
    sessions: async_sessionmaker[AsyncSession], tmp_path
) -> None:
    """The read-only pass must survive the halt that stops placement.

    Reconciliation was coupled to the placement fence through
    `get_adapter`, so engaging the switch STOPPED it — the read-only pass
    an operator most wants during a halt. The two concerns are now
    separate: placement asks `assert_placement_allowed()`, reconciliation
    asks `get_read_adapter()` for an adapter that reads and nothing more.
    """
    switch = tmp_path / "HALT_TRADING"
    switch.write_text("")
    settings = build_settings(kill_switch_path=str(switch))
    ledger = CapitalLedger.paper(settings)
    adapter = PaperVenueAdapter(polymarket_inner(), None, ledger)

    # Placement is halted...
    with pytest.raises(KillSwitchEngaged):
        assert_placement_allowed(settings)

    # ...and a PENDING row with no venue record still gets resolved.
    stale = utcnow() - timedelta(seconds=settings.reconcile_grace_s + 60)
    async with sessions() as session:
        market = Market(
            venue="polymarket",
            condition_id=PM_MARKET,
            question="Will the fixture resolve YES?",
            outcomes=["YES", "NO"],
            token_ids={"YES": f"{PM_MARKET}-yes", "NO": f"{PM_MARKET}-no"},
        )
        session.add(market)
        await session.flush()
        session.add(
            OrderRow(
                market_id=market.id,
                venue="polymarket",
                client_order_id="halted-recon:0:0",
                token_id=f"{PM_MARKET}-yes",
                outcome="YES",
                side=OrderSide.BUY,
                order_type=OrderType.GTC,
                status=OrderStatus.PENDING,
                mode="paper",
                price=0.50,
                size=10.0,
                filled_size=0.0,
                remaining_size=10.0,
                created_at=stale,
            )
        )
        await session.commit()

    report = await reconcile(
        "polymarket", adapter, sessions, mode="paper", settings_obj=settings
    )

    assert report.checked == 1
    assert report.marked_failed == 1
    async with sessions() as session:
        row = await session.scalar(select(OrderRow))
        assert row is not None
        assert row.status is OrderStatus.FAILED
        assert row.error_message == NO_ACK_REASON

    # And the beat's own adapter lookup is the read-only one, which in
    # live mode cannot place at all.
    read_adapter = get_read_adapter("polymarket", "live")
    try:
        assert not hasattr(read_adapter, "place_order")
    finally:
        await read_adapter.aclose()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# T14 REMEDIATION 3. A resting order keeps its capital reserved.
# ---------------------------------------------------------------------------


async def test_a_submitted_order_that_rests_keeps_its_capital_reserved(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Capital committed to an order the venue is still working stays locked.

    `CapitalLedger.locked` is "the only thing in the system tracking
    capital committed to open orders" (`app/execution/ledger.py`), and
    nothing re-reserves: not this router, and not `reconcile`, which does
    not touch the ledger. Releasing a resting leg's reservation therefore
    told the ledger the money was free while the venue could fill the
    order a second later.

    Hand-computed (20 contracts at a 0.50 limit, Polymarket rate 0.04):
        worst-case fee = 20 * 0.04 * 0.50 * 0.50 = 0.20
        reserved       = 20 * 0.50 + 0.20 + 0.10 = 10.30
        free           = 1000.00 - 10.30         = 989.70
    """
    settings = build_settings()
    ledger = CapitalLedger.paper(settings)
    # The whole book is above the limit, so nothing fills and all 20
    # contracts rest as a GTC residual.
    adapter = PaperVenueAdapter(
        polymarket_inner(yes_asks=[(0.60, 500.0)]), None, ledger
    )
    router = OrderRouter({"polymarket": adapter}, ledger, sessions, settings)

    routed = await router.submit(
        single_leg_intent(size=20.0, intent_id="rests"), "unit-test"
    )

    assert routed.legs[0].status == "open"
    assert routed.legs[0].filled_size == pytest.approx(0.0)
    # `best_effort` places GTC, so the venue is still working this order.
    assert routed.status == "pending"
    assert ledger.locked("polymarket") == pytest.approx(10.30)
    assert ledger.available("polymarket") == pytest.approx(989.70)

    async with sessions() as session:
        row = await session.scalar(select(OrderRow))
        assert row is not None
        assert row.status is OrderStatus.OPEN
        # Findable again: the ledger's ids are opaque, so a reservation
        # held for a resting order has to be reachable from the order it
        # belongs to or the capital could never be freed.
        assert row.extra_data["reservation_id"]
        order_row_id = row.id

    # Cancelling takes the exposure off the venue, and only THEN is the
    # capital genuinely free again.
    result = await router.cancel(order_row_id)

    assert result.cancelled is True
    assert ledger.locked("polymarket") == pytest.approx(0.0)
    assert ledger.available("polymarket") == pytest.approx(1000.0)


# ---------------------------------------------------------------------------
# T14 REMEDIATION 5. Today's realized P&L is TODAY's, not a lifetime.
# ---------------------------------------------------------------------------


async def test_todays_pnl_ignores_a_historical_gain_on_a_row_touched_today(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A gain realized last month must not mask a loss realized today.

    The daily-loss fence summed `PositionRow.realized_pnl` over rows with
    `updated_at >= day_start` — but that column is CUMULATIVE, so a
    position that gained +50 historically and is touched today
    contributed its whole lifetime gain and the fence saw a PROFIT on a
    losing day.

    Hand-computed: the first intent realizes a 0.20 loss today (buy 10 @
    0.50 fee 0.10, unwound at 0.50 fee 0.10). With
    `max_daily_loss_usd = 0.10`, today's −0.20 must halt the next order.
    The seeded position's lifetime +50.00 must not rescue it.
    """
    settings = build_settings(max_daily_loss_usd=0.10)
    ledger = CapitalLedger.paper(settings)
    inner = polymarket_inner(no_asks=[], no_bids=[(0.49, 500.0)])
    adapter = PaperVenueAdapter(inner, None, ledger)
    router = OrderRouter({"polymarket": adapter}, ledger, sessions, settings)

    def complement(intent_id: str) -> Intent:
        return Intent(
            kind="complement",
            legs=[
                Leg(
                    market_id=PM_MARKET,
                    outcome="YES",
                    side="BUY",
                    limit_price=0.50,
                    size_contracts=10.0,
                    venue="polymarket",
                ),
                Leg(
                    market_id=PM_MARKET,
                    outcome="NO",
                    side="BUY",
                    limit_price=0.50,
                    size_contracts=10.0,
                    venue="polymarket",
                ),
            ],
            hold_to_resolution=True,
            atomicity="all_or_none",
            confidence=0.9,
            metadata={"intent_id": intent_id},
        )

    first = await router.submit(complement("loss-today"), "complement-arb")
    assert first.unwind_cost_usd == pytest.approx(0.20)

    # A position that made +50.00 in a previous month and is touched
    # today. It carries no per-day record, exactly like every row written
    # before this bookkeeping existed.
    async with sessions() as session:
        market_row_id = await session.scalar(select(Market.id))
        session.add(
            PositionRow(
                market_id=market_row_id,
                venue="polymarket",
                mode="paper",
                token_id=f"{PM_MARKET}-no",
                outcome="NO",
                size=0.0,
                avg_entry_price=0.40,
                total_cost=0.0,
                realized_pnl=50.0,
                opened_at=utcnow() - timedelta(days=40),
                closed_at=utcnow() - timedelta(days=30),
                extra_data={},
            )
        )
        await session.commit()

    second = await router.submit(complement("blocked"), "complement-arb")

    assert second.status == "rejected"
    assert second.reason == "risk_limit"
    async with sessions() as session:
        record = await session.get(IntentRecord, "blocked")
        assert record is not None
        assert "daily_pnl" in record.extra_data["rejected_detail"]


# ---------------------------------------------------------------------------
# T14 REMEDIATION 7. Naked exposure is durable; `UnknownVenue` cannot escape.
# ---------------------------------------------------------------------------


async def test_an_unseeded_venue_is_a_rejection_not_an_escaped_exception(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A ledger that never heard of a venue must reject, not raise.

    `CapitalLedger.reserve` raises `UnknownVenue`, which is a
    `LedgerError` and NOT the `InsufficientCapital` the reservation step
    used to catch — so the exception escaped `submit()` entirely, past
    reservations already taken, with nothing persisted.
    """
    settings = build_settings()
    ledger = CapitalLedger({"polymarket": 1000.0})
    adapter = PaperVenueAdapter(kalshi_inner(), None, ledger)
    router = OrderRouter({"kalshi": adapter}, ledger, sessions, settings)

    routed = await router.submit(
        Intent(
            kind="single",
            legs=[
                Leg(
                    market_id=KALSHI_MARKET,
                    outcome="YES",
                    side="BUY",
                    limit_price=0.50,
                    size_contracts=10.0,
                    venue="kalshi",
                )
            ],
            hold_to_resolution=False,
            atomicity="best_effort",
            confidence=0.9,
            metadata={"intent_id": "unseeded"},
        ),
        "unit-test",
    )

    assert routed.status == "rejected"
    assert routed.reason == "capital_unavailable"
    async with sessions() as session:
        record = await session.get(IntentRecord, "unseeded")
        assert record is not None
        assert record.status == "rejected"
        assert "UnknownVenue" in record.extra_data["rejected_detail"]


async def test_paper_starting_balances_must_seed_every_venue() -> None:
    """The other half of the same defect, closed at configuration load.

    `PAPER_STARTING_BALANCES={"polymarket": 500}` was a LEGAL config that
    produced a ledger with no `kalshi` entry at all.
    """
    with pytest.raises(ValidationError) as excinfo:
        build_settings(paper_starting_balances={"polymarket": 500.0})

    assert "kalshi" in str(excinfo.value)


async def test_naked_exposure_survives_a_failure_between_placement_and_settle(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The worst state in the system must be durable on its own.

    The naked record used to be written only by `_finalize_intent`,
    inside `_settle`'s single commit — after placement and after the
    unwind. Anything raising in between lost it permanently while the
    fills stayed real at the venue.
    """

    class ExplodingSettleRouter(OrderRouter):
        """An `OrderRouter` whose settlement fails after the unwind."""

        async def _settle(self, **kwargs: object) -> RoutedIntent:  # noqa: ARG002
            raise RuntimeError("database went away between placement and settle")

    settings = build_settings()
    ledger = CapitalLedger.paper(settings)
    # YES can be bought but has no bid to sell back into, so the unwind
    # cannot complete and the leg is left naked.
    inner = polymarket_inner(
        yes_bids=[], yes_asks=[(0.50, 500.0)], no_asks=[], no_bids=[]
    )
    adapter = PaperVenueAdapter(inner, None, ledger)
    router = ExplodingSettleRouter({"polymarket": adapter}, ledger, sessions, settings)

    intent = Intent(
        kind="complement",
        legs=[
            Leg(
                market_id=PM_MARKET,
                outcome="YES",
                side="BUY",
                limit_price=0.50,
                size_contracts=10.0,
                venue="polymarket",
            ),
            Leg(
                market_id=PM_MARKET,
                outcome="NO",
                side="BUY",
                limit_price=0.50,
                size_contracts=10.0,
                venue="polymarket",
            ),
        ],
        hold_to_resolution=True,
        atomicity="all_or_none",
        confidence=0.9,
        metadata={"intent_id": "durable-naked"},
    )

    with pytest.raises(RuntimeError):
        await router.submit(intent, "complement-arb")

    async with sessions() as session:
        record = await session.get(IntentRecord, "durable-naked")
        assert record is not None
        assert record.extra_data["naked_exposure"] is True
        legs = record.extra_data["unwind"]["naked_legs"]
        assert len(legs) == 1
        assert legs[0]["reason"] == "no_bid"
        assert legs[0]["size"] == pytest.approx(10.0)


async def test_an_unhedgeable_sell_leg_is_recorded_as_naked_exposure(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """An `all_or_none` SELL that filled alone is exposure, not a no-op.

    Neither venue supports naked shorts, so a filled SELL leg cannot be
    unwound — but it reduced a position the strategy wanted to keep, as
    one half of a trade whose other half never happened. It used to be
    skipped silently, with no `NakedLeg` recorded at all.
    """
    settings = build_settings()
    ledger = CapitalLedger.paper(settings)
    # NO cannot be bought (no asks); YES has bids, so a SELL fills.
    inner = polymarket_inner(no_asks=[], no_bids=[(0.49, 500.0)])
    adapter = PaperVenueAdapter(inner, None, ledger)
    router = OrderRouter({"polymarket": adapter}, ledger, sessions, settings)

    # Build the holding the SELL leg will close.
    opened = await router.submit(
        single_leg_intent(size=10.0, intent_id="opener"), "unit-test"
    )
    assert opened.legs[0].filled_size == pytest.approx(10.0)

    routed = await router.submit(
        Intent(
            kind="complement",
            legs=[
                Leg(
                    market_id=PM_MARKET,
                    outcome="YES",
                    side="SELL",
                    limit_price=0.50,
                    size_contracts=10.0,
                    venue="polymarket",
                ),
                Leg(
                    market_id=PM_MARKET,
                    outcome="NO",
                    side="BUY",
                    limit_price=0.50,
                    size_contracts=10.0,
                    venue="polymarket",
                ),
            ],
            hold_to_resolution=False,
            atomicity="all_or_none",
            confidence=0.9,
            metadata={"intent_id": "half-sold"},
        ),
        "complement-arb",
    )

    assert routed.has_naked_exposure is True
    assert len(routed.naked_legs) == 1
    naked = routed.naked_legs[0]
    assert naked.reason == "sell_leg_not_unwindable"
    assert naked.size == pytest.approx(10.0)
    assert naked.outcome == "YES"

    async with sessions() as session:
        record = await session.get(IntentRecord, "half-sold")
        assert record is not None
        assert record.extra_data["naked_exposure"] is True


# ---------------------------------------------------------------------------
# T14 REMEDIATION 8. `unrealized_pnl` is a number that means something.
# ---------------------------------------------------------------------------


async def test_an_open_position_reports_its_unrealized_pnl(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """`unrealized_pnl`/`_pct` are written, not permanently 0.0.

    Both columns are returned on every `GET /positions` row, and nothing
    ever wrote them — so a human read "flat" on a position that is not.

    Hand-computed (buy 10 @ 0.50, fee 0.10, marked at 0.50):
        cost basis    = 10 * 0.50 + 0.10 = 5.10
        current value = 10 * 0.50        = 5.00
        unrealized    = 5.00 - 5.10      = -0.10   (the entry fee)
        pct           = -0.10 / 5.10     = -0.019607...
    """
    settings = build_settings()
    ledger = CapitalLedger.paper(settings)
    adapter = PaperVenueAdapter(polymarket_inner(), None, ledger)
    router = OrderRouter({"polymarket": adapter}, ledger, sessions, settings)

    await router.submit(single_leg_intent(), "unit-test")

    async with sessions() as session:
        position = await session.scalar(select(PositionRow))
        assert position is not None
        assert position.current_value == pytest.approx(5.00)
        assert position.total_cost == pytest.approx(5.10)
        assert position.unrealized_pnl == pytest.approx(-0.10)
        assert position.unrealized_pnl_pct == pytest.approx(-0.10 / 5.10)


# ---------------------------------------------------------------------------
# T25 -- a reconciled fill must not erase exposure from the risk fences
# ---------------------------------------------------------------------------


class _FillOnlyAdapter:
    """The minimal `app.venues.base.ReconcileAdapter`: fills, nothing else.

    `PaperVenueAdapter` refuses a SELL it has no simulated position for
    (`reason="insufficient_position"`), which is correct of it and makes
    it the wrong instrument for staging a SELL the VENUE already
    executed. This reports the fill the way a venue's `get_fills()`
    would and has no `place_order` at all — GUARDRAILS.md §1.1 by
    construction, the same property `tests.venues.fixture_adapter
    .FixtureAdapter` has.
    """

    def __init__(self, venue: str, fills: list[Fill]) -> None:
        """Store the venue and the fills this adapter will report."""
        self.venue = venue
        self._fills = fills

    async def get_open_orders(self):  # type: ignore[no-untyped-def]
        """Report nothing resting: the order in question already filled."""
        return []

    async def get_fills(self, since):  # type: ignore[no-untyped-def]
        """Report the staged fills, regardless of `since`."""
        return list(self._fills)


async def test_a_reconciled_fill_keeps_its_notional_in_both_fence_aggregates(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Booking a fill in reconciliation moved exposure NOWHERE.

    Both fences are the sum of exactly two things — resting orders'
    `remaining_size * price` and open positions' `size * avg_entry_price`
    (`OrderRouter._risk_context` for `max_open_notional_usd`,
    `_bucket_open_notional` for `max_near_resolution_notional_usd`).
    `reconcile()` sets `remaining_size = 0` and marks the order `FILLED`,
    so the notional left the first sum; with no position written it
    entered nothing, and both caps permanently under-counted. The account
    could then exceed its configured open cap with no fence firing.

    The arithmetic, by hand:

        10 contracts @ 0.50            -> $5.00 notional
        resting, pre-reconcile        : orders  $5.00 + positions $0.00
        held, post-reconcile          : orders  $0.00 + positions $5.00

    so BOTH readings must be $5.00 and the total must not move. The
    position's fee-EXCLUSIVE entry basis is what both aggregates use, so
    the Polymarket taker fee (10 * 0.04 * 0.50 * 0.50 = $0.10) shows up
    in `total_cost` ($5.10) and NOT in `size * avg_entry_price`.

    The order is tagged `near_resolution` through its owning intent, so
    this covers the bucket cap too: a fold that credited the position but
    not `extra_data["bucket_notional"]` would repair the account-wide cap
    and leave the bucket cap just as blind as before.
    """
    settings = build_settings()
    ledger = CapitalLedger.paper(settings)
    adapter = PaperVenueAdapter(polymarket_inner(), None, ledger)
    router = OrderRouter({"polymarket": adapter}, ledger, sessions, settings)
    client_order_id = "recon-near:0:0"

    now = utcnow()
    async with sessions() as session:
        market = Market(venue="polymarket", condition_id=PM_MARKET, question="Q?")
        session.add(market)
        await session.flush()
        session.add(
            IntentRecord(
                id="recon-near",
                kind="single",
                strategy="settlement_edge",
                mode="paper",
                status="pending",
                legs=[],
                score={},
                extra_data={
                    "atomicity": "best_effort",
                    "hold_to_resolution": True,
                    "confidence": 0.9,
                    "tif": "GTC",
                    "bucket": "near_resolution",
                },
            )
        )
        session.add(
            OrderRow(
                market_id=market.id,
                venue="polymarket",
                client_order_id=client_order_id,
                intent_id="recon-near",
                token_id=f"{PM_MARKET}-yes",
                outcome="YES",
                side=OrderSide.BUY,
                order_type=OrderType.GTC,
                status=OrderStatus.OPEN,
                mode="paper",
                price=0.50,
                size=10.0,
                filled_size=0.0,
                remaining_size=10.0,
                created_at=now - timedelta(minutes=10),
                updated_at=now - timedelta(minutes=10),
            )
        )
        await session.commit()

    open_notional, _ = await router._risk_context()
    assert open_notional == pytest.approx(5.00)
    assert await router._bucket_open_notional("near_resolution") == pytest.approx(5.00)

    # The venue really did fill it after `submit()` had already returned
    # (the GTC/`best_effort` case), so only reconciliation can find it.
    ack = await adapter.place_order(
        OrderRequest(
            venue="polymarket",
            market_id=PM_MARKET,
            outcome="YES",
            side="BUY",
            price=0.50,
            size=10.0,
            tif="IOC",
            client_order_id=client_order_id,
        )
    )
    assert ack.status == "filled"

    report = await reconcile(
        "polymarket", adapter, sessions, mode="paper", settings_obj=settings
    )
    assert report.marked_filled == 1
    assert report.trades_recorded == 1
    assert report.positions_credited == 1

    open_notional, _ = await router._risk_context()
    assert open_notional == pytest.approx(5.00)
    assert await router._bucket_open_notional("near_resolution") == pytest.approx(5.00)

    async with sessions() as session:
        order = await session.scalar(
            select(OrderRow).where(OrderRow.client_order_id == client_order_id)
        )
        positions = list((await session.execute(select(PositionRow))).scalars().all())
    assert order is not None
    assert order.status is OrderStatus.FILLED
    assert order.remaining_size == pytest.approx(0.0)
    assert len(positions) == 1
    position = positions[0]
    assert position.size == pytest.approx(10.0)
    assert position.avg_entry_price == pytest.approx(0.50)
    # Fee-inclusive basis: $5.00 + $0.10.
    assert position.total_cost == pytest.approx(5.10)
    assert position.intent_id == "recon-near"
    assert position.hold_to_resolution is True
    assert position.extra_data["bucket_notional"] == {
        "near_resolution": pytest.approx(5.00)
    }

    # A second pass must not double-book: the trade id is deterministic,
    # so the fill is already recorded and the fold sees no NEW fills.
    again = await reconcile(
        "polymarket", adapter, sessions, mode="paper", settings_obj=settings
    )
    assert again.positions_credited == 0
    assert await router._bucket_open_notional("near_resolution") == pytest.approx(5.00)


async def test_a_reconciled_sell_is_still_left_to_the_position_authority_pass(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """SELLs stay out of the fold, and that is the SAFE direction.

    A discovered SELL is where a basis/lot decision lives — the P&L
    `app/execution/reconcile.py` deliberately refuses to write from
    half-informed local state — so it is left to the `get_positions()`
    authority follow-up. The cost is that the sold position stays open
    locally, which makes both fences read exposure that is GONE:
    STRICTER caps, never looser. Under-counting was the bug being fixed;
    over-counting is the conservative failure this pass has always had,
    and this pins that the fix did not quietly change it into a P&L write.

    10 contracts sold @ 0.50 = $5.00. `realized_pnl` must stay exactly
    0.0 and no `extra_data["realized_by_day"]` key may appear: the
    daily-loss fence's inputs are untouched by reconciliation.
    """
    settings = build_settings()
    client_order_id = "recon-sell:0:0"

    now = utcnow()
    adapter = _FillOnlyAdapter(
        "polymarket",
        [
            Fill(
                venue="polymarket",
                order_id="venue-sell-1",
                price=0.50,
                size=10.0,
                fee=0.10,
                ts=now - timedelta(minutes=1),
                liquidity="taker",
                metadata={"client_order_id": client_order_id},
            )
        ],
    )
    async with sessions() as session:
        market = Market(venue="polymarket", condition_id=PM_MARKET, question="Q?")
        session.add(market)
        await session.flush()
        session.add(
            OrderRow(
                market_id=market.id,
                venue="polymarket",
                client_order_id=client_order_id,
                intent_id=None,
                token_id=f"{PM_MARKET}-yes",
                outcome="YES",
                side=OrderSide.SELL,
                order_type=OrderType.GTC,
                status=OrderStatus.OPEN,
                mode="paper",
                price=0.50,
                size=10.0,
                filled_size=0.0,
                remaining_size=10.0,
                created_at=now - timedelta(minutes=10),
                updated_at=now - timedelta(minutes=10),
            )
        )
        await session.commit()

    report = await reconcile(
        "polymarket", adapter, sessions, mode="paper", settings_obj=settings
    )

    assert report.marked_filled == 1
    assert report.trades_recorded == 1
    assert report.positions_credited == 0
    async with sessions() as session:
        positions = list((await session.execute(select(PositionRow))).scalars().all())
    assert positions == []
