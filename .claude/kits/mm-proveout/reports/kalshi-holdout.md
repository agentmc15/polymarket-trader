# Kalshi holdout — the clean test of T4's candidate

**Does the candidate survive?** Yes, at the resolution that removes the need for a correction
rather than applying one. At **1-minute resolution** — the honest interval a live quoter actually
experiences, not an hourly candle standing in for it — the candidate's pessimistic out-of-sample
test CI is `[+0.4522, +1.0977]`, `n_trading=1477`, `n_events_trading=1057`: clear of zero, at
power **48% above** Gate 1's own 1,000-market floor. Optimistic agrees: `[+0.5940, +1.1983]`,
`n_trading=1509`. The defaults, at the same resolution and comparable power (`n_trading=2150`),
give `[-0.1646, +0.3154]` — spans zero, the cleanest and best-powered NO-GO this kit has produced
for them. Both hourly windows tried first (10-day, then 30-day, buying power from history) were
directionally consistent but underpowered — reported in full below, because the path there is
part of the evidence. **Net read: this is the strongest retrospective evidence this project has
produced. `edge_fraction=0.90, min_spread=0.25, max_inventory=50.0` clears an out-of-sample,
resolution-honest, adequately-powered bar the defaults do not. Gate 2 (T14) is now the right
conversation for this candidate. No default in `app/strategies/market_making.py` changes as part
of this report — that is a decision this task was not asked to make, and it remains T4's rule and
the user's call.**

---

## 0. Why this report exists

T4's calibration sweep (`kalshi-calibration.md`) found a venue-wide candidate —
`edge_fraction=0.90, min_spread=0.25, max_inventory=50.0` — that beat the shipped defaults
4.1x on temporal-test ROC and, scored through Gate 1's own verdict machinery on **the same
cache the candidate was tuned on**, produced a pessimistic test CI of `[+0.4983, +1.3061]`
(n_trading=884, n_events_trading=618). T4's own report already flagged this as circular: the
two-split rule's pass condition includes beating the defaults on the temporal test, so a
candidate that wins that split carries `temporal_win=true` partly *because* it was chosen for
winning it. Every one of the 15,283 markets in `backend/.cache/mm/kalshi-60m.json` participated
in that selection. This report runs the only test that settles it: the same two parameter sets,
replayed on Kalshi markets that never touched selection at all — first at the brief's original
10-day, hourly lookback; then at 30-day, hourly lookback to try to buy statistical power from
history; then, once that lever measured out as insufficient, at **1-minute resolution** on the
same 10-day window, which turned out to be the lever that actually worked — both because it
multiplies usable observations per market far more than a longer lookback can, and because it
is the resolution T5 already showed hourly candles understate.

## 1. Method common to all three windows

**Stage 1 — the exclusion.** `backend/app/scripts/mm_backtest.py` gained
`--exclude-markets-from PATH`: it reads a previous `--cache` file, collects every `market_id`
it holds, and removes them from the settled universe **before** `random.sample` draws the fresh
sample — never after, so an excluded market can never be drawn and then discarded, and never
merely filtered out of a report after being spent on a candle fetch. `tests/scripts/
test_mm_backtest.py` gained three tests: `_load_excluded_market_ids` reads ids only (not
candles, for speed against a 56MB cache); `_exclude_collected` drops exactly the excluded ids
from a synthetic universe; and an end-to-end test reproduces `_collect_or_load`'s own order of
operations (filter, then `random.sample`) and asserts the sample has zero overlap with the
excluded set. All 58 tests in that file pass, ruff is clean, and the full backend suite (1,364
tests) passes unchanged. No other logic in `mm_backtest.py` — the P&L convention, the
event-aware power floor, the universe provenance block, the split-exclusion logic, the
transport-error handling, the cache flush — was touched, at any window.

**The settled-universe walk is shared, not re-walked.** All three windows draw from the same
50,470 settled markets at `min_volume=2000` (`kalshi-60m.universe.json`, `collected_at
2026-09-07T11:32:00Z`), copied to each run's own `--cache`-derived universe path rather than
re-walked live — same query, no code change, no new network traffic, `collected_at` reported
exactly as originally walked.

**Same seed, same exclusion, same universe -> the same 12,000-candidate draw at every window.**
All three collections used `--exclude-markets-from .cache/mm/kalshi-60m.json --seed 20260908
--sample 12000 --min-volume 2000`, differing only in `--interval`/`--days`. Verified directly:
of the 7,985 markets usable at 10-day/hourly, **all 7,985** are also usable at 30-day/hourly (a
strict superset, +95 markets) and **all 7,985** are also usable at 10-day/1-minute (a strict
superset again, +3,926 markets — see §4). This is the *identical* market draw at every window,
not three fresh samples that happen to be similar.

**Chunking, and why 1-minute costs ~3x the requests of hourly.** `fetch_candles` chunks a
request at `CHUNK_SIZE_PERIODS=4,800` periods. At `interval=60`, a 10-day (240-period) or
30-day (720-period) window fits in one chunk — one request per market. At `interval=1`, a
10-day window is 14,400 minutes, which needs `ceil(14400/4800)=3` chunks — three requests per
market regardless of how much of that window a given market actually traded in, since chunking
splits the requested TIME SPAN, not the data. This matches T5's own measurement (900 GETs for
300 markets, exactly 3/market) and is why the 1-minute collection below is the largest in the
kit: 12,000 markets x 3 requests, serialized through the adapter's single global rate limiter
(`kalshi_min_request_interval_s=0.10s`, shared across all concurrency) — a ~10 req/s ceiling
regardless of `--concurrency`.

## 2. Stage 2a — 10-day, hourly lookback (the original run)

Both policies ran against one collection:

```
--exclude-markets-from .cache/mm/kalshi-60m.json --min-volume 2000 --interval 60 --days 10
--sample 12000 --seed 20260908 --cache .cache/mm/kalshi-holdout-60m.json
```

`seventy_percent_cutoff()` (imported from `mm_calibrate`, not modified) on the 7,985 collected
markets returned `cutoff_ts=1788636630` (`2026-09-05T19:30:30Z`); realised train share **70.01%**
(5,590 of 7,985 closes strictly before it).

**Overlap, verified twice:** once inside the harness (sample drawn only from what remained after
exclusion) and once independently, loading both cache files' `market_id` sets directly:

```
old_ids (kalshi-60m.json)          15,283
new_ids (kalshi-holdout-60m.json)   7,985
overlap                                 0
```

**`overlap=0`** (10-day/hourly window).

**Data window.** Venue `kalshi`, hourly candles, 10-day lookback. Closes span
**2026-07-03T16:52:51Z -> 2026-09-07T11:21:04Z**, `n_candles=502,052`. `n_quoted=5,220`
(defaults) / `4,401` (candidate). `n_unmarkable_intervals=0` both policies. Fees:
`KalshiFeeModel maker_rate=0.0175 taker_rate=0.07 maker_rebate_rate=0.0 source=settings_default`
(rebate $0.0000 either way, never in P&L, GUARDRAILS §2.3).

### 2.1 Verdicts, `fill_model=pessimistic` first, `terminal=settled`, `split=temporal-test`

| policy | n_quoted | n_trading | n_events_trading | mean pnl/mkt | raw CI (event-clustered) | raw verdict | CI +64% widened (T5) | widened verdict |
|---|---:|---:|---:|---:|---|:---:|---|:---:|
| **defaults** (0.80/0.10/20.0) | 1,656 | 630 | 441 | +0.0671 | `[-0.3298, +0.4315]` | NO-GO | `[-0.5735, +0.6752]` | NO-GO |
| **candidate** (0.90/0.25/50.0) | 1,409 | 325 | 268 | +0.6762 | `[+0.1105, +1.2075]` | GO | `[-0.2405, +1.5585]` | NO-GO (spans zero) |

`held_into_settlement` (test, pessimistic): defaults 490/630 = 77.8%, candidate 279/325 = 85.8%.
`n_trading` (test) at either policy is below Gate 1's 1,000-market floor.

## 3. Stage 2b — 30-day, hourly lookback: buying power from history (did not reach the floor)

**Rationale.** The 10-day result was underpowered (candidate test `n_trading=325` against a
1,000-market floor), and reaching 1,000 by sampling more markets was not available (only ~23,000
of the ~35,187-market universe remained unsampled). Thirty days of hourly candles is up to 720
periods per market against up to 240 at 10 days — more history per market, without touching the
universe, exclusion, seed, or thresholds.

**Collection.** Identical to Stage 2a except `--days 30` and a new cache path; `kalshi-60m.json`
and `kalshi-holdout-60m.json` were both left untouched. 8,080 markets collected (3,920 too
short, 0 payload/request errors) — a 95-market superset over the 10-day run's 7,985.
`seventy_percent_cutoff()` returned `cutoff_ts=1788635800` (`2026-09-05T19:16:40Z`); realised
train share **70.00%**. **`overlap=0`**, verified independently again.

**Did the lookback actually buy history?** Measured directly, market by market: only **1,247 of
7,985 (15.6%)** picked up any candles beyond what the 10-day run already had; **84.4% got the
identical candle count either way**, because median real trading lifetime (38-40 hourly candles,
under two days) is already shorter than the 10-day request window — only 0.2% of markets were
even near the 10-day cap, and zero approached the 30-day cap. This is why `n_quoted` rose only
5.0-7.5% rather than the "roughly tripling" the lever's own rationale anticipated, stated plainly
because the outcome did not match the premise.

### 3.1 Verdicts, `fill_model=pessimistic` first, `terminal=settled`, `split=temporal-test`

| policy | n_quoted | n_trading | n_events_trading | mean pnl/mkt | raw CI (event-clustered) | raw verdict | CI +64% widened (T5) | widened verdict |
|---|---:|---:|---:|---:|---|:---:|---|:---:|
| **defaults** (0.80/0.10/20.0) | 1,715 | 668 | 471 | +0.1972 | `[-0.1828, +0.5746]` | NO-GO | `[-0.4252, +0.8170]` | NO-GO |
| **candidate** (0.90/0.25/50.0) | 1,486 | 354 | 294 | +0.8816 | `[+0.3672, +1.4040]` | GO | `[+0.0354, +1.7357]` | GO (barely — 3.5 cents above zero) |

`held_into_settlement` (test, pessimistic): defaults 521/668 = 78.0%, candidate 305/354 = 86.2%
— essentially unchanged from the 10-day window (77.8%, 85.8%), because a longer *lookback*
extends history further into the *past*, not closer to settlement; the near-close candles that
decide whether inventory gets flattened are identical between the two runs. `n_trading` (test)
still below the 1,000-market floor at either policy — the lever raised candidate `n_trading`
from 325 to only 354 (+8.9%), not enough to matter on its own; the full three-window comparison
is in §5.

## 4. Stage 2c — 1-minute resolution: the honest interval (the result that settles this)

**Rationale (coordinator's instruction, after the 30-day lever measured out as insufficient).**
Neither more markets (unavailable at the needed scale) nor more lookback (measured in §3 as
hitting a ceiling near where it already sat) can reach Gate 1's power floor. The one dimension
left is resolution: a 2-day market gives roughly 2,880 one-minute quote/fill/mark triples against
roughly 48 hourly ones — attacking the power problem without touching the universe, the
exclusion, the seed, or any threshold. It also does something the 60-minute windows structurally
cannot: it **measures** the interval T5 could previously only *estimate a correction for*. T5
found that hourly clustered CIs understate uncertainty by 64% relative to 1-minute, on the same
markets, same mean, same fill model. A 1-minute replay does not need that correction — it **is**
the resolution a live quoter experiences, so there is no widened column in this section.

**Collection.** Identical universe, exclusion, seed and sample as both prior windows;
`--interval 1 --days 10` (not 30 — measured in §3 to buy almost nothing), into a new cache path,
leaving every earlier cache untouched:

```
--exclude-markets-from .cache/mm/kalshi-60m.json --min-volume 2000 --interval 1 --days 10
--sample 12000 --seed 20260908 --cache .cache/mm/kalshi-holdout-1m.json
```

**11,911 of 12,000 markets** collected usable history — only **89** too short, **0** payload
errors, **0** request errors (`failure_rate=0.0000`), against 7,985 at 60-minute/10-day. This is
the second lever this task tried that actually worked at the scale needed: at 1-minute
resolution, `MIN_CANDLES=4` requires only 4 minutes of any trading activity, which nearly every
volume-qualifying settled market clears, where 4 separate *hours* of activity is a real bar many
short-lived markets do not. `seventy_percent_cutoff()` on these 11,911 markets returned
`cutoff_ts=1788633041` (`2026-09-05T18:30:41Z`); realised train share **69.98%**.

**Overlap, verified twice again:**

```
old_ids (kalshi-60m.json)              15,283
new_ids (kalshi-holdout-1m.json)       11,911
overlap                                     0
```

**`overlap=0`** (1-minute window). Also confirmed: the 10-day/60-minute run's 7,985 markets are
an exact subset of this run's 11,911 (same 10-day window, finer resolution only ever adds
markets, never removes one) — one more internal-consistency check that this is genuinely the
same underlying draw.

**Data window.** Venue `kalshi`, 1-minute candles, 10-day lookback. Closes span the identical
**2026-07-03T16:52:51Z -> 2026-09-07T11:21:04Z**. `n_candles=7,303,230` (against 502,052 at
hourly — the same markets, ~14.5x the candles). `n_quoted=7,788` (defaults) / `7,153`
(candidate). `n_unmarkable_intervals=0` both policies, across 7,279,408 quote/fill/mark
intervals examined. Same fee schedule as both prior windows.

### 4.1 Verdicts, `fill_model=pessimistic` first, `terminal=settled`, `split=temporal-test` — NO widened column (this IS the resolution T5's correction estimates for)

| policy | n_quoted | n_trading | n_events_trading | mean pnl/mkt | CI (event-clustered) | vs 1,000-floor | verdict |
|---|---:|---:|---:|---:|---|---|:---:|
| **defaults** (0.80/0.10/20.0) | 7,788 | 2,150 | 1,402 | +0.0661 | `[-0.1646, +0.3154]` | 2.15x floor | **NO-GO** |
| **candidate** (0.90/0.25/50.0) | 7,153 | 1,477 | 1,057 | +0.7717 | `[+0.4522, +1.0977]` | 1.48x floor | **GO** |

```
VERDICT kalshi-holdout-1m fill_model=pessimistic terminal=settled split=temporal-test policy=defaults(0.80/0.10/20.0) ci_low=-0.1646 ci_high=+0.3154 n_trading=2150 n_events_trading=1402 -> NO-GO
VERDICT kalshi-holdout-1m fill_model=pessimistic terminal=settled split=temporal-test policy=candidate(0.90/0.25/50.0) ci_low=+0.4522 ci_high=+1.0977 n_trading=1477 n_events_trading=1057 -> GO
```

`fill_model=optimistic`, same split, same pattern: defaults `[-0.0471, +0.4387]`
(`n_trading=2184`) — spans zero; candidate `[+0.5940, +1.1983]` (`n_trading=1509`) — clears
zero. Both fill models agree at 1-minute resolution, at power comfortably above the floor on
both sides of the comparison — the first window in this report where that is true of both the
positive and the negative result at once.

### 4.2 Full detail — `overall` / `train` / `test`, pessimistic first

**Defaults (0.80/0.10/20.0):**

| block | n_quoted | n_trading | n_events_trading | n_fills | mean pnl/mkt | ci95 clustered by event | roc | collateral mean | held | short→yes |
|---|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|
| overall | 7,788 | 6,284 | 4,148 | 29,306 | -0.1707 | `[-0.3143, -0.0434]` | -0.0195 | 7.0664 | 5,152 | 2,188 |
| train | 5,202 | 4,124 | 2,744 | 18,769 | -0.2959 | `[-0.4603, -0.1264]` | -0.0333 | 7.0534 | 3,357 | 1,421 |
| test | 2,576 | 2,150 | 1,402 | 10,489 | +0.0661 | `[-0.1646, +0.3154]` | +0.0078 | 7.0912 | 1,787 | 762 |

**Candidate (0.90/0.25/50.0):**

| block | n_quoted | n_trading | n_events_trading | n_fills | mean pnl/mkt | ci95 clustered by event | roc | collateral mean | held | short→yes |
|---|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|
| overall | 7,153 | 4,198 | 3,009 | 10,549 | +0.7170 | `[+0.5150, +0.9063]` | 0.0798 | 5.2696 | 3,623 | 1,583 |
| train | 4,713 | 2,716 | 1,950 | 6,628 | +0.6804 | `[+0.4630, +0.8985]` | 0.0745 | 5.2600 | 2,355 | 1,013 |
| test | 2,430 | 1,477 | 1,057 | 3,910 | +0.7717 | `[+0.4522, +1.0977]` | 0.0887 | 5.2861 | 1,263 | 570 |

`held_into_settlement` (test, pessimistic): defaults 1,787/2,150 = **83.1%** (up from 77.8% at
hourly — finer resolution resolves more markets as "traded" at all, and more of those carry
inventory to close), candidate 1,263/1,477 = **85.5%** (essentially unchanged from hourly's
85.8%).

### 4.3 Power (test split, pessimistic, resampling whole events with replacement)

| policy | portfolio | events drawn | 5th pct total | P(profit) |
|---|---:|---:|---:|---:|
| defaults | 500 | 326 | -151.23 | 0.662 |
| defaults | 1,000 | 652 | -195.34 | 0.670 |
| defaults | 2,500 | 1,630 | -297.34 | 0.724 |
| defaults | 5,000 | 3,260 | -248.49 | 0.818 |
| candidate | 500 | 358 | +166.28 | 0.998 |
| candidate | 1,000 | 716 | +458.62 | 1.000 |
| candidate | 2,500 | 1,789 | +1,472.54 | 1.000 |
| candidate | 5,000 | 3,578 | +3,224.33 | 1.000 |

Both pools clear `MIN_POWER_POOL_EVENTS=30` by more than an order of magnitude (1,402 and 1,057
events respectively) — the largest, best-powered pools in this report.

### 4.4 The per-fill edge: more fills, each smaller — measured against T5's own reference

T5 measured, on the shipped defaults policy, its own 300-market subsample, `fill_model=
pessimistic`, `terminal=settled`, **overall**: hourly cash per fill **$0.4588** (2,583 fills,
$1,185.05 total) becomes **$0.2352** at 1-minute (5,031 fills, $1,183.41 total) — 1.95x the
fills for essentially the same money, a **48.7% shrink** in edge per fill. This report's own
holdout data, same policy, same fill model, same `overall` block: **$0.0972** hourly (4,783
fills) becomes **-$0.0366** at 1-minute (29,306 fills, 6.13x) — the per-fill edge does not just
shrink, it goes negative overall (though the `test` split alone stays positive: $0.0396 -> $0.0136,
a 65.7% shrink, 9.82x the fills). The mechanism is the same one T5 identified: at finer
resolution the replay sees intra-hour price movement an hourly candle averages away, so more
touches register as fills and each one is closer to a fair-value cross than the hourly candle's
coarser bid/ask made it look.

The candidate shows the identical mechanism, smaller in magnitude: `overall` **$0.5528** hourly
(1,868 fills) becomes **$0.2853** at 1-minute (10,549 fills, 5.65x) — a **48.4% shrink**,
matching T5's reference almost exactly. `test`: **$0.4841** -> **$0.2915** (8.61x fills, 39.8%
shrink). **This is exactly why the CI still clears zero despite the per-fill edge shrinking by
roughly the same proportion T5 measured for the defaults: the candidate's wider `min_spread`
and larger `max_inventory` bank enough total money across far more fills that the smaller edge
per fill is not enough to erase it** — the reverse of the defaults, where the smaller edge per
fill (already thinner to begin with) pushes the `overall` block negative.

| | fill_model | block | 60m per-fill | 60m n_fills | 1m per-fill | 1m n_fills | fills x | shrink |
|---|---|---|---:|---:|---:|---:|---:|---:|
| defaults | pessimistic | overall | +$0.0972 | 4,783 | -$0.0366 | 29,306 | 6.13x | sign flip |
| defaults | pessimistic | test | +$0.0396 | 1,068 | +$0.0136 | 10,489 | 9.82x | 65.7% |
| candidate | pessimistic | overall | +$0.5528 | 1,868 | +$0.2853 | 10,549 | 5.65x | 48.4% |
| candidate | pessimistic | test | +$0.4841 | 454 | +$0.2915 | 3,910 | 8.61x | 39.8% |
| T5 reference (defaults, pessimistic, overall, 300-mkt subsample) | | | +$0.4588 | 2,583 | +$0.2352 | 5,031 | 1.95x | 48.7% |

## 5. All three windows, side by side

| | n_quoted | n_trading (test) | n_events_trading (test) | mean pnl/mkt (test) | CI (test) | vs 1,000-floor |
|---|---:|---:|---:|---:|---|---|
| defaults, 10d/60m | 5,220 | 630 | 441 | +0.0671 | `[-0.3298, +0.4315]` | 0.63x |
| defaults, 30d/60m | 5,483 | 668 | 471 | +0.1972 | `[-0.1828, +0.5746]` | 0.67x |
| **defaults, 10d/1m** | **7,788** | **2,150** | **1,402** | **+0.0661** | **`[-0.1646, +0.3154]`** | **2.15x** |
| candidate, 10d/60m | 4,401 | 325 | 268 | +0.6762 | `[+0.1105, +1.2075]` | 0.33x |
| candidate, 30d/60m | 4,729 | 354 | 294 | +0.8816 | `[+0.3672, +1.4040]` | 0.35x |
| **candidate, 10d/1m** | **7,153** | **1,477** | **1,057** | **+0.7717** | **`[+0.4522, +1.0977]`** | **1.48x** |

Resolution did what lookback could not: candidate `n_trading` (test) moved from 325 (10d/60m) to
354 (30d/60m, +8.9%) to **1,477 (10d/1m, +354% over the 10-day hourly baseline)** — a jump
lookback could never deliver, because resolution is gated by how finely the replay can see
markets that were already there, not by how far back the request reaches or how many markets
exist to sample. The defaults'
point estimate stays small and the CI stays centered near zero at every window (+0.0671, +0.1972,
+0.0661) — the 1-minute run does not manufacture a defaults edge, it only gives the existing
near-zero estimate enough power to say so with confidence, whereas the candidate's positive point
estimate holds up (+0.6762, +0.8816, +0.7717) AND finally clears the power bar needed to trust it.

## 6. Comparison against the two numbers this report exists to check

| | n_trading (test) | n_events_trading (test) | mean pnl/mkt (test) | pessimistic test CI |
|---|---:|---:|---:|---|
| T3 Gate 1, defaults, in-sample (`kalshi-60m.json`, 10d/60m) | 1,239 | — | — | `[-0.1205, +0.4507]` (NO-GO) |
| this report, defaults, out-of-sample, 10d/60m | 630 | 441 | +0.0671 | `[-0.3298, +0.4315]` (NO-GO) |
| this report, defaults, out-of-sample, 30d/60m | 668 | 471 | +0.1972 | `[-0.1828, +0.5746]` (NO-GO) |
| **this report, defaults, out-of-sample, 10d/1m** | **2,150** | **1,402** | **+0.0661** | **`[-0.1646, +0.3154]` (NO-GO)** |
| T4 candidate check, in-sample (`kalshi-60m.json`, 10d/60m, circular) | 884 | 618 | +0.8929 | `[+0.4983, +1.3061]` |
| this report, candidate, out-of-sample, 10d/60m | 325 | 268 | +0.6762 | `[+0.1105, +1.2075]` |
| this report, candidate, out-of-sample, 30d/60m | 354 | 294 | +0.8816 | `[+0.3672, +1.4040]` |
| **this report, candidate, out-of-sample, 10d/1m** | **1,477** | **1,057** | **+0.7717** | **`[+0.4522, +1.0977]`** |

The defaults' NO-GO reproduces out of sample at all three windows, on markets that never touched
selection — the same negative finding measured four times now, never spanning into positive
territory, and now measured at power (2,150) that comfortably exceeds T3's own in-sample test
split (1,239). The candidate's out-of-sample point estimate is consistently within the same
order of magnitude as the circular in-sample estimate (+0.8929) across all three out-of-sample
measurements (+0.6762, +0.8816, +0.7717) — the sign and rough size were never the open question;
whether the sample was large enough to trust them was, and at 1-minute resolution it now is.

## 7. What this does and does not conclude

- **The 4.1x in-sample ROC was not selection artifact.** At all three windows, on markets that
  never participated in choosing the candidate, the point estimate stayed an order of magnitude
  above the defaults' own out-of-sample estimate and the raw CI cleared zero every time. At the
  resolution with adequate power to trust the interval outright, it still clears zero.
- **This is the strongest-evidence case the interpretation fixed in advance for this task.** The
  rule: the candidate's 1-minute test CI clears zero at `n_trading >= 1,000` -> strongest
  retrospective evidence, Gate 2 becomes the right conversation. Measured: `n_trading=1,477`
  (pessimistic) / `1,509` (optimistic), both above the floor; CI `[+0.4522, +1.0977]`
  (pessimistic) / `[+0.5940, +1.1983]` (optimistic), both clear of zero. No widening applies —
  1-minute is the resolution the widening exists to approximate, not a number that needs it.
- **The defaults' NO-GO is now the best-powered negative result in this report.** `n_trading=
  2,150` at 1-minute, more than double the 1,000-market floor, CI `[-0.1646, +0.3154]` — spans
  zero cleanly, with no reasonable reading of the interval calling it a GO. Reported with equal
  prominence to the candidate's result, as GUARDRAILS requires and as this report has done at
  every window.
- **The two levers tried before resolution are now closed off, and both closures are measured,
  not assumed.** Sampling more markets could not reach the floor (only ~23,000 of ~35,187
  remained). Buying power from a longer lookback bought almost nothing (§3: 84.4% of markets
  identical at 10 vs 30 days, because median real trading lifetime is under two days). Resolution
  was the one dimension left, and it worked because it is gated by data density per market, not
  by how far back a request reaches or how many markets exist to sample.
- **The mechanism is now measured, not inferred.** §4.4: per-fill edge shrinks at 1-minute
  resolution by almost exactly the proportion T5 measured on its own subsample (candidate 48.4%
  vs T5's reference 48.7%), while total fills rise 5.65-9.82x — the same phenomenon T5 first
  identified, now reproduced independently on a disjoint, ten-times-larger sample.
- **T4's own two-split rule never certified this candidate in the first place** — it won 51 of 60
  random-half draws venue-wide, three short of the 90% floor (`kalshi-calibration.md` §1). That
  fact is independent of this report and remains true; what this report adds is that the
  candidate ALSO clears a much stricter bar — held-out, resolution-honest, adequately-powered
  out-of-sample scoring — that T4's own rule never tested.
- **No default in `app/strategies/market_making.py` changes as part of this report.** This task
  was not asked to re-run T4's default-change rule, only to score the exact parameters out of
  sample at the resolution needed to trust the answer, and report plainly. It has. Whether to
  invoke T4's rule again with this evidence, and whether to proceed to Gate 2 (T14), are decisions
  this report deliberately leaves to the kit's next task and the user, consistent with PLAN.md's
  own separation between measurement and the decision to trade.

## 8. Provenance

- Command (Stage 1 test, applies to all three windows): `cd backend && python3 -m pytest -q
  tests/scripts/test_mm_backtest.py` → 58 passed. Full suite: 1,364 passed. `ruff check
  app/scripts/mm_backtest.py tests/scripts/test_mm_backtest.py` → clean.
- 10-day/60-minute window: universe cache copied to `kalshi-holdout-60m.universe.json`;
  collection + cutoff via a driver importing `_collect_or_load` and
  `mm_calibrate.seventy_percent_cutoff` directly (`--days 10 --interval 60 --cache
  .cache/mm/kalshi-holdout-60m.json`); two `python3 -m app.scripts.mm_backtest` invocations
  against that cache, one per policy, both `--temporal-cutoff 2026-09-05T19:30:30+00:00 --seed
  20260908`.
- 30-day/60-minute window: universe cache copied to `kalshi-holdout-30d.universe.json`;
  identical driver pattern with `--days 30 --interval 60 --cache
  .cache/mm/kalshi-holdout-30d.json`; two `python3 -m app.scripts.mm_backtest` invocations, both
  `--temporal-cutoff 2026-09-05T19:16:40+00:00 --seed 20260908`.
- 10-day/1-minute window: universe cache copied to `kalshi-holdout-1m.universe.json`; identical
  driver pattern with `--days 10 --interval 1 --cache .cache/mm/kalshi-holdout-1m.json`; two
  `python3 -m app.scripts.mm_backtest` invocations, both `--temporal-cutoff
  2026-09-05T18:30:41+00:00 --seed 20260908`.
- `backend/.cache/mm/kalshi-holdout-60m.json`, `kalshi-holdout-30d.json`,
  `kalshi-holdout-1m.json`, and their `.universe.json` sidecars are new, `.gitignore`d cache
  files, not part of this change; `kalshi-60m.json` (T3/T4's tuning cache) was opened read-only
  at every step, at every window, and never written.
- Every P&L, ROC and CI figure above carries `fill_model` and `terminal=settled` in its source
  JSON; pessimistic is reported first throughout, per GUARDRAILS §2.1/§2.2.
