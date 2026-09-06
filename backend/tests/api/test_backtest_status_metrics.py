"""`GET /backtests/{id}` must report every metric it can, and NOTHING it cannot (T33).

`get_backtest_status` built a `TradeMetrics` and a `RiskMetrics` and
filled 4 of their 15 combined fields; the other 11 left the endpoint as
the pydantic default `0.0`. That is not a small omission — a metric that
was never computed rendering as `0.00` is indistinguishable from a real
zero, which is why the frontend deliberately refused to render any of
the eleven (`frontend/src/types/index.ts`'s `TradeMetrics`/`RiskMetrics`
doc comments) and why GUARDRAILS.md §1.7 exists.

The eleven ARE computable: `app.tasks.backtesting._populate_run_from_
result` runs `calculate_metrics` over the finished backtest and keeps
only six of its values, but it persists that function's INPUTS in full
(`BacktestRun.equity_curve` and `.trades_list`), so the route can run
the same function over the same inputs. These tests fix the numbers by
hand (GUARDRAILS.md §5) and, just as importantly, fix what happens when
the inputs cannot support the calculation: `None`, never `0.0`.

The equity curve below is deliberately a four-step path over one year,
so every intermediate quantity is exact:

    2024-01-01  10000.00
    2024-04-01  11000.00   +10%
    2024-07-01   9900.00   -10%
    2024-10-01  11880.00   +20%
    2025-01-01   9504.00   -20%
"""
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.backtest_run import BacktestRun, BacktestRunStatus

#: The equity path above, in the shape `_populate_run_from_result` writes.
EQUITY_CURVE: list[dict[str, Any]] = [
    {"timestamp": "2024-01-01T00:00:00+00:00", "equity": 10000.0},
    {"timestamp": "2024-04-01T00:00:00+00:00", "equity": 11000.0},
    {"timestamp": "2024-07-01T00:00:00+00:00", "equity": 9900.0},
    {"timestamp": "2024-10-01T00:00:00+00:00", "equity": 11880.0},
    {"timestamp": "2025-01-01T00:00:00+00:00", "equity": 9504.0},
]

#: Five persisted fills: one OPENING (`pnl` null, excluded from every
#: win/loss statistic) and four closed round trips at +60, -20, +40, -80.
TRADES_LIST: list[dict[str, Any]] = [
    {
        "timestamp": "2024-01-02T00:00:00+00:00",
        "market_id": "PM-1",
        "outcome": "YES",
        "side": "BUY",
        "price": 0.40,
        "size": 100.0,
        "fee": 1.0,
        "pnl": None,
        "signal_confidence": 0.8,
    },
    {
        "timestamp": "2024-04-02T00:00:00+00:00",
        "market_id": "PM-1",
        "outcome": "YES",
        "side": "SELL",
        "price": 0.60,
        "size": 100.0,
        "fee": 1.0,
        "pnl": 60.0,
        "signal_confidence": 0.8,
    },
    {
        "timestamp": "2024-07-02T00:00:00+00:00",
        "market_id": "PM-2",
        "outcome": "NO",
        "side": "SELL",
        "price": 0.30,
        "size": 100.0,
        "fee": 1.0,
        "pnl": -20.0,
        "signal_confidence": 0.5,
    },
    {
        "timestamp": "2024-10-02T00:00:00+00:00",
        "market_id": "PM-3",
        "outcome": "YES",
        "side": "SELL",
        "price": 0.70,
        "size": 100.0,
        "fee": 1.0,
        "pnl": 40.0,
        "signal_confidence": 0.6,
    },
    {
        "timestamp": "2024-12-02T00:00:00+00:00",
        "market_id": "PM-4",
        "outcome": "NO",
        "side": "SELL",
        "price": 0.20,
        "size": 100.0,
        "fee": 1.0,
        "pnl": -80.0,
        "signal_confidence": 0.4,
    },
]

#: Every field of `TradeMetrics` + `RiskMetrics`, so a test can assert on
#: the whole surface rather than the handful it remembered to name.
TRADE_METRIC_FIELDS = (
    "total_trades",
    "winning_trades",
    "losing_trades",
    "win_rate",
    "profit_factor",
    "avg_win",
    "avg_loss",
    "largest_win",
    "largest_loss",
)
RISK_METRIC_FIELDS = (
    "sharpe_ratio",
    "sortino_ratio",
    "max_drawdown",
    "max_drawdown_pct",
    "volatility",
    "var_95",
)


def _completed_run(**overrides: Any) -> BacktestRun:
    """A COMPLETED `BacktestRun` populated the way the Celery task does.

    The four column-backed values are what
    `_populate_run_from_result` would have written for this curve and
    these trades: 4 closed trades, 2 of them winners, and the
    `sharpe_ratio`/`max_drawdown` it computed at run time.

    Args:
        **overrides: Column overrides.

    Returns:
        BacktestRun: An uncommitted row.
    """
    fields: dict[str, Any] = {
        "strategy_name": "binary_complement_arbitrage",
        "strategy_config": {},
        "start_date": datetime(2024, 1, 1, tzinfo=UTC),
        "end_date": datetime(2025, 1, 1, tzinfo=UTC),
        "initial_capital": 10000.0,
        "fee_rate": 0.0,
        "status": BacktestRunStatus.COMPLETED,
        "final_value": 9504.0,
        "total_return": -0.0496,
        # mean(excess) / std(daily) * sqrt(252) over the four daily
        # returns, risk-free 4%: (0 - 0.04/252) / 0.15811388 * 15.874508
        "sharpe_ratio": -0.015936381457792415,
        # (11880 - 9504) / 11880 = 0.20
        "max_drawdown": 0.20,
        # 2 winners out of 4 closed trades
        "win_rate": 0.5,
        "total_trades": 4,
        "equity_curve": EQUITY_CURVE,
        "trades_list": TRADES_LIST,
        "report": {"depth_source": "recorded", "fill_at": "next"},
        "completed_at": datetime(2025, 1, 2, tzinfo=UTC),
    }
    fields.update(overrides)
    return BacktestRun(**fields)


async def _persist(session: AsyncSession, run: BacktestRun) -> int:
    """Commit `run` and return its id."""
    session.add(run)
    await session.commit()
    await session.refresh(run)
    return run.id


async def test_a_completed_run_reports_all_fifteen_metrics(
    client: AsyncClient,
    test_session: AsyncSession,
) -> None:
    """Every `TradeMetrics`/`RiskMetrics` field is populated, by hand-checked math.

    Daily returns are +0.10, -0.10, +0.20, -0.20 (one per curve step),
    and every expectation below is computed from those four numbers and
    the four closed trades' P&L, not from the endpoint.
    """
    backtest_id = await _persist(test_session, _completed_run())

    response = await client.get(f"/api/v1/backtests/{backtest_id}")

    assert response.status_code == 200, response.text
    body = response.json()
    trade = body["trade_metrics"]
    risk = body["risk_metrics"]

    for field in TRADE_METRIC_FIELDS:
        assert trade[field] is not None, field
    for field in RISK_METRIC_FIELDS:
        assert risk[field] is not None, field

    # --- from the columns the run itself recorded -------------------
    assert trade["total_trades"] == 4
    assert trade["win_rate"] == 0.5
    assert risk["sharpe_ratio"] == pytest.approx(-0.015936381, rel=1e-6)
    assert risk["max_drawdown"] == pytest.approx(0.20)

    # --- recomputed from `trades_list`: P&L +60, -20, +40, -80 ------
    assert trade["winning_trades"] == 2
    assert trade["losing_trades"] == 2
    # gross profit 60 + 40 = 100; gross loss |-20 - 80| = 100; 100/100
    assert trade["profit_factor"] == pytest.approx(1.0)
    # (60 + 40) / 2 = 50; (-20 + -80) / 2 = -50
    assert trade["avg_win"] == pytest.approx(50.0)
    assert trade["avg_loss"] == pytest.approx(-50.0)
    assert trade["largest_win"] == pytest.approx(60.0)
    assert trade["largest_loss"] == pytest.approx(-80.0)

    # --- recomputed from `equity_curve` -----------------------------
    # Peak 11880 -> trough 9504: (11880 - 9504) / 11880 = 0.20 -> 20%.
    assert risk["max_drawdown_pct"] == pytest.approx(20.0)
    # Population std of [0.1, -0.1, 0.2, -0.2] is
    # sqrt((0.01 + 0.01 + 0.04 + 0.04) / 4) = sqrt(0.025) = 0.15811388,
    # annualized by sqrt(252) = 15.8745079 -> 2.50998008.
    assert risk["volatility"] == pytest.approx(2.50998008, rel=1e-6)
    # 5th percentile of the sorted returns [-0.2, -0.1, 0.1, 0.2] with
    # numpy's linear interpolation: index (4-1)*0.05 = 0.15, so
    # -0.2 + 0.15 * ((-0.1) - (-0.2)) = -0.185.
    assert risk["var_95"] == pytest.approx(-0.185)
    # Downside std is over [-0.1, -0.2]: 0.05 * 15.8745079 = 0.79372539.
    # Annualized return over 366 days: 0.9504 ** (365.25 / 366) - 1 =
    # -0.04950092. Sortino = (-0.04950092 - 0.04) / 0.79372539.
    assert risk["sortino_ratio"] == pytest.approx(-0.11276056, rel=1e-6)


async def test_a_run_with_no_usable_equity_curve_reports_none_not_zero(
    client: AsyncClient,
    test_session: AsyncSession,
) -> None:
    """A sweep PARENT row can honestly report nothing, and must say so.

    `_run_sweep_async` never writes an `equity_curve`/`trades_list` on
    the parent — a sweep is many results, not one, and the per-level
    numbers live on its children. So there is nothing to recompute from,
    and every recomputed field has to come back `null`. Returning `0.0`
    would tell a reader this sweep had zero volatility, a zero drawdown
    and no winning trades, none of which was measured.

    The SAME sweep-parent row also leaves `win_rate`/`sharpe_ratio`/
    `max_drawdown` NULL in the database (T36): before T36 those three
    were coerced with `backtest.win_rate or 0.0` and would have left
    this endpoint as `0.0`, indistinguishable from a real zero win rate
    on a run that never traded. `total_trades` is the one field that
    genuinely IS `0`-or-more by construction (a `BacktestRun` column
    with a non-null default), so it alone is asserted as a real number.
    """
    backtest_id = await _persist(
        test_session,
        _completed_run(
            strategy_name="sweep:binary_complement_arbitrage",
            equity_curve=[],
            trades_list=[],
            final_value=None,
            total_return=None,
            sharpe_ratio=None,
            max_drawdown=None,
            win_rate=None,
            total_trades=17,
            report={"edge_decay": {}, "child_backtest_ids": [2, 3]},
        ),
    )

    response = await client.get(f"/api/v1/backtests/{backtest_id}")

    assert response.status_code == 200, response.text
    trade = response.json()["trade_metrics"]
    risk = response.json()["risk_metrics"]

    recomputed_only = [
        trade["winning_trades"],
        trade["losing_trades"],
        trade["profit_factor"],
        trade["avg_win"],
        trade["avg_loss"],
        trade["largest_win"],
        trade["largest_loss"],
        risk["sortino_ratio"],
        risk["max_drawdown_pct"],
        risk["volatility"],
        risk["var_95"],
    ]
    assert recomputed_only == [None] * 11

    # T36: the three NULL columns come through as `null`, not `0.0`.
    assert trade["win_rate"] is None
    assert risk["sharpe_ratio"] is None
    assert risk["max_drawdown"] is None

    # `total_trades` still comes through, and the parent's own value
    # (summed across levels) is not overwritten by a recomputation that
    # would have said 0.
    assert trade["total_trades"] == 17


async def test_a_run_with_no_closed_trades_reports_none_for_the_win_loss_ratios(
    client: AsyncClient,
    test_session: AsyncSession,
) -> None:
    """Open fills are not closed round trips, and their statistics are not zero.

    With every `pnl` null, `calculate_metrics` takes its "no closing
    trades" branch and returns `0.0` for `profit_factor`/`avg_win`/
    `avg_loss`/`largest_win`/`largest_loss` — values it never computed.
    Those must be reported as `null`. `winning_trades`/`losing_trades`
    are different in kind: "no trade won and none lost" is a true
    statement about this run, so those stay `0`.
    """
    opening_only = [{**trade, "pnl": None} for trade in TRADES_LIST]

    backtest_id = await _persist(
        test_session,
        _completed_run(trades_list=opening_only, win_rate=0.0, total_trades=5),
    )

    response = await client.get(f"/api/v1/backtests/{backtest_id}")

    assert response.status_code == 200, response.text
    trade = response.json()["trade_metrics"]
    risk = response.json()["risk_metrics"]

    assert trade["winning_trades"] == 0
    assert trade["losing_trades"] == 0
    assert trade["profit_factor"] is None
    assert trade["avg_win"] is None
    assert trade["avg_loss"] is None
    assert trade["largest_win"] is None
    assert trade["largest_loss"] is None

    # The equity curve is untouched, so the risk metrics are still real.
    assert risk["volatility"] == pytest.approx(2.50998008, rel=1e-6)
    assert risk["max_drawdown_pct"] == pytest.approx(20.0)


async def test_a_run_with_genuine_zero_win_rate_sharpe_and_drawdown_reports_zero_not_none(
    client: AsyncClient,
    test_session: AsyncSession,
) -> None:
    """A MEASURED zero in the three column-backed fields must stay `0.0` (T36).

    T36 stops `get_backtest_status` coercing a NULL `win_rate`/
    `sharpe_ratio`/`max_drawdown` column with `... or 0.0` so that a
    NULL column can finally report `null` (see the sibling
    `..._reports_none_not_zero` test above). That change is only safe
    if it does not ALSO turn a genuine `0.0` into `null` — `x or 0.0`
    and a hypothetical `x if x else None` are both "falsy" checks that
    would treat a real `0.0` the same as a missing value. This asserts
    the pass-through the other direction: a run that genuinely measured
    zero on all three reports exactly `0.0`, not `null`.
    """
    backtest_id = await _persist(
        test_session,
        _completed_run(win_rate=0.0, sharpe_ratio=0.0, max_drawdown=0.0),
    )

    response = await client.get(f"/api/v1/backtests/{backtest_id}")

    assert response.status_code == 200, response.text
    trade = response.json()["trade_metrics"]
    risk = response.json()["risk_metrics"]

    assert trade["win_rate"] == 0.0
    assert trade["win_rate"] is not None
    assert risk["sharpe_ratio"] == 0.0
    assert risk["sharpe_ratio"] is not None
    assert risk["max_drawdown"] == 0.0
    assert risk["max_drawdown"] is not None


async def test_an_unfinished_run_reports_no_metrics_block_at_all(
    client: AsyncClient,
    test_session: AsyncSession,
) -> None:
    """A RUNNING backtest has no metrics yet, and says `null` rather than zeros.

    Unchanged by T33, asserted here so the nullability added to the
    individual fields cannot be mistaken for the block itself becoming
    optional-shaped.
    """
    backtest_id = await _persist(
        test_session,
        _completed_run(
            status=BacktestRunStatus.RUNNING,
            final_value=None,
            total_return=None,
            completed_at=None,
        ),
    )

    response = await client.get(f"/api/v1/backtests/{backtest_id}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["trade_metrics"] is None
    assert body["risk_metrics"] is None
