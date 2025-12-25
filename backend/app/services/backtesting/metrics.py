"""Performance metrics calculation for backtesting.

Provides comprehensive risk-adjusted performance metrics for
evaluating trading strategy results.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import numpy as np
from scipy import stats


@dataclass
class PerformanceMetrics:
    """Comprehensive performance metrics from a backtest.

    Attributes:
        total_return: Total return (final/initial - 1).
        total_return_pct: Total return as percentage.
        annualized_return: Annualized return rate.
        sharpe_ratio: Risk-adjusted return (excess return / volatility).
        sortino_ratio: Downside risk-adjusted return.
        calmar_ratio: Return / max drawdown.
        max_drawdown: Maximum peak-to-trough decline.
        max_drawdown_pct: Max drawdown as percentage.
        max_drawdown_duration: Longest drawdown period in days.
        volatility: Annualized return volatility.
        downside_volatility: Volatility of negative returns only.
        win_rate: Percentage of profitable trades.
        profit_factor: Gross profit / gross loss.
        avg_win: Average winning trade amount.
        avg_loss: Average losing trade amount.
        avg_win_pct: Average winning trade percentage.
        avg_loss_pct: Average losing trade percentage.
        largest_win: Largest single winning trade.
        largest_loss: Largest single losing trade.
        total_trades: Total number of trades.
        winning_trades: Number of winning trades.
        losing_trades: Number of losing trades.
        avg_trade_duration: Average time in position.
        total_fees: Total fees paid.
        total_slippage: Total slippage cost.
        expectancy: Expected value per trade.
        risk_reward_ratio: Average win / average loss.
        recovery_factor: Total return / max drawdown.
        ulcer_index: Measure of drawdown severity.
        var_95: Value at Risk (95% confidence).
        cvar_95: Conditional VaR (expected shortfall).
        skewness: Return distribution skewness.
        kurtosis: Return distribution kurtosis.
        best_day: Best single day return.
        worst_day: Worst single day return.
        positive_days: Number of positive return days.
        negative_days: Number of negative return days.
    """

    # Return metrics
    total_return: float = 0.0
    total_return_pct: float = 0.0
    annualized_return: float = 0.0

    # Risk-adjusted metrics
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    calmar_ratio: float = 0.0

    # Drawdown metrics
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    max_drawdown_duration: float = 0.0

    # Volatility metrics
    volatility: float = 0.0
    downside_volatility: float = 0.0

    # Trade metrics
    win_rate: float = 0.0
    profit_factor: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0

    # Duration metrics
    avg_trade_duration: float = 0.0  # in hours

    # Cost metrics
    total_fees: float = 0.0
    total_slippage: float = 0.0

    # Advanced metrics
    expectancy: float = 0.0
    risk_reward_ratio: float = 0.0
    recovery_factor: float = 0.0
    ulcer_index: float = 0.0

    # Distribution metrics
    var_95: float = 0.0
    cvar_95: float = 0.0
    skewness: float = 0.0
    kurtosis: float = 0.0

    # Daily metrics
    best_day: float = 0.0
    worst_day: float = 0.0
    positive_days: int = 0
    negative_days: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Convert metrics to dictionary."""
        return {
            "returns": {
                "total_return": self.total_return,
                "total_return_pct": self.total_return_pct,
                "annualized_return": self.annualized_return,
            },
            "risk_adjusted": {
                "sharpe_ratio": self.sharpe_ratio,
                "sortino_ratio": self.sortino_ratio,
                "calmar_ratio": self.calmar_ratio,
            },
            "drawdown": {
                "max_drawdown": self.max_drawdown,
                "max_drawdown_pct": self.max_drawdown_pct,
                "max_drawdown_duration_days": self.max_drawdown_duration,
            },
            "volatility": {
                "volatility": self.volatility,
                "downside_volatility": self.downside_volatility,
            },
            "trades": {
                "total_trades": self.total_trades,
                "winning_trades": self.winning_trades,
                "losing_trades": self.losing_trades,
                "win_rate": self.win_rate,
                "profit_factor": self.profit_factor,
                "avg_win": self.avg_win,
                "avg_loss": self.avg_loss,
                "largest_win": self.largest_win,
                "largest_loss": self.largest_loss,
                "expectancy": self.expectancy,
                "risk_reward_ratio": self.risk_reward_ratio,
            },
            "risk": {
                "var_95": self.var_95,
                "cvar_95": self.cvar_95,
                "ulcer_index": self.ulcer_index,
            },
            "distribution": {
                "skewness": self.skewness,
                "kurtosis": self.kurtosis,
            },
            "costs": {
                "total_fees": self.total_fees,
                "total_slippage": self.total_slippage,
            },
        }


def calculate_metrics(
    equity_curve: list[tuple[datetime, float]],
    trades: list[Any],
    initial_capital: float = 10000.0,
    risk_free_rate: float = 0.04,
    trading_days_per_year: int = 252,
) -> PerformanceMetrics:
    """Calculate comprehensive performance metrics.

    Args:
        equity_curve: List of (timestamp, equity) tuples.
        trades: List of TradeRecord objects.
        initial_capital: Starting capital.
        risk_free_rate: Annual risk-free rate for Sharpe calculation.
        trading_days_per_year: Trading days per year for annualization.

    Returns:
        PerformanceMetrics with all calculated values.
    """
    metrics = PerformanceMetrics()

    if not equity_curve or len(equity_curve) < 2:
        return metrics

    # Extract values
    timestamps = [e[0] for e in equity_curve]
    equity_values = np.array([e[1] for e in equity_curve])

    # Basic return metrics
    metrics.total_return = (equity_values[-1] / initial_capital) - 1
    metrics.total_return_pct = metrics.total_return * 100

    # Calculate period in years
    total_days = (timestamps[-1] - timestamps[0]).days
    years = max(total_days / 365.25, 1 / 365.25)  # Minimum 1 day

    # Annualized return
    if metrics.total_return > -1:
        metrics.annualized_return = (1 + metrics.total_return) ** (1 / years) - 1
    else:
        metrics.annualized_return = -1.0

    # Calculate returns
    returns = np.diff(equity_values) / equity_values[:-1]
    returns = returns[np.isfinite(returns)]  # Remove inf/nan

    if len(returns) == 0:
        return metrics

    # Daily returns for volatility calculation
    # Resample to daily if needed
    daily_returns = _resample_to_daily(timestamps[1:], returns)

    if len(daily_returns) > 0:
        # Volatility (annualized)
        metrics.volatility = float(np.std(daily_returns) * np.sqrt(trading_days_per_year))

        # Downside volatility
        negative_returns = daily_returns[daily_returns < 0]
        if len(negative_returns) > 0:
            metrics.downside_volatility = float(
                np.std(negative_returns) * np.sqrt(trading_days_per_year)
            )

        # Sharpe Ratio
        excess_returns = daily_returns - (risk_free_rate / trading_days_per_year)
        if np.std(daily_returns) > 0:
            metrics.sharpe_ratio = float(
                np.mean(excess_returns) / np.std(daily_returns) * np.sqrt(trading_days_per_year)
            )

        # Sortino Ratio
        if metrics.downside_volatility > 0:
            metrics.sortino_ratio = float(
                (metrics.annualized_return - risk_free_rate) / metrics.downside_volatility
            )

        # Distribution metrics
        if len(daily_returns) > 3:
            metrics.skewness = float(stats.skew(daily_returns))
            metrics.kurtosis = float(stats.kurtosis(daily_returns))

        # VaR and CVaR
        metrics.var_95 = float(np.percentile(daily_returns, 5))
        metrics.cvar_95 = float(np.mean(daily_returns[daily_returns <= metrics.var_95]))

        # Best/worst days
        metrics.best_day = float(np.max(daily_returns))
        metrics.worst_day = float(np.min(daily_returns))
        metrics.positive_days = int(np.sum(daily_returns > 0))
        metrics.negative_days = int(np.sum(daily_returns < 0))

    # Drawdown metrics
    drawdown_data = _calculate_drawdowns(equity_values, timestamps)
    metrics.max_drawdown = drawdown_data["max_drawdown"]
    metrics.max_drawdown_pct = drawdown_data["max_drawdown_pct"]
    metrics.max_drawdown_duration = drawdown_data["max_duration_days"]
    metrics.ulcer_index = drawdown_data["ulcer_index"]

    # Calmar Ratio
    if metrics.max_drawdown_pct > 0:
        metrics.calmar_ratio = metrics.annualized_return / metrics.max_drawdown_pct

    # Recovery Factor
    if metrics.max_drawdown > 0:
        metrics.recovery_factor = metrics.total_return / metrics.max_drawdown

    # Trade metrics
    if trades:
        trade_metrics = _calculate_trade_metrics(trades)
        metrics.total_trades = trade_metrics["total_trades"]
        metrics.winning_trades = trade_metrics["winning_trades"]
        metrics.losing_trades = trade_metrics["losing_trades"]
        metrics.win_rate = trade_metrics["win_rate"]
        metrics.profit_factor = trade_metrics["profit_factor"]
        metrics.avg_win = trade_metrics["avg_win"]
        metrics.avg_loss = trade_metrics["avg_loss"]
        metrics.avg_win_pct = trade_metrics["avg_win_pct"]
        metrics.avg_loss_pct = trade_metrics["avg_loss_pct"]
        metrics.largest_win = trade_metrics["largest_win"]
        metrics.largest_loss = trade_metrics["largest_loss"]
        metrics.total_fees = trade_metrics["total_fees"]
        metrics.total_slippage = trade_metrics["total_slippage"]
        metrics.expectancy = trade_metrics["expectancy"]
        metrics.risk_reward_ratio = trade_metrics["risk_reward_ratio"]
        metrics.avg_trade_duration = trade_metrics["avg_duration_hours"]

    return metrics


def _resample_to_daily(
    timestamps: list[datetime],
    returns: np.ndarray,
) -> np.ndarray:
    """Resample returns to daily frequency.

    Args:
        timestamps: Return timestamps.
        returns: Return values.

    Returns:
        Daily returns array.
    """
    if len(timestamps) == 0 or len(returns) == 0:
        return np.array([])

    # Group returns by date
    daily_returns_dict: dict[datetime, list[float]] = {}

    for ts, ret in zip(timestamps, returns):
        date = ts.date() if hasattr(ts, 'date') else ts
        if date not in daily_returns_dict:
            daily_returns_dict[date] = []
        daily_returns_dict[date].append(ret)

    # Compound intraday returns
    daily_returns = []
    for date in sorted(daily_returns_dict.keys()):
        day_returns = daily_returns_dict[date]
        # Compound: (1+r1) * (1+r2) * ... - 1
        compounded = np.prod([1 + r for r in day_returns]) - 1
        daily_returns.append(compounded)

    return np.array(daily_returns)


def _calculate_drawdowns(
    equity: np.ndarray,
    timestamps: list[datetime],
) -> dict[str, float]:
    """Calculate drawdown metrics.

    Args:
        equity: Equity curve values.
        timestamps: Timestamps.

    Returns:
        Dictionary with drawdown metrics.
    """
    # Running maximum
    running_max = np.maximum.accumulate(equity)

    # Drawdown at each point
    drawdowns = (running_max - equity) / running_max
    drawdowns = np.nan_to_num(drawdowns, nan=0.0)

    max_drawdown = float(np.max(drawdowns))
    max_drawdown_pct = max_drawdown * 100

    # Calculate drawdown duration
    in_drawdown = drawdowns > 0
    max_duration_days = 0.0
    current_duration = 0.0
    drawdown_start = None

    for i, (ts, is_dd) in enumerate(zip(timestamps, in_drawdown)):
        if is_dd:
            if drawdown_start is None:
                drawdown_start = ts
            current_duration = (ts - drawdown_start).total_seconds() / 86400
            max_duration_days = max(max_duration_days, current_duration)
        else:
            drawdown_start = None
            current_duration = 0.0

    # Ulcer Index: RMS of drawdowns
    ulcer_index = float(np.sqrt(np.mean(drawdowns ** 2)))

    return {
        "max_drawdown": max_drawdown,
        "max_drawdown_pct": max_drawdown_pct,
        "max_duration_days": max_duration_days,
        "ulcer_index": ulcer_index,
    }


def _calculate_trade_metrics(trades: list[Any]) -> dict[str, Any]:
    """Calculate trade-based metrics.

    Args:
        trades: List of TradeRecord objects.

    Returns:
        Dictionary with trade metrics.
    """
    # Filter trades with P&L (closing trades)
    pnl_trades = [t for t in trades if hasattr(t, 'pnl') and t.pnl is not None]

    if not pnl_trades:
        return {
            "total_trades": len(trades),
            "winning_trades": 0,
            "losing_trades": 0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "avg_win_pct": 0.0,
            "avg_loss_pct": 0.0,
            "largest_win": 0.0,
            "largest_loss": 0.0,
            "total_fees": sum(t.fee for t in trades if hasattr(t, 'fee')),
            "total_slippage": sum(abs(t.slippage) for t in trades if hasattr(t, 'slippage')),
            "expectancy": 0.0,
            "risk_reward_ratio": 0.0,
            "avg_duration_hours": 0.0,
        }

    pnls = [t.pnl for t in pnl_trades]
    winning_pnls = [p for p in pnls if p > 0]
    losing_pnls = [p for p in pnls if p < 0]

    total_trades = len(pnl_trades)
    winning_trades = len(winning_pnls)
    losing_trades = len(losing_pnls)

    win_rate = winning_trades / total_trades if total_trades > 0 else 0

    # Profit factor
    gross_profit = sum(winning_pnls) if winning_pnls else 0
    gross_loss = abs(sum(losing_pnls)) if losing_pnls else 0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')

    # Average win/loss
    avg_win = np.mean(winning_pnls) if winning_pnls else 0
    avg_loss = np.mean(losing_pnls) if losing_pnls else 0

    # Win/loss percentages (relative to trade cost)
    avg_win_pct = 0.0
    avg_loss_pct = 0.0

    win_pcts = []
    loss_pcts = []
    for t in pnl_trades:
        if hasattr(t, 'price') and hasattr(t, 'size') and t.price > 0:
            trade_value = t.price * t.size
            if trade_value > 0:
                pct = t.pnl / trade_value
                if t.pnl > 0:
                    win_pcts.append(pct)
                elif t.pnl < 0:
                    loss_pcts.append(pct)

    if win_pcts:
        avg_win_pct = float(np.mean(win_pcts))
    if loss_pcts:
        avg_loss_pct = float(np.mean(loss_pcts))

    # Largest win/loss
    largest_win = max(winning_pnls) if winning_pnls else 0
    largest_loss = min(losing_pnls) if losing_pnls else 0

    # Fees and slippage
    total_fees = sum(t.fee for t in trades if hasattr(t, 'fee'))
    total_slippage = sum(abs(t.slippage) for t in trades if hasattr(t, 'slippage'))

    # Expectancy
    expectancy = np.mean(pnls) if pnls else 0

    # Risk/reward ratio
    risk_reward = abs(avg_win / avg_loss) if avg_loss != 0 else float('inf')

    # Average trade duration (would need entry/exit matching)
    avg_duration_hours = 0.0

    return {
        "total_trades": total_trades,
        "winning_trades": winning_trades,
        "losing_trades": losing_trades,
        "win_rate": win_rate,
        "profit_factor": profit_factor if profit_factor != float('inf') else 999.99,
        "avg_win": float(avg_win),
        "avg_loss": float(avg_loss),
        "avg_win_pct": avg_win_pct,
        "avg_loss_pct": avg_loss_pct,
        "largest_win": float(largest_win),
        "largest_loss": float(largest_loss),
        "total_fees": float(total_fees),
        "total_slippage": float(total_slippage),
        "expectancy": float(expectancy),
        "risk_reward_ratio": risk_reward if risk_reward != float('inf') else 999.99,
        "avg_duration_hours": avg_duration_hours,
    }


def calculate_rolling_metrics(
    equity_curve: list[tuple[datetime, float]],
    window_days: int = 30,
) -> list[dict[str, Any]]:
    """Calculate rolling performance metrics.

    Args:
        equity_curve: List of (timestamp, equity) tuples.
        window_days: Rolling window size in days.

    Returns:
        List of metrics dictionaries with timestamps.
    """
    if len(equity_curve) < 2:
        return []

    results = []
    window_delta = timedelta(days=window_days)

    # Convert to arrays
    timestamps = [e[0] for e in equity_curve]
    equity_values = np.array([e[1] for e in equity_curve])

    for i, (ts, eq) in enumerate(equity_curve):
        # Find window start
        window_start = ts - window_delta
        window_indices = [
            j for j, t in enumerate(timestamps[:i + 1])
            if t >= window_start
        ]

        if len(window_indices) < 2:
            continue

        window_equity = equity_values[window_indices]
        window_returns = np.diff(window_equity) / window_equity[:-1]
        window_returns = window_returns[np.isfinite(window_returns)]

        if len(window_returns) == 0:
            continue

        # Calculate rolling metrics
        rolling_return = (window_equity[-1] / window_equity[0]) - 1
        rolling_vol = np.std(window_returns) * np.sqrt(252)
        rolling_sharpe = (
            np.mean(window_returns) / np.std(window_returns) * np.sqrt(252)
            if np.std(window_returns) > 0 else 0
        )

        # Rolling drawdown
        running_max = np.maximum.accumulate(window_equity)
        drawdowns = (running_max - window_equity) / running_max
        rolling_max_dd = float(np.max(drawdowns))

        results.append({
            "timestamp": ts,
            "equity": float(eq),
            "rolling_return": float(rolling_return),
            "rolling_volatility": float(rolling_vol),
            "rolling_sharpe": float(rolling_sharpe),
            "rolling_max_drawdown": rolling_max_dd,
        })

    return results
