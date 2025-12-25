"""Celery task definitions."""
from celery import Celery

from app.config import settings

celery_app = Celery(
    "polymarket_trader",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=[
        "app.tasks.market_sync",
        "app.tasks.trader_tracking",
        "app.tasks.bot_execution",
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
    "track-traders-every-hour": {
        "task": "app.tasks.trader_tracking.update_trader_stats",
        "schedule": 3600.0,  # 1 hour
    },
    "reset-daily-stats-at-midnight": {
        "task": "app.tasks.bot_execution.reset_daily_stats",
        "schedule": 86400.0,  # 24 hours
    },
}
