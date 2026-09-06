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
        "app.tasks.matching",
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
#
# T41 (deployment coherence audit): `app.tasks.market_sync.sync_markets`,
# `.sync_prices` and `app.tasks.bot_execution.reset_daily_stats` used to
# be registered here as "sync-markets-every-5-minutes",
# "sync-prices-every-minute" and "reset-daily-stats-at-midnight". All
# three are `# TODO` stubs that unconditionally return
# `{"status": "success", ...: 0}` — so they fired on schedule, did
# nothing, and reported health while doing it. A monitoring dashboard
# reading these beats sees three green heartbeats and an operator
# concludes market sync is running, which is false: the real market
# sync path is `app.services.data_collector.DataCollector`, driven
# manually via `app.scripts.sync_markets` / `app.scripts.collect_prices`,
# entirely outside Celery.
#
# Deliberately UNREGISTERED rather than converted to "honest failure"
# beats: every entry below carries a comment justifying why it is safe
# and useful to fire on a clock, matching this file's own convention —
# a stub with no such justification is the anomaly, not a candidate for
# a manufactured non-success status. `sync_orderbooks`,
# `check_stop_loss` and `check_take_profit` (also stubs, in
# market_sync.py / bot_execution.py) were never scheduled either; this
# just makes the other three consistent with that existing, correct
# treatment. A beat that fires every 60s and always reports failure
# would also page/alert on a "regression" that never happened, which is
# its own kind of misleading. Absence from this dict is the honest
# signal: nothing here claims to run market sync, so nobody checking
# THIS FILE (not the task bodies) concludes that it does. Re-add the
# entry here, with the same kind of justifying comment as its
# neighbors, once the callable it points to does real work.
#
#   "sync-markets-every-5-minutes": {
#       "task": "app.tasks.market_sync.sync_markets", "schedule": 300.0,
#   },
#   "sync-prices-every-minute": {
#       "task": "app.tasks.market_sync.sync_prices", "schedule": 60.0,
#   },
#   "reset-daily-stats-at-midnight": {
#       "task": "app.tasks.bot_execution.reset_daily_stats",
#       "schedule": 86400.0,
#   },
celery_app.conf.beat_schedule = {
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
    # T30 (PLAN.md D9): `app.services.matching.propose_links` had ONE
    # caller, the manual `POST /links/propose`. With no beat,
    # `event_links` on a fresh install started empty and stayed empty,
    # and `app.services.scanner._build_strategies` filters to
    # `status="approved"` before building `LinkBook` — so
    # `cross_venue_arbitrage`, this repo's centerpiece, was constructed
    # with nothing and emitted nothing, permanently. This is its feeder.
    # It PROPOSES ONLY: approval stays a human act in
    # `app/api/routes/links.py` (PLAN.md D9/R1), and re-running it can
    # neither duplicate a queued proposal nor resurrect a rejected one —
    # see `app.services.matching.persist`.
    # Its own interval (`settings.link_proposal_interval_s`, 3600s by
    # default), not a reuse of either scan interval: a venue's market
    # LIST turns over on a scale of hours, the output is a queue a human
    # reads rather than a trade, and the pass reads every open market on
    # both venues — see `app/config.py` for the full reasoning.
    "propose-event-links": {
        "task": "app.tasks.matching.propose_event_links",
        "schedule": settings.link_proposal_interval_s,
    },
}
