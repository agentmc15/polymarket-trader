"""Reconcile locally-persisted orders against what the venue actually has.

WHY THIS EXISTS. `OrderRouter` commits a `PENDING` `Order` row BEFORE it
calls the venue (PLAN.md D4, crash-safety): if the process dies between
the commit and the acknowledgement, the row survives and the truth is
recoverable. That guarantee is only worth anything if something later
goes and LOOKS — otherwise the durable record of a possibly-live order
just sits there forever, and the local view of exposure silently
diverges from the venue's.

`reconcile()` is that pass. For one venue, in one mode, it compares every
local `PENDING`/`OPEN`/`PARTIALLY_FILLED` order against
`adapter.get_open_orders()` and `adapter.get_fills(since)` and resolves
each one:

- present in the venue's open orders  -> `OPEN` (sizes refreshed)
- fills found for it                  -> `FILLED`/`PARTIALLY_FILLED`,
                                         with a `Trade` row per fill
- was `OPEN`, now absent, no fills    -> `CANCELLED` (the venue took it)
- `PENDING`, absent, older than
  `Settings.reconcile_grace_s`        -> `FAILED`, reason `no_ack`
- `PENDING`, absent, still inside
  the grace window                    -> left alone; the request may
                                         still be in flight

A VENUE THAT CANNOT BE READ IS NOT A VENUE WITH NOTHING ON IT. If either
`get_open_orders()` or `get_fills()` raises (a 429, a timeout, an outage),
this pass changes NOTHING and reports every row as unresolved. Treating an
unreadable venue as an empty one would mark live, working orders `FAILED`
and `CANCELLED` en masse the moment the venue had a bad minute — turning a
transient outage into a corrupted local view of real exposure.

`mode` IS PART OF EVERY QUERY. Paper and live orders share these tables
by D4's design, so a reconciliation that forgot the filter would compare
simulated orders against a live venue (and mark them all lost) or the
reverse. Nothing here is mode-agnostic.

WHAT THIS DELIBERATELY DOES NOT DO: it does not REBUILD `positions`. A
`Trade` row is a fact about one fill and can be written idempotently
from the fill itself; a position is a running cost basis, and rebuilding
one correctly needs the venue's own `get_positions()` as the authority
plus a decision about what to do when it disagrees. Writing a
half-informed position row here would corrupt exactly the P&L this pass
is supposed to protect. The order/trade repair is what makes that
follow-up possible.

THE ONE EXCEPTION, AND WHY IT IS NOT THAT (T25): a discovered BUY fill
IS folded into the position, by `_fold_buys_into_position`. Without it,
marking an order `FILLED` DELETED exposure from both risk fences. Both
`OrderRouter._risk_context` (`max_open_notional_usd`) and
`OrderRouter._bucket_open_notional`
(`max_near_resolution_notional_usd`) are the sum of exactly two things:
resting orders' `remaining_size * price`, and open positions'
`size * avg_entry_price`. `_apply_fills` sets `remaining_size = 0`, so
that notional left the first sum — and, with no position written, it
entered nothing. The contracts were bought and are held, and every cap
in the system read the account as having room it did not have. Reachable
in paper mode by any `best_effort` (GTC) intent that fills after
`submit()` returns, and on every crash-recovery path.

That fold is NOT "rebuilding a position", and the reasoning above stands
untouched, for three reasons:

  - It is INCREMENTAL, not authoritative. It adds exactly the fills this
    pass just discovered — the ones `_record_fills` found had no local
    `Trade` row, i.e. the ones the router demonstrably never booked —
    using the venue's own `Fill.price`/`size`/`fee`. It never reads a
    venue position, never reconciles against one, and never overwrites
    a local number with a remote one. It is the same arithmetic
    `OrderRouter._upsert_position` performs for a fill the router saw
    itself; the only difference is who noticed the fill.
  - It carries NO P&L DECISION. `size`, `avg_entry_price` and
    `total_cost` on a BUY are fully determined by the fills. Nothing
    here computes or writes `realized_pnl`, `extra_data
    ["realized_by_day"]`, or any basis choice — so the daily-loss fence's
    inputs are untouched by this pass, exactly as before.
  - SELLS ARE STILL LEFT ALONE, deliberately, and that is the SAFE
    direction. A discovered SELL is where a basis/lot decision (and
    therefore the P&L this pass must not corrupt) actually lives, so it
    is left to the `get_positions()`-authority follow-up. Its cost is
    that a position sold-out-from-under-us stays open in the local view,
    which makes both fences read exposure that is GONE — stricter caps,
    never looser. Under-counting was the bug; over-counting is the
    conservative failure this pass has always had.

The per-bucket ledger is credited through the ROUTER'S OWN writers
(`_credit_bucket`, `_mark_position`), imported rather than reimplemented:
`Position.extra_data["bucket_notional"]` has an invariant relative to
`size * avg_entry_price` that only holds if every writer agrees on it,
and a second implementation of it here would be precisely the divergence
this fix exists to remove.

GUARDRAILS.md §1.1: nothing here places or cancels a venue order. It
reads, and it writes to the local database.
"""
import logging
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.config import settings as _default_settings

# The router's OWN position writers, reused rather than reimplemented —
# see this module's docstring ("The per-bucket ledger is credited through
# the ROUTER'S OWN writers"). `app.execution.router` does not import this
# module, so this direction adds no cycle.
from app.execution.router import _credit_bucket, _mark_position
from app.models.intent import IntentRecord
from app.models.position import Position as PositionRow
from app.models.trade import Order as OrderRow
from app.models.trade import OrderSide, OrderStatus
from app.models.trade import Trade as TradeRow
from app.utils.time import ensure_aware, utcnow
from app.venues.base import ReconcileAdapter
from app.venues.types import Fill, OrderAck, VenueId

logger = logging.getLogger(__name__)

#: Local statuses a reconciliation pass considers unresolved and
#: therefore worth checking. A `FILLED`/`CANCELLED`/`FAILED`/`EXPIRED`
#: order is terminal — re-examining it every minute forever would turn a
#: repair pass into a full-table scan of history.
_UNRESOLVED = (
    OrderStatus.PENDING,
    OrderStatus.OPEN,
    OrderStatus.PARTIALLY_FILLED,
)

#: Reason recorded on an order that never got an acknowledgement.
#: `Order.error_message` is free text, but this exact token is what a
#: monitor greps for, so it is a constant rather than an inline string.
NO_ACK_REASON = "no_ack"

#: How far back `get_fills()` is asked to look, beyond the oldest
#: unresolved order's own age. A venue's fill timestamps and the local
#: clock need not agree to the second, and matching is exact (by
#: `client_order_id`, else by venue `order_id`), so a wider window can
#: only avoid missing a fill — it cannot pull in someone else's.
_FILL_LOOKBACK_PAD = timedelta(minutes=5)

#: Absolute tolerance (contracts) for "is this order complete?", the same
#: magnitude the fill engine and `OrderBook.walk()` use.
_SIZE_EPSILON = 1e-9


@dataclass(frozen=True)
class ReconcileReport:
    """What one `reconcile()` pass found and changed.

    Attributes:
        venue: The venue reconciled.
        mode: `"paper"` or `"live"` — which rows were considered.
        checked: Local unresolved orders examined.
        marked_open: Orders confirmed still resting on the venue.
        marked_filled: Orders moved to `FILLED`/`PARTIALLY_FILLED`
            because fills were found.
        marked_cancelled: Orders the venue no longer has and never
            filled.
        marked_failed: `PENDING` orders past the grace window with no
            venue record at all — reason `no_ack`.
        unresolved: `PENDING` orders still inside the grace window, left
            as they were. A non-zero value is normal; a value that stays
            non-zero across passes is not.
        trades_recorded: `Trade` rows written for fills that had no local
            record yet.
        positions_credited: Orders whose newly-discovered BUY fills were
            folded into a `Position` (T25). Always `<=` the number of
            orders counted in `marked_filled`: a SELL, or a fill the
            router already booked, contributes nothing here.
    """

    venue: VenueId
    mode: str
    checked: int = 0
    marked_open: int = 0
    marked_filled: int = 0
    marked_cancelled: int = 0
    marked_failed: int = 0
    unresolved: int = 0
    trades_recorded: int = 0
    positions_credited: int = 0

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-serializable form, for the Celery task's result."""
        return {
            "venue": self.venue,
            "mode": self.mode,
            "checked": self.checked,
            "marked_open": self.marked_open,
            "marked_filled": self.marked_filled,
            "marked_cancelled": self.marked_cancelled,
            "marked_failed": self.marked_failed,
            "unresolved": self.unresolved,
            "trades_recorded": self.trades_recorded,
            "positions_credited": self.positions_credited,
        }


async def reconcile(
    venue: VenueId,
    adapter: ReconcileAdapter,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    mode: str | None = None,
    now: datetime | None = None,
    settings_obj: Settings | None = None,
) -> ReconcileReport:
    """Bring one venue's local order rows back in line with the venue.

    Args:
        venue: The venue to reconcile.
        adapter: That venue's adapter, already in `mode`. Typed as
            `ReconcileAdapter` — the READ-ONLY subset — rather than the
            full `VenueAdapter`, so this pass structurally cannot place
            or cancel anything and, in live mode, never has to be handed
            an object built behind the ORDER-PLACEMENT fence. See
            `app.venues.registry.get_read_adapter`.
        session_factory: Async session factory; one session is opened and
            committed.
        mode: `"paper"` or `"live"`. Defaults to
            `settings_obj.trading_mode`. Every query filters on it —
            paper and live rows share these tables (PLAN.md D4).
        now: Aware UTC "current" time, for deterministic tests. Defaults
            to `utcnow()`.
        settings_obj: `Settings` supplying `reconcile_grace_s`. Defaults
            to the process-wide singleton.

    Returns:
        ReconcileReport: Counts of what was examined and changed.

    Raises:
        ValueError: If `now` is naive.
        TypeError: If `now` is not a `datetime`.
    """
    cfg = settings_obj if settings_obj is not None else _default_settings
    resolved_mode = mode if mode is not None else cfg.trading_mode
    moment = now if now is not None else utcnow()
    ensure_aware(moment)
    grace = timedelta(seconds=cfg.reconcile_grace_s)

    checked = opened = filled = cancelled = failed = unresolved = trades = 0
    credited = 0

    async with session_factory() as session:
        rows = list(
            (
                await session.execute(
                    select(OrderRow).where(
                        OrderRow.venue == venue,
                        OrderRow.mode == resolved_mode,
                        OrderRow.status.in_(_UNRESOLVED),
                    )
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return ReconcileReport(venue=venue, mode=resolved_mode)

        open_acks, open_ok = await _venue_open_orders(adapter, venue)
        since = _fills_since(rows, moment)
        by_key, fills_ok = await _venue_fills(adapter, venue, since)
        if not (open_ok and fills_ok):
            # See the module docstring: an unreadable venue tells us
            # nothing about our orders, so nothing is concluded.
            report = ReconcileReport(
                venue=venue,
                mode=resolved_mode,
                checked=len(rows),
                unresolved=len(rows),
            )
            logger.warning(
                "order",
                extra={
                    "event": "reconcile_skipped_unreadable_venue",
                    **report.as_dict(),
                },
            )
            return report

        for row in rows:
            checked += 1
            ack = open_acks.get(row.client_order_id)
            if ack is None and row.order_id is not None:
                ack = open_acks.get(row.order_id)
            fills = by_key.get(row.client_order_id) or (
                by_key.get(row.order_id) if row.order_id else None
            )

            if fills:
                new_fills = await _record_fills(session, row, fills, resolved_mode)
                trades += len(new_fills)
                # Fold BEFORE `_apply_fills` zeroes `remaining_size`, so
                # the exposure never leaves both fence aggregates at once
                # even if the commit below fails partway (T25 — see the
                # module docstring's "THE ONE EXCEPTION").
                if new_fills and row.side is OrderSide.BUY:
                    if await _fold_buys_into_position(
                        session, row, new_fills, resolved_mode, moment
                    ):
                        credited += 1
                _apply_fills(row, fills, moment)
                filled += 1
                _log(row, "reconciled_filled", resolved_mode)
                continue

            if ack is not None:
                row.order_id = ack.order_id
                row.filled_size = ack.filled_size
                row.remaining_size = ack.remaining_size
                row.status = OrderStatus.OPEN
                opened += 1
                _log(row, "reconciled_open", resolved_mode)
                continue

            if row.status in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED):
                # The venue had it, the venue no longer has it, and no
                # fill explains where it went: the venue cancelled or
                # expired it. Recording that is what keeps
                # `open_notional` from counting exposure that is gone.
                row.status = OrderStatus.CANCELLED
                row.remaining_size = 0.0
                cancelled += 1
                _log(row, "reconciled_cancelled", resolved_mode)
                continue

            age = moment - _aware(row.created_at)
            if age > grace:
                # A `PENDING` row is written BEFORE the venue call, so
                # "no venue record" is the EXPECTED state while the
                # request is in flight. Past the grace window it is no
                # longer plausible in-flight: the request never reached
                # the venue, or its response never reached us. Either
                # way the local row must stop claiming to be live.
                row.status = OrderStatus.FAILED
                row.error_message = NO_ACK_REASON
                row.remaining_size = 0.0
                failed += 1
                _log(row, "reconciled_no_ack", resolved_mode)
                continue

            unresolved += 1

        await session.commit()

    report = ReconcileReport(
        venue=venue,
        mode=resolved_mode,
        checked=checked,
        marked_open=opened,
        marked_filled=filled,
        marked_cancelled=cancelled,
        marked_failed=failed,
        unresolved=unresolved,
        trades_recorded=trades,
        positions_credited=credited,
    )
    logger.info("order", extra={"event": "reconciled", **report.as_dict()})
    return report


async def _venue_open_orders(
    adapter: ReconcileAdapter, venue: VenueId
) -> tuple[dict[str, OrderAck], bool]:
    """Return the venue's resting orders, keyed by BOTH ids.

    Keyed by `client_order_id` and by `order_id`, because a local row
    that never got an acknowledgement has only the former, while one
    recovered from a partial write may have only the latter.

    Args:
        adapter: The venue adapter.
        venue: The venue, for the log line.

    Returns:
        tuple[dict[str, OrderAck], bool]: Acks by id, and whether the
            read SUCCEEDED. The flag is the load-bearing half: an empty
            dict from a failed read means "we do not know", not "the
            venue has nothing", and `reconcile()` refuses to conclude
            anything from the former.
    """
    try:
        acks = await adapter.get_open_orders()
    except Exception:  # noqa: BLE001 - reported to the caller, not swallowed
        logger.exception(
            "order", extra={"event": "get_open_orders_failed", "venue": venue}
        )
        return {}, False
    index: dict[str, OrderAck] = {}
    for ack in acks:
        index[ack.client_order_id] = ack
        index[ack.order_id] = ack
    return index, True


async def _venue_fills(
    adapter: ReconcileAdapter, venue: VenueId, since: datetime
) -> tuple[dict[str, list[Fill]], bool]:
    """Return the venue's recent fills, grouped by the ids they can match.

    Args:
        adapter: The venue adapter.
        venue: The venue, for the log line.
        since: Aware UTC lower bound.

    Returns:
        tuple[dict[str, list[Fill]], bool]: Fills keyed by
            `metadata["client_order_id"]` where present, and by
            `Fill.order_id` — plus whether the read SUCCEEDED. A fill
            reachable under both keys is stored under both; matching in
            `reconcile()` prefers the client key, which is the only one a
            `PENDING` row has.
    """
    try:
        fills = await adapter.get_fills(since)
    except Exception:  # noqa: BLE001 - reported to the caller, not swallowed
        logger.exception("order", extra={"event": "get_fills_failed", "venue": venue})
        return {}, False
    grouped: dict[str, list[Fill]] = defaultdict(list)
    for fill in fills:
        client_key = fill.metadata.get("client_order_id")
        if isinstance(client_key, str) and client_key:
            grouped[client_key].append(fill)
        if fill.order_id and fill.order_id != client_key:
            grouped[fill.order_id].append(fill)
    return dict(grouped), True


def _fills_since(rows: list[OrderRow], now: datetime) -> datetime:
    """Return how far back to ask the venue for fills.

    Args:
        rows: The unresolved local orders.
        now: Aware UTC current time.

    Returns:
        datetime: The oldest unresolved order's creation time, padded.
            Bounded by the orders actually in question rather than a
            fixed window, so a long-resting order is not missed and a
            quiet venue is not asked for a year of history.
    """
    oldest = min((_aware(row.created_at) for row in rows), default=now)
    return oldest - _FILL_LOOKBACK_PAD


async def _record_fills(
    session: AsyncSession, row: OrderRow, fills: list[Fill], mode: str
) -> list[Fill]:
    """Write a `Trade` per fill that has no local record yet.

    The `trade_id` scheme is the router's own
    (`f"{client_order_id}#{ordinal}"`), so a fill the router already
    booked is skipped here and a fill it never got to is repaired —
    running this pass twice changes nothing the second time.

    Args:
        session: Session to write in; the caller commits.
        row: The local order the fills belong to.
        fills: The venue's fills for it.
        mode: `"paper"` or `"live"`, stamped on every row written.

    Returns:
        list[Fill]: The fills that were actually NEW here, in order. A
            `Trade` row and a `Position` contribution are written by the
            router together (`OrderRouter._book_fills`), so "had no local
            `Trade`" is also exactly "was never folded into the position"
            — which is why `_fold_buys_into_position` is given THIS list
            and not `fills`, and why folding cannot double-count.
    """
    written: list[Fill] = []
    for ordinal, fill in enumerate(fills):
        trade_id = f"{row.client_order_id}#{ordinal}"
        exists = await session.scalar(
            select(TradeRow.id).where(TradeRow.trade_id == trade_id)
        )
        if exists is not None:
            continue
        session.add(
            TradeRow(
                trade_id=trade_id,
                order_id=row.id,
                market_id=row.market_id,
                venue=row.venue,
                token_id=row.token_id,
                outcome=row.outcome,
                side=row.side,
                price=fill.price,
                size=fill.size,
                fee=fill.fee,
                liquidity=fill.liquidity,
                mode=mode,
                executed_at=fill.ts,
                extra_data={"reconciled": True},
            )
        )
        written.append(fill)
    if written:
        await session.flush()
    return written


async def _owning_intent_tags(
    session: AsyncSession, intent_id: str | None
) -> tuple[str | None, bool]:
    """Return `(bucket, hold_to_resolution)` off the order's owning intent.

    `OrderRouter._persist_pending` copies `intent.metadata["bucket"]` and
    `hold_to_resolution` onto `IntentRecord.extra_data` at submission
    time. Reading them back here is what keeps a reconciled fill inside
    `max_near_resolution_notional_usd`: a `near_resolution` BUY folded
    into a position with NO bucket credit would be counted by
    `_risk_context` but invisible to `_bucket_open_notional`, i.e. the
    account-wide cap repaired and the bucket cap still blind.

    Args:
        session: Session to read in.
        intent_id: The order's `intent_id`, or `None` for an order that
            predates intent persistence.

    Returns:
        tuple[str | None, bool]: The bucket tag (`None` for an untagged
            or unknown intent — which contributes to no bucket, the same
            conservative reading `OrderRouter._upsert_position` gives an
            untagged buy) and whether the intent meant to hold to
            settlement (`False` when unknown).
    """
    if intent_id is None:
        return None, False
    extra = await session.scalar(
        select(IntentRecord.extra_data).where(IntentRecord.id == intent_id)
    )
    if not isinstance(extra, dict):
        return None, False
    raw_bucket = extra.get("bucket")
    bucket = (
        None
        if raw_bucket is None
        else (raw_bucket if isinstance(raw_bucket, str) else str(raw_bucket))
    )
    return bucket, bool(extra.get("hold_to_resolution", False))


async def _fold_buys_into_position(
    session: AsyncSession,
    row: OrderRow,
    fills: list[Fill],
    mode: str,
    now: datetime,
) -> bool:
    """Fold newly-discovered BUY fills into this order's open `Position`.

    See this module's docstring ("THE ONE EXCEPTION") for why a BUY fold
    is not the position REBUILD this pass still refuses to do, and why
    SELLs are left alone.

    The position identity is PLAN.md D7's, the same one
    `OrderRouter._upsert_position` uses: `(mode, venue, market_id,
    outcome)` with `closed_at IS NULL`. Every field is read off the local
    order row and the venue's own fills; nothing is read from a venue
    position endpoint.

    Args:
        session: Session to write in; the caller commits.
        row: The local order these fills belong to. Must be a BUY (the
            caller checks).
        fills: ONLY the fills `_record_fills` found were new — see its
            `Returns:` for why that is also "never folded before".
        mode: `"paper"` or `"live"`, stamped on a created row and
            filtered on when an existing one is looked up.
        now: Aware UTC booking time; `opened_at` for a created row.

    Returns:
        bool: Whether a position was created or credited. `False` only
            when the fills carry no size at all.
    """
    qty = math.fsum(fill.size for fill in fills)
    if qty <= 0.0:
        return False
    notional = math.fsum(fill.price * fill.size for fill in fills)
    fee = math.fsum(fill.fee for fill in fills)
    price = notional / qty

    bucket, hold_to_resolution = await _owning_intent_tags(session, row.intent_id)
    position = await session.scalar(
        select(PositionRow).where(
            PositionRow.mode == mode,
            PositionRow.venue == row.venue,
            PositionRow.market_id == row.market_id,
            PositionRow.outcome == row.outcome,
            PositionRow.closed_at.is_(None),
        )
    )

    if position is None:
        position = PositionRow(
            market_id=row.market_id,
            venue=row.venue,
            mode=mode,
            intent_id=row.intent_id,
            token_id=row.token_id,
            outcome=row.outcome,
            size=qty,
            avg_entry_price=price,
            total_cost=notional + fee,
            hold_to_resolution=hold_to_resolution,
            opened_at=now,
            extra_data={},
        )
        # Writes `extra_data["bucket_notional"]` even when `bucket` is
        # `None`, so the key's ABSENCE keeps meaning "row predates the
        # per-bucket ledger" — the distinction
        # `OrderRouter._bucket_open_notional` needs to decide between the
        # ledger and its legacy opener-based fallback.
        _credit_bucket(position, bucket, notional)
        _mark_position(position, price)
        session.add(position)
    else:
        total = position.size + qty
        position.avg_entry_price = (
            (position.avg_entry_price * position.size + notional) / total
            if total > 0.0
            else 0.0
        )
        position.size = total
        position.total_cost += notional + fee
        _credit_bucket(position, bucket, notional)
        _mark_position(position, price)
    await session.flush()
    return True


def _apply_fills(row: OrderRow, fills: list[Fill], now: datetime) -> None:
    """Fold discovered fills into the local order row's sizes and status."""
    filled = math.fsum(fill.size for fill in fills)
    row.filled_size = filled
    row.remaining_size = max(0.0, row.size - filled)
    row.filled_at = max((fill.ts for fill in fills), default=now)
    if row.remaining_size <= _SIZE_EPSILON:
        row.remaining_size = 0.0
        row.status = OrderStatus.FILLED
    else:
        row.status = OrderStatus.PARTIALLY_FILLED


def _aware(value: datetime) -> datetime:
    """Return `value` as an aware UTC datetime.

    SQLite round-trips a `DateTime(timezone=True)` column as NAIVE, so a
    row read back in a test carries no tzinfo even though it was written
    aware. Comparing that against `utcnow()` raises `TypeError`
    (PLAN.md R9), which would make the grace-window check blow up rather
    than answer. Postgres returns it aware and this is a no-op there.

    Args:
        value: A timestamp read from the database.

    Returns:
        datetime: The same instant, guaranteed aware UTC.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _log(row: OrderRow, event: str, mode: str) -> None:
    """Emit one structured order line. Never carries a secret."""
    logger.info(
        "order",
        extra={
            "event": event,
            "mode": mode,
            "venue": row.venue,
            "client_order_id": row.client_order_id,
            "order_id": row.order_id,
            "status": row.status.value,
            "filled_size": row.filled_size,
            "remaining_size": row.remaining_size,
        },
    )
