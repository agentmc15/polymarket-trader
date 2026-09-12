#!/usr/bin/env python
"""Live probe: Kalshi candlestick retention and the no-look-ahead facts
`app/venues/kalshi/candles.py` (T1) is built on (PLAN.md D8: "each task
that reads a venue field has a live probe in its verify command").

Answers three questions the retrospective replay depends on, against a
LIVE sample, never a fixture:

  1. RETENTION. Does the venue still serve candles for a market that
     closed >= 60 days ago, at both 60-minute and 1-minute granularity?
     PLAN.md Risks: "If retention is shorter, T3 samples only from
     closes inside it and the report states the reduced temporal-split
     power; do not silently widen `--days`." This is the check that
     decides which branch applies.
  2. `end_period_ts` IS THE PERIOD END, not the start. Consecutive
     hourly candles' `end_period_ts` must differ by EXACTLY 3600
     seconds -- the fact `candle_at_or_before`'s no-look-ahead guarantee
     rests on (a candle satisfying `end_ts <= cutoff` had already
     closed, only because `end_ts` is where it closed).
  3. `price.*` IS ABSENT (not zeroed) on a candle with no trades
     (`volume_fp == "0.00"`). `Candle` reads that as `px_* = None`;
     this confirms the venue behaves that way TODAY, not only in the
     hand-written fixtures `test_kalshi_candles.py` was written against.

Read-only: GET only, no order-placement code path exists here
(GUARDRAILS.md §1.1/§1.4 -- read-only public/signed GETs are the only
network this script performs).

Exit 0 on success. Exit 1 if fewer than 10 hourly candles came back for
the probed window (the retention question could not even be asked), or
if no settled market with volume_fp > `_MIN_VOLUME` could be found at
all.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import timedelta

from app.utils.time import utcnow
from app.venues.kalshi.adapter import KalshiAdapter
from app.venues.kalshi.candles import Candle, fetch_candles, series_for
from app.venues.types import VenueMarket

#: A market must have closed at least this long ago to exercise the
#: retention question T3's temporal hold-out depends on (TASKS.md T1
#: brief: "closing >= 60 days ago").
_MIN_AGE_DAYS = 60

#: Minimum lifetime volume for a candidate market -- below this a
#: market's candle history is mostly empty periods and says nothing
#: useful about retention (TASKS.md T1 brief: "volume_fp > 2000").
_MIN_VOLUME = 2000.0

#: Below this many hourly candles for the probed window, the retention
#: question could not even be asked (TASKS.md T1 brief).
_MIN_HOURLY_CANDLES = 10


def _volume_fp(market: VenueMarket) -> float:
    """`volume_fp` read directly off the raw payload (GUARDRAILS.md §3.2:
    the venue's own field name, never a fixture's)."""
    try:
        return float(market.raw.get("volume_fp") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _pick_market(markets: list[VenueMarket]) -> VenueMarket | None:
    """Pick the settled market this probe measures retention against.

    Prefers the candidate that closed AS RECENTLY AS POSSIBLE while
    still being `>= _MIN_AGE_DAYS` old -- the tightest live test of the
    retention boundary T3 depends on. Falls back to the single
    oldest-closing candidate when nothing qualifies that old (TASKS.md
    T1 brief: "if none exists, the oldest available").

    Args:
        markets: Settled `VenueMarket`s, any order.

    Returns:
        VenueMarket | None: The chosen market, or `None` if no settled
            market anywhere clears `_MIN_VOLUME`.
    """
    cutoff = utcnow() - timedelta(days=_MIN_AGE_DAYS)
    candidates = [
        m for m in markets if m.result in ("yes", "no") and _volume_fp(m) > _MIN_VOLUME
    ]
    if not candidates:
        return None
    old_enough = [m for m in candidates if m.close_time <= cutoff]
    if old_enough:
        return max(old_enough, key=lambda m: m.close_time)
    return min(candidates, key=lambda m: m.close_time)


async def _run() -> int:
    adapter = KalshiAdapter()
    print("listing settled Kalshi markets (walks /events?status=settled)...")
    markets = await adapter.list_markets(status="resolved")
    print(f"{len(markets)} settled markets listed")

    chosen = _pick_market(markets)
    if chosen is None:
        print(f"FAIL: no settled market with volume_fp > {_MIN_VOLUME:.0f} found")
        return 1

    age_days = (utcnow() - chosen.close_time).days
    old_enough = age_days >= _MIN_AGE_DAYS
    print(
        f"probing {chosen.market_id} (series={series_for(chosen)}): "
        f"closed {chosen.close_time.isoformat()} "
        f"({age_days} days ago, {'>=' if old_enough else '<'} {_MIN_AGE_DAYS}), "
        f"volume_fp={_volume_fp(chosen):.2f}"
    )
    if not old_enough:
        print(
            f"NOTE: no market closing >= {_MIN_AGE_DAYS} days ago cleared "
            f"volume_fp > {_MIN_VOLUME:.0f} -- using the oldest available "
            "candidate instead (TASKS.md T1 brief fallback)"
        )

    window_start = chosen.close_time - timedelta(days=1)
    window_end = chosen.close_time

    hourly = await fetch_candles(
        adapter,
        series=series_for(chosen),
        ticker=chosen.market_id,
        start=window_start,
        end=window_end,
        interval_minutes=60,
    )
    minute = await fetch_candles(
        adapter,
        series=series_for(chosen),
        ticker=chosen.market_id,
        start=window_start,
        end=window_end,
        interval_minutes=1,
    )

    print(f"\nwindow: {window_start.isoformat()} -> {window_end.isoformat()} (day before close)")
    print(f"RETENTION 60m: {len(hourly)} candles returned")
    print(f"RETENTION  1m: {len(minute)} candles returned")

    # Deliberately asymmetric lengths (`hourly[1:]` is one shorter) -- this
    # is the standard pairwise-consecutive-elements idiom, not a mistake
    # `strict=True` should catch.
    diffs = [b.end_ts - a.end_ts for a, b in zip(hourly, hourly[1:], strict=False)]
    all_3600 = bool(diffs) and all(d == 3600 for d in diffs)
    print(
        "end_period_ts spacing (consecutive 60m candles): "
        + (
            "ALL exactly 3600s"
            if all_3600
            else f"NOT uniform -- sample diffs {diffs[:5]}"
            if diffs
            else "n/a (fewer than 2 candles)"
        )
    )

    zero_volume: Candle | None = next(
        (c for c in (hourly + minute) if c.volume == 0.0), None
    )
    if zero_volume is None:
        print("price.* on a zero-volume candle: none observed in this window")
    else:
        absent = zero_volume.px_close is None
        print(
            f"price.* on a zero-volume candle (end_ts={zero_volume.end_ts}): "
            + (
                "ABSENT (px_close is None), as expected"
                if absent
                else f"PRESENT -- px_close={zero_volume.px_close!r} (unexpected)"
            )
        )

    if len(hourly) < _MIN_HOURLY_CANDLES:
        print(f"\nFAIL: only {len(hourly)} hourly candles returned, need >= {_MIN_HOURLY_CANDLES}")
        return 1
    print("\nOK")
    return 0


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
