"""T29 defect 1 — `unmarked_positions` must survive the persistence boundary.

`BacktestResult.unmarked_positions` exists so that a PARTLY FICTIONAL
equity curve announces itself. When the engine cannot resolve a mark
price for an open position it carries that position at its ENTRY price
(`Portfolio.total_equity`), which is indistinguishable from a position
that simply has not moved — so `equity_curve`/`final_value` stop being
market numbers and nothing in the figure itself says so.

The engine logs each id once at WARNING and publishes them on the
in-process result. That is only half the fix: `build_report()`
(`app/tasks/backtesting.py`) is what writes a run's qualifications into
`BacktestRun.report`, and until this task it wrote only `depth_source`
and `fill_at` plus the counters — so the ids were computed, logged, and
then DROPPED at the boundary. `GET /backtests/{id}` could not return
them and no UI could show them, even though
`frontend/src/components/backtesting/BacktestResults.tsx` already
renders `report.unmarked_positions` defensively (and
`frontend/src/types/index.ts` already types it optional).

These tests assert the field survives all the way to a re-read database
row and out through the API — not merely that `build_report()` returns
a dict containing the key.
"""
from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.backtest_run import BacktestRun, BacktestRunStatus
from app.services.backtesting import BacktestConfig, BacktestResult
from app.tasks.backtesting import _populate_run_from_result, build_report
from app.utils.time import utcnow

#: Two position ids in the engine's own `f"{venue}:{market}:{outcome}"`
#: shape (see `tests/backtesting/test_outcome_identity.py`, which asserts
#: the engine produces exactly this form).
UNMARKED = ("polymarket:M:trump", "kalshi:KXPRES:YES")

START = utcnow().replace(microsecond=0) - timedelta(days=7)
END = START + timedelta(days=7)


def make_result(unmarked: tuple[str, ...] = UNMARKED) -> BacktestResult:
    """Build a minimal completed `BacktestResult` carrying `unmarked`.

    Args:
        unmarked: The `unmarked_positions` tuple to publish on the
            result.

    Returns:
        BacktestResult: A two-point, no-trade run — everything except
            `unmarked_positions` is deliberately uninteresting, so a
            failure can only be about the field under test.
    """
    return BacktestResult(
        config=BacktestConfig(start_date=START, end_date=END, initial_capital=10_000.0),
        strategy_name="settlement_edge",
        strategy_config={},
        start_time=START,
        end_time=END,
        initial_capital=10_000.0,
        final_value=10_020.0,
        total_return=0.002,
        equity_curve=[(START, 10_000.0), (END, 10_020.0)],
        trades=[],
        positions_final={},
        depth_source="recorded",
        fill_at="next",
        unmarked_positions=unmarked,
    )


def test_build_report_carries_unmarked_positions_as_json_primitives() -> None:
    """The ids reach the report payload, as a `list` of `str`.

    `BacktestResult.unmarked_positions` is a `tuple`, and `report` is
    written straight into a JSON/JSONB column — so the payload must be a
    list, not a tuple, exactly as `undeclared_zero_fee_markets` already
    is. Asserted structurally (and not merely `== list(UNMARKED)`) so a
    future change that persisted, say, a count instead of the ids is
    caught here.
    """
    report = build_report(make_result())

    assert report["unmarked_positions"] == ["polymarket:M:trump", "kalshi:KXPRES:YES"]
    assert isinstance(report["unmarked_positions"], list)
    assert all(isinstance(pid, str) for pid in report["unmarked_positions"])
    # The labels that were already persisted are still persisted — this
    # is an addition, not a replacement.
    assert report["depth_source"] == "recorded"
    assert report["fill_at"] == "next"


def test_an_all_marked_run_reports_an_empty_list_not_a_missing_key() -> None:
    """Empty is the EXPECTED state, and it must be stated, not omitted.

    A missing key reads as "this run predates the field" (exactly how
    `frontend/src/types/index.ts` documents an empty `{}` report), which
    is a different claim from "every position in this run had a real
    mark price". The second claim is the one a clean run is entitled to
    make, so the key is always present.
    """
    report = build_report(make_result(unmarked=()))

    assert "unmarked_positions" in report
    assert report["unmarked_positions"] == []


async def test_unmarked_positions_survive_the_database_round_trip(
    test_session: AsyncSession,
) -> None:
    """The ids are readable from a re-queried `BacktestRun.report` row.

    This is the boundary the defect lived at: `_populate_run_from_result`
    (shared by the single-run task and by every child row a T22 sweep
    writes) is the ONE place that fills a run's result columns, so a
    field it does not write is unrecoverable once the Celery task's
    process is gone. The row is committed and then re-read through a
    fresh `select()` rather than asserted on the in-memory object, so a
    value that only lives in Python attributes and never made it through
    the JSON column would fail here.
    """
    run = BacktestRun(
        strategy_name="settlement_edge",
        strategy_config={},
        start_date=START,
        end_date=END,
        initial_capital=10_000.0,
        fee_rate=0.0,
        status=BacktestRunStatus.COMPLETED,
    )
    _populate_run_from_result(run, make_result(), 10_000.0)
    test_session.add(run)
    await test_session.commit()
    run_id = run.id
    test_session.expunge_all()

    reread = (
        await test_session.execute(select(BacktestRun).where(BacktestRun.id == run_id))
    ).scalar_one()

    assert reread.report["unmarked_positions"] == [
        "polymarket:M:trump",
        "kalshi:KXPRES:YES",
    ]
    # And the number the ids qualify is right there beside them: 10,020
    # is not a market figure while that list is non-empty.
    assert reread.final_value == pytest.approx(10_020.0, abs=1e-9)


async def test_get_backtest_returns_unmarked_positions_in_its_report(
    test_session: AsyncSession,
    client: AsyncClient,
) -> None:
    """`GET /backtests/{id}` surfaces the ids the frontend already reads.

    `BacktestResults.tsx` renders an "N position(s) unmarked — equity
    curve partly fictional" badge whenever `report.unmarked_positions` is
    a non-empty array. That code path could never fire before this task
    because the API had nothing to send it; this asserts the API now
    does, in the array shape the component checks with `Array.isArray`.
    """
    run = BacktestRun(
        strategy_name="settlement_edge",
        strategy_config={},
        start_date=START,
        end_date=END,
        initial_capital=10_000.0,
        fee_rate=0.0,
        status=BacktestRunStatus.COMPLETED,
    )
    _populate_run_from_result(run, make_result(), 10_000.0)
    test_session.add(run)
    await test_session.commit()

    response = await client.get(f"/api/v1/backtests/{run.id}")

    assert response.status_code == 200
    report = response.json()["report"]
    assert report["unmarked_positions"] == [
        "polymarket:M:trump",
        "kalshi:KXPRES:YES",
    ]
    assert len(report["unmarked_positions"]) == 2
