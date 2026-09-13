"""Run order-book collection on a timer -- and nothing else (mm-proveout T26).

`app/tasks/__init__.py`'s beat schedule fires five things: the arbitrage
scanner, the near-resolution scanner, execution reconciliation, event-link
proposals, and collection. Starting `celery beat` starts all five. The
authorisation that unblocked forward collection on 2026-09-12 covered the
COLLECTOR, so this module runs the collector's two coroutines on their
configured intervals and no others.

It is deliberately NOT a second implementation of anything. Each tick calls
the SAME coroutine the Celery wrapper calls -- `run_collect_books()` from
`app.tasks.collection`, `collection_health._run` from
`app.scripts.collection_health` -- through the SAME `run_async_task`
(`app.database`), for the reason that function's own docstring gives: a
bare `asyncio.run` per tick leaves the module-level engine's pooled asyncpg
connections bound to a closed loop, and the second tick raises. The only
production code this loop does not exercise is the one-line Celery task
wrapper and the broker; that is stated in TASKS.md rather than implied
away.

THE INTERVAL IS A GAP, NOT A PERIOD. Ticks run sequentially on one thread,
and `--collect-interval-s` is the idle time between the END of one pass and
the START of the next. That is the deliberate choice, not an accident of
the arithmetic: the first real pass (2026-09-12, both venues, 1,256 rows,
1,763 HTTP requests) took 239s against the 180s the config names -- the
110-125s that derived 180s was measured on a smaller Kalshi listing. A
start-to-start schedule at 180s would have run passes back to back at 100%
duty, ~7 requests/s sustained against Kalshi's measured ~10/s ceiling, with
no headroom. A gap schedule averages under half that. The cost is time
resolution -- the real cadence is pass time plus gap, ~7 minutes here, and
every report on this data must state the cadence it measured rather than
the config value.

Output is one JSON object per line on stdout, one line per tick, so a log
file of this loop can be read back by a script without parsing prose. A
failing tick emits its line with `"ok": false` and the exception's class
name, and the loop continues -- exactly what a beat does when a task
raises. `VenueEscalationError` (a venue writing zero rows for
`_ZERO_WRITE_ESCALATION_THRESHOLD` consecutive ticks) is therefore visible
in the log as a repeated line, not as a crash; the operator reads the log.
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from app.config import settings
from app.database import run_async_task
from app.scripts import collection_health
from app.tasks.collection import run_collect_books

#: Set by the signal handlers; checked once a second by the loop. Module
#: level so tests can reset it, matching how `app.tasks.collection` exposes
#: `_consecutive_zero_writes`.
_stop_requested = False


def _request_stop(signum: int, frame: Any) -> None:  # noqa: ARG001 - signal handler signature
    global _stop_requested
    _stop_requested = True


def _emit(record: dict[str, Any]) -> None:
    """Write one JSON line. `default=str` so a `datetime` or exception in a
    result payload cannot make the LOG line the thing that fails."""
    record = {"ts": datetime.now(UTC).isoformat(timespec="seconds"), **record}
    sys.stdout.write(json.dumps(record, default=str) + "\n")
    sys.stdout.flush()


def run_collect_tick() -> dict[str, Any]:
    """One collection pass; never raises."""
    started = time.monotonic()
    try:
        result = run_async_task(run_collect_books())
    except Exception as exc:  # noqa: BLE001 - a tick's failure is a log line, not a crash
        return {
            "tick": "collect",
            "ok": False,
            "elapsed_s": round(time.monotonic() - started, 1),
            "error": type(exc).__name__,
            "message": str(exc)[:500],
        }
    return {
        "tick": "collect",
        "ok": True,
        "elapsed_s": round(time.monotonic() - started, 1),
        "written": result.get("written", {}),
        "venues": result.get("venues", []),
    }


def run_health_tick(hours: float, out: str | None) -> dict[str, Any]:
    """One health check; never raises. `collection_health._run` returns
    its CLI exit code (0 healthy, non-zero otherwise) and prints its own
    report, so `ok` here is that code being zero."""
    started = time.monotonic()
    try:
        code = run_async_task(collection_health._run(hours, out))
    except Exception as exc:  # noqa: BLE001 - same rule as the collect tick
        return {
            "tick": "health",
            "ok": False,
            "elapsed_s": round(time.monotonic() - started, 1),
            "error": type(exc).__name__,
            "message": str(exc)[:500],
        }
    return {
        "tick": "health",
        "ok": code == 0,
        "elapsed_s": round(time.monotonic() - started, 1),
        "exit_code": code,
        "hours": hours,
    }


def main(argv: Sequence[str] | None = None) -> int:
    global _stop_requested
    parser = argparse.ArgumentParser(
        description="Run book collection and collection health on their configured "
        "intervals, without the rest of the beat schedule."
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one collect tick and one health tick, then exit (non-zero if the "
        "collect tick failed).",
    )
    parser.add_argument(
        "--max-ticks",
        type=int,
        default=0,
        help="Stop after this many collect ticks (0 = run until signalled).",
    )
    parser.add_argument(
        "--collect-interval-s",
        type=float,
        default=settings.book_collection_interval_s,
        help="Seconds between collect ticks (default: settings.book_collection_interval_s).",
    )
    parser.add_argument(
        "--health-interval-s",
        type=float,
        default=settings.collection_health_interval_s,
        help="Seconds between health ticks (default: settings.collection_health_interval_s).",
    )
    parser.add_argument(
        "--health-hours",
        type=float,
        default=1.0,
        help="Lookback window passed to collection_health (default 1.0).",
    )
    parser.add_argument(
        "--health-out",
        type=str,
        default=None,
        help="Optional path collection_health writes its JSON report to each tick.",
    )
    args = parser.parse_args(argv)

    # The adapters log every HTTP request at INFO through the app's JSON
    # logging -- ~1,700 lines per pass, measured on the first real one.
    # That is right for a debugging session and wrong for a log that runs
    # for weeks; the tick lines below are the record, and a venue's
    # WARNING/ERROR lines still come through.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    _stop_requested = False
    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)

    _emit({
        "tick": "start",
        "ok": True,
        "mode": settings.trading_mode,
        "collect_interval_s": args.collect_interval_s,
        "health_interval_s": args.health_interval_s,
        "once": args.once,
        "max_ticks": args.max_ticks,
    })

    if args.once:
        collect = run_collect_tick()
        _emit(collect)
        _emit(run_health_tick(args.health_hours, args.health_out))
        return 0 if collect["ok"] else 1

    ticks = 0
    now = time.monotonic()
    next_collect = now
    next_health = now
    while not _stop_requested:
        now = time.monotonic()
        if now >= next_collect:
            _emit(run_collect_tick())
            ticks += 1
            next_collect = time.monotonic() + args.collect_interval_s
            if args.max_ticks and ticks >= args.max_ticks:
                break
        if now >= next_health:
            _emit(run_health_tick(args.health_hours, args.health_out))
            next_health = time.monotonic() + args.health_interval_s
        # Wake once a second so a signal is honoured promptly even on a
        # long interval; the arithmetic above, not this sleep, sets pace.
        time.sleep(min(1.0, max(0.0, min(next_collect, next_health) - time.monotonic())))

    _emit({"tick": "stop", "ok": True, "collect_ticks": ticks, "signalled": _stop_requested})
    return 0


if __name__ == "__main__":
    sys.exit(main())
