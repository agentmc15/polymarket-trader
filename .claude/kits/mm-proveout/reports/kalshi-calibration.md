# Kalshi calibration sweep — per series, out of sample

**Data window.** Venue `kalshi`, hourly (`interval_minutes=60`) candles, 10-day lookback per
market, sampled settled-market closes spanning **2026-07-03T07:03:47Z -> 2026-09-07T11:25:53Z** —
the identical `backend/.cache/mm/kalshi-60m.json` cache T3's Gate 1 report was scored on
(`n_markets`=15,283, `n_candles`=951,788). **This sweep ran offline against that cache; no new
Kalshi collection was performed** (PLAN.md D10 / the task brief: the cache is already collected).
Temporal cutoff = **2026-09-05T19:40:26Z**, computed by `seventy_percent_cutoff()` rather than
hand-picked, and it reproduces T3's own manually-chosen cutoff timestamp exactly
(`1788637226`, **69.99%** of venue-wide closes in train) — the compliant ~70%-train split, not
the harness's median-close (~50/50) default. That distinction is not cosmetic: NOTES.md records
that reading the wrong split reversed T3's read from a NO-GO to an apparent GO once already this
session.

**What this measures.** `backend/app/scripts/mm_calibrate.py` sweeps `edge_fraction ∈
{0.6,0.7,0.8,0.9}`, `min_spread ∈ {0.05,0.10,0.15,0.25}`, `max_inventory ∈ {10,20,50}` — 48 grid
points including the shipped combination `(0.80, 0.10, 20.0)` — objective **return on capital**
(`roc = total cash pnl / (n_quoted * mean collateral)`), under the kit's two-split rule: tune on a
random event half, score on the other half AND the temporal test split, repeated over **60**
independent random event-halves (never market-halves — T2's red team found the power table
treating one 49-market event as 49 independent draws, and a market-level half-split here would
reproduce that error one layer up). A challenger wins only if it beats the current defaults on
**>= 90%** of the 60 halves **and** on the temporal test. `two_split_rule()` is built generically
(`Mapping[K, ReplayResult] -> TwoSplitResult`) so T6's `taper_hours` sweep can call the identical
rule rather than re-deriving the 90%-and-temporal gate a second time.

**Search fill model.** The sweep's objective is computed under `fill_model=pessimistic` only
(GUARDRAILS §2.2: the conservative model governs decisions). Optimistic is replayed and reported
BESIDE every pessimistic number below, for the same policy, and never decides anything (PLAN.md
D3).

**The result, stated first because it is the whole finding: no challenger passed the rule, at
any of the 11 scopes tested (venue-wide plus the 10 largest series). The shipped defaults —
`min_spread=0.10`, `edge_fraction=0.80`, `max_inventory=20.0` — are unchanged, and
`app/strategies/market_making.py` was not touched, AS OF THIS REPORT.** A sweep that finds
nothing is a result, not a failure, and GUARDRAILS §2.6 forbids lowering the 90% floor or the
60-halve count to manufacture a pass — neither was touched to produce this outcome.

**STATUS UPDATE, 2026-09-08 — no longer current.** `app/strategies/market_making.py` WAS
subsequently touched: its defaults moved to `edge_fraction=0.90`, `min_spread=0.25`,
`max_inventory=50.0` (the same candidate this report's venue-wide sweep found at **51/60**,
three short of the 90% floor and therefore NOT passing the rule above) on the strength of a
LATER report (`kalshi-holdout.md`), not this one. A Phase 1 review of that later report found
its holdout event-contaminated (51% of its markets share an event with the tuning cache this
report used) and its temporal window a single 1.70-day holiday weekend, and confirmed that the
two-split rule this report implements has never been run at 1-minute resolution for the new
values — only the hourly 51/60 result above exists at this grid. **The current defaults are
therefore not certified by this kit's own rule**; see `app.strategies.market_making`'s module
docstring for the full account. Everything else in this report (the 51/60 sweep, its numbers,
and its "no default changed" conclusion) describes what was true when it was written and is
left as originally measured.

## 1. Venue-wide: the near-miss, reported plainly

| scope | n_markets | n_events | wins/60 | win rate | best candidate (`edge`/`spread`/`max_inv`) | temporal win | passed |
|---|---:|---:|---:|---:|---|:---:|:---:|
| **venue-wide** | 15,283 | 7,297 | **51/60** | **85.00%** | 0.90 / 0.25 / 50.0 | true | **NO** |

Fifty-one of sixty is not fifty-four of sixty. The 90% floor needs **54/60**; the best-performing
grid point found here cleared 51 — three halves short, 5 percentage points under the line. This
is reported as a near-miss on purpose: the floor was fixed by PLAN.md before any of this was
visible, GUARDRAILS §2.6 forbids relaxing it to manufacture a pass, and doing so here — after
seeing how close 51/60 came — would be exactly the kind of after-the-fact threshold move that rule
exists to prevent. No default changed. What follows states the candidate's numbers plainly so a
reader can decide what, if anything, to test next; none of it is a recommendation to trade on this
candidate.

**The candidate's return-on-capital margin over the defaults, `fill_model=pessimistic,
terminal=settled`:**

| statistic | defaults (0.80/0.10/20.0) | candidate (0.90/0.25/50.0) | ratio |
|---|---:|---:|---:|
| full-sample ROC (train+test) | 0.0195 | 0.0531 | 2.7x |
| temporal-test ROC | 0.0090 | 0.0373 | 4.1x |

The candidate beat the defaults' temporal-test ROC on **51 of 60** random-half draws and on the
temporal test itself (`temporal_win=true`) — the margin is real and reproduces across most halves,
just not the 9-in-10 the kit requires before a default moves.

## 2. What the near-passing candidate is NOT: three reasons it does not overturn T3's NO-GO

Scoring the candidate's exact parameters (`min_spread=0.25 edge_fraction=0.90 max_inventory=50.0`)
through T2/T3's own Gate 1 verdict machinery (`app.scripts.mm_backtest`, same cache) gives a
CI that looks decisively positive at first glance —
`fill_model=pessimistic terminal=settled split=temporal-test`: n_trading=884, n_events_trading=618,
mean +0.8929, ROC 0.0434, **CI [+0.4983, +1.3061]**; optimistic n_trading=910, mean +0.9637, CI
[+0.6198, +1.3442]. **This is included for completeness and is NOT a second verdict — it is not a
GO, for three separate reasons, all of which apply simultaneously:**

1. **It is circular.** The two-split rule's own pass condition includes beating the defaults ON
   the temporal test — that is one of the two gates `two_split_rule()` checks before calling
   anything a candidate at all. So this candidate carries `temporal_win=true` PARTLY BECAUSE it
   was selected for winning that exact test. Re-scoring it on that same split and reading the
   resulting CI as an independent verdict is a number produced by a process that guarantees its
   own sign; it says nothing about a market this policy has not already looked at.
2. **It is underpowered against T3's own acceptance floor.** T3 required `n_trading >= 2,500`
   overall and `>= 1,000` on the scored test split before a verdict counts as adequately powered.
   This candidate's `min_spread=0.25` quotes far fewer markets (only the widest, most selectively
   profitable books) — overall `n_trading=2,300` (below 2,500) and test `n_trading=884` (below
   1,000). A CI computed on a thinner-than-required sample is not comparable to T3's own NO-GO
   verdict, which cleared both floors (4,320 overall, 1,239 test).
3. **The split used to produce those numbers was not the compliant one.** The run above used
   `--temporal-cutoff 2026-09-05` (midnight, an argument that happened to be convenient to pass),
   which lands at **61.4%** train on this candidate's own quoted-market timeline — not the ~70%
   this report and T3 both use elsewhere. A different split than the one T3's verdict and this
   report's own `seventy_percent_cutoff()` use is not a like-for-like comparison, and is stated
   here rather than silently reused.

**What would actually settle whether `0.90/0.25/50.0` is better than the shipped defaults: score
it on data that played no part in selecting it** — a fresh forward collection window (T7-T10's
machinery), or Kalshi settled markets outside this cache's window once more of them exist. Nothing
in this sweep does that, and nothing in this report should be read as though it did.

### 2a. The interval above is narrower than the truth

T5's 1-minute sub-study (`kalshi-minute-study.md`), on the SAME markets, `fill_model=pessimistic,
terminal=settled`: mean P&L per trading market is nearly unchanged between resolutions
(**$3.9502** at 60m vs **$3.9447** at 1m), but the clustered CI widens materially — **from
[+3.1799, +4.7370] at 60m to [+2.7764, +5.3300] at 1m, a 64% wider half-width** (0.7786 -> 1.2768)
purely from seeing intra-hour inventory swings hourly candles cannot. Applying that SAME 1.64x
widening factor to the candidate's pessimistic test-split half-width above (0.4039) gives
**~0.66**, moving the interval from the reported **[+0.4983, +1.3061]** to roughly **[+0.24,
+1.55]**. Still clear of zero at this scale, but the margin is visibly smaller than the raw
hourly number suggests — and this correction has not been applied to any OTHER interval in this
report, T3's, or T5's own hourly tables, so every hourly-computed CI in this kit should be read as
narrower than its 1-minute counterpart by a similar-order factor.

Separately, T5 measured that peak `|inventory|` never exceeds `max_inventory=20.0` at either
resolution across its 300-market sample — the inventory LIMIT, not `skew_strength`, is what bounds
exposure, which agrees with this sweep's own finding below that widening `max_inventory` (not
tightening it) is the direction that raised return on capital here.

## 3. `max_inventory` alone: measured, not assumed

The motivating question this task predates the answer to: does tightening `max_inventory` buy
out-of-sample stability, given 3,390 of 4,320 trading markets (78.5%) carried inventory into
settlement in T3's Gate 1 sample? Holding `edge_fraction`/`min_spread` at the CURRENT shipped
defaults (0.80 / 0.10) and varying only `max_inventory` over its three grid values, on the FULL
venue-wide sample (`fill_model=pessimistic, terminal=settled`, train+test together):

| `max_inventory` | n_trading | held into settlement | held share | settled short->yes | ROC | mean pnl/mkt |
|---:|---:|---:|---:|---:|---:|---:|
| 10.0 | 4,320 | 3,151 | **72.9%** | 1,327 | 0.0061 | +0.0882 |
| 20.0 *(current default)* | 4,320 | 3,390 | **78.5%** | 1,414 | 0.0195 | +0.2913 |
| 50.0 | 4,320 | 3,530 | **81.7%** | 1,463 | 0.0344 | +0.5151 |

**The measured answer is the opposite of the hypothesis.** Tightening `max_inventory` from 20 to
10 does lower the held-into-settlement share (78.5% -> 72.9%, a real 5.6-point reduction) — but it
also lowers return on capital by more than 3x (0.0195 -> 0.0061) and cuts mean P&L per trading
market by more than two-thirds. Widening it to 50 moves in the OTHER direction on both axes at
once: MORE inventory held into settlement (81.7%) and MORE return on capital (0.0344, the highest
of the three). This is consistent with, not contradicted by, the near-passing candidate above
using `max_inventory=50` rather than a tighter value — the grid search independently landed on the
same direction this isolated slice shows. `n_trading` is identical across all three rows by
construction (only the inventory cap changes, not what quotes or fills), so every difference in
this table is the inventory cap's effect alone, holding everything else the coin-flip-inventory
concern named in this task's brief fixed.

**What this table does NOT show.** `roc` here is a point estimate on raw return, not a
risk-adjusted or variance-penalized statistic — this slice does not test whether a wider limit's
larger raw return comes with a wider tail, only that it comes with a larger mean. The full
two-split rule (Section 1) accounts for this only insofar as beating the defaults' ROC on 51 of 60
independently-drawn halves is itself some evidence of robustness across resampled data, not
because the objective itself penalizes variance. A reader wanting a stated tail statistic for
`max_inventory=50` specifically should look to T3/T13's power tables, which this sweep did not
rerun per grid point (48 points x a 4-size power table would have cost hours, not minutes — see
`mm_calibrate.py`'s module docstring for the same reasoning applied to `report()`'s bootstrap).

## 4. Per-series (10 largest by `n_trading` under the shipped defaults)

Ranking is fixed to the CURRENT defaults' trading counts, so "the 10 largest series" means the
same thing whether or not any series-level challenger looked promising. **No per-series result
changes any default — PLAN.md and this task's brief reserve that for a venue-wide win, which did
not happen (Section 1).** These numbers are informational: do the same grid and rule, applied to
one series' own history, look different from the venue-wide picture, and if so, how.

### 4a. Two-split summary, every series

| series | n_markets | n_events | wins/60 | win rate | best candidate (`edge`/`spread`/`max_inv`) | temporal win | train frac |
|---|---:|---:|---:|---:|---|:---:|---:|
| KXNCAAFSPREAD    | 742 | 109 | 14/60 | 23.3% | 0.80 / 0.10 / 50.0 | true  | 31.4% |
| KXNCAAFTOTAL     | 587 | 106 | 42/60 | 70.0% | 0.80 / 0.25 / 50.0 | true  | 31.2% |
| KXITFMATCH       | 446 | 352 | 12/60 | 20.0% | 0.90 / 0.25 / 50.0 | false | 80.0% |
| KXMLBKS          | 274 |  82 | 17/60 | 28.3% | 0.90 / 0.05 / 50.0 | true  | 64.2% |
| KXITFWMATCH      | 358 | 297 | 18/60 | 30.0% | 0.90 / 0.15 / 50.0 | true  | 81.0% |
| KXTRUMPMENTION   |  96 |   9 |  8/60 | 13.3% | 0.80 / 0.25 / 50.0 | n/a   | 100%  |
| KXVOTEPRIMARY    | 145 |  49 | 34/60 | 56.7% | 0.90 / 0.25 / 50.0 | n/a   | 100%  |
| KXNCAAFTEAMTOTAL |  99 |  40 | 15/60 | 25.0% | 0.90 / 0.25 / 50.0 | true  | 45.4% |
| KXNCAAF1HSPREAD  | 166 |  57 | 15/60 | 25.0% | 0.80 / 0.05 / 20.0 | true  | 44.0% |
| KXCS2GAME        | 149 | 124 | 20/60 | 33.3% | 0.90 / 0.25 / 50.0 | true  | 71.1% |

`temporal win: n/a` means the venue-wide cutoff falls AFTER every one of that series' closes
(`train_frac 100%`) — KXTRUMPMENTION and KXVOTEPRIMARY have no test-split markets at all under
this cutoff, so no candidate could ever pass regardless of its random-half win rate; their two-
split result is purely from the 60 random halves, and their "candidate" row is informational only.
**No series comes close to 90%.** Venue-wide's 85% is in fact the STRONGEST result of all 11
scopes measured — aggregating across the whole venue captured a more consistent direction than any
single series did on its own, which is the opposite of what a "some series secretly hide a bigger
effect" read of Section 1 might suggest.

Every series' candidate lands on `min_spread ∈ {0.05, 0.10, 0.15, 0.25}` and `max_inventory=50.0`
in nine of ten cases (KXNCAAF1HSPREAD is the one exception, at `max_inventory=20.0` — its current
value) — the same wider-inventory direction Section 3 measured venue-wide, reproduced
independently at the series level in nearly every one of the ten largest series.

### 4b. Per-series `pnl`, pessimistic first (`fill_model=pessimistic` then `fill_model=optimistic`, `terminal=settled`), scored under the shipped defaults

Every number below is the CURRENT DEFAULT policy's own performance on that series (no challenger
won anywhere, so nothing here is a different policy than what is shipped) — `overall` scores the
whole series, `train`/`test` are the temporal split at the SAME 2026-09-05T19:40:26Z cutoff used
venue-wide, which lands at a different train/test proportion inside each series (`train_frac`
above) than it does venue-wide (69.99%).

**KXNCAAFSPREAD** (742 markets, 109 events, train_frac 31.4%)

| split | fill_model | n_trading | n_events | mean pnl/mkt | 95% CI (event-clustered) | roc | held | short->yes |
|---|---|---:|---:|---:|:---|---:|---:|---:|
| overall | pessimistic | 384 | 100 | +0.2972 | [-0.1740, +0.7955] | 0.0218 | 278 | 115 |
| train   | pessimistic |  97 |  25 | +0.5867 | [-0.1427, +1.3102] | 0.0330 |  75 |  26 |
| test    | pessimistic | 286 |  74 | +0.2076 | [-0.4000, +0.8999] | 0.0171 | 202 |  88 |
| overall | optimistic  | 396 | 101 | +0.3264 | [-0.1214, +0.7837] | 0.0247 | -   | -  |
| train   | optimistic  |  99 |  26 | +0.6113 | [-0.1024, +1.2731] | 0.0351 | -   | -  |
| test    | optimistic  | 296 |  74 | +0.2394 | [-0.3119, +0.8689] | 0.0204 | -   | -  |

**KXNCAAFTOTAL** (587 markets, 106 events, train_frac 31.2%)

| split | fill_model | n_trading | n_events | mean pnl/mkt | 95% CI (event-clustered) | roc | held | short->yes |
|---|---|---:|---:|---:|:---|---:|---:|---:|
| overall | pessimistic | 192 | 78 | +0.1328 | [-0.5674, +0.8441] | 0.0065 | 151 | 32 |
| train   | pessimistic |  38 | 19 | -0.5134 | [-1.5790, +0.4209] | -0.0145 |  30 |  8 |
| test    | pessimistic | 148 | 57 | +0.2515 | [-0.6465, +1.2442] | 0.0155 | 115 | 24 |
| overall | optimistic  | 201 | 80 | +0.2151 | [-0.4756, +1.0139] | 0.0110 | -  | -  |
| train   | optimistic  |  43 | 21 | -0.3960 | [-1.6422, +0.7028] | -0.0127 | -  | -  |
| test    | optimistic  | 152 | 58 | +0.3453 | [-0.4704, +1.3710] | 0.0218 | -  | -  |

**KXITFMATCH** (446 markets, 352 events, train_frac 80.0%)

| split | fill_model | n_trading | n_events | mean pnl/mkt | 95% CI (event-clustered) | roc | held | short->yes |
|---|---|---:|---:|---:|:---|---:|---:|---:|
| overall | pessimistic | 163 | 145 | +0.7540 | [+0.1474, +1.3933] | 0.0570 | 142 | 72 |
| train   | pessimistic | 105 |  94 | +0.3859 | [-0.4476, +1.1879] | 0.0237 |  91 | 49 |
| test    | pessimistic |  58 |  51 | +1.4205 | [+0.3170, +2.4491] | 0.1852 |  51 | 23 |
| overall | optimistic  | 171 | 150 | +0.6829 | [+0.0185, +1.3676] | 0.0541 | -  | -  |
| train   | optimistic  | 112 |  98 | +0.2851 | [-0.5865, +1.0693] | 0.0186 | -  | -  |
| test    | optimistic  |  59 |  52 | +1.4381 | [+0.4053, +2.5052] | 0.1907 | -  | -  |

KXITFMATCH's test split is thin (58/59 trading markets across 51/52 events) and the CI reflects
it; this is also the one series whose "candidate" failed the temporal check outright
(`temporal_win=false`), so the tables above are simply the shipped defaults, not evidence for any
different parameter set.

**KXMLBKS** (274 markets, 82 events, train_frac 64.2%) — the one series with a NEGATIVE result
under the shipped defaults, at both resolutions and splits

| split | fill_model | n_trading | n_events | mean pnl/mkt | 95% CI (event-clustered) | roc | held | short->yes |
|---|---|---:|---:|---:|:---|---:|---:|---:|
| overall | pessimistic | 124 | 56 | -1.2977 | [-1.9944, -0.6043] | -0.1304 | 103 | 62 |
| train   | pessimistic |  87 | 38 | -1.0736 | [-1.9744, -0.2597] | -0.1137 |  72 | 40 |
| test    | pessimistic |  37 | 18 | -1.8246 | [-2.9010, -0.5797] | -0.1634 |  31 | 22 |
| overall | optimistic  | 128 | 58 | -1.2762 | [-2.0395, -0.5191] | -0.1323 | -  | -  |
| train   | optimistic  |  87 | 38 | -1.1086 | [-2.0431, -0.2979] | -0.1174 | -  | -  |
| test    | optimistic  |  41 | 20 | -1.6317 | [-2.7788, -0.4585] | -0.1619 | -  | -  |

KXMLBKS is the only one of the ten largest series whose CI sits entirely BELOW zero at every
split, under both fill models. It is also the only series where the best grid point found narrows
`min_spread` to 0.05 (looser selectivity does not help here) — and even that candidate's own
full-sample ROC (-0.0788) stays negative (Section 4a's summary table). Nothing in this sweep
recommends quoting this series; nothing in the shipped defaults singles it out for exclusion
either, and this report does not add such a rule (out of this task's scope).

**KXITFWMATCH** (358 markets, 297 events, train_frac 81.0%)

| split | fill_model | n_trading | n_events | mean pnl/mkt | 95% CI (event-clustered) | roc | held | short->yes |
|---|---|---:|---:|---:|:---|---:|---:|---:|
| overall | pessimistic | 121 | 114 | -0.0921 | [-0.8600, +0.7566] | -0.0062 | 94 | 55 |
| train   | pessimistic |  79 |  75 | -0.1271 | [-0.9988, +0.7996] | -0.0071 | 56 | 33 |
| test    | pessimistic |  42 |  39 | -0.0264 | [-1.5307, +1.5611] | -0.0029 | 38 | 22 |
| overall | optimistic  | 124 | 115 | +0.2519 | [-0.4142, +1.0458] | 0.0174 | -  | -  |
| train   | optimistic  |  80 |  75 | +0.2508 | [-0.6456, +1.1918] | 0.0142 | -  | -  |
| test    | optimistic  |  44 |  40 | +0.2539 | [-1.2367, +2.0700] | 0.0291 | -  | -  |

**KXTRUMPMENTION** (96 markets, 9 events, train_frac 100% — no test-split markets)

| split | fill_model | n_trading | n_events | mean pnl/mkt | 95% CI (event-clustered) | roc | held | short->yes |
|---|---|---:|---:|---:|:---|---:|---:|---:|
| overall = train | pessimistic | 79 | 9 | +0.4024 | [-0.2185, +1.0422] | 0.0462 | 60 | 24 |
| test             | pessimistic |  0 | 0 | n/a | [n/a, n/a] | n/a | 0 | 0 |
| overall = train  | optimistic  | 79 | 9 | +0.7414 | [+0.0955, +1.5093] | 0.0859 | -  | -  |
| test             | optimistic  |  0 | 0 | n/a | [n/a, n/a] | n/a | -  | -  |

Only **9 distinct events** underlie this series — every table above rests on a pool below the
kit's own `MIN_POWER_POOL_EVENTS=30` floor and `CI_FLOOR_EVENTS=5` for a per-market power read,
so the clustered CI here is the largest few-event bootstrap the kit trusts, not a well-powered
read. Reported for completeness, weighted accordingly by a reader.

**KXVOTEPRIMARY** (145 markets, 49 events, train_frac 100% — no test-split markets)

| split | fill_model | n_trading | n_events | mean pnl/mkt | 95% CI (event-clustered) | roc | held | short->yes |
|---|---|---:|---:|---:|:---|---:|---:|---:|
| overall = train | pessimistic | 69 | 28 | -0.3484 | [-1.3537, +0.7241] | -0.0389 | 55 | 20 |
| test             | pessimistic |  0 | 0 | n/a | [n/a, n/a] | n/a | 0 | 0 |
| overall = train  | optimistic  | 70 | 28 | -0.3017 | [-1.3076, +0.7660] | -0.0342 | -  | -  |
| test             | optimistic  |  0 | 0 | n/a | [n/a, n/a] | n/a | -  | -  |

**KXNCAAFTEAMTOTAL** (99 markets, 40 events, train_frac 45.4%)

| split | fill_model | n_trading | n_events | mean pnl/mkt | 95% CI (event-clustered) | roc | held | short->yes |
|---|---|---:|---:|---:|:---|---:|---:|---:|
| overall | pessimistic | 65 | 29 | +1.6542 | [+0.5962, +2.8406] | 0.1440 | 47 | 10 |
| train   | pessimistic | 24 | 13 | +2.4650 | [+0.9811, +4.0708] | 0.1756 | 17 |  5 |
| test    | pessimistic | 41 | 16 | +1.1795 | [-0.1457, +2.5979] | 0.1180 | 30 |  5 |
| overall | optimistic  | 65 | 29 | +1.7423 | [+0.7344, +2.8311] | 0.1517 | -  | -  |
| train   | optimistic  | 24 | 13 | +2.4396 | [+0.9811, +4.0454] | 0.1738 | -  | -  |
| test    | optimistic  | 41 | 16 | +1.3341 | [-0.0663, +2.5979] | 0.1335 | -  | -  |

**KXNCAAF1HSPREAD** (166 markets, 57 events, train_frac 44.0%)

| split | fill_model | n_trading | n_events | mean pnl/mkt | 95% CI (event-clustered) | roc | held | short->yes |
|---|---|---:|---:|---:|:---|---:|---:|---:|
| overall | pessimistic | 64 | 31 | +1.4677 | [-0.1866, +2.8821] | 0.0919 | 57 | 24 |
| train   | pessimistic | 30 | 16 | +0.6937 | [-1.7897, +2.7716] | 0.0439 | 29 | 14 |
| test    | pessimistic | 34 | 15 | +2.1506 | [-0.1548, +4.4206] | 0.1335 | 28 | 10 |
| overall | optimistic  | 64 | 31 | +1.5870 | [-0.1104, +3.0851] | 0.0995 | -  | -  |
| train   | optimistic  | 30 | 16 | +0.8587 | [-1.7888, +2.9338] | 0.0543 | -  | -  |
| test    | optimistic  | 34 | 15 | +2.2297 | [-0.0316, +4.6455] | 0.1386 | -  | -  |

**KXCS2GAME** (149 markets, 124 events, train_frac 71.1%)

| split | fill_model | n_trading | n_events | mean pnl/mkt | 95% CI (event-clustered) | roc | held | short->yes |
|---|---|---:|---:|---:|:---|---:|---:|---:|
| overall | pessimistic | 58 | 49 | +0.6803 | [-0.3523, +1.9233] | 0.0449 | 43 | 21 |
| train   | pessimistic | 36 | 31 | +0.5733 | [-0.7953, +2.1411] | 0.0328 | 27 | 14 |
| test    | pessimistic | 22 | 18 | +0.8555 | [-1.2300, +2.7361] | 0.0756 | 16 |  7 |
| overall | optimistic  | 64 | 54 | +0.6939 | [-0.5079, +1.8122] | 0.0506 | -  | -  |
| train   | optimistic  | 40 | 35 | +0.4408 | [-0.8187, +1.9418] | 0.0280 | -  | -  |
| test    | optimistic  | 24 | 19 | +1.1158 | [-0.9271, +2.9730] | 0.1075 | -  | -  |

Every CI table above spans zero except KXNCAAFTOTAL's train (below zero), KXITFMATCH's overall
and test, KXMLBKS's every split (below zero, both models), and KXNCAAFTEAMTOTAL's overall and
train (above zero) — mostly consistent with samples of a few dozen to a few hundred trading
markets, well short of the ~2,500-market scale T3's power table associates with the pessimistic
5th percentile turning positive.

## 5. Conclusion

**No challenger passed the two-split rule at any of the 11 scopes tested.** `app/strategies/
market_making.py`'s defaults — `min_spread=0.10`, `edge_fraction=0.80`, `max_inventory=20.0`,
`skew_strength=1.0` — are unchanged, and neither that file nor
`tests/strategies/test_market_making.py` was edited by this task. The venue-wide sweep came the
closest (51/60, 85%) with a candidate (`edge_fraction=0.90`, `min_spread=0.25`,
`max_inventory=50.0`) that clears the temporal test and shows a 2.7-4.1x ROC improvement over the
defaults — a real, reproducible signal, three halves short of the evidence bar this kit set before
looking. Section 2 states in full why re-scoring that candidate through the Gate 1 verdict
machinery is not itself a second verdict (circularity, underpowered sample, non-compliant split),
and Section 2a applies T5's measured 1-minute CI-widening correction to show the margin is
narrower than the raw hourly number suggests even setting those three objections aside.

The one substantive measurement this task set out to make — whether tightening `max_inventory`
reduces the settlement coin-flip's footprint at no cost — came back the OPPOSITE of the
hypothesis: tightening it to 10 does lower the held-into-settlement share (78.5% -> 72.9%) but at
more than 3x the cost in return on capital, and every one of the ten largest series' own grid
searches independently landed on `max_inventory=50`, not a tighter value, in nine of ten cases.
This is measured, not assumed, exactly as instructed, and it argues against `max_inventory` being
the free lever it might otherwise have looked like.
