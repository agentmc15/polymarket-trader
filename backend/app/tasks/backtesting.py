"""Celery tasks for backtesting."""
import asyncio
import logging
from datetime import datetime
from typing import Any

from celery import current_task
from sqlalchemy import select

from app.tasks import celery_app
from app.database import get_session_context
from app.models.backtest_run import BacktestRun, BacktestRunStatus
from app.strategies import get_strategy
from app.services.backtesting import (
    Backtester,
    BacktestConfig,
    DataReplayer,
    SlippageModel,
    calculate_metrics,
)


logger = logging.getLogger(__name__)


# Map string slippage model to enum
SLIPPAGE_MODEL_MAP = {
    "none": SlippageModel.NONE,
    "fixed": SlippageModel.FIXED,
    "volume_based": SlippageModel.VOLUME_BASED,
    "spread_based": SlippageModel.SPREAD_BASED,
}


@celery_app.task(
    name="app.tasks.backtesting.run_backtest_task",
    bind=True,
    max_retries=0,
    time_limit=3600,  # 1 hour max
    soft_time_limit=3300,  # 55 min soft limit
)
def run_backtest_task(
    self,
    backtest_id: int,
    request_data: dict[str, Any],
) -> dict[str, Any]:
    """Run a backtest as a Celery task.

    This task loads the strategy, creates the backtester and data replayer,
    runs the backtest, and saves results to the database.

    Args:
        self: Celery task instance (bound).
        backtest_id: Database ID of the BacktestRun record.
        request_data: Dictionary containing:
            - strategy_name: Name of the strategy to use
            - strategy_config: Strategy configuration overrides
            - start_date: Backtest start date (ISO format string)
            - end_date: Backtest end date (ISO format string)
            - initial_capital: Starting capital
            - fee_rate: Trading fee rate
            - slippage_model: Slippage model name
            - slippage_value: Slippage parameter value
            - markets: Optional list of market IDs to filter

    Returns:
        dict: Task result with status and metrics.
    """
    logger.info(f"Starting backtest task {self.request.id} for backtest {backtest_id}")

    # Extract request data
    strategy_name = request_data["strategy_name"]
    strategy_config = request_data.get("strategy_config", {})
    start_date = datetime.fromisoformat(request_data["start_date"])
    end_date = datetime.fromisoformat(request_data["end_date"])
    initial_capital = request_data["initial_capital"]
    fee_rate = request_data.get("fee_rate", 0.0)
    slippage_model_str = request_data.get("slippage_model", "fixed")
    slippage_value = request_data.get("slippage_value", 0.001)
    markets = request_data.get("markets")

    # Convert slippage model
    slippage_model = SLIPPAGE_MODEL_MAP.get(slippage_model_str, SlippageModel.FIXED)

    # Run the async backtest logic
    try:
        result = asyncio.run(
            _run_backtest_async(
                task=self,
                backtest_id=backtest_id,
                strategy_name=strategy_name,
                strategy_config=strategy_config,
                start_date=start_date,
                end_date=end_date,
                initial_capital=initial_capital,
                fee_rate=fee_rate,
                slippage_model=slippage_model,
                slippage_value=slippage_value,
                markets=markets,
            )
        )
        return result

    except Exception as e:
        logger.exception(f"Backtest task {backtest_id} failed with error: {e}")

        # Mark as failed in database
        asyncio.run(_mark_backtest_failed(backtest_id, str(e)))

        return {
            "status": "FAILED",
            "backtest_id": backtest_id,
            "error": str(e),
        }


async def _run_backtest_async(
    task,
    backtest_id: int,
    strategy_name: str,
    strategy_config: dict[str, Any],
    start_date: datetime,
    end_date: datetime,
    initial_capital: float,
    fee_rate: float,
    slippage_model: SlippageModel,
    slippage_value: float,
    markets: list[str] | None,
) -> dict[str, Any]:
    """Run the backtest asynchronously.

    Args:
        task: Celery task instance for progress updates.
        backtest_id: Database record ID.
        strategy_name: Strategy to use.
        strategy_config: Strategy configuration.
        start_date: Backtest start.
        end_date: Backtest end.
        initial_capital: Starting capital.
        fee_rate: Trading fee rate.
        slippage_model: Slippage model.
        slippage_value: Slippage parameter.
        markets: Optional market filter.

    Returns:
        dict: Task result with status and metrics.
    """
    async with get_session_context() as session:
        # Update status to running
        query = select(BacktestRun).where(BacktestRun.id == backtest_id)
        result = await session.execute(query)
        backtest = result.scalar_one_or_none()

        if not backtest:
            raise ValueError(f"Backtest {backtest_id} not found")

        backtest.status = BacktestRunStatus.RUNNING
        await session.commit()

        logger.info(f"Backtest {backtest_id}: Loading strategy {strategy_name}")

        # Load strategy
        strategy = get_strategy(strategy_name, strategy_config)

        # Create backtest config
        config = BacktestConfig(
            start_date=start_date,
            end_date=end_date,
            initial_capital=initial_capital,
            fee_rate=fee_rate,
            slippage_model=slippage_model,
            slippage_value=slippage_value,
            markets_filter=markets,
        )

        # Progress callback that updates Celery task state
        def update_progress(progress: float) -> None:
            task.update_state(
                state="PROGRESS",
                meta={
                    "progress": progress,
                    "backtest_id": backtest_id,
                    "strategy": strategy_name,
                },
            )

        # Create backtester with progress callback
        backtester = Backtester(config, strategy, progress_callback=update_progress)

        # Create data replayer
        replayer = DataReplayer(
            session=session,
            start_date=start_date,
            end_date=end_date,
            market_ids=markets,
        )

        logger.info(f"Backtest {backtest_id}: Running simulation...")

        # Run backtest
        backtest_result = await backtester.run(replayer)

        logger.info(f"Backtest {backtest_id}: Calculating metrics...")

        # Calculate metrics
        metrics = calculate_metrics(
            backtest_result.equity_curve,
            backtest_result.trades,
            initial_capital,
        )

        # Update record with results
        backtest.status = BacktestRunStatus.COMPLETED
        backtest.final_value = backtest_result.final_value
        backtest.total_return = backtest_result.total_return
        backtest.sharpe_ratio = metrics.sharpe_ratio
        backtest.max_drawdown = metrics.max_drawdown
        backtest.win_rate = metrics.win_rate
        backtest.total_trades = metrics.total_trades

        # Store equity curve as JSON
        backtest.equity_curve = [
            {"timestamp": ts.isoformat(), "equity": eq}
            for ts, eq in backtest_result.equity_curve
        ]

        # Store trades as JSON
        backtest.trades_list = [
            {
                "timestamp": t.timestamp.isoformat(),
                "market_id": t.market_id,
                "outcome": t.outcome,
                "side": t.side,
                "price": t.price,
                "size": t.size,
                "fee": t.fee,
                "pnl": t.pnl,
                "signal_confidence": getattr(t, "signal_confidence", 0),
            }
            for t in backtest_result.trades
        ]

        backtest.completed_at = datetime.utcnow()
        await session.commit()

        logger.info(
            f"Backtest {backtest_id} completed: "
            f"return={backtest_result.total_return:.2%}, "
            f"sharpe={metrics.sharpe_ratio:.2f}, "
            f"trades={metrics.total_trades}"
        )

        return {
            "status": "COMPLETED",
            "backtest_id": backtest_id,
            "final_value": backtest_result.final_value,
            "total_return": backtest_result.total_return,
            "total_return_pct": backtest_result.total_return * 100,
            "sharpe_ratio": metrics.sharpe_ratio,
            "max_drawdown": metrics.max_drawdown,
            "win_rate": metrics.win_rate,
            "total_trades": metrics.total_trades,
        }


async def _mark_backtest_failed(backtest_id: int, error_message: str) -> None:
    """Mark a backtest as failed in the database.

    Args:
        backtest_id: Backtest record ID.
        error_message: Error description.
    """
    try:
        async with get_session_context() as session:
            query = select(BacktestRun).where(BacktestRun.id == backtest_id)
            result = await session.execute(query)
            backtest = result.scalar_one_or_none()

            if backtest:
                backtest.status = BacktestRunStatus.FAILED
                # Note: Would need to add error_message field to model if we want to store it
                await session.commit()
                logger.info(f"Marked backtest {backtest_id} as failed")

    except Exception as e:
        logger.error(f"Failed to mark backtest {backtest_id} as failed: {e}")


@celery_app.task(name="app.tasks.backtesting.cancel_backtest")
def cancel_backtest(backtest_id: int) -> dict[str, Any]:
    """Cancel a running backtest.

    Args:
        backtest_id: Backtest ID to cancel.

    Returns:
        dict: Cancellation result.
    """

    async def _cancel():
        async with get_session_context() as session:
            query = select(BacktestRun).where(BacktestRun.id == backtest_id)
            result = await session.execute(query)
            backtest = result.scalar_one_or_none()

            if not backtest:
                return {"status": "NOT_FOUND", "backtest_id": backtest_id}

            if backtest.status not in (BacktestRunStatus.PENDING, BacktestRunStatus.RUNNING):
                return {
                    "status": "INVALID",
                    "message": f"Cannot cancel backtest with status {backtest.status.value}",
                }

            backtest.status = BacktestRunStatus.CANCELLED
            await session.commit()

            return {"status": "CANCELLED", "backtest_id": backtest_id}

    return asyncio.run(_cancel())


@celery_app.task(name="app.tasks.backtesting.cleanup_old_backtests")
def cleanup_old_backtests(days: int = 30) -> dict[str, Any]:
    """Clean up old backtest records.

    Args:
        days: Delete backtests older than this many days.

    Returns:
        dict: Cleanup result.
    """
    from datetime import timedelta
    from sqlalchemy import delete

    async def _cleanup():
        async with get_session_context() as session:
            cutoff_date = datetime.utcnow() - timedelta(days=days)

            # Delete old completed/failed backtests
            stmt = delete(BacktestRun).where(
                BacktestRun.created_at < cutoff_date,
                BacktestRun.status.in_([
                    BacktestRunStatus.COMPLETED,
                    BacktestRunStatus.FAILED,
                    BacktestRunStatus.CANCELLED,
                ]),
            )

            result = await session.execute(stmt)
            await session.commit()

            deleted_count = result.rowcount
            logger.info(f"Cleaned up {deleted_count} old backtest records")

            return {"deleted": deleted_count, "older_than_days": days}

    return asyncio.run(_cleanup())
