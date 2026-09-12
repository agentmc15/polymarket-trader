# Kalshi honest holdout — the event-disjoint, multi-week test

**NO-GO. This project does not have its first honest GO.** On a sample that is
genuinely event-disjoint from the tuning cache (`event_overlap=0`, verified
outside the harness), drawn stratified across close weeks, scored at 1-minute
resolution over a **17.18-day** test window, the candidate
`edge_fraction=0.90, min_spread=0.25, max_inventory=50.0` produces a pessimistic
test CI of `[+0.0365, +0.8615]` at `n_trading=919`, `n_events_trading=789`
(`fill_model=pessimistic`, `terminal=settled`). Of the five conditions fixed in
advance, **four are met and one is not: `n_trading=919` is below the 1,000-market
floor.** That alone forbids calling this a pass.

It is worse than a near miss, and the second finding matters more than the first.
**The CI's clearance of zero does not survive removing any single largest
contributor.** Dropping the biggest series (`KXCS2GAME`, 14.4% of test P&L) gives
`[-0.0088, +0.7768]`; dropping the second (`KXITFWMATCH`) gives
`[-0.0225, +0.7653]`; dropping the third gives `[-0.0091, +0.8214]`. All three are
NO-GO. **The five largest single markets carry 39.95% of the entire test P&L, and
one market carries 10.73%.** A result that a single market's settlement can flip is
not an edge that has been established.

The one unambiguously positive result is elsewhere, and it is a real one:
**`mm_calibrate.two_split_rule` now PASSES at 1-minute, 59 of 60 event-disjoint
halves** (`temporal_win=true`), against the 51/60 failure T4 measured on hourly
candles. The candidate really does beat `0.80/0.10/20.0` on random event-halves of
this sample — but that is a tuning gate evaluated on the very sample being scored,
not independent evidence of an edge.

And the shipped-before defaults are now decisively loss-making out of sample:
`[-0.7607, -0.2473]` at `n_trading=1448`, `n_events_trading=1202`
(`fill_model=pessimistic`, `terminal=settled`) — the best-powered negative result
this kit has produced, and a **sign reversal** from the T18 holdout's `+0.0661` on
the same policy.

---

## 0. Why this report exists, and what was wrong with the last one

`kalshi-holdout.md` reported a GO for this candidate at 1-minute resolution:
`[+0.4522, +1.0977]`, `n_trading=1477`. Its `overlap=0` claim was true and
insufficient. Re-measured here, directly, on the two shipped caches:

```
tuning  .cache/mm/kalshi-60m.json        15,283 markets   7,297 events
T18 holdout .cache/mm/kalshi-holdout-1m.json  11,911 markets  7,817 events
  market_id overlap                            0
  EVENT overlap                            3,183
  holdout markets belonging to a shared event  6,073  (51.0%)
```

Every confidence interval in this kit is **clustered by event** (GUARDRAILS §2.4)
because markets inside one event settle on one real-world outcome. So the event —
not the ticker — is the unit of independence, and 51% of that "out-of-sample" test
shared its unit of independence with the tuning set. Its test window was also
1.70 days.

This report fixes both, at the cost of the `n` that the fix turns out to make
impossible on the venue's visible history.

## 1. What was built

Two capabilities were added to `backend/app/scripts/mm_backtest.py`. Nothing else
in that module changed — not the P&L convention, the event-clustered bootstrap,
the power floor, the straddle rule, the universe provenance block, the transport
handling, or the cache flush.

**`--exclude-events-from PATH`** (`_load_excluded_events`, `_exclude_events`,
`_market_event`). Reads the `event` field of a previous `--cache` file and removes
every universe market belonging to one of those events, **before** the sample is
drawn. The event key applies the same `event_id or market_id` fallback
`_collect_one` stamps on `MarketCandles.event`, so a market with no event — a
cluster of one, cached under its own id — cannot be redrawn through a hole in the
event filter.

**`--stratify-by-close-week`** (`_close_week`,
`_stratified_sample_by_close_week`). Buckets the filtered universe by ISO close
week and gives each week an equal quota, processing weeks in **ascending order of
supply** so that a week which cannot fill its quota contributes everything it has
and the shortfall spills onto the weeks that can. The draw within a week is
uniform and seeded, so `--seed` still reproduces the sample exactly. The collection
run prints the achieved per-week counts beside each week's available supply,
because the achieved distribution — never the requested one — is what a reader has
to be able to check.

**Tests** (`backend/tests/scripts/test_mm_backtest.py`, 60 → 68 tests). Four for
the event contract: that `--exclude-events-from` drops a market
`--exclude-markets-from` would have kept (both filters asserted side by side, so
the gap is pinned rather than described); that a fresh draw has **zero EVENT
overlap** with the excluded cache, reproducing `_collect_or_load`'s own
exclude-then-draw order; that the event key falls back to the market id on both
sides; and that a missing path raises rather than silently excluding nothing. Four
for stratification: that the achieved per-week distribution is materially flatter
than a natural draw (measured as the coefficient of variation of per-week counts
over **every** universe week, not only the weeks a draw happened to reach —
a draw landing entirely in one week has a spread of zero by the second reading);
that a starved week gives everything it has and the shortfall spills; that no
market is drawn twice and `k > pool` returns the pool; and that the bucket key is
the ISO week of the UTC close.

**Mutation-proven**, in memory only — the kit's files are untracked, so
`git checkout` cannot restore one and no mutant was ever written over a target
(GUARDRAILS §3.4). Eight mutants, eight trips: `_exclude_events` as the identity
(3 tests trip); `_exclude_events` keyed on a bare `event_id` with no fallback
(trips); the stratifier replaced by `random.sample` (2 trip); the stratifier
walking weeks in **descending** supply order (trips — the ascending order is
load-bearing, not decoration); and `_load_excluded_events` reading `market_id`
instead of `event` (trips).

## 2. The sample

```
--exclude-events-from .cache/mm/kalshi-60m.json --stratify-by-close-week
--min-volume 2000 --interval 1 --days 10 --sample 5000 --seed 20260912
--cache .cache/mm/kalshi-honest-1m.json --concurrency 8
```

The settled universe was **not re-walked**: `.cache/mm/kalshi-60m.universe.json`
(50,470 volume-qualified settled markets, `collected_at 2026-09-07T11:32:00Z`, 168
excluded for a non-binary result) was copied verbatim to this run's sidecar, so
this sample and the tuning cache are drawn from the identical universe and the
exclusion is exact rather than approximate. No new `/events` walk, no change to
the page-cap caveat that universe carries.

**The event exclusion is enormous, and it is the finding underneath every number
below.** The tuning cache's 7,297 events cover **33,213 of the 50,470 universe
markets (65.8%)**. Only **17,257** markets remain to sample from at all.

### 2.1 `event_overlap=0` — checkable, verified outside the harness

```
$ cd backend && python3 -c "
import json
a=json.load(open('.cache/mm/kalshi-60m.json'))['markets']
b=json.load(open('.cache/mm/kalshi-honest-1m.json'))['markets']
ea={m['event'] for m in a}; eb={m['event'] for m in b}
ia={m['market_id'] for m in a}; ib={m['market_id'] for m in b}
print('event_overlap=%d' % len(ea&eb)); print('market_id_overlap=%d' % len(ia&ib))"
event_overlap=0
market_id_overlap=0
```

Both figures are also in `kalshi-honest-holdout.json` under `overlap`, computed
there by raw `json.load` of both files rather than through
`_load_excluded_events` — the harness's own filter is not evidence about the
harness's own filter. Tuning cache 15,283 markets / 7,297 events; this holdout
4,903 markets / 3,817 events.

### 2.2 Achieved per-week counts — and why the distribution is still not flat

| ISO week | dates | available after event exclusion | drawn | usable history | one seeded natural draw of the same size |
|---|---|---:|---:|---:|---:|
| 2026-W27 | 06-29..07-05 | 8 | 8 | 0 | 3 |
| 2026-W28 | 07-06..07-12 | 52 | 52 | 43 | 10 |
| 2026-W29 | 07-13..07-19 | 43 | 43 | 35 | 11 |
| 2026-W30 | 07-20..07-26 | 63 | 63 | 36 | 19 |
| 2026-W31 | 07-27..08-02 | 38 | 38 | 29 | 8 |
| 2026-W32 | 08-03..08-09 | 94 | 94 | 91 | 23 |
| 2026-W33 | 08-10..08-16 | 52 | 52 | 47 | 15 |
| 2026-W34 | 08-17..08-23 | 54 | 54 | 51 | 12 |
| 2026-W35 | 08-24..08-30 | 96 | 96 | 78 | 33 |
| **2026-W36** | **08-31..09-06** | **15,475** | **3,218** | **3,217** | **4,379** |
| 2026-W37 | 09-07..09-13 | 1,282 | 1,282 | 1,276 | 390 |
| **total** | | **17,257** | **5,000** | **4,903** | **4,903** |

The last column is one `random.sample(remaining, 4903)` at the same seed — one
draw, not an expectation, drawn at the size actually collected so the two columns
are comparable. Both it and the supply column are in the JSON under `week_supply`.

**Every older week was drawn to exhaustion.** The stratifier took 100% of the
supply in ten of the eleven weeks and capped only W36. That is the design working
exactly as intended and still failing to produce a flat distribution, because
**after event-level exclusion the venue's visible history contains only ~520
markets closing before 2026-09-01 against 16,737 closing in the seven days after
it.** The per-week coefficient of variation — computed over every week the
post-exclusion universe holds, so a week a draw misses counts as zero — falls from
**2.8009 (natural) to 2.1194 (stratified)**: materially flatter, and nowhere near
flat.

This is a finding about the venue's listing, not a defect in the draw. It is the
same page-cap artifact `SETTLED_LISTING_PROVENANCE` already records (2026-08 is
~94% missing from the settled listing), sharpened by the fact that the 65.8% of
the universe removed by the event exclusion is not removed evenly across weeks.

### 2.3 The split, and the window it buys

| | |
|---|---|
| markets collected | 4,903 of 5,000 requested (77 too short, 0 payload errors, 20 request errors, `failure_rate=0.0040`) |
| candles | 1,344,429 (274 per market) |
| closes span | 2026-07-07T23:52:22Z → 2026-09-07T11:20:53Z (61.48 days) |
| temporal cutoff | **2026-08-21T00:00:00Z** (`cutoff_source=argument`) |
| train | 324 markets — **achieved train share 6.61%** |
| test | 4,579 markets |
| **test window** | 2026-08-21T07:00:06Z → 2026-09-07T11:20:53Z = **17.18 days** |
| straddle rule | 8 events straddle the cutoff; 10 post-cutoff markets dropped from test (they remain in `overall`) |
| `n_unmarkable_intervals` | 0, both policies, both fill models |
| fees | `KalshiFeeModel maker_rate=0.0175 taker_rate=0.07 maker_rebate_rate=0.0 source=settings_default`; rebate would add **$0.0000** if paid as published, and is in no P&L figure here (GUARDRAILS §2.3) |

A 6.61% train share is not a mistake and not a hold-out ratio anyone would choose;
it is the arithmetic consequence of demanding a ≥14-day test window from a pool
whose entire pre-September content is ~520 markets. The verdict is computed on the
test half only, and the parameters under test were fixed elsewhere and long before
this sample existed, so the train half is reported for completeness rather than
used. **A ≥14-day window was the goal and was achieved in span. It was not
achieved in density**: within the test split, 3,217 of 4,579 markets close in
W36 and 1,276 on the single day 2026-09-07 — see §4.3.

## 3. Verdicts — `fill_model=pessimistic` first, `terminal=settled`, `split=temporal-test`

```
VERDICT kalshi-honest-1m fill_model=pessimistic terminal=settled split=temporal-test policy=defaults(0.80/0.10/20.0) ci_low=-0.7607 ci_high=-0.2473 n_trading=1448 n_events_trading=1202 -> NO-GO
VERDICT kalshi-honest-1m fill_model=pessimistic terminal=settled split=temporal-test policy=candidate(0.90/0.25/50.0) ci_low=+0.0365 ci_high=+0.8615 n_trading=919 n_events_trading=789 -> GO (but see n_trading and section 4)
VERDICT kalshi-honest-1m fill_model=optimistic terminal=settled split=temporal-test policy=defaults(0.80/0.10/20.0) ci_low=-0.6816 ci_high=-0.1345 n_trading=1465 n_events_trading=1213 -> NO-GO
VERDICT kalshi-honest-1m fill_model=optimistic terminal=settled split=temporal-test policy=candidate(0.90/0.25/50.0) ci_low=+0.0457 ci_high=+0.8595 n_trading=935 n_events_trading=804 -> GO (same caveats)
```

| policy | fill_model | n_quoted | n_trading | n_events_trading | mean pnl/mkt | CI (event-clustered) | vs 1,000 floor |
|---|---|---:|---:|---:|---:|---|---|
| **defaults** (0.80/0.10/20.0) | **pessimistic** | 1,771 | **1,448** | 1,202 | −0.5170 | `[-0.7607, -0.2473]` | 1.45× |
| **candidate** (0.90/0.25/50.0) | **pessimistic** | 1,581 | **919** | 789 | +0.4360 | `[+0.0365, +0.8615]` | **0.92× — BELOW** |
| defaults | optimistic | 1,771 | 1,465 | 1,213 | −0.4138 | `[-0.6816, -0.1345]` | 1.47× |
| candidate | optimistic | 1,581 | 935 | 804 | +0.4530 | `[+0.0457, +0.8595]` | **0.94× — BELOW** |

### 3.1 Full detail — `overall` / `train` / `test`, pessimistic first, `terminal=settled`

**Defaults (0.80/0.10/20.0), `fill_model=pessimistic`:**

| block | n_markets | n_quoted | n_trading | n_events_trading | n_fills | mean pnl/mkt | ci95 clustered by event | roc | collateral mean | held | short→yes |
|---|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|
| overall | 4,903 | 1,894 | 1,546 | 1,259 | 6,623 | −0.5508 | `[-0.8051, -0.3112]` | −0.0657 | 6.8415 | 1,254 | 606 |
| train | 324 | 123 | 98 | 57 | 384 | −1.0500 | `[-1.9414, -0.1273]` | −0.1169 | 7.1587 | 82 | 47 |
| test | 4,569 | 1,771 | 1,448 | 1,202 | 6,239 | −0.5170 | `[-0.7607, -0.2473]` | −0.0620 | 6.8195 | 1,172 | 559 |

**Candidate (0.90/0.25/50.0), `fill_model=pessimistic`:**

| block | n_markets | n_quoted | n_trading | n_events_trading | n_fills | mean pnl/mkt | ci95 clustered by event | roc | collateral mean | held | short→yes |
|---|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|
| overall | 4,903 | 1,684 | 990 | 834 | 2,533 | +0.4471 | `[+0.0724, +0.8072]` | +0.0526 | 4.9920 | 868 | 417 |
| train | 324 | 103 | 71 | 45 | 141 | +0.5906 | `[-0.4760, +1.7912]` | +0.0765 | 5.3234 | 63 | 41 |
| test | 4,569 | 1,581 | 919 | 789 | 2,392 | +0.4360 | `[+0.0365, +0.8615]` | +0.0510 | 4.9704 | 805 | 376 |

`held_into_settlement` (test, pessimistic): defaults 1,172/1,448 = 80.9%; candidate
805/919 = 87.6%.

### 3.2 Why `n_trading` came in at 919, stated plainly

`--sample 5000` was chosen **before any result was seen**, from the T18 sample's
measured trading rate (`kalshi-holdout.md` §4.2: 4,198 trading markets of 11,911
collected, 35%), which projected roughly 1,600 on a test split of this size. The
realized rate is 20% — 919 of 4,569 test markets. The cause is measurable: this
sample averages **274 candles per market against the T18 sample's 613**, because
stratification deliberately pulls in older weeks whose markets are short-lived and
because the W36/W37 markets that remain after event exclusion are thinner than the
ones the tuning cache took. Fewer intervals per market means fewer chances for a
resting quote to be reached at all — `n_quoted` is 1,684 of 4,903 (34%) against
T18's 7,153 of 11,911 (60%).

**No additional markets were collected after seeing this.** ~12,000 W36 markets
remain unsampled and collecting ~2,000 of them would very likely push `n_trading`
past 1,000. Doing so after observing a marginal positive is precisely the
"widen the sample to make `n`" move GUARDRAILS §2.6 forbids one level up, and the
honest report is the power of the sample drawn, not the sample that would have
given the desired answer. If a future task wants that `n`, it must fix the sample
size in advance and accept whatever comes out.

### 3.3 Power (test split, `fill_model=pessimistic`, `terminal=settled`, resampling whole events with replacement)

| policy | portfolio | events drawn | 5th pct total | P(profit) |
|---|---:|---:|---:|---:|
| defaults | 500 | 415 | −436.75 | 0.012 |
| defaults | 1,000 | 830 | −761.58 | 0.000 |
| defaults | 2,500 | 2,075 | −1,704.01 | 0.000 |
| defaults | 5,000 | 4,151 | −3,166.99 | 0.000 |
| candidate | 500 | 429 | +6.47 | 0.954 |
| candidate | 1,000 | 859 | +93.24 | 0.998 |
| candidate | 2,500 | 2,146 | +615.25 | 1.000 |
| candidate | 5,000 | 4,293 | +1,494.99 | 1.000 |

Both pools clear `MIN_POWER_POOL_EVENTS=30` by more than an order of magnitude
(1,202 and 789 events).

## 4. Concentration — the finding that outranks the CI

### 4.1 By series, candidate, `fill_model=pessimistic`, `terminal=settled`, test split (total +$400.67 over 401 series)

| series | n_trading | n_events | total P&L | share of test P&L | mean/mkt |
|---|---:|---:|---:|---:|---:|
| KXCS2GAME | 19 | 15 | +57.85 | **14.44%** | +3.0447 |
| KXITFWMATCH | 25 | 23 | +48.54 | 12.11% | +1.9416 |
| KXNCAAFFIRSTTDTEAM | **1** | 1 | +43.00 | 10.73% | +43.0000 |
| KXPGAPLAYOFF | **1** | 1 | +42.75 | 10.67% | +42.7500 |
| KXNCAAF1QSPREAD | 10 | 4 | +42.31 | 10.56% | +4.2310 |
| KXITFMATCH | 33 | 30 | +34.88 | 8.71% | +1.0570 |
| KXCPLTEAMTOTAL | 9 | 4 | +31.54 | 7.87% | +3.5044 |
| KXEPL2H | 3 | 2 | +30.59 | 7.63% | +10.1967 |
| … | | | | | |
| KXMLBF3 | 11 | 11 | −46.06 | −11.50% | −4.1873 |

**The verdict with the largest contributor removed, recomputed through the
harness's own `_block` (cluster bootstrap included):**

| removed | share removed | n_trading | n_events_trading | mean pnl/mkt | CI | verdict |
|---|---:|---:|---:|---:|---|:--:|
| KXCS2GAME (largest) | 14.44% | 900 | 774 | +0.3809 | `[-0.0088, +0.7768]` | **NO-GO** |
| KXITFWMATCH (2nd) | 12.11% | 894 | 766 | +0.3939 | `[-0.0225, +0.7653]` | **NO-GO** |
| KXNCAAFFIRSTTDTEAM (3rd) | 10.73% | 918 | 788 | +0.3896 | `[-0.0091, +0.8214]` | **NO-GO** |
| top two, cumulatively | 26.55% | 875 | — | +0.3363 | `[-0.0908, +0.7900]` | **NO-GO** |
| top three, cumulatively | 37.28% | 874 | — | +0.2875 | `[-0.0990, +0.6838]` | **NO-GO** |

This is not "one sport carries the result" in the shape the brief anticipated — no
single sport is 75% of it, as NCAAF was in the rejected report. It is worse in a
different way: **the positive result is a thin margin over a long tail, and any of
at least three independent slices removes it.**

### 4.2 It is not really about sports — it is about a handful of markets

**The five largest single markets carry 39.95% of the candidate's whole test P&L.**
And 221 of the 401 series in the test split contributed exactly one trading market
each; those 221 one-market series together carry 61.15% of the test P&L, so the
"series" axis is in large part a market axis wearing a label.

| market | series | n_fills | P&L | share | close |
|---|---|---:|---:|---:|---|
| KXNCAAFFIRSTTDTEAM-26SEP03COLOGT-COLO | KXNCAAFFIRSTTDTEAM | 22 | +43.00 | 10.73% | 2026-09-04T01:17Z |
| KXPGAPLAYOFF-BMC26 | KXPGAPLAYOFF | 39 | +42.75 | 10.67% | 2026-08-23T22:50Z |
| KXLEAGUESCUPADVANCE-26SEP05LEOAME-AME | KXLEAGUESCUPADVANCE | **4** | +27.53 | 6.87% | 2026-09-07T00:30Z |
| KXATPSETWINNER-26SEP03SWEMUS-4-MUS | KXATPSETWINNER-… | **9** | +23.48 | 5.86% | 2026-09-03T21:12Z |
| KXCS2GAME-26SEP021400REVR4G-R4G | KXCS2GAME | **5** | +23.30 | 5.82% | 2026-09-02T21:59Z |
| KXCS2GAME-26SEP060400MEGTRA-MEG | KXCS2GAME | **3** | +22.00 | 5.49% | 2026-09-06T10:49Z |

Four fills earning $27.53 at `quote_size=10` is not spread capture. It is an
inventory position carried into a settlement that went the right way. The
decomposition confirms the shape at the aggregate level: the candidate's test
`pnl` of **+$400.67** is the residual of **+$5,820.67 of fill cash** against
**−$5,420.00 paid out at settlement** (`fill_model=pessimistic`,
`terminal=settled`) — a 6.9% net on two opposing flows each fourteen times larger
than it. The defaults sit on the same knife edge with the sign against them:
+$6,441.44 of fill cash against −$7,190.00 of settlement, netting −$748.56. (The
ratio "settlement share of total" is not quoted as a percentage anywhere here
because its denominator is the small residual, which makes it meaningless.)

### 4.3 And it is concentrated in time, inside a window that *spans* 17 days

| ISO close week | test markets | candidate n_trading | candidate test P&L | share |
|---|---:|---:|---:|---:|
| 2026-W34 (08-21..08-23) | 8 | 4 | +51.41 | 12.83% |
| 2026-W35 (08-24..08-30) | 78 | 37 | +73.28 | 18.29% |
| 2026-W36 (08-31..09-06) | 3,217 | 731 | +79.46 | 19.83% |
| **2026-W37 (09-07 only)** | **1,276** | **147** | **+196.52** | **49.05%** |

**Half the candidate's test P&L closes on one day.** The 41 trading markets before
September contribute +$124.69 (mean +$3.04/market) against +$79.46 from the 731
trading markets of W36 (mean +$0.11) — a 28× difference in mean between adjacent
slices of the same test split, which is what small-sample noise looks like, not a
stable mechanism. The ≥14-day window criterion was met in span and, on this
evidence, does not deliver what it was asked for.

The defaults' test P&L is concentrated the same way with the opposite sign: W36
alone contributes −$875.92, i.e. 117% of the −$748.56 total, with the other three
weeks slightly positive.

## 5. The two-split rule at 1-minute — it passes

Run through `mm_calibrate.two_split_rule` (imported, not reimplemented) over the
full 48-point `grid()`, all replayed on the identical market list under
`fill_model="pessimistic"`, `objective=_return_on_capital`, `n_halves=60`,
`seed=20260912`. The halves are event-disjoint by construction —
`two_split_rule` partitions through `mm_backtest._split(split="event")`, which
assigns whole events to one side or the other.

| baseline | selected candidate | wins / 60 | win rate | times selected by tuning | temporal default ROC | temporal candidate ROC | temporal_win | **passed** |
|---|---|---:|---:|---:|---:|---:|:--:|:--:|
| `0.80/0.10/20.0` (the defaults T4 measured against) | `0.90/0.25/50.0` | **59 / 60** | 0.9833 | 59 | −0.0620 | +0.0510 | true | **YES** |
| `0.90/0.25/50.0` (what is shipped today) | none | 0 / 60 | 0.0000 | 0 | — | — | — | no |

**`wins=59/60`, `passed=true`.** The only prior 60-halves run on this grid was
hourly and failed at 51/60 (`kalshi-calibration.md` §1); at 1-minute resolution on
an event-disjoint, close-week-stratified sample the same rule clears its 90% floor
with room. The second row says the obvious complement: with the candidate itself as
the baseline, no grid point beats it on enough halves to be selected at all.

What this is *not*: independent evidence of an edge. The rule tunes and scores on
random halves of **this same sample**, which is exactly the "tunes and scores on
the same split" the report's own verdict is forbidden to rest on (GUARDRAILS §4.1).
It establishes that `0.90/0.25/50.0` is robustly the better of the two policies on
this data. It does not establish that the better of two policies makes money.

## 6. The five conditions fixed in advance, one line each

| # | condition | measured | met? |
|---|---|---|:--:|
| 1 | candidate pessimistic test CI clears zero | `[+0.0365, +0.8615]` | **YES** |
| 2 | on an event-disjoint sample | `event_overlap=0` | **YES** |
| 3 | over a ≥14-day test window | 17.18 days | **YES** |
| 4 | at `n_trading ≥ 1000` | **919** | **NO** |
| 5 | and the two-split rule passes | 59/60, `passed=true` | **YES** |

**Condition 4 is not met, so this is not a GO, and no partial reading of the other
four makes it one.** Beyond the mechanical miss, §4 shows condition 1 is itself
fragile: it fails on removal of any of the three largest series contributors, and
39.95% of the P&L behind it comes from five markets.

## 7. What this does and does not conclude

- **The T18 GO does not survive event-level disjointness.** Same candidate, same
  resolution, same fill model, same harness: `[+0.4522, +1.0977]` at
  `n_trading=1477` becomes `[+0.0365, +0.8615]` at `n_trading=919` once the 51% of
  markets sharing an event with the tuning set are gone and the sample is spread
  across weeks. The point estimate falls from +0.7717 to +0.4360 and the lower
  bound from +0.45 to +0.04. The Phase 1 review's event-disjoint subset finding
  (`[-0.1760, +0.9415]`) is directionally reproduced on an independently collected
  sample.
- **The defaults are now clearly loss-making out of sample, and that is a sign
  reversal worth naming.** `[-0.7607, -0.2473]`, `n_trading=1448` here against
  `[-0.1646, +0.3154]`, `n_trading=2150` in T18 — both `fill_model=pessimistic`,
  `terminal=settled`, both 1-minute. The T18 reading was "near zero, well
  powered"; this one is "negative, well powered". The difference is what the
  exclusion removed: 65.8% of the universe, being every market in an event the
  defaults were previously scored on.
- **The mechanism claim is not settled by this report and is dented by it.** The
  wide quoter still out-earns the tight quoter here by every measure. But the wide
  quoter's positive block is a 6.9% residual of two flows fourteen times its size,
  with 40% of it in five markets, several of which earned it on three to five
  fills. That is not the shape of a per-fill edge compounding over many fills.
- **Stratified sampling worked as designed and could not overcome the venue's
  visible history.** Ten of eleven weeks were drawn to exhaustion; the achieved
  spread fell from CV 2.8009 to 2.1194; and the test window still has 98.1% of its
  markets in the last eight days. The binding constraint is that after event
  exclusion only ~520 event-disjoint markets close before 2026-09-01 — a fact
  about the page-capped settled listing, not about the sampler.
- **The two-split rule's 59/60 pass is real, is the first time this gate has been
  cleared for these parameters, and is not a substitute for the verdict.**
- **Nothing in `app/strategies/market_making.py` was read as a decision here.**
  The shipped defaults have already moved to `0.90/0.25/50.0` (by another task);
  this report neither endorses nor reverses that. What it says is that the
  evidence for it out of sample is weaker than `kalshi-holdout.md` reported, and
  that it does not meet the bar this task was asked to test against.
- **What a next attempt would need.** A test window whose *density* is multi-week,
  not only its span. On this venue's visible listing that is not obtainable by
  sampling — it needs either a listing that is not page-capped (the cap
  over-represents recent closes by construction) or a forward-collected sample
  accumulated over weeks. A larger draw from W36 would buy `n_trading` and buy
  nothing at all against the concentration in §4.

## 8. Provenance

- **Source JSON: `.claude/kits/mm-proveout/reports/kalshi-honest-holdout.json`.**
  Every figure in this report is in it, including the full `overall`/`train`/`test`
  blocks for both policies under both fill models, the per-week and per-day sample
  histograms, the per-week available supply and the seeded natural comparison draw
  with both coefficients of variation (`week_supply`), the complete per-series
  breakdown, the leave-one-out and cumulative removals, the top-market table, the
  P&L decomposition, the power tables and both `two_split_rule` blocks. It was
  written by one script (`analyse_honest.py`), which imports `replay`, `report`,
  `_split`, `_block` from `mm_backtest` and `two_split_rule`/`grid`/`policy_for`
  from `mm_calibrate` — nothing is reimplemented, and every CI including the
  leave-one-out ones goes through the harness's own `cluster_bootstrap`.
- **Deliberately unfinished, and named rather than hidden:** that script lives in
  the session scratchpad and is NOT added to the repo, because the brief named two
  capabilities in `mm_backtest.py` plus this report and its JSON, and adding a
  third file is scope this task did not have. The consequence is that the JSON is
  the reproducible artifact and the generator is not; a future task that wants the
  generator committed should say so and pick its home.
- **Reproducibility, measured rather than asserted.** The analysis was run four
  times. Runs 2, 3 and 4 produced identical verdict, leave-one-out and two-split
  lines (checked with `diff` after stripping elapsed-time annotations); run 1
  differed only in those annotations. Runs 3 and 4 each added a block without
  moving a single existing number.
- **Collection.** `.cache/mm/kalshi-honest-1m.json` and its
  `.universe.json` sidecar are new, `.gitignore`d cache files (the sidecar a
  verbatim copy of `kalshi-60m.universe.json`). `kalshi-60m.json`,
  `kalshi-holdout-1m.json` and every other existing cache were opened read-only
  and never written. Collection was 4,903/5,000 markets, `failure_rate=0.0040`,
  0 payload errors — below `MAX_COLLECT_FAILURE_RATE=0.02`.
- **Network.** Read-only: the cached `/events` universe walk was reused, and the
  only live traffic was the adapter's candlestick `GET`s (3 chunks per market at
  `interval=1` over a 10-day span). No order path exists in anything run here;
  `.env` was never opened.
- **Tests.** `cd backend && python3 -m pytest -q tests/scripts/test_mm_backtest.py`
  → **68 passed** (60 before this change; the 8 added are the ones §1 describes,
  and that is the only test count this work is entitled to quote). The full suite
  passed green at every run, but its total is not a number this report can pin:
  measured **1,424** early in the session and **1,433** at the end, because other
  agents are concurrently adding tests to this same working tree (`git status`
  shows modifications to `app/strategies/market_making.py`,
  `app/services/data_collector.py`, `app/tasks/` and others that this task never
  touched). The brief's stated 1,411 baseline was already stale when this task
  began. Re-measure rather than trust any of these three numbers.
  `python3 -m ruff check app/scripts/mm_backtest.py
  tests/scripts/test_mm_backtest.py` → clean.
- **Files this task changed:** `backend/app/scripts/mm_backtest.py`,
  `backend/tests/scripts/test_mm_backtest.py`,
  `.claude/kits/mm-proveout/reports/kalshi-honest-holdout.md` and
  `.../kalshi-honest-holdout.json`. Nothing else. In particular
  `app/strategies/market_making.py`, `mm_replay_snapshots.py`, `data_collector.py`,
  `app/tasks/`, `app/scripts/preflight.py` and `app/venues/` were not opened for
  writing — other agents' modifications to them are visible in `git status` and
  are not this task's.
- **Labels.** Every P&L, ROC and CI figure above carries `fill_model` and
  `terminal=settled` in its source JSON; pessimistic is reported first throughout
  and the verdict is computed on it (GUARDRAILS §2.1/§2.2). The maker rebate is
  $0.0000 at the shipped schedule and appears in no P&L (§2.3).
