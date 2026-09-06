"""Regressions for the three money-fence exploits found in T21 (T21e).

Every test here routes the REAL `OrderRouter` over a `PaperVenueAdapter`
wrapping a `FixtureAdapter`: no network, no live mode, no real order
(GUARDRAILS.md §1.1/§1.2/§1.4). Every `Settings` is constructed
explicitly and passed in, never read from the environment
(GUARDRAILS.md §1.2).

THE THREE EXPLOITS, EACH REPRODUCED HERE AS IT WAS REPRODUCED AGAINST
THE BROKEN CODE:

1. A single UNTAGGED $0.50 buy on `(venue, market, outcome)` made every
   later `near_resolution` buy on that identity invisible to the bucket
   cap, forever: `Position.intent_id` names only the intent that OPENED
   the row, and the cap was attributed through it. Six $200 buys under
   a $500 cap all executed while the fence read the bucket as $0.00.
2. Two concurrent `submit()` calls (two overlapping `POST
   /trading/orders` against the process-wide router in
   `app/api/deps.py`) both passed a cap their sum breached, AND lost one
   whole 900-contract fill to a lost update between two
   `_upsert_position` select-then-writes — the venue filled 1802
   contracts and the ledger recorded 902.
3. Any bucket spelling but the exact one (`"Near_Resolution"`,
   `" near_resolution"`, `""`, ...) silently skipped the cap entirely.

THE CONCURRENCY TESTS ARE DETERMINISTIC, NOT HOPEFUL. A money fence
tested by a flaky race is worse than one not tested at all, so neither
test relies on the scheduler happening to interleave two coroutines.
`_RendezvousAdapter` blocks the first N calls to a chosen adapter method
until all N have arrived, which pins both submits to the same point in
the flow before either is allowed past it. Both rendezvous points sit
OUTSIDE the router's submit gate (`get_market` is `_plan`'s only venue
call; `get_fills` is hoisted above the gate by `_settle`), so arming one
cannot deadlock against the very lock under test. The assertions are
then invariants that must hold under EVERY interleaving, not one
schedule's expected transcript.
"""
import asyncio
import logging
from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings
from app.execution.fences import (
    KNOWN_BUCKETS,
    RiskLimitExceeded,
    check_order_limits,
)
from app.execution.ledger import CapitalLedger
from app.execution.router import OrderRouter
from app.models.intent import IntentRecord
from app.models.position import Position as PositionRow
from app.models.trade import Trade as TradeRow
from app.strategies.base import Intent, Leg
from app.venues.paper import PaperVenueAdapter
from app.venues.types import Fill
from tests.helpers import make_book
from tests.venues.fixture_adapter import FixtureAdapter, make_venue_market

BUCKET_MARKET = "PM-BUCKET"
NEAR_RESOLUTION = "near_resolution"

#: Long enough that a correct rendezvous never reaches it, short enough
#: that a MISCOUNTED one fails the test instead of hanging the suite.
_RENDEZVOUS_TIMEOUT_S = 10.0


def build_settings(**overrides: object) -> Settings:
    """Build an explicit paper-mode `Settings` for a bucket-cap test.

    Balances are deliberately large: these tests are about the RISK
    fences, and a capital rejection would mask the limit they mean to
    exercise.
    """
    fields: dict[str, object] = {
        "trading_mode": "paper",
        "paper_starting_balances": {"polymarket": 10_000.0, "kalshi": 1_000.0},
        "max_order_notional_usd": 1_000.0,
        "max_open_notional_usd": 10_000.0,
        "max_daily_loss_usd": 10_000.0,
        "max_near_resolution_notional_usd": 500.0,
    }
    fields.update(overrides)
    return Settings(**fields)  # type: ignore[arg-type]


class _RendezvousAdapter(PaperVenueAdapter):
    """A `PaperVenueAdapter` that pins N concurrent submits together.

    Arming `get_market` holds every arriving submit at the end of
    `OrderRouter._plan`, i.e. immediately BEFORE the risk check —
    exactly where two submits read the same pre-trade aggregate.
    Arming `get_fills` holds them at the start of `_settle`, i.e.
    immediately before the `Position` read-modify-write. Both points are
    outside the router's submit gate, so neither can deadlock against
    it.

    Calls beyond the armed count pass straight through (the paper
    adapter calls `get_market` again from inside `place_order`, and
    those must not be captured).
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        """Build the adapter with no rendezvous armed."""
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._pending: dict[str, int] = {}
        self._barriers: dict[str, asyncio.Barrier] = {}

    def arm(self, method: str, parties: int) -> None:
        """Make the next `parties` calls to `method` wait for each other.

        Args:
            method: `"get_market"` or `"get_fills"`.
            parties: How many calls must arrive before any proceeds.
        """
        self._pending[method] = parties
        self._barriers[method] = asyncio.Barrier(parties)

    async def _rendezvous(self, method: str) -> None:
        """Block until every armed caller of `method` has arrived."""
        remaining = self._pending.get(method, 0)
        if remaining <= 0:
            return
        self._pending[method] = remaining - 1
        async with asyncio.timeout(_RENDEZVOUS_TIMEOUT_S):
            await self._barriers[method].wait()

    async def get_market(self, market_id: str):  # type: ignore[no-untyped-def]
        """Rendezvous (if armed), then read the market."""
        await self._rendezvous("get_market")
        return await super().get_market(market_id)

    async def get_fills(self, since: datetime) -> list[Fill]:
        """Rendezvous (if armed), then read the fills."""
        await self._rendezvous("get_fills")
        return await super().get_fills(since)


def bucket_adapter(settings_obj: Settings) -> tuple[_RendezvousAdapter, CapitalLedger]:
    """Build the paper adapter and ledger over a locked 0.50/0.50 book.

    Depth is 100,000 contracts a side so no test below is ever bounded
    by liquidity — the only thing allowed to stop an order here is a
    fence.
    """
    ledger = CapitalLedger.paper(settings_obj)
    inner = FixtureAdapter("polymarket")
    inner.add_market(make_venue_market("polymarket", BUCKET_MARKET))
    inner.set_book(
        make_book(
            bids=[(0.50, 100_000.0)],
            asks=[(0.50, 100_000.0)],
            venue="polymarket",
            market_id=BUCKET_MARKET,
            outcome="YES",
        )
    )
    return _RendezvousAdapter(inner, None, ledger), ledger


def bucket_intent(
    *, size: float, bucket: str | None, price: float = 0.50
) -> Intent:
    """Build a one-leg BUY on the shared identity, tagged or not.

    Args:
        size: Contracts.
        bucket: `metadata["bucket"]` tag, or `None` to send an intent
            carrying NO tag at all — the shape `/trading`'s
            `strategy="api"` path produces.
        price: Limit price (a probability).
    """
    metadata: dict[str, object] = {} if bucket is None else {"bucket": bucket}
    return Intent(
        kind="single",
        legs=[
            Leg(
                market_id=BUCKET_MARKET,
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
        metadata=metadata,
    )


@pytest_asyncio.fixture
async def sessions(test_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Return a session factory over the in-memory test database."""
    return async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


async def _ledger_totals(
    sessions: async_sessionmaker[AsyncSession],
) -> tuple[float, float]:
    """Return `(contracts held in positions, contracts actually traded)`.

    These two must agree. `Trade` rows are the venue's own record of what
    was filled; `Position.size` is the ledger every downstream fence
    reads. The exploit made them disagree by a factor of two.
    """
    async with sessions() as session:
        held = (
            await session.execute(
                select(func.coalesce(func.sum(PositionRow.size), 0.0)).where(
                    PositionRow.mode == "paper"
                )
            )
        ).scalar_one()
        traded = (
            await session.execute(
                select(func.coalesce(func.sum(TradeRow.size), 0.0)).where(
                    TradeRow.mode == "paper"
                )
            )
        ).scalar_one()
    return float(held), float(traded)


# ---------------------------------------------------------------------------
# EXPLOIT 1 -- an untagged buy must not blind the bucket cap
# ---------------------------------------------------------------------------


async def test_untagged_buy_first_cannot_blind_the_near_resolution_cap(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """One untagged $0.50 order used to uncap the bucket permanently.

    The sequence, against `max_near_resolution_notional_usd=500`:

        untagged  1 contract  @ 0.50 ->   $0.50, bucket contribution 0
        tagged  400 contracts @ 0.50 -> $200.00, bucket ->   $200.00
        tagged  400 contracts @ 0.50 -> $200.00, bucket ->   $400.00
        tagged  400 contracts @ 0.50 -> $200.00, 400 + 200 = $600 > $500

    So exactly two tagged intents fit and the third must be rejected.
    Before the fix all three (and three more after them) executed, while
    `_bucket_open_notional` reported $0.00 throughout: the position row
    was opened by the UNTAGGED intent, `Position.intent_id` still named
    it after every fold, and the bucket was attributed through that
    column.

    The position's OPENER is asserted too. Rewriting `intent_id` on each
    fold would also have "fixed" the cap, by destroying the one thing
    that column promises (`app/models/position.py`: the intent that
    OPENED this position); this asserts the fix did not take that route.
    """
    settings_obj = build_settings()
    adapter, ledger = bucket_adapter(settings_obj)
    router = OrderRouter({"polymarket": adapter}, ledger, sessions, settings_obj)

    seed = await router.submit(
        bucket_intent(size=1.0, bucket=None), "some-other-strategy"
    )
    assert seed.status == "executed"
    assert await router._bucket_open_notional(NEAR_RESOLUTION) == pytest.approx(0.0)

    first = await router.submit(bucket_intent(size=400.0, bucket=NEAR_RESOLUTION), "near")
    assert first.status == "executed"
    assert await router._bucket_open_notional(NEAR_RESOLUTION) == pytest.approx(200.0)

    second = await router.submit(bucket_intent(size=400.0, bucket=NEAR_RESOLUTION), "near")
    assert second.status == "executed"
    assert await router._bucket_open_notional(NEAR_RESOLUTION) == pytest.approx(400.0)

    third = await router.submit(bucket_intent(size=400.0, bucket=NEAR_RESOLUTION), "near")
    assert third.status == "rejected"
    assert third.reason == "risk_limit"

    async with sessions() as session:
        rows = list((await session.execute(select(PositionRow))).scalars().all())
    assert len(rows) == 1, "all four intents share one (venue, market, outcome)"
    position = rows[0]
    # 1 + 400 + 400 contracts held; the third intent never placed.
    assert position.size == pytest.approx(801.0)
    # $0.50 untagged + $200 + $200 tagged = $400.50 of entry basis, of
    # which exactly $400.00 belongs to the bucket.
    assert position.size * position.avg_entry_price == pytest.approx(400.50)
    assert position.extra_data["bucket_notional"] == {NEAR_RESOLUTION: pytest.approx(400.0)}
    assert position.intent_id == seed.intent_id, (
        "intent_id must still name the intent that OPENED the row"
    )


async def test_bucket_exposure_is_released_pro_rata_when_the_position_is_sold(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Selling half a mixed position must free half its bucket exposure.

    Buy 100 untagged @ 0.50 ($50) then 300 tagged @ 0.50 ($150), so the
    row holds 400 contracts at an average of 0.50 and the bucket holds
    $150. An unwind of 200 contracts closes half the position, so the
    bucket must fall to $75.00 -- pro rata, because contracts are
    fungible and no lot can be said to have been sold. Anything else
    (releasing all of it, or none of it) would leave the cap disagreeing
    with `size * avg_entry_price`, which is the account-wide number the
    same fence reads.
    """
    settings_obj = build_settings()
    adapter, ledger = bucket_adapter(settings_obj)
    router = OrderRouter({"polymarket": adapter}, ledger, sessions, settings_obj)

    await router.submit(bucket_intent(size=100.0, bucket=None), "other")
    await router.submit(bucket_intent(size=300.0, bucket=NEAR_RESOLUTION), "near")
    assert await router._bucket_open_notional(NEAR_RESOLUTION) == pytest.approx(150.0)

    sell = Intent(
        kind="single",
        legs=[
            Leg(
                market_id=BUCKET_MARKET,
                outcome="YES",
                side="SELL",
                limit_price=0.50,
                size_contracts=200.0,
                venue="polymarket",
            )
        ],
        hold_to_resolution=False,
        atomicity="best_effort",
        confidence=0.9,
        metadata={},
    )
    sold = await router.submit(sell, "exit")
    assert sold.status == "executed"

    async with sessions() as session:
        position = await session.scalar(select(PositionRow))
    assert position is not None
    assert position.size == pytest.approx(200.0)
    # 400 -> 200 contracts is half the position, so $150.00 -> $75.00.
    assert position.extra_data["bucket_notional"][NEAR_RESOLUTION] == pytest.approx(75.0)
    assert await router._bucket_open_notional(NEAR_RESOLUTION) == pytest.approx(75.0)


# ---------------------------------------------------------------------------
# EXPLOIT 2 -- concurrent submits: the cap, and the position ledger
# ---------------------------------------------------------------------------


async def test_concurrent_submits_cannot_both_pass_one_bucket_cap(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Two overlapping submits must not both spend the same headroom.

    A warmup of 2 contracts @ 0.50 puts $1.00 in the bucket. Two
    concurrent intents of 900 contracts @ 0.50 = $450.00 each then race,
    against `max_near_resolution_notional_usd=500`:

        the first to be measured:   $1.00 + $450.00 = $451.00  <= $500 -> passes
        the second, measured after: $451.00 + $450.00 = $901.00 > $500 -> rejected

    Both submits are pinned together at the end of `_plan` (the last
    step before the risk check) so this is not left to the scheduler.
    Before the fix both read `bucket = $1.00`, both passed, and $901.00
    of near-resolution exposure stood against a $500.00 cap.

    WHICH one wins is not asserted -- it is not defined and does not
    matter. What is asserted is the invariant: exactly one executes, the
    other is rejected on the bucket limit, and the bucket never exceeds
    its cap.
    """
    settings_obj = build_settings()
    adapter, ledger = bucket_adapter(settings_obj)
    router = OrderRouter({"polymarket": adapter}, ledger, sessions, settings_obj)

    warmup = await router.submit(bucket_intent(size=2.0, bucket=NEAR_RESOLUTION), "near")
    assert warmup.status == "executed"
    assert await router._bucket_open_notional(NEAR_RESOLUTION) == pytest.approx(1.0)

    adapter.arm("get_market", 2)
    first, second = await asyncio.gather(
        router.submit(bucket_intent(size=900.0, bucket=NEAR_RESOLUTION), "near"),
        router.submit(bucket_intent(size=900.0, bucket=NEAR_RESOLUTION), "near"),
    )

    assert sorted([first.status, second.status]) == ["executed", "rejected"]
    loser = first if first.status == "rejected" else second
    assert loser.reason == "risk_limit"
    async with sessions() as session:
        record = await session.get(IntentRecord, loser.intent_id)
    assert record is not None
    assert NEAR_RESOLUTION in record.extra_data["rejected_detail"]

    bucket = await router._bucket_open_notional(NEAR_RESOLUTION)
    # $1.00 warmup + $450.00 from the one winner.
    assert bucket == pytest.approx(451.0)
    assert bucket <= settings_obj.max_near_resolution_notional_usd

    held, traded = await _ledger_totals(sessions)
    # 2 + 900 contracts, and every one of them recorded in both places.
    assert traded == pytest.approx(902.0)
    assert held == pytest.approx(traded)


async def test_concurrent_fills_are_never_lost_from_the_position_ledger(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Two concurrent fills on one identity must both reach the ledger.

    The bucket cap is opened wide here ON PURPOSE, so both intents
    execute and the test is about the LEDGER, not the fence: 2 + 900 +
    900 = 1802 contracts are filled by the venue and 1802 must be
    recorded. Both submits are pinned together at `_settle`'s fill read
    -- immediately before `_upsert_position`'s select-then-write -- so
    the race is forced, not hoped for.

    Before the fix this recorded 902: both settles read `size = 2.0`,
    both wrote `2 + 900`, and the second write erased the first. In live
    mode that is 900 contracts of real, unhedged exposure the system
    does not know it holds, and every fence downstream (`_risk_context`,
    `_bucket_open_notional`, the reconciliation baseline) then reads a
    ledger understating the account by half.
    """
    settings_obj = build_settings(max_near_resolution_notional_usd=1_000_000.0)
    adapter, ledger = bucket_adapter(settings_obj)
    router = OrderRouter({"polymarket": adapter}, ledger, sessions, settings_obj)

    warmup = await router.submit(bucket_intent(size=2.0, bucket=NEAR_RESOLUTION), "near")
    assert warmup.status == "executed"

    adapter.arm("get_fills", 2)
    first, second = await asyncio.gather(
        router.submit(bucket_intent(size=900.0, bucket=NEAR_RESOLUTION), "near"),
        router.submit(bucket_intent(size=900.0, bucket=NEAR_RESOLUTION), "near"),
    )
    assert [first.status, second.status] == ["executed", "executed"]

    async with sessions() as session:
        rows = list((await session.execute(select(PositionRow))).scalars().all())
    assert len(rows) == 1, "one (venue, market, outcome) is one position row"
    # 2 + 900 + 900 = 1802 contracts.
    assert rows[0].size == pytest.approx(1802.0)

    held, traded = await _ledger_totals(sessions)
    assert traded == pytest.approx(1802.0)
    assert held == pytest.approx(traded), "the ledger must not lose a fill"

    # 1802 contracts at 0.50, every one of them bought under the tag.
    assert await router._bucket_open_notional(NEAR_RESOLUTION) == pytest.approx(901.0)


# ---------------------------------------------------------------------------
# EXPLOIT 3 -- an unrecognized bucket tag must be loud, not fatal
# ---------------------------------------------------------------------------

_MISSPELLINGS = [
    "Near_Resolution",
    "NEAR_RESOLUTION",
    " near_resolution",
    "near_resolution ",
    "",
    "near-resolution",
]


@pytest.mark.parametrize("tag", _MISSPELLINGS)
def test_misspelled_bucket_tag_warns_and_still_does_not_crash(
    tag: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Every near-miss spelling must log a WARNING naming the tag.

    The cap itself is deliberately NOT applied to an unrecognized tag
    and this function deliberately does NOT raise over one: a typo in a
    strategy's metadata must never crash the order-placement path
    (`app.execution.fences.check_order_limits`'s own documented
    reasoning, which T21e did not change). What the exploit showed is
    that it was also SILENT -- $10,000 through a $500 cap with nothing
    logged -- so the fix is the warning, and this asserts both halves:
    the tag is named at WARNING level, and nothing is raised.
    """
    # Only the BUCKET cap is in play: the per-order and account caps are
    # lifted above $10,000 so the outcome cannot be attributed to them.
    settings_obj = build_settings(
        max_order_notional_usd=100_000.0, max_open_notional_usd=100_000.0
    )
    with caplog.at_level(logging.WARNING, logger="app.execution.fences"):
        check_order_limits(
            10_000.0, 0.0, 0.0, settings_obj, bucket=tag, bucket_notional=0.0
        )

    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING
        and getattr(record, "event", None) == "unknown_bucket_tag"
    ]
    assert len(warnings) == 1, "exactly one warning per unrecognized tag"
    assert warnings[0].bucket == tag  # type: ignore[attr-defined]
    assert repr(tag) in warnings[0].getMessage()
    assert NEAR_RESOLUTION in warnings[0].getMessage()


def test_the_exact_tag_is_capped_and_silent(caplog: pytest.LogCaptureFixture) -> None:
    """The one recognized spelling caps, and says nothing about itself.

    $10,000 against `max_near_resolution_notional_usd=500` must raise,
    and a correctly spelled tag must not produce warning noise -- a
    warning that fires on the healthy path is one operators learn to
    ignore.
    """
    settings_obj = build_settings(
        max_order_notional_usd=100_000.0, max_open_notional_usd=100_000.0
    )
    assert set(KNOWN_BUCKETS) == {NEAR_RESOLUTION}
    with (
        caplog.at_level(logging.WARNING, logger="app.execution.fences"),
        pytest.raises(RiskLimitExceeded, match="max_near_resolution_notional_usd"),
    ):
        check_order_limits(
            10_000.0,
            0.0,
            0.0,
            settings_obj,
            bucket=NEAR_RESOLUTION,
            bucket_notional=0.0,
        )
    assert [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "unknown_bucket_tag"
    ] == []


async def test_router_warns_once_when_an_intent_carries_a_misspelled_bucket(
    sessions: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A typo'd tag reaching the router is warned about, and still routes.

    This is the end-to-end half of the exploit: a strategy that tags
    `"Near_Resolution"` gets NO bucket cap (the tag is matched exactly,
    on purpose -- see `KNOWN_BUCKETS`), so its $450.00 order executes
    where a correctly tagged one of the same size would have been capped
    at $500.00 after the first. The difference is that it is no longer
    invisible: the operator gets a WARNING naming the tag, exactly once,
    at the submission that carried it.
    """
    settings_obj = build_settings()
    adapter, ledger = bucket_adapter(settings_obj)
    router = OrderRouter({"polymarket": adapter}, ledger, sessions, settings_obj)

    with caplog.at_level(logging.WARNING, logger="app.execution.fences"):
        routed = await router.submit(
            bucket_intent(size=900.0, bucket="Near_Resolution"), "typo-strategy"
        )

    assert routed.status == "executed"
    warnings = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "unknown_bucket_tag"
    ]
    assert len(warnings) == 1
    assert warnings[0].bucket == "Near_Resolution"  # type: ignore[attr-defined]
    # And the real bucket is untouched by it: nothing was added there.
    assert await router._bucket_open_notional(NEAR_RESOLUTION) == pytest.approx(0.0)
