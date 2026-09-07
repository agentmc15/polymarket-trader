"""Measure whether venue prices are calibrated, and whether it is tradeable.

The question behind every non-arbitrage thesis in this kit: are prices
systematically wrong in a direction you can act on? The classic claim is
the FAVORITE-LONGSHOT BIAS — longshots overpriced, favorites underpriced.
This script measures it against settled markets and then asks the only
question that matters, which is whether acting on it survives the spread
and the fees.

THREE METHODOLOGICAL TRAPS, all of which produce a confident wrong
answer, and all of which this script is built to avoid.

1. THE SETTLEMENT PRICE IS POSTERIOR. A settled market's
   `last_price_dollars` is recorded at settlement: measured on live data,
   516 of 566 settled markets carried it within 5c of the outcome.
   Bucketing on that yields a perfect calibration curve that says nothing
   at all, because the "prediction" post-dates the event. Every price
   here therefore comes from the CANDLESTICK history at a fixed horizon
   BEFORE the market closed.

2. THE MID IS NOT TRADEABLE. Calibration measured at the mid can look
   exploitable while the trade loses money, because you buy at the ask
   and sell at the bid. On this sample the mean quoted spread is 16
   cents against calibration deviations of 3-9 cents, so pricing at the
   mid is the difference between a thesis and an artifact. The P&L here
   executes at the touch.

3. OUTCOMES INSIDE ONE EVENT ARE CORRELATED. The candidates in a race
   and the props on one game are not independent draws; treating them as
   such shrinks the intervals dishonestly. Intervals here are a cluster
   bootstrap resampling whole EVENTS.

Fees come from `KalshiFeeModel` and `Settings`, never a literal
(GUARDRAILS.md §1.5). Read-only: this script places no orders and has no
code path that could.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from app.config import Settings
from app.venues.fees import KalshiFeeModel
from app.venues.kalshi.adapter import KalshiAdapter
from app.venues.types import FeeSchedule

#: Price-bucket edges. Wider than a naive decile split at the tails on
#: purpose: the extremes are where the favorite-longshot claim lives, and
#: a 0.98-1.00 bucket holds enough observations to say something.
BUCKET_EDGES: tuple[float, ...] = (
    0.0, 0.05, 0.10, 0.20, 0.35, 0.50, 0.65, 0.80, 0.90, 0.95, 1.0
)

#: Minimum traded volume for a settled market to enter the sample. Below
#: this a "price" is one or two prints and carries no information.
MIN_VOLUME = 200.0

#: Events resampled per bootstrap replicate.
BOOTSTRAP_REPLICATES = 500

#: How far back to request candles. A market that traded for longer still
#: yields its horizon candle; one that traded for less is simply dropped.
CANDLE_LOOKBACK_DAYS = 45


def _float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _time(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def bucket_of(price: float) -> int:
    """Return the index of `price`'s bucket in `BUCKET_EDGES`."""
    for i in range(len(BUCKET_EDGES) - 1):
        if BUCKET_EDGES[i] <= price < BUCKET_EDGES[i + 1]:
            return i
    return len(BUCKET_EDGES) - 2


def cluster_bootstrap(
    sample: list[dict[str, Any]],
    statistic: Any,
    replicates: int = BOOTSTRAP_REPLICATES,
) -> tuple[float, float]:
    """A 95% interval that resamples whole EVENTS, not single markets.

    Markets sharing an `event` share an outcome — the candidates in one
    race, the props on one game. Resampling markets independently would
    treat those as independent evidence and report an interval narrower
    than the data supports.

    Args:
        sample: Observations, each carrying an `event` key.
        statistic: Callable taking a list of observations, returning a
            float.
        replicates: Bootstrap replicates.

    Returns:
        tuple[float, float]: The 2.5th and 97.5th percentiles, or
            `(nan, nan)` when there are too few distinct events to
            resample meaningfully.
    """
    by_event: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in sample:
        by_event[str(row["event"])].append(row)
    events = list(by_event)
    if len(events) < 5:
        return (math.nan, math.nan)
    values: list[float] = []
    for _ in range(replicates):
        drawn = [r for e in random.choices(events, k=len(events)) for r in by_event[e]]
        if drawn:
            values.append(statistic(drawn))
    if not values:
        return (math.nan, math.nan)
    values.sort()
    return values[int(0.025 * len(values))], values[int(0.975 * len(values))]


class Calibration:
    """Collects settled-market observations and scores them."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        self.model = KalshiFeeModel()
        self.schedule = FeeSchedule(
            taker_rate=self.settings.kalshi_taker_fee_rate,
            maker_rate=self.settings.kalshi_maker_fee_rate,
            source="settings",
        )

    # -- P&L at executable prices -------------------------------------

    def net_buy(self, row: dict[str, Any]) -> float:
        """P&L per contract from buying YES at the ask, held to settlement."""
        cost = row["ask"] + self.model.fee(row["ask"], 1.0, "taker", self.schedule)
        return row["outcome"] - cost

    def net_sell(self, row: dict[str, Any]) -> float:
        """P&L per contract from selling YES at the bid, held to settlement."""
        proceeds = row["bid"] - self.model.fee(row["bid"], 1.0, "taker", self.schedule)
        return proceeds - row["outcome"]

    # -- Collection ---------------------------------------------------

    async def collect(
        self, adapter: KalshiAdapter, sample_size: int, horizons_h: tuple[int, ...]
    ) -> list[dict[str, Any]]:
        """Sample settled markets and price each one before it resolved."""
        universe = await self._settled_universe(adapter)
        chosen = random.sample(universe, min(sample_size, len(universe)))
        semaphore = asyncio.Semaphore(8)
        rows: list[dict[str, Any]] = []
        for start in range(0, len(chosen), 250):
            batch = chosen[start : start + 250]
            done = await asyncio.gather(
                *(self._price(adapter, m, horizons_h, semaphore) for m in batch)
            )
            rows.extend(r for r in done if r)
        return rows

    async def _settled_universe(
        self, adapter: KalshiAdapter
    ) -> list[dict[str, Any]]:
        """Every settled market with a known result and real volume."""
        markets = await adapter.list_markets(status="resolved")
        universe = []
        for market in markets:
            if market.result not in ("yes", "no"):
                continue
            if (_float(market.raw.get("volume_fp")) or 0.0) < MIN_VOLUME:
                continue
            universe.append({
                "ticker": market.market_id,
                "series": str(market.raw.get("event_ticker") or "").rsplit("-", 1)[0]
                or str(market.market_id).split("-")[0],
                "event": market.event_id or market.market_id,
                "close": market.close_time,
                "outcome": 1.0 if market.result == "yes" else 0.0,
                "volume": _float(market.raw.get("volume_fp")),
                "title": market.question,
            })
        return universe

    async def _price(
        self,
        adapter: KalshiAdapter,
        market: dict[str, Any],
        horizons_h: tuple[int, ...],
        semaphore: asyncio.Semaphore,
    ) -> dict[str, Any] | None:
        """Quote this market at each horizon before its close."""
        close: datetime = market["close"]
        async with semaphore:
            try:
                body = await adapter._get(  # noqa: SLF001 - read-only history
                    f"/series/{market['series']}/markets/{market['ticker']}"
                    "/candlesticks",
                    params={
                        "start_ts": str(
                            int((close - timedelta(days=CANDLE_LOOKBACK_DAYS)).timestamp())
                        ),
                        "end_ts": str(int(close.timestamp())),
                        "period_interval": "1440",
                    },
                )
            except Exception:
                # A market whose series does not serve candles is simply
                # not observable; it is not an error in the study.
                return None
        candles = body.get("candlesticks") or []
        if not candles:
            return None
        out = {k: v for k, v in market.items() if k != "close"}
        out["close"] = close.isoformat()
        for hours in horizons_h:
            cutoff = (close - timedelta(hours=hours)).timestamp()
            picked = None
            for candle in candles:
                ts = _float(candle.get("end_period_ts"))
                if ts is not None and ts <= cutoff:
                    picked = candle
            bid = ask = None
            if picked is not None:
                bid = _float((picked.get("yes_bid") or {}).get("close_dollars"))
                ask = _float((picked.get("yes_ask") or {}).get("close_dollars"))
            out[f"bid_{hours}h"] = bid
            out[f"ask_{hours}h"] = ask
        return out

    # -- Scoring ------------------------------------------------------

    @staticmethod
    def observations(rows: list[dict[str, Any]], hours: int) -> list[dict[str, Any]]:
        """Keep rows quotable at `hours` before close, with a usable mid."""
        out = []
        for row in rows:
            bid, ask = row.get(f"bid_{hours}h"), row.get(f"ask_{hours}h")
            if bid is None or ask is None:
                continue
            mid = (bid + ask) / 2.0
            if not 0.0 < mid < 1.0:
                continue
            # `outcome` is the numeric settlement this study scores
            # against. Accept the venue's own `"yes"`/`"no"` spelling too,
            # so a cache collected before the numeric field existed still
            # reads — and so a row carrying NEITHER is dropped rather
            # than silently scored as a loss, which would bias every
            # bucket downward.
            outcome = row.get("outcome")
            if outcome is None:
                result = row.get("result")
                if result not in ("yes", "no"):
                    continue
                outcome = 1.0 if result == "yes" else 0.0
            out.append({**row, "outcome": float(outcome), "bid": bid,
                        "ask": ask, "mid": mid, "spread": ask - bid})
        return out

    def report(self, rows: list[dict[str, Any]], hours: int) -> None:
        """Print the calibration table and the net-P&L table."""
        obs = self.observations(rows, hours)
        if not obs:
            print(f"T-{hours}h: no observations")
            return
        print(
            f"\n=== T-{hours}h: {len(obs)} observations, "
            f"{len({r['event'] for r in obs})} events, "
            f"mean quoted spread {statistics.fmean(r['spread'] for r in obs):.4f} ==="
        )
        buckets: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in obs:
            buckets[bucket_of(row["mid"])].append(row)

        print(f"{'bucket':>12} {'n':>6} {'mid':>8} {'realized':>9} {'diff':>8}"
              f"  {'clustered 95% CI':>20}")
        for i in sorted(buckets):
            group = buckets[i]
            mid = statistics.fmean(r["mid"] for r in group)
            realized = statistics.fmean(r["outcome"] for r in group)
            lo, hi = cluster_bootstrap(
                group, lambda z: statistics.fmean(r["outcome"] for r in z)
            )
            tag = ""
            if not math.isnan(lo):
                tag = ("  overpriced" if hi < mid
                       else "  underpriced" if lo > mid else "")
            print(f"{BUCKET_EDGES[i]:.2f}-{BUCKET_EDGES[i+1]:<6.2f} {len(group):6d} "
                  f"{mid:8.4f} {realized:9.4f} {realized - mid:+8.4f}"
                  f"  [{lo:6.4f},{hi:6.4f}]{tag}")

        print(f"\n{'bucket':>12} {'n':>6} {'BUY at ask':>12} {'95% CI':>20}"
              f" {'SELL at bid':>12} {'95% CI':>20}")
        for i in sorted(buckets):
            group = buckets[i]
            buy = statistics.fmean(self.net_buy(r) for r in group)
            sell = statistics.fmean(self.net_sell(r) for r in group)
            blo, bhi = cluster_bootstrap(
                group, lambda z: statistics.fmean(self.net_buy(r) for r in z)
            )
            slo, shi = cluster_bootstrap(
                group, lambda z: statistics.fmean(self.net_sell(r) for r in z)
            )
            edge = ""
            if not math.isnan(blo) and blo > 0:
                edge = "  BUY EDGE"
            elif not math.isnan(slo) and slo > 0:
                edge = "  SELL EDGE"
            print(f"{BUCKET_EDGES[i]:.2f}-{BUCKET_EDGES[i+1]:<6.2f} {len(group):6d} "
                  f"{buy:+12.4f} [{blo:+7.4f},{bhi:+7.4f}] {sell:+12.4f} "
                  f"[{slo:+7.4f},{shi:+7.4f}]{edge}")


async def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=int, default=4000,
                        help="settled markets to price")
    parser.add_argument("--horizons", type=int, nargs="+", default=[24, 168],
                        help="hours before close to quote at")
    parser.add_argument("--seed", type=int, default=20260906)
    parser.add_argument("--cache", type=str, default=None,
                        help="read/write collected observations here as JSON")
    args = parser.parse_args(argv)
    random.seed(args.seed)

    study = Calibration()
    rows: list[dict[str, Any]] | None = None
    if args.cache:
        try:
            with open(args.cache, encoding="utf-8") as fh:
                rows = json.load(fh)
            print(f"loaded {len(rows)} cached observations from {args.cache}")
        except FileNotFoundError:
            rows = None
    if rows is None:
        rows = await study.collect(
            KalshiAdapter(), args.sample, tuple(args.horizons)
        )
        print(f"collected {len(rows)} observations")
        if args.cache:
            with open(args.cache, "w", encoding="utf-8") as fh:
                json.dump(rows, fh)
    for hours in args.horizons:
        study.report(rows, hours)
    return 0


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    sys.exit(main())
