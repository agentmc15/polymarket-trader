"""Celery tasks for backtesting."""
import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.database import get_session_context
from app.models.backtest_run import BacktestRun, BacktestRunStatus
from app.services.backtesting import (
    BacktestConfig,
    Backtester,
    BacktestResult,
    DataReplayer,
    PerformanceMetrics,
    SlippageModel,
    calculate_metrics,
    edge_decay_report_to_dict,
    run_sweep,
)
from app.strategies import get_strategy
from app.tasks import celery_app
from app.utils.time import ensure_aware, utcnow

logger = logging.getLogger(__name__)


def build_report(result: BacktestResult) -> dict[str, Any]:
    """Build the JSON trustworthiness/coverage payload for one run.

    GUARDRAILS.md §1.7: a result produced from synthesized depth, from
    same-snapshot (look-ahead) fills, or from a market population that
    mostly never resolved must SAY SO wherever it is shown. Persisting
    this alongside the return figure (`BacktestRun.report`, migration
    `003`) is what keeps that qualification attached to the number after
    the in-process `BacktestResult` is gone.

    Args:
        result: The completed backtest result.

    Returns:
        dict[str, Any]: JSON-serializable report. Every value is a
            primitive, list or dict — no datetimes, tuples or
            dataclasses — because this is written straight into a
            JSON/JSONB column.
    """
    coverage = result.coverage
    return {
        # Fill realism.
        "depth_source": result.depth_source,
        "fill_at": result.fill_at,
        "tick_unvalidated_fills": result.tick_unvalidated_fills,
        "crossed_book_skips": result.crossed_book_skips,
        "undeclared_zero_fee_markets": [
            list(pair) for pair in result.undeclared_zero_fee_markets
        ],
        "unfilled_counts": dict(result.unfilled_counts),
        # Intent lifecycle.
        "intents_generated": result.intents_generated,
        "intents_executed": result.intents_executed,
        "intent_rejections": result.intent_rejections,
        "intent_expirations": result.intent_expirations,
        "rejection_reasons": dict(result.rejection_reasons),
        # Settlement and what is still an estimate.
        "positions_settled": result.positions_settled,
        "unrealized_at_end": len(result.unrealized_at_end),
        "unrealized_notional_at_end": result.unrealized_notional_at_end,
        # Survivorship census.
        "coverage": {
            "markets_seen": coverage.markets_seen,
            "markets_resolved": coverage.markets_resolved,
            "markets_closed_unresolved": coverage.markets_closed_unresolved,
            "snapshots_per_market": dict(coverage.snapshots_per_market),
            "resolution_coverage": coverage.resolution_coverage,
            "low_resolution_coverage": coverage.low_resolution_coverage,
        },
    }


def _populate_run_from_result(
    run: BacktestRun,
    result: BacktestResult,
    initial_capital: float,
) -> PerformanceMetrics:
    """Fill a `BacktestRun`'s result columns from a completed `BacktestResult`.

    Shared by the single-run task (`_run_backtest_async`) and the T22
    sweep task (`_persist_sweep_level`) so a swept level's DB row is
    populated IDENTICALLY to a standalone run's — one place computes
    metrics and builds the trustworthiness `report` payload, not two.

    Args:
        run: The `BacktestRun` row to populate (NOT committed here — the
            caller commits, since the sweep task also needs to record
            the row's id first via a flush).
        result: The completed backtest result.
        initial_capital: This run's starting capital, for
            `calculate_metrics`.

    Returns:
        PerformanceMetrics: The computed metrics, for the caller's own
            logging/summary use.
    """
    metrics = calculate_metrics(result.equity_curve, result.trades, initial_capital)

    run.final_value = result.final_value
    run.total_return = result.total_return
    run.sharpe_ratio = metrics.sharpe_ratio
    run.max_drawdown = metrics.max_drawdown
    run.win_rate = metrics.win_rate
    run.total_trades = metrics.total_trades

    run.equity_curve = [
        {"timestamp": ts.isoformat(), "equity": eq} for ts, eq in result.equity_curve
    ]
    run.trades_list = [
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
        for t in result.trades
    ]
    run.report = build_report(result)
    return metrics


def _parse_iso_utc(value: str, *, field: str) -> datetime:
    """Parse an ISO-8601 string from a Celery payload into aware UTC.

    Celery arguments must be JSON-serializable, so `BacktestConfig`'s
    dates cross this boundary as strings and come back as whatever
    `datetime.fromisoformat` makes of them. On Python 3.11+ a
    `Z`-suffixed or offset-bearing string parses AWARE; a bare
    `"2024-01-01"` or `"2024-01-01T00:00:00"` parses NAIVE. A naive value
    reaching `BacktestConfig` is a defect (GUARDRAILS.md §4), and
    silently comparing it against tz-aware `PriceHistory.timestamp`s is
    exactly the naive/aware mixing PLAN.md §3 flags — so a naive parse is
    interpreted as UTC here, at the boundary, and nowhere else.

    Args:
        value: ISO-8601 datetime string from the task payload.
        field: Payload key name, for the error message.

    Returns:
        datetime: The parsed value, guaranteed aware.

    Raises:
        ValueError: If `value` is not a parseable ISO-8601 datetime.
        TypeError: If `value` is not a string.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} is not an ISO-8601 datetime: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return ensure_aware(parsed)


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
    start_date = _parse_iso_utc(request_data["start_date"], field="start_date")
    end_date = _parse_iso_utc(request_data["end_date"], field="end_date")
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

        # Update record with results (T22: shared with the sweep task's
        # per-level persistence — see `_populate_run_from_result`).
        backtest.status = BacktestRunStatus.COMPLETED
        metrics = _populate_run_from_result(backtest, backtest_result, initial_capital)
        backtest.completed_at = utcnow()
        await session.commit()

        # GUARDRAILS.md §1.7: a result built on synthesized depth or on
        # same-snapshot (look-ahead) fills is labeled WHEREVER it is
        # shown. These labels therefore travel with the numbers instead
        # of living only inside the in-process `BacktestResult`.
        logger.info(
            f"Backtest {backtest_id} completed: "
            f"return={backtest_result.total_return:.2%}, "
            f"sharpe={metrics.sharpe_ratio:.2f}, "
            f"trades={metrics.total_trades}, "
            f"depth_source={backtest_result.depth_source}, "
            f"fill_at={backtest_result.fill_at}, "
            f"crossed_book_skips={backtest_result.crossed_book_skips}, "
            f"resolution_coverage="
            f"{backtest_result.coverage.resolution_coverage:.2f}, "
            f"low_resolution_coverage="
            f"{backtest_result.coverage.low_resolution_coverage}, "
            f"unrealized_at_end={len(backtest_result.unrealized_at_end)}, "
            f"undeclared_zero_fee_markets="
            f"{len(backtest_result.undeclared_zero_fee_markets)}"
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
            # Trustworthiness + coverage labels (GUARDRAILS.md §1.7).
            "report": backtest.report,
        }


async def _mark_backtest_failed(backtest_id: int, error_message: str) -> None:
    """Mark a backtest as failed in the database.

    `error_message` is persisted into `BacktestRun.report` (migration
    `003`). Before that column existed the reason a run failed was
    discarded here and survived only in a log line, which meant a FAILED
    row in the API was indistinguishable from any other FAILED row.

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
                backtest.report = {
                    **(backtest.report or {}),
                    "error_message": error_message,
                }
                await session.commit()
                logger.info(
                    f"Marked backtest {backtest_id} as failed: {error_message}"
                )

    except Exception as e:
        logger.error(f"Failed to mark backtest {backtest_id} as failed: {e}")


@celery_app.task(
    name="app.tasks.backtesting.run_sweep_task",
    bind=True,
    max_retries=0,
    time_limit=4 * 3600,  # a sweep runs N backtests; give it room
    soft_time_limit=4 * 3600 - 60,
)
def run_sweep_task(
    self,
    parent_backtest_id: int,
    request_data: dict[str, Any],
) -> dict[str, Any]:
    """Run a capital sweep (PLAN.md D12, T22) as a Celery task.

    Mirrors `run_backtest_task`'s shape: `parent_backtest_id` names an
    already-created `BacktestRun` row (`strategy_name=f"sweep:{name}"`,
    created by `POST /api/v1/backtests/sweep`) that this task fills in
    with the aggregate `EdgeDecayReport` (`report["edge_decay"]`), after
    ALSO creating and populating one CHILD `BacktestRun` per capital
    level — each indistinguishable from a standalone single-capital run
    (`_populate_run_from_result` is the same helper `run_backtest_task`
    uses).

    Args:
        self: Celery task instance (bound).
        parent_backtest_id: Database ID of the parent `BacktestRun`.
        request_data: Same shape as `run_backtest_task`'s, plus
            `capital_levels: list[float] | None`.

    Returns:
        dict: Task result with status, the parent id, every child id,
            and `edge_dies_at`.
    """
    logger.info(
        f"Starting sweep task {self.request.id} for backtest {parent_backtest_id}"
    )

    strategy_name = request_data["strategy_name"]
    strategy_config = request_data.get("strategy_config", {})
    start_date = _parse_iso_utc(request_data["start_date"], field="start_date")
    end_date = _parse_iso_utc(request_data["end_date"], field="end_date")
    fee_rate = request_data.get("fee_rate", 0.0)
    slippage_model_str = request_data.get("slippage_model", "fixed")
    slippage_value = request_data.get("slippage_value", 0.001)
    markets = request_data.get("markets")
    capital_levels = request_data.get("capital_levels")
    slippage_model = SLIPPAGE_MODEL_MAP.get(slippage_model_str, SlippageModel.FIXED)

    try:
        return asyncio.run(
            _run_sweep_async(
                task=self,
                parent_backtest_id=parent_backtest_id,
                strategy_name=strategy_name,
                strategy_config=strategy_config,
                start_date=start_date,
                end_date=end_date,
                fee_rate=fee_rate,
                slippage_model=slippage_model,
                slippage_value=slippage_value,
                markets=markets,
                capital_levels=capital_levels,
            )
        )
    except Exception as e:
        logger.exception(f"Sweep task {parent_backtest_id} failed with error: {e}")
        asyncio.run(_mark_backtest_failed(parent_backtest_id, str(e)))
        return {
            "status": "FAILED",
            "backtest_id": parent_backtest_id,
            "error": str(e),
        }


async def _run_sweep_async(
    task,
    parent_backtest_id: int,
    strategy_name: str,
    strategy_config: dict[str, Any],
    start_date: datetime,
    end_date: datetime,
    fee_rate: float,
    slippage_model: SlippageModel,
    slippage_value: float,
    markets: list[str] | None,
    capital_levels: list[float] | None,
) -> dict[str, Any]:
    """Run the sweep asynchronously and persist every level's own run.

    Args:
        task: Celery task instance, for progress updates.
        parent_backtest_id: The parent `BacktestRun` row's id.
        strategy_name: Strategy to sweep.
        strategy_config: Strategy configuration.
        start_date: Replay window start.
        end_date: Replay window end.
        fee_rate: Deprecated legacy fee rate (see `BacktestConfig.fee_rate`).
        slippage_model: Slippage model.
        slippage_value: Slippage parameter.
        markets: Optional market filter.
        capital_levels: Capital levels to sweep, or `None` for the
            `run_sweep` default.

    Returns:
        dict: Task result — status, ids, and `edge_dies_at`.
    """
    async with get_session_context() as session:
        query = select(BacktestRun).where(BacktestRun.id == parent_backtest_id)
        result = await session.execute(query)
        parent = result.scalar_one_or_none()

        if not parent:
            raise ValueError(f"Backtest {parent_backtest_id} not found")

        parent.status = BacktestRunStatus.RUNNING
        await session.commit()

        base_config = BacktestConfig(
            start_date=start_date,
            end_date=end_date,
            fee_rate=fee_rate,
            slippage_model=slippage_model,
            slippage_value=slippage_value,
            markets_filter=markets,
        )

        child_backtest_ids: list[int] = []

        def _replayer_factory() -> DataReplayer:
            # A fresh `DataReplayer` per level, sharing this task's one
            # open session -- each instance re-queries independently
            # (T22: `run_sweep` calls this once per capital level).
            return DataReplayer(
                session=session,
                start_date=start_date,
                end_date=end_date,
                market_ids=markets,
            )

        async def _persist_level(level: float, level_result: BacktestResult) -> None:
            child = BacktestRun(
                strategy_name=strategy_name,
                strategy_config=strategy_config,
                start_date=start_date,
                end_date=end_date,
                initial_capital=level,
                fee_rate=fee_rate,
                status=BacktestRunStatus.COMPLETED,
            )
            _populate_run_from_result(child, level_result, level)
            child.completed_at = utcnow()
            session.add(child)
            await session.commit()
            await session.refresh(child)
            child_backtest_ids.append(child.id)
            task.update_state(
                state="PROGRESS",
                meta={
                    "backtest_id": parent_backtest_id,
                    "strategy": strategy_name,
                    "levels_completed": len(child_backtest_ids),
                    "last_level": level,
                },
            )

        logger.info(f"Sweep {parent_backtest_id}: running {strategy_name}...")
        report = await run_sweep(
            strategy_name,
            strategy_config,
            base_config,
            _replayer_factory,
            capital_levels,
            on_level_result=_persist_level,
        )

        edge_decay = edge_decay_report_to_dict(report)
        parent.status = BacktestRunStatus.COMPLETED
        if report.rows:
            # The parent row has no single "final_value"/"total_return" of
            # its own -- a sweep is many results, not one -- so those
            # columns are left at their PENDING-time placeholder and the
            # per-level numbers live in `report["edge_decay"]["rows"]`
            # and in each child `BacktestRun` instead.
            parent.initial_capital = max(row.capital for row in report.rows)
            parent.total_trades = sum(row.trades for row in report.rows)
        parent.report = {
            "edge_decay": edge_decay,
            "child_backtest_ids": child_backtest_ids,
        }
        parent.completed_at = utcnow()
        await session.commit()

        logger.info(
            f"Sweep {parent_backtest_id} completed: "
            f"levels={len(report.rows)}, edge_dies_at={report.edge_dies_at}, "
            f"depth_source={report.depth_source}, fill_at={report.fill_at}"
        )

        return {
            "status": "COMPLETED",
            "backtest_id": parent_backtest_id,
            "child_backtest_ids": child_backtest_ids,
            "edge_dies_at": report.edge_dies_at,
            "report": parent.report,
        }


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
            cutoff_date = utcnow() - timedelta(days=days)

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
