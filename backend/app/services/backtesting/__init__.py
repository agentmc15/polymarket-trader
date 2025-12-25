"""Backtesting service for strategy evaluation.

This module provides a complete backtesting framework for testing
trading strategies against historical Polymarket data.

Example:
    ```python
    from datetime import datetime
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
        start_date=datetime(2024, 1, 1),
        end_date=datetime(2024, 6, 1),
        initial_capital=10000,
        fee_rate=0.001,
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
from app.services.backtesting.engine import (
    BacktestConfig,
    BacktestResult,
    Backtester,
    Portfolio,
    Position,
    SlippageModel,
    TradeRecord,
)
from app.services.backtesting.metrics import (
    PerformanceMetrics,
    calculate_metrics,
    calculate_rolling_metrics,
)
from app.services.backtesting.data_replay import (
    DataReplayer,
    InMemoryDataReplayer,
    create_sample_snapshots,
)


__all__ = [
    # Engine
    "BacktestConfig",
    "BacktestResult",
    "Backtester",
    "Portfolio",
    "Position",
    "SlippageModel",
    "TradeRecord",
    # Metrics
    "PerformanceMetrics",
    "calculate_metrics",
    "calculate_rolling_metrics",
    # Data replay
    "DataReplayer",
    "InMemoryDataReplayer",
    "create_sample_snapshots",
]
