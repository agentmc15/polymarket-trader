"""Backtesting service for strategy evaluation.

This module provides a complete backtesting framework for testing
trading strategies against historical Polymarket data.

Every `BacktestConfig` datetime must be aware UTC (GUARDRAILS.md §4) and
fees come from `app/venues/fees.py` via the shared `SimulatedFillEngine`
(`BacktestConfig.fee_rate` is deprecated — see its docstring).

Example:
    ```python
    from datetime import UTC, datetime
    from app.services.backtesting import (
        Backtester,
        BacktestConfig,
        DataReplayer,
        calculate_metrics,
    )
    from app.strategies import get_strategy
    from app.database import get_session_context

    # Configure backtest
    config = BacktestConfig(
        start_date=datetime(2024, 1, 1, tzinfo=UTC),
        end_date=datetime(2024, 6, 1, tzinfo=UTC),
        initial_capital=10000,
        fill_at="next",
    )

    # Get strategy
    strategy = get_strategy("catalyst_momentum", {
        "min_price_change": 0.08,
    })

    # Run backtest
    async with get_session_context() as session:
        replayer = DataReplayer(
            session=session,
            start_date=config.start_date,
            end_date=config.end_date,
        )

        backtester = Backtester(config, strategy)
        result = await backtester.run(replayer)

    # Calculate metrics
    metrics = calculate_metrics(
        result.equity_curve,
        result.trades,
        result.initial_capital,
    )

    print(f"Total Return: {metrics.total_return_pct:.2f}%")
    print(f"Sharpe Ratio: {metrics.sharpe_ratio:.2f}")
    print(f"Max Drawdown: {metrics.max_drawdown_pct:.2f}%")
    ```
"""
from app.services.backtesting.data_replay import (
    FUTURE_ENCODING_MARKET_FIELDS,
    DataReplayer,
    InMemoryDataReplayer,
    ReplayItem,
    ResolutionEvent,
    create_sample_snapshots,
)
from app.services.backtesting.engine import (
    DIAGNOSTIC_SAME_SNAPSHOT_KEY,
    SETTLE_SIDE,
    SETTLEMENT_KEY,
    VENUE_MISMATCH_REASON,
    BacktestConfig,
    Backtester,
    BacktestResult,
    CoverageReport,
    FillAt,
    PendingIntent,
    Portfolio,
    Position,
    ResultDepthSource,
    SlippageModel,
    TradeRecord,
)
from app.services.backtesting.metrics import (
    PerformanceMetrics,
    calculate_metrics,
    calculate_rolling_metrics,
)
from app.services.backtesting.sweep import (
    DEFAULT_CAPITAL_LEVELS,
    CapitalRow,
    EdgeDecayReport,
    capital_row_to_dict,
    edge_decay_report_to_dict,
    run_sweep,
)

__all__ = [
    # Engine
    "DIAGNOSTIC_SAME_SNAPSHOT_KEY",
    "SETTLE_SIDE",
    "SETTLEMENT_KEY",
    "VENUE_MISMATCH_REASON",
    "BacktestConfig",
    "BacktestResult",
    "Backtester",
    "CoverageReport",
    "FillAt",
    "PendingIntent",
    "Portfolio",
    "Position",
    "ResultDepthSource",
    "SlippageModel",
    "TradeRecord",
    # Metrics
    "PerformanceMetrics",
    "calculate_metrics",
    "calculate_rolling_metrics",
    # Data replay
    "FUTURE_ENCODING_MARKET_FIELDS",
    "DataReplayer",
    "InMemoryDataReplayer",
    "ReplayItem",
    "ResolutionEvent",
    "create_sample_snapshots",
    # Capital sweep (T22)
    "DEFAULT_CAPITAL_LEVELS",
    "CapitalRow",
    "EdgeDecayReport",
    "run_sweep",
    "capital_row_to_dict",
    "edge_decay_report_to_dict",
]
