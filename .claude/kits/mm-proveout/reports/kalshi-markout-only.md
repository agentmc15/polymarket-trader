# Kalshi markout-only: does the maker edge survive without settlement?

Generated 2026-09-13T02:16:55+00:00. Interpretation was fixed in the script docstring before the first run. **This decides nothing about trading.**

**Verdict branch: A** — all conditions met.

## Pessimistic (first), temporal-test split

n_trading=919, n_events_trading=789, n_fills=2392, held_into_settlement=805. Bootstrap: 5000 replicates × 20 seeds, event-clustered.

| statistic | terminal | mean/mkt | CI95 (seed 0) | min lower bound over seeds | seeds clearing zero | per fill |
|---|---|---|---|---|---|---|
| cash | settled | +0.4360 | [+0.0464, +0.8295] | +0.0277 | 20/20 | +0.16750 |
| markout_settled | settled | +0.6281 | [+0.2809, +0.9953] | +0.2666 | 20/20 | +0.24131 |
| markout_only | excluded | +2.3671 | [+2.1087, +2.6427] | +2.0933 | 20/20 | +0.90941 |

**Settlement term** (markout_settled − markout_only): total -1598.10, mean -1.7390 per trading market, nonzero on 805 rows.

**Concentration of markout_only:** top event `KXNCAAFFIRSTTDTEAM-26SEP03COLOGT` = 2.2% of total; without it CI95 = [+2.0717, +2.5703].

## Optimistic cross-check

| statistic | terminal | mean/mkt | CI95 (seed 0) | min lower bound over seeds | seeds clearing zero | per fill |
|---|---|---|---|---|---|---|
| cash | settled | +0.4530 | [+0.0434, +0.8579] | +0.0327 | 20/20 | +0.16642 |
| markout_settled | settled | +0.7081 | [+0.3444, +1.0765] | +0.3387 | 20/20 | +0.26014 |
| markout_only | excluded | +2.4964 | [+2.2381, +2.7779] | +2.2166 | 20/20 | +0.91715 |

## Provenance

```
{
  "cache": ".cache/mm/kalshi-honest-1m.json",
  "n_markets_in_cache": 4903,
  "cutoff_ts": 1787270400,
  "split_seed": 20260912,
  "policy": {
    "edge_fraction": 0.9,
    "min_spread": 0.25,
    "max_inventory": 50.0
  },
  "replicates": 5000,
  "seeds": 20,
  "harness": "app.scripts.mm_backtest.replay/_split; app.scripts.mm_replay_snapshots._strip_terminal_settlement; app.scripts.calibration.cluster_bootstrap"
}
```
