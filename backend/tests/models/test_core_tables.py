"""Tests for the T15 model/migration changes (PLAN.md D7/D13).

Uses the `test_session` fixture from `tests/conftest.py`, which runs
`Base.metadata.create_all` on an in-memory SQLite engine with
`PRAGMA foreign_keys=ON` — so these tests exercise the SAME schema the
hand-written migration `004` must produce (verified separately, offline,
via `alembic upgrade head --sql`), not a second, drifted definition of
it. Every new/changed table (`markets`, `orders`, `trades`, `positions`,
`intents`) gets at least one row inserted with `venue="kalshi"`, per the
brief's acceptance line — the pre-T15 code path only ever exercised
`venue="polymarket"` implicitly (there was no `venue` column at all), so
proving Kalshi rows round-trip is the actual regression this file
guards.
"""
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.intent import IntentRecord
from app.models.market import Market
from app.models.position import Position
from app.models.trade import Order, OrderSide, OrderStatus, OrderType, Trade
from app.utils.time import utcnow


async def _make_market(
    session: AsyncSession, *, venue: str = "kalshi", condition_id: str = "COND-1"
) -> Market:
    """Insert and flush one `Market` row, returning it with `id` populated."""
    market = Market(
        venue=venue,
        condition_id=condition_id,
        question="Will this test pass?",
    )
    session.add(market)
    await session.flush()
    return market


class TestMarketVenueColumn:
    """`Market.venue` and the `(venue, condition_id)` composite unique."""

    async def test_insert_kalshi_market(self, test_session: AsyncSession) -> None:
        """A `venue="kalshi"` market round-trips with the new column set."""
        market = await _make_market(test_session, venue="kalshi")
        await test_session.commit()

        fetched = await test_session.get(Market, market.id)
        assert fetched is not None
        assert fetched.venue == "kalshi"
        assert fetched.condition_id == "COND-1"

    async def test_default_venue_is_polymarket(
        self, test_session: AsyncSession
    ) -> None:
        """A `Market` constructed without `venue` defaults to `"polymarket"`
        on flush (the ORM-side `default=`, per `app.models.base.Base`'s
        docstring: not visible until flush, visible after).
        """
        market = Market(condition_id="COND-DEFAULT", question="Q?")
        test_session.add(market)
        await test_session.flush()
        assert market.venue == "polymarket"

    async def test_same_venue_condition_id_conflicts(
        self, test_session: AsyncSession
    ) -> None:
        """Two markets with the SAME `(venue, condition_id)` violate the
        composite `UniqueConstraint` — this is the exact case that was
        previously caught by a single-column unique index on
        `condition_id` alone.
        """
        await _make_market(test_session, venue="kalshi", condition_id="DUP")
        await test_session.commit()

        test_session.add(
            Market(venue="kalshi", condition_id="DUP", question="Second copy")
        )
        with pytest.raises(IntegrityError):
            await test_session.commit()
        await test_session.rollback()

    async def test_same_condition_id_different_venue_does_not_conflict(
        self, test_session: AsyncSession
    ) -> None:
        """The SAME `condition_id` string on two DIFFERENT venues is not a
        conflict — this is precisely what the old single-column unique
        index on `condition_id` would have wrongly rejected, and precisely
        why T15 replaced it with a composite `(venue, condition_id)`
        constraint.
        """
        await _make_market(test_session, venue="polymarket", condition_id="SHARED")
        await test_session.commit()

        test_session.add(
            Market(venue="kalshi", condition_id="SHARED", question="Kalshi twin")
        )
        # Must NOT raise.
        await test_session.commit()

        result = await test_session.execute(
            select(Market).where(Market.condition_id == "SHARED")
        )
        rows = result.scalars().all()
        assert {m.venue for m in rows} == {"polymarket", "kalshi"}

    async def test_invalid_venue_rejected_by_check_constraint(
        self, test_session: AsyncSession
    ) -> None:
        """A `venue` outside `{"polymarket", "kalshi"}` is rejected at the
        DB layer by the `CheckConstraint` — defense in depth alongside
        pydantic-layer validation (see `app.models.trade`'s module
        docstring on mass assignment).
        """
        test_session.add(
            Market(venue="sportsbook", condition_id="BAD-VENUE", question="Q?")
        )
        with pytest.raises(IntegrityError):
            await test_session.commit()
        await test_session.rollback()


class TestOrderVenueColumns:
    """`Order.venue`/`client_order_id`/`intent_id`/`outcome`/`mode`."""

    async def test_insert_kalshi_order_with_nullable_order_id(
        self, test_session: AsyncSession
    ) -> None:
        """An `Order` with `venue="kalshi"`, no `order_id` yet (not acked
        by the venue), a `client_order_id`, an `intent_id`, a canonical
        `outcome`, and `mode="paper"` round-trips.
        """
        market = await _make_market(test_session, condition_id="ORDER-MKT")

        order = Order(
            market_id=market.id,
            venue="kalshi",
            client_order_id="intent-abc:0:0",
            intent_id="intent-abc",
            token_id="",
            outcome="YES",
            side=OrderSide.BUY,
            order_type=OrderType.GTC,
            status=OrderStatus.PENDING,
            mode="paper",
            price=0.60,
            size=10.0,
            filled_size=0.0,
            remaining_size=10.0,
        )
        test_session.add(order)
        await test_session.commit()

        fetched = await test_session.get(Order, order.id)
        assert fetched is not None
        assert fetched.order_id is None
        assert fetched.venue == "kalshi"
        assert fetched.client_order_id == "intent-abc:0:0"
        assert fetched.intent_id == "intent-abc"
        assert fetched.outcome == "YES"
        assert fetched.mode == "paper"

    async def test_duplicate_client_order_id_raises_integrity_error(
        self, test_session: AsyncSession
    ) -> None:
        """A retried submission with the SAME `client_order_id` is a
        LEGITIMATE idempotent-retry outcome (PLAN.md D4), not a crash —
        it must surface as a catchable `IntegrityError`, which `OrderRouter`
        (T14) is expected to catch and resolve to the original order.
        """
        market = await _make_market(test_session, condition_id="DUP-COID-MKT")
        base_kwargs = dict(
            market_id=market.id,
            venue="kalshi",
            client_order_id="dup-key:0:0",
            token_id="",
            outcome="YES",
            side=OrderSide.BUY,
            order_type=OrderType.GTC,
            status=OrderStatus.PENDING,
            mode="paper",
            price=0.5,
            size=1.0,
            filled_size=0.0,
            remaining_size=1.0,
        )
        test_session.add(Order(**base_kwargs))
        await test_session.commit()

        test_session.add(Order(**base_kwargs))
        with pytest.raises(IntegrityError):
            await test_session.commit()
        await test_session.rollback()

    async def test_invalid_mode_rejected_by_check_constraint(
        self, test_session: AsyncSession
    ) -> None:
        """`mode` outside `{"paper", "live"}` is rejected at the DB layer."""
        market = await _make_market(test_session, condition_id="BAD-MODE-MKT")
        test_session.add(
            Order(
                market_id=market.id,
                venue="kalshi",
                client_order_id="bad-mode:0:0",
                token_id="",
                outcome="YES",
                side=OrderSide.BUY,
                order_type=OrderType.GTC,
                status=OrderStatus.PENDING,
                mode="simulated",
                price=0.5,
                size=1.0,
                filled_size=0.0,
                remaining_size=1.0,
            )
        )
        with pytest.raises(IntegrityError):
            await test_session.commit()
        await test_session.rollback()


class TestTradeVenueColumns:
    """`Trade.venue`/`outcome`/`liquidity`/`mode`, and nullable `tx_hash`."""

    async def test_insert_kalshi_trade_with_no_tx_hash(
        self, test_session: AsyncSession
    ) -> None:
        """Kalshi trades have no on-chain settlement, so `tx_hash` must be
        insertable as `None`.
        """
        market = await _make_market(test_session, condition_id="TRADE-MKT")

        trade = Trade(
            trade_id="kalshi-trade-1",
            market_id=market.id,
            venue="kalshi",
            token_id="",
            outcome="NO",
            side=OrderSide.SELL,
            price=0.42,
            size=5.0,
            fee=0.10,
            liquidity="taker",
            mode="live",
            tx_hash=None,
            executed_at=utcnow(),
        )
        test_session.add(trade)
        await test_session.commit()

        fetched = await test_session.get(Trade, trade.id)
        assert fetched is not None
        assert fetched.tx_hash is None
        assert fetched.venue == "kalshi"
        assert fetched.outcome == "NO"
        assert fetched.liquidity == "taker"
        assert fetched.mode == "live"

    async def test_invalid_liquidity_rejected_by_check_constraint(
        self, test_session: AsyncSession
    ) -> None:
        """`liquidity` outside `{"maker", "taker", NULL}` is rejected."""
        market = await _make_market(test_session, condition_id="BAD-LIQ-MKT")
        test_session.add(
            Trade(
                trade_id="bad-liq-trade",
                market_id=market.id,
                venue="kalshi",
                token_id="",
                outcome="YES",
                side=OrderSide.BUY,
                price=0.5,
                size=1.0,
                fee=0.0,
                liquidity="both",
                mode="paper",
                executed_at=utcnow(),
            )
        )
        with pytest.raises(IntegrityError):
            await test_session.commit()
        await test_session.rollback()


class TestPositionVenueColumns:
    """`Position.venue`/`mode`/`intent_id`/`hold_to_resolution`/settlement."""

    async def test_insert_kalshi_position(self, test_session: AsyncSession) -> None:
        """A `venue="kalshi"` position with `hold_to_resolution=True` and
        settlement fields unset (not yet settled) round-trips.
        """
        market = await _make_market(test_session, condition_id="POSITION-MKT")

        position = Position(
            market_id=market.id,
            venue="kalshi",
            mode="paper",
            intent_id="intent-xyz",
            token_id="",
            outcome="YES",
            size=10.0,
            avg_entry_price=0.55,
            total_cost=5.50,
            hold_to_resolution=True,
            opened_at=utcnow(),
        )
        test_session.add(position)
        await test_session.commit()

        fetched = await test_session.get(Position, position.id)
        assert fetched is not None
        assert fetched.venue == "kalshi"
        assert fetched.mode == "paper"
        assert fetched.intent_id == "intent-xyz"
        assert fetched.hold_to_resolution is True
        assert fetched.settled_at is None
        assert fetched.settlement_outcome is None

    async def test_settled_position_round_trips(
        self, test_session: AsyncSession
    ) -> None:
        """A settled position carries `settled_at`/`settlement_outcome`."""
        market = await _make_market(test_session, condition_id="SETTLED-MKT")
        now = utcnow()

        position = Position(
            market_id=market.id,
            venue="polymarket",
            mode="live",
            token_id="",
            outcome="YES",
            size=10.0,
            avg_entry_price=0.55,
            total_cost=5.50,
            hold_to_resolution=True,
            opened_at=now - timedelta(days=1),
            settled_at=now,
            settlement_outcome="YES",
        )
        test_session.add(position)
        await test_session.commit()

        fetched = await test_session.get(Position, position.id)
        assert fetched is not None
        assert fetched.settled_at is not None
        assert fetched.settlement_outcome == "YES"


class TestIntentRecordTable:
    """The new `intents` table (`app.models.intent.IntentRecord`)."""

    async def test_insert_intent_record(self, test_session: AsyncSession) -> None:
        """One `IntentRecord` round-trips with its JSON `legs`/`score`."""
        record = IntentRecord(
            id="intent-001",
            kind="complement",
            strategy="binary_complement_arbitrage",
            mode="paper",
            status="pending",
            legs=[
                {"venue": "kalshi", "market_id": "M1", "outcome": "YES"},
                {"venue": "kalshi", "market_id": "M1", "outcome": "NO"},
            ],
            score={"net_edge": 0.02, "composite": 0.7},
        )
        test_session.add(record)
        await test_session.commit()

        fetched = await test_session.get(IntentRecord, "intent-001")
        assert fetched is not None
        assert fetched.kind == "complement"
        assert fetched.mode == "paper"
        assert fetched.status == "pending"
        assert len(fetched.legs) == 2
        assert fetched.score["net_edge"] == 0.02
        assert fetched.created_at is not None

    async def test_invalid_kind_rejected_by_check_constraint(
        self, test_session: AsyncSession
    ) -> None:
        """`kind` outside the four `IntentKind` values is rejected."""
        test_session.add(
            IntentRecord(
                id="intent-bad-kind",
                kind="triangle",
                strategy="some_strategy",
                mode="paper",
                status="pending",
            )
        )
        with pytest.raises(IntegrityError):
            await test_session.commit()
        await test_session.rollback()

    async def test_invalid_status_rejected_by_check_constraint(
        self, test_session: AsyncSession
    ) -> None:
        """`status` outside the four `IntentRecordStatus` values is
        rejected.
        """
        test_session.add(
            IntentRecord(
                id="intent-bad-status",
                kind="single",
                strategy="some_strategy",
                mode="paper",
                status="in_flight",
            )
        )
        with pytest.raises(IntegrityError):
            await test_session.commit()
        await test_session.rollback()
