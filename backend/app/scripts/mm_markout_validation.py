"""Markout-only on the Kalshi honest holdout: does the maker edge survive
without settlement?

WHY THIS EXISTS. The Phase 3/4 review found that `markout_pnl` in the default
`terminal=settled` mode is NOT settlement-free: `mm_backtest.replay()` folds
`terminal_inventory * (settle - last_mid)` into the markout accumulator as
well as into cash, and 805 of the 919 test markets on the honest holdout
held inventory into settlement. So the +$0.6281-vs-+$0.4360 markout/cash
gap recorded for that sample says nothing about whether the QUOTING earned
money or the SETTLEMENT did. `mm_replay_snapshots._strip_terminal_settlement`
reverses that term exactly -- it was built for Polymarket, where settlement
is years away -- and it has never been run on Kalshi, the one venue where the
cash answer is already known. Running it here validates the instrument
before T23 leans on it, and answers a question the kit's own verdict left
open: is the Kalshi edge a MAKER edge at all?

WHAT IT COMPUTES, on `.cache/mm/kalshi-honest-1m.json` at T20's exact
parameters (shipped policy 0.90/0.25/50.0, temporal cutoff 1787270400,
split seed 20260912), for both fill models, pessimistic first:

    cash             row.pnl                         the money, settled
    markout_settled  row.markout_pnl as replay() left it   contains the term
    markout_only     _strip_terminal_settlement(row)       the term removed

Each statistic is the mean per trading market on the TEST split, with an
event-clustered CI95 from `cluster_bootstrap` at 5,000 replicates across 20
seeds -- the standard T23 pre-committed on 2026-09-12, after the review
measured the shipped 500 replicates giving the Kalshi bound a Monte-Carlo sd
of 0.022. The settlement term's total is reported separately, because
`markout_settled - markout_only` is exactly what settlement contributed to
the maker statistic.

INTERPRETATION, FIXED IN THIS DOCSTRING BEFORE THE SCRIPT WAS FIRST RUN
(2026-09-13). On the pessimistic `markout_only` statistic:

  A. Lower bound > 0 on ALL 20 seeds at 5,000 replicates, AND no single
     event > 25% of its total, AND the interval still clears zero with the
     largest event removed  -> spread capture is positive on Kalshi
     independent of settlement. The instrument is validated for T23, and
     the Kalshi NO-GO stands for its stated reasons (power floor, fragility,
     multiplicity) and not because the mechanism is absent.
  B. Lower bound <= 0 on ANY seed while cash's lower bound > 0 on all seeds
     -> the Kalshi cash edge is NOT demonstrated to be spread capture;
     settlement carried the result. T23's prior for Polymarket should be
     lowered, and the kit's mechanism claim ("wide quoting earns the
     spread") loses its strongest evidence.
  C. Lower bound < 0 on the majority of seeds -> spread capture is negative;
     the quoting loses money and settlement luck hid it.
  D. Anything else -> name exactly which condition failed. No partial pass.

This script decides nothing about trading and changes no default.

No network, no database: it reads the cache T20 wrote and calls only the
committed harness. Reproducible from the repo, unlike the T20/T22
generators the review noted were never committed.
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.scripts import mm_backtest as mb
from app.scripts.calibration import cluster_bootstrap
from app.scripts.mm_replay_snapshots import _strip_terminal_settlement
from app.strategies.market_making import MarketMaker

#: T20's exact conditions -- changing any of these makes the result a
#: different measurement, not a re-run.
CACHE = ".cache/mm/kalshi-honest-1m.json"
CUTOFF_TS = 1787270400  # 2026-08-21T00:00:00Z
SPLIT_SEED = 20260912
POLICY = {"edge_fraction": 0.90, "min_spread": 0.25, "max_inventory": 50.0}

#: T23's pre-committed bootstrap standard.
REPLICATES = 5000
SEEDS = 20
TOP_EVENT_SHARE_CEILING = 0.25

STATISTICS = ("cash", "markout_settled", "markout_only")


def settlement_term(candles: mb.MarketCandles, row: mb.MarketRow) -> float:
    """What `replay()` added to `markout_pnl` for terminal inventory --
    `markout_settled - markout_only` for this row, by construction."""
    return row.markout_pnl - _strip_terminal_settlement(candles, row).markout_pnl


def _values(rows: Sequence[mb.MarketRow], stripped: Sequence[mb.MarketRow], stat: str) -> list[float]:
    if stat == "cash":
        return [r.pnl for r in rows]
    if stat == "markout_settled":
        return [r.markout_pnl for r in rows]
    if stat == "markout_only":
        return [r.markout_pnl for r in stripped]
    raise ValueError(stat)


def _ci(events: Sequence[str], values: Sequence[float], seed: int, replicates: int) -> tuple[float, float]:
    random.seed(seed)
    sample = [{"event": e, "pnl": v} for e, v in zip(events, values, strict=True)]
    return cluster_bootstrap(sample, lambda s: statistics.fmean(x["pnl"] for x in s), replicates)


def _block(
    events: Sequence[str], values: Sequence[float], *, replicates: int, seeds: int
) -> dict[str, Any]:
    """Mean, the shipped-seed interval, and the interval's behaviour across
    seeds -- the two facts T23 requires together."""
    lows: list[float] = []
    highs: list[float] = []
    for seed in range(seeds):
        lo, hi = _ci(events, values, seed, replicates)
        lows.append(lo)
        highs.append(hi)
    return {
        "n": len(values),
        "mean": statistics.fmean(values),
        "sd": statistics.pstdev(values) if len(values) > 1 else 0.0,
        "total": sum(values),
        "ci95_seed0": [lows[0], highs[0]],
        "replicates": replicates,
        "seeds": seeds,
        "lower_bound_min": min(lows),
        "lower_bound_max": max(lows),
        "lower_bound_sd": statistics.pstdev(lows),
        "seeds_with_lower_bound_gt_zero": sum(1 for lo in lows if lo > 0),
        "clears_zero_on_all_seeds": all(lo > 0 for lo in lows),
    }


def _concentration(
    events: Sequence[str], values: Sequence[float], *, replicates: int
) -> dict[str, Any]:
    by_event: dict[str, float] = defaultdict(float)
    for e, v in zip(events, values, strict=True):
        by_event[e] += v
    total = sum(values)
    top_event, top_total = max(by_event.items(), key=lambda kv: kv[1])
    share = (top_total / total) if total else float("nan")
    keep = [i for i, e in enumerate(events) if e != top_event]
    lo, hi = _ci([events[i] for i in keep], [values[i] for i in keep], 0, replicates)
    return {
        "n_events": len(by_event),
        "top_event": top_event,
        "top_event_total": top_total,
        "top_event_share": share,
        "share_under_ceiling": share < TOP_EVENT_SHARE_CEILING,
        "ci95_seed0_without_top_event": [lo, hi],
        "clears_zero_without_top_event": lo > 0,
    }


def run(
    cache_path: str, *, replicates: int = REPLICATES, seeds: int = SEEDS
) -> dict[str, Any]:
    collected = mb.read_cache(cache_path)
    by_id = {m.market_id: m for m in collected.markets}
    policy = MarketMaker(**POLICY)

    report: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "what": "Kalshi honest holdout, shipped policy, TEST split: cash vs markout with and "
        "without the terminal-settlement term. Pessimistic first.",
        "provenance": {
            "cache": cache_path,
            "n_markets_in_cache": len(collected.markets),
            "cutoff_ts": CUTOFF_TS,
            "split_seed": SPLIT_SEED,
            "policy": POLICY,
            "replicates": replicates,
            "seeds": seeds,
            "harness": "app.scripts.mm_backtest.replay/_split; "
            "app.scripts.mm_replay_snapshots._strip_terminal_settlement; "
            "app.scripts.calibration.cluster_bootstrap",
        },
        "interpretation_fixed_in_advance": {
            "A": "markout_only pessimistic lower bound > 0 on all seeds AND top event share < 0.25 "
            "AND clears without top event -> spread capture positive independent of settlement",
            "B": "markout_only lower bound <= 0 on any seed while cash clears on all seeds -> the "
            "cash edge is not demonstrated to be spread capture",
            "C": "markout_only lower bound < 0 on a majority of seeds -> spread capture negative",
            "D": "anything else -> name which condition failed; no partial pass",
        },
        "fill_models": {},
    }

    for fill_model in ("pessimistic", "optimistic"):
        result = mb.replay(collected.markets, policy=policy, fill_model=fill_model)
        split = mb._split(result.rows, cutoff_ts=CUTOFF_TS, split="temporal", seed=SPLIT_SEED)
        rows = [r for r in split.test if r.n_fills > 0]
        stripped = [_strip_terminal_settlement(by_id[r.market_id], r) for r in rows]
        events = [r.event for r in rows]
        terms = [settlement_term(by_id[r.market_id], r) for r in rows]

        block: dict[str, Any] = {
            "fill_model": fill_model,
            "split": "temporal-test",
            "n_trading": len(rows),
            "n_events_trading": len(set(events)),
            "n_fills": sum(r.n_fills for r in rows),
            "held_into_settlement": sum(1 for r in rows if r.held_into_settlement),
            "settlement_term": {
                "total": sum(terms),
                "mean_per_trading_market": statistics.fmean(terms) if terms else 0.0,
                "n_rows_nonzero": sum(1 for t in terms if t != 0.0),
                "note": "markout_settled minus markout_only, summed; exactly what replay() "
                "added to the maker statistic for terminal inventory",
            },
        }
        for stat in STATISTICS:
            values = _values(rows, stripped, stat)
            terminal = "excluded" if stat == "markout_only" else "settled"
            block[stat] = {
                "terminal": terminal,
                "basis": {
                    "cash": "cash_settled",
                    "markout_settled": "marked_at_i_plus_2_plus_terminal_term",
                    "markout_only": "marked_at_i_plus_2_only",
                }[stat],
                **_block(events, values, replicates=replicates, seeds=seeds),
                "per_fill": (sum(values) / block["n_fills"]) if block["n_fills"] else float("nan"),
            }
        block["markout_only"]["concentration"] = _concentration(
            events, _values(rows, stripped, "markout_only"), replicates=replicates
        )
        report["fill_models"][fill_model] = block

    pess = report["fill_models"]["pessimistic"]
    mo, cash = pess["markout_only"], pess["cash"]
    conc = mo["concentration"]
    if mo["clears_zero_on_all_seeds"] and conc["share_under_ceiling"] and conc["clears_zero_without_top_event"]:
        branch = "A"
    elif not mo["clears_zero_on_all_seeds"] and cash["clears_zero_on_all_seeds"]:
        branch = "B"
    elif mo["seeds_with_lower_bound_gt_zero"] < (seeds / 2):
        branch = "C"
    else:
        branch = "D"
    failed = [
        name
        for name, ok in (
            ("markout_only clears zero on all seeds", mo["clears_zero_on_all_seeds"]),
            ("top event share < 0.25", conc["share_under_ceiling"]),
            ("clears zero without top event", conc["clears_zero_without_top_event"]),
        )
        if not ok
    ]
    report["verdict"] = {
        "statistic": "markout_only, pessimistic, temporal-test, terminal=excluded",
        "branch": branch,
        "conditions_failed": failed,
        "decides_trading": False,
    }
    return report


def _markdown(report: dict[str, Any]) -> str:
    p = report["fill_models"]["pessimistic"]
    o = report["fill_models"]["optimistic"]

    def row(fm: dict[str, Any], stat: str) -> str:
        b = fm[stat]
        lo, hi = b["ci95_seed0"]
        return (
            f"| {stat} | {b['terminal']} | {b['mean']:+.4f} | [{lo:+.4f}, {hi:+.4f}] | "
            f"{b['lower_bound_min']:+.4f} | {b['seeds_with_lower_bound_gt_zero']}/{b['seeds']} | "
            f"{b['per_fill']:+.5f} |"
        )

    lines = [
        "# Kalshi markout-only: does the maker edge survive without settlement?",
        "",
        f"Generated {report['generated_at']}. Interpretation was fixed in the script docstring "
        "before the first run. **This decides nothing about trading.**",
        "",
        f"**Verdict branch: {report['verdict']['branch']}** — "
        + (
            "all conditions met."
            if not report["verdict"]["conditions_failed"]
            else "failed: " + "; ".join(report["verdict"]["conditions_failed"]) + "."
        ),
        "",
        "## Pessimistic (first), temporal-test split",
        "",
        f"n_trading={p['n_trading']}, n_events_trading={p['n_events_trading']}, "
        f"n_fills={p['n_fills']}, held_into_settlement={p['held_into_settlement']}. "
        f"Bootstrap: {p['cash']['replicates']} replicates × {p['cash']['seeds']} seeds, "
        "event-clustered.",
        "",
        "| statistic | terminal | mean/mkt | CI95 (seed 0) | min lower bound over seeds | seeds clearing zero | per fill |",
        "|---|---|---|---|---|---|---|",
        row(p, "cash"),
        row(p, "markout_settled"),
        row(p, "markout_only"),
        "",
        f"**Settlement term** (markout_settled − markout_only): total "
        f"{p['settlement_term']['total']:+.2f}, mean {p['settlement_term']['mean_per_trading_market']:+.4f} "
        f"per trading market, nonzero on {p['settlement_term']['n_rows_nonzero']} rows.",
        "",
        f"**Concentration of markout_only:** top event `{p['markout_only']['concentration']['top_event']}` "
        f"= {p['markout_only']['concentration']['top_event_share']:.1%} of total; "
        f"without it CI95 = [{p['markout_only']['concentration']['ci95_seed0_without_top_event'][0]:+.4f}, "
        f"{p['markout_only']['concentration']['ci95_seed0_without_top_event'][1]:+.4f}].",
        "",
        "## Optimistic cross-check",
        "",
        "| statistic | terminal | mean/mkt | CI95 (seed 0) | min lower bound over seeds | seeds clearing zero | per fill |",
        "|---|---|---|---|---|---|---|",
        row(o, "cash"),
        row(o, "markout_settled"),
        row(o, "markout_only"),
        "",
        "## Provenance",
        "",
        "```",
        json.dumps(report["provenance"], indent=2),
        "```",
    ]
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--cache", default=CACHE)
    parser.add_argument("--out", default="../.claude/kits/mm-proveout/reports/kalshi-markout-only")
    parser.add_argument("--replicates", type=int, default=REPLICATES)
    parser.add_argument("--seeds", type=int, default=SEEDS)
    args = parser.parse_args(argv)

    report = run(args.cache, replicates=args.replicates, seeds=args.seeds)
    out = Path(args.out)
    out.with_suffix(".json").write_text(json.dumps(report, indent=2, default=str))
    out.with_suffix(".md").write_text(_markdown(report))
    p = report["fill_models"]["pessimistic"]
    for stat in STATISTICS:
        b = p[stat]
        lo, hi = b["ci95_seed0"]
        print(
            f"{stat:16} terminal={b['terminal']:8} mean={b['mean']:+.4f} "
            f"ci=[{lo:+.4f},{hi:+.4f}] min_low={b['lower_bound_min']:+.4f} "
            f"clears={b['seeds_with_lower_bound_gt_zero']}/{b['seeds']}"
        )
    print(f"settlement_term_total={p['settlement_term']['total']:+.2f}")
    print(f"verdict branch={report['verdict']['branch']} failed={report['verdict']['conditions_failed']}")
    print(f"wrote {out.with_suffix('.json')} and {out.with_suffix('.md')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
