"""`app.scripts.collection_loop` (mm-proveout T26): the loop calls the two
production coroutines through `run_async_task`, survives a failing tick,
and stops when told.

No database, no network: `run_collect_books` and `collection_health._run`
are replaced with coroutine fakes on the loop module's own namespace --
the loop must reference them through its module globals for that to hold,
which is itself the property under test (a loop that imported them into a
closure could not be patched, and could not be told apart from a loop
that silently called something else).
"""
from __future__ import annotations

import json

import pytest

from app.scripts import collection_loop


def _json_lines(out: str) -> list[dict]:
    return [json.loads(line) for line in out.splitlines() if line.startswith("{")]


@pytest.fixture(autouse=True)
def _no_signal_handlers(monkeypatch):
    """`signal.signal` only works in the main thread and pytest-xdist or an
    IDE runner may not be there; the handlers are not what these tests
    exercise."""
    monkeypatch.setattr(collection_loop.signal, "signal", lambda *_a, **_k: None)


class TestOnce:
    def test_runs_collect_then_health_and_exits_zero(self, monkeypatch, capsys):
        calls: list[object] = []

        async def fake_collect():
            calls.append("collect")
            return {"mode": "paper", "venues": ["kalshi"], "written": {"kalshi": 7}}

        async def fake_health(hours, out):
            calls.append(("health", hours, out))
            return 0

        monkeypatch.setattr(collection_loop, "run_collect_books", fake_collect)
        monkeypatch.setattr(collection_loop.collection_health, "_run", fake_health)

        rc = collection_loop.main(["--once", "--health-hours", "2.5"])

        assert rc == 0
        assert calls == ["collect", ("health", 2.5, None)]
        lines = _json_lines(capsys.readouterr().out)
        assert [line["tick"] for line in lines] == ["start", "collect", "health"]
        collect = lines[1]
        assert collect["ok"] is True
        assert collect["written"] == {"kalshi": 7}
        assert lines[2]["ok"] is True and lines[2]["exit_code"] == 0

    def test_collect_failure_is_a_line_not_a_crash_and_health_still_runs(
        self, monkeypatch, capsys
    ):
        async def fake_collect():
            raise RuntimeError("preflight said no")

        health_ran = []

        async def fake_health(hours, out):
            health_ran.append(True)
            return 0

        monkeypatch.setattr(collection_loop, "run_collect_books", fake_collect)
        monkeypatch.setattr(collection_loop.collection_health, "_run", fake_health)

        rc = collection_loop.main(["--once"])

        assert rc == 1, "--once reports the collect tick's failure in its exit code"
        assert health_ran == [True]
        lines = _json_lines(capsys.readouterr().out)
        collect = next(line for line in lines if line["tick"] == "collect")
        assert collect["ok"] is False
        assert collect["error"] == "RuntimeError"
        assert "preflight said no" in collect["message"]

    def test_unhealthy_exit_code_is_reported_not_raised(self, monkeypatch, capsys):
        async def fake_collect():
            return {"written": {}}

        async def fake_health(hours, out):
            return 2

        monkeypatch.setattr(collection_loop, "run_collect_books", fake_collect)
        monkeypatch.setattr(collection_loop.collection_health, "_run", fake_health)

        rc = collection_loop.main(["--once"])

        assert rc == 0, "--once's exit code is the collect tick's, not health's"
        health = next(
            line for line in _json_lines(capsys.readouterr().out) if line["tick"] == "health"
        )
        assert health["ok"] is False and health["exit_code"] == 2


class TestLoop:
    def test_survives_a_failing_tick_and_stops_at_max_ticks(self, monkeypatch, capsys):
        seen = {"collect": 0, "health": 0}

        async def fake_collect():
            seen["collect"] += 1
            if seen["collect"] == 1:
                raise RuntimeError("first tick fails")
            return {"written": {"polymarket": 1}}

        async def fake_health(hours, out):
            seen["health"] += 1
            return 0

        monkeypatch.setattr(collection_loop, "run_collect_books", fake_collect)
        monkeypatch.setattr(collection_loop.collection_health, "_run", fake_health)

        rc = collection_loop.main(
            [
                "--max-ticks", "3",
                "--collect-interval-s", "0",
                "--health-interval-s", "1000",
            ]
        )

        assert rc == 0
        assert seen["collect"] == 3, "a failing tick did not stop the loop"
        assert seen["health"] == 1, "health runs once at start, then not again inside 1000s"
        lines = _json_lines(capsys.readouterr().out)
        collect_ok = [line["ok"] for line in lines if line["tick"] == "collect"]
        assert collect_ok == [False, True, True]
        assert lines[-1]["tick"] == "stop" and lines[-1]["collect_ticks"] == 3

    def test_stop_flag_ends_the_loop(self, monkeypatch, capsys):
        async def fake_collect():
            # Simulate a SIGTERM arriving mid-run: the handler only sets a
            # flag, and the loop must notice it at the next check.
            collection_loop._stop_requested = True
            return {"written": {}}

        async def fake_health(hours, out):
            return 0

        monkeypatch.setattr(collection_loop, "run_collect_books", fake_collect)
        monkeypatch.setattr(collection_loop.collection_health, "_run", fake_health)

        rc = collection_loop.main(["--collect-interval-s", "0", "--health-interval-s", "1000"])

        assert rc == 0
        stop = _json_lines(capsys.readouterr().out)[-1]
        assert stop["tick"] == "stop" and stop["signalled"] is True
