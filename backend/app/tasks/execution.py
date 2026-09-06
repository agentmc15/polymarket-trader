"""Celery tasks for the execution path (T14).

`reconcile_all` runs `app.execution.reconcile.reconcile()` for every
venue, in the process's configured trading mode, on the beat schedule in
`app/tasks/__init__.py` (every 60 seconds). It is the thing that
periodically LOOKS — `OrderRouter` writes a `PENDING` order row before it
calls the venue precisely so a crash leaves a recoverable record, and
that record is only worth writing if something later goes and resolves
it.

GUARDRAILS.md §1.1: nothing here places or cancels a venue order.
`reconcile()` only reads (`get_open_orders`, `get_fills`) and writes to
the local database.

MODE, AND WHY THIS ASKS FOR A READ-ONLY ADAPTER. Adapters come from
`app.venues.registry.get_read_adapter(venue, settings.trading_mode)`, so
in the default `"paper"` mode this reconciles paper rows against the
`PaperVenueAdapter` singleton and never touches a venue at all, and in
`"live"` mode it gets the venue's READ adapter (`PolymarketAdapter`/
`KalshiAdapter`), which has no `place_order` at all.

It deliberately does NOT call `get_adapter()`, which in live mode builds
an order-PLACING adapter behind `assert_live_allowed()`. That coupling
inverted the kill switch: engaging it made the live adapter
unconstructible and so stopped this beat — the read-only pass an
operator most wants during a halt — while `app/api/deps.py`'s cached
router went on placing orders with adapters it had built before the
switch was thrown. The halt now lives at PLACEMENT
(`app.execution.fences.assert_placement_allowed`, called by
`OrderRouter.submit`), and this pass, which places nothing, keeps
running.
"""
import logging
from typing import Any

from app.config import settings
from app.database import async_session_factory, run_async_task
from app.execution.reconcile import reconcile
from app.tasks import celery_app
from app.venues.registry import get_read_adapter
from app.venues.types import VenueId

logger = logging.getLogger(__name__)

#: Venues reconciled on every pass. Mirrors `app.venues.types.VenueId`;
#: a third venue means adding it here as well as to the registry.
RECONCILED_VENUES: tuple[VenueId, ...] = ("polymarket", "kalshi")


async def reconcile_venues() -> dict[str, Any]:
    """Reconcile every venue once, in the configured trading mode.

    A failure on one venue does not abort the others: a rate-limited or
    unreachable Polymarket must not prevent Kalshi orders from being
    resolved, and vice versa. Each venue's outcome — a report or an
    error — is returned so the beat's result records what actually
    happened.

    Returns:
        dict[str, Any]: `{"mode": ..., "venues": {venue: report-or-error}}`.
    """
    results: dict[str, Any] = {}
    for venue in RECONCILED_VENUES:
        try:
            adapter = get_read_adapter(venue, settings.trading_mode)
        except Exception as exc:  # noqa: BLE001 - one venue must not stop the rest
            logger.warning(
                "order",
                extra={
                    "event": "reconcile_adapter_unavailable",
                    "venue": venue,
                    "mode": settings.trading_mode,
                    "reason": type(exc).__name__,
                },
            )
            results[venue] = {"skipped": type(exc).__name__}
            continue
        try:
            report = await reconcile(venue, adapter, async_session_factory)
        except Exception as exc:  # noqa: BLE001 - one venue must not stop the rest
            logger.exception(
                "order",
                extra={
                    "event": "reconcile_failed",
                    "venue": venue,
                    "mode": settings.trading_mode,
                },
            )
            results[venue] = {"error": f"{type(exc).__name__}: {exc}"}
            continue
        results[venue] = report.as_dict()
    return {"mode": settings.trading_mode, "venues": results}


@celery_app.task(name="app.tasks.execution.reconcile_all")
def reconcile_all() -> dict[str, Any]:
    """Celery entry point for `reconcile_venues()`.

    Returns:
        dict[str, Any]: The per-venue reconciliation result.
    """
    return run_async_task(reconcile_venues())
