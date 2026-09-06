"""Celery task definitions."""
from celery import Celery

from app.config import settings
from app.logging_config import configure_logging

# The worker emits the same structured JSON lines the API process does
# (`app/logging_config.py`). `worker_hijack_root_logger=False` below is
# the other half: left at its default, Celery replaces the root logger's
# handlers on worker start and every `extra={...}` field on an order
# event is thrown away again.
configure_logging()

celery_app = Celery(
    "polymarket_trader",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=[
        "app.tasks.market_sync",
        "app.tasks.bot_execution",
        "app.tasks.backtesting",
        "app.tasks.execution",
        "app.tasks.scanner",
    ],
)

# Celery configuration
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=300,
    worker_prefetch_multiplier=1,
    result_expires=3600,
    worker_hijack_root_logger=False,
)

# Beat schedule for periodic tasks
celery_app.conf.beat_schedule = {
    "sync-markets-every-5-minutes": {
        "task": "app.tasks.market_sync.sync_markets",
        "schedule": 300.0,  # 5 minutes
    },
    "sync-prices-every-minute": {
        "task": "app.tasks.market_sync.sync_prices",
        "schedule": 60.0,  # 1 minute
    },
    "reset-daily-stats-at-midnight": {
        "task": "app.tasks.bot_execution.reset_daily_stats",
        "schedule": 86400.0,  # 24 hours
    },
    # T14: an `Order` row is committed PENDING before the venue is
    # called (crash-safety), so something has to come back and resolve
    # it. 60s is short enough that a lost order is noticed while it
    # still matters, and `Settings.reconcile_grace_s` (120s by default)
    # is deliberately longer than one interval so an in-flight request
    # is never mistaken for a lost one on the very next pass.
    "reconcile-orders-every-60-seconds": {
        "task": "app.tasks.execution.reconcile_all",
        "schedule": 60.0,  # 1 minute
    },
    # T19 (PLAN.md D10): discovery only, never routing — see
    # `app.services.scanner`'s module docstring. `settings.scan_interval_s`
    # (120s by default) rather than a bare literal, per GUARDRAILS.md §1.5's
    # "no literal that belongs in config" spirit.
    "scan-opportunities": {
        "task": "app.tasks.scanner.scan_opportunities",
        "schedule": settings.scan_interval_s,
    },
    # T25: `scan_opportunities` above runs only
    # `ARBITRAGE_STRATEGIES`, and `settlement_edge` is in the "edge"
    # category, so PLAN.md D10(a)-(d)'s near-resolution work had NO
    # periodic caller at all — the one strategy that stamps
    # `metadata["bucket"] = "near_resolution"` ran nowhere in
    # production, and every fence keyed on that tag guarded an empty
    # path. This is that caller. Its own interval
    # (`settings.near_resolution_scan_interval_s`, 300s by default)
    # rather than a reuse of `scan_interval_s`: the two passes look for
    # different things on different clocks and cost different amounts to
    # run — see `app/config.py` for the full reasoning.
    "scan-near-resolution": {
        "task": "app.tasks.scanner.scan_near_resolution",
        "schedule": settings.near_resolution_scan_interval_s,
    },
}
