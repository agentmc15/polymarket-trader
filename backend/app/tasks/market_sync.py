"""Market data synchronization tasks."""
from app.tasks import celery_app


@celery_app.task(name="app.tasks.market_sync.sync_markets")
def sync_markets() -> dict:
    """Sync market data from Polymarket.

    Returns:
        dict: Sync results.
    """
    # TODO: Implement market sync
    return {"status": "success", "markets_synced": 0}


@celery_app.task(name="app.tasks.market_sync.sync_prices")
def sync_prices() -> dict:
    """Sync current prices for active markets.

    Returns:
        dict: Sync results.
    """
    # TODO: Implement price sync
    return {"status": "success", "prices_synced": 0}


@celery_app.task(name="app.tasks.market_sync.sync_orderbooks")
def sync_orderbooks(market_ids: list[str] | None = None) -> dict:  # noqa: ARG001
    """Sync orderbooks for specified markets.

    Args:
        market_ids: List of market IDs to sync, or None for all.

    Returns:
        dict: Sync results.
    """
    # TODO: Implement orderbook sync. `market_ids` is part of this
    # Celery task's declared signature (callers already pass it by
    # name), so it is kept and the unused-argument warning suppressed
    # rather than the parameter removed — dropping it would break the
    # task's published shape for a stub that is about to use it.
    return {"status": "success", "orderbooks_synced": 0}
