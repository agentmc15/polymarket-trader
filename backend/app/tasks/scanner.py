"""Celery tasks for the periodic opportunity scans (PLAN.md D10, T19/T25).

`scan_opportunities` runs `app.services.scanner.scan()` once, on the beat
schedule in `app/tasks/__init__.py` (every `settings.scan_interval_s`,
120s by default). It is DISCOVERY ONLY: this module's only output is
scored, persisted `pending` `app.models.intent.IntentRecord` rows.

`scan_near_resolution` (T25) is the SECOND beat, running
`app.services.scanner.near_resolution_pass()` every
`settings.near_resolution_scan_interval_s` (300s by default). It is the
production caller PLAN.md D10(a)-(d)'s near-resolution work never had:
without it, `settlement_edge` — the one strategy that stamps
`metadata["bucket"] = "near_resolution"` — ran nowhere outside its own
unit tests, and `scan()` could not stand in for it (see
`app.services.scanner`'s module docstring: wrong strategy category, and
`score()` without `allow_past_close=True` drops every past-close intent).
It is a SEPARATE task on a SEPARATE interval rather than another
strategy name handed to `run_scan`, for the same two reasons the pass
itself is separate: it needs `score(..., allow_past_close=True)`, and
it reads `list_markets(status=None)` rather than the top
`scan_top_n` OPEN markets. Both tasks are equally discovery-only — every
guarantee below applies to both.

GUARDRAILS.md §1.1's structural analogue for routing: NOTHING in this
file calls `app.execution.router.OrderRouter.submit`, and
`app.execution.router` is not even imported here. A 120-second automatic
beat that could also route would turn a scoring bug into an automatic
trading bug at machine speed; routing an intent this task discovers is a
separate, explicit call made by something else, with a human or an
explicit process in between (see `app.services.scanner`'s module
docstring for the full rationale).

ADAPTERS COME FROM `get_read_adapter`, same rationale as
`app.tasks.execution.reconcile_venues`: in `"live"` mode `get_adapter`
constructs an order-PLACING adapter behind `assert_live_allowed()`, and
this task places nothing, ever. Using `get_read_adapter` here means an
engaged kill switch (which only blocks PLACEMENT) has no bearing on
whether discovery keeps running, and discovery genuinely never touches
the placement fence at all.
"""
import logging
from typing import Any

from sqlalchemy import select

from app.config import settings
from app.database import async_session_factory, run_async_task
from app.models.event_link import EventLink
from app.services.scanner import (
    ARBITRAGE_STRATEGIES,
    near_resolution_pass,
    scan,
)
from app.tasks import celery_app
from app.venues.base import MarketDataAdapter
from app.venues.registry import get_read_adapter
from app.venues.types import VenueId

logger = logging.getLogger(__name__)

#: Venues scanned on every pass. Mirrors `app.venues.types.VenueId`; a
#: third venue means adding it here too (same convention as
#: `app.tasks.execution.RECONCILED_VENUES`).
SCANNED_VENUES: tuple[VenueId, ...] = ("polymarket", "kalshi")


def read_adapters() -> dict[VenueId, MarketDataAdapter]:
    """Return one READ-ONLY adapter per `SCANNED_VENUES` entry.

    A venue whose read adapter cannot be constructed is skipped (logged),
    not fatal — the same "one venue's trouble does not blank out the
    other's opportunities" policy `app.tasks.execution.reconcile_venues`
    already uses. Shared by both scan tasks so they cannot drift apart on
    which venues they cover or on `get_read_adapter` vs `get_adapter`
    (see this module's docstring).

    Returns:
        dict[VenueId, MarketDataAdapter]: The adapters that could be
            built; possibly empty, which is a scan over nothing rather
            than an error.
    """
    adapters: dict[VenueId, MarketDataAdapter] = {}
    for venue in SCANNED_VENUES:
        try:
            adapter = get_read_adapter(venue, settings.trading_mode)
        except Exception as exc:  # noqa: BLE001 - one venue must not stop the rest
            logger.warning(
                "scanner",
                extra={
                    "event": "scan_adapter_unavailable",
                    "venue": venue,
                    "mode": settings.trading_mode,
                    "reason": type(exc).__name__,
                },
            )
            continue
        if isinstance(adapter, MarketDataAdapter):
            adapters[venue] = adapter
    return adapters


async def run_scan() -> dict[str, Any]:
    """Read every scanned venue's read adapter and run one `scan()` pass.

    Returns:
        dict[str, Any]: `{"mode", "scanned_venues", "opportunities_found"}`.
    """
    adapters = read_adapters()

    async with async_session_factory() as session:
        links = list((await session.execute(select(EventLink))).scalars().all())
        scored = await scan(list(ARBITRAGE_STRATEGIES), adapters, links, session)

    return {
        "mode": settings.trading_mode,
        "scanned_venues": list(adapters),
        "opportunities_found": len(scored),
    }


async def run_near_resolution_scan() -> dict[str, Any]:
    """Read every scanned venue's read adapter and run one near-resolution pass.

    The T25 counterpart to `run_scan()`: same adapters
    (`read_adapters()`), same discovery-only contract, but
    `app.services.scanner.near_resolution_pass()` rather than `scan()`.
    No `EventLink` read — `SettlementEdgeStrategy` is single-venue and
    the pass does not construct `cross_venue_arbitrage` at all.

    `strategy_config` is left `None`, so the strategy's shipped
    `DEFAULT_CONFIG` applies: in particular `allow_dispute_window` stays
    OFF in production, and only a test opts a market inside its dispute
    window into being tradeable.

    Returns:
        dict[str, Any]: `{"mode", "scanned_venues", "opportunities_found"}`,
            the same shape `run_scan()` returns.
    """
    adapters = read_adapters()

    async with async_session_factory() as session:
        scored = await near_resolution_pass(adapters, session)

    return {
        "mode": settings.trading_mode,
        "scanned_venues": list(adapters),
        "opportunities_found": len(scored),
    }


@celery_app.task(name="app.tasks.scanner.scan_opportunities")
def scan_opportunities() -> dict[str, Any]:
    """Celery entry point for `run_scan()`.

    Returns:
        dict[str, Any]: The scan summary.
    """
    return run_async_task(run_scan())


@celery_app.task(name="app.tasks.scanner.scan_near_resolution")
def scan_near_resolution() -> dict[str, Any]:
    """Celery entry point for `run_near_resolution_scan()`.

    Returns:
        dict[str, Any]: The scan summary.
    """
    return run_async_task(run_near_resolution_scan())
