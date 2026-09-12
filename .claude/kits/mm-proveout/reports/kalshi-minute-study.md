# T5 — What hourly candles cannot see: the 1-minute sub-study

**Status: measure-and-report only. No default was changed, no repo file was modified.**
`DEFAULT_SKEW_STRENGTH` remains 1.0. The recommendation and the evidence that would justify
acting on it are in §7.

---

## 0. Data window, before any number

| | |
|---|---|
| venue | kalshi |
| policy | `MarketMaker(min_spread=0.10, edge_fraction=0.80, max_inventory=20.0, quote_size=10.0, skew_strength=1.0)` |
| tick_size | 0.01 |
| fee model | `KalshiFeeModel`, `maker_rate=0.0175`, `taker_rate=0.07`, `maker_rebate_rate=0.0`, `fee_source=settings_default` |
| markets | 300 (the same 300 at both resolutions; no market is in one set and not the other) |
| events | 201 distinct; 201 among trading markets |
| first close | 2026-07-05T14:50:41+00:00 |
| last close | 2026-09-07T10:09:57+00:00 |
| **temporal cutoff** | **`cutoff_ts=1788513075` = 2026-09-04T09:11:15+00:00** |
| cutoff source | the 0.70 quantile of these 300 markets' own closes. **Realised train share 210/300 = 0.7000.** NOT the harness's median-close default |
| train / test | 210 markets (140 trading events) / 90 markets (61 trading events) |
| straddling events | 0; markets dropped from test 0 |
| interval=60 | 10-day lookback, 35,043 hourly candles, 34,443 quote/fill/mark triples |
| **interval=1** | 10-day lookback, 601,816 one-minute candles, 601,216 quote/fill/mark triples |
| seed | 20260906 |
| collection | 300/300 collected, 0 too short, 0 payload errors, 0 request errors, failure_rate 0.0000 |
| universe | 50,470 settled markets at `min_volume=2000`, 168 excluded for a non-binary result. **Page-capped listing** — see `SETTLED_LISTING_PROVENANCE`: ~38% of the tradeable settled universe is visible, month-correlated, so nothing here generalises to "Kalshi" |

**Selection rule.** The 300 markets with the highest `n_fills` under `fill_model=pessimistic`,
`terminal=settled` at interval=60 over the full 15,283-market T3 cache, ties broken by
`market_id` ascending. `n_fills` range **[5, 52]**. 102 markets share the boundary value
`n_fills=5`, so the boundary is a tie broken deterministically by id, not by rank.

**This is a deliberately unrepresentative sample and every number below inherits that.**
These are the busiest 2.0% of the cache. They are the markets where a live quoter would
actually rest size, which is what makes them the right sample for a resolution question,
and they are *not* a random draw — from the venue (the listing is page-capped) or from the
cache (they are the top tail of a fill distribution). No figure here is a Gate 1 verdict and
none should be read as one.

---

## 1. Collection: a hard venue limit the kit had not recorded

`--interval 1 --days 10` **cannot be served by one request**, and the first attempt at this
study failed 3/3 markets with `400 Bad Request` before a single candle was parsed.

Measured live 2026-09-07 against three markets from this study's own selection, walking the
window up:

      window (minutes at period_interval=1)     result
      60, 720, 1440, 2880, 4320, 5000           200 OK
      5040, 7200, 14400                         400 Bad Request

**Kalshi's candlestick endpoint refuses more than 5,000 periods per request.** A 10-day
one-minute window is 14,400 periods. `app/venues/kalshi/candles.py::fetch_candles` issues
exactly one GET, so `--interval 1` at the harness's default `--days 10` is a guaranteed 400
for every market — not a slow path, an impossible one. TASKS.md's "T2 supports it" was true
of the flag and false of the venue.

**What was done instead, and what was not.** The study's driver requests the window in
4,800-minute chunks and concatenates the parsed candles, de-duplicating on `end_ts`. Each
chunk goes through the real `fetch_candles` unchanged — same parser, same envelope
validation, same T1 strictness — and a raise in any chunk propagates rather than yielding a
silently shortened series. The chunking lives in the study's scratchpad driver and
**`app/venues/kalshi/candles.py` was not edited**, because this kit's files are untracked
and T4 may be editing `market_making.py` concurrently (GUARDRAILS.md §3.4). Collection
otherwise ran through `mm_backtest._collect_and_flush` verbatim: same batching, same
per-market failure isolation, same atomic flush.

Result: 900 GETs, 300/300 markets, zero failures, 601,816 candles into
`backend/.cache/mm/kalshi-1m.json` (min 52, median 1,691, max 8,819 candles per market).

**Carry-forward.** Anything in this kit that plans a 1-minute collection — T6's taper, T11's
Polymarket replay if it ever wants minute bars — needs the chunk, or it will 400. Making
`fetch_candles` chunk internally is the right fix and is deliberately left undone here.

---

## 2. Side-by-side: 60m vs 1m, same 300 markets, same cutoff

All figures `terminal=settled`. **Pessimistic first** (GUARDRAILS.md §2.2). Cutoff
2026-09-04T09:11:15+00:00 (0.70 quantile, 210/300 train) for every block in this section.
`pnl` is cash-settled money; `markout` is the mark-dependent quote-quality statistic beside
it, never a verdict basis. Maker rebate: **$0.00 would be added if paid as published**
(`maker_rebate_rate=0.0` on this schedule), and it is not in any P&L figure.

### 2.1 `fill_model=pessimistic`, `terminal=settled`

| block | interval | n_markets | n_trading | n_events_trading | quote intervals | n_fills | total pnl | mean pnl / trading market | sd | ci95 clustered by event | roc | collateral mean |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| overall | **60** | 300 | 300 | 201 | 13,158 | 2,583 | $1,185.05 | **$3.9502** | 6.0437 | [+3.1799, +4.7370] | 0.5406 | $7.3074 |
| overall | **1** | 300 | 300 | 201 | 236,003 | 5,031 | $1,183.41 | **$3.9447** | 8.7044 | [+2.7764, +5.3300] | 0.5386 | $7.3236 |
| train | **60** | 210 | 210 | 140 | 8,580 | 1,793 | $774.08 | $3.6861 | 5.6016 | [+2.8935, +4.5286] | 0.4910 | $7.5075 |
| train | **1** | 210 | 210 | 140 | 170,029 | 3,402 | $767.58 | $3.6551 | 8.2656 | [+2.3742, +4.8948] | 0.4866 | $7.5120 |
| test | **60** | 90 | 90 | 61 | 4,578 | 790 | $410.97 | $4.5663 | 6.9626 | [+2.6949, +6.2300] | 0.6675 | $6.8407 |
| test | **1** | 90 | 90 | 61 | 65,974 | 1,629 | $415.83 | $4.6203 | 9.6668 | [+2.1380, +7.6201] | 0.6712 | $6.8839 |

### 2.2 `fill_model=optimistic`, `terminal=settled`

| block | interval | n_trading | n_fills | total pnl | mean pnl / trading market | sd | ci95 clustered by event | roc |
|---|---|---|---|---|---|---|---|---|
| overall | **60** | 300 | 2,705 | $1,302.18 | $4.3406 | 6.2316 | [+3.5894, +5.1369] | 0.5965 |
| overall | **1** | 300 | 5,216 | $1,207.67 | $4.0256 | 8.9152 | [+2.8146, +5.4674] | 0.5536 |
| train | **60** | 210 | 1,884 | $880.78 | $4.1942 | 5.7649 | [+3.3398, +5.1252] | 0.5606 |
| train | **1** | 210 | 3,527 | $787.14 | $3.7483 | 8.5502 | [+2.4666, +5.0905] | 0.5028 |
| test | **60** | 90 | 821 | $421.40 | $4.6822 | 7.2283 | [+2.7612, +6.4438] | 0.6886 |
| test | **1** | 90 | 1,689 | $420.53 | $4.6726 | 9.7333 | [+2.0775, +7.7396] | 0.6828 |

### 2.3 What the side-by-side says

**The mean P&L is the same and the uncertainty is not.** `fill_model=pessimistic`,
`terminal=settled`, overall: $3.9502 at interval=60 against $3.9447 at interval=1 — a
difference of half a cent per market on $1,185 of total P&L. But the per-market standard
deviation rises from 6.04 to 8.70 (+44.0%) and the clustered interval widens from
[+3.1799, +4.7370] (width 1.557) to [+2.7764, +5.3300] (width 2.554, +64.0%). On the
out-of-sample half the widening is worse: [+2.6949, +6.2300] becomes
[+2.1380, +7.6201] (+63.7%).

**The edge per fill halves.** 2,583 fills at interval=60 against 5,031 at interval=1 —
1.95x the fills for the same money. $0.4588 of cash per fill becomes $0.2352. A quoter at
minute resolution trades twice as much to earn the same amount, which is twice the fee
exposure, twice the queue risk and twice the operational surface for the same expectation.
Return on capital is nearly unchanged (0.5406 vs 0.5386) because mean collateral per quoted
market barely moves ($7.3074 vs $7.3236) — the capital intensity is a property of the quote,
not of how often you look at it.

**`n_unmarkable_intervals` is 0 at both resolutions** — 0 of 34,443 at interval=60 and
**0 of 601,216 at interval=1**. This settles T2's carry-forward question in the place it was
most likely to fail. The mark gate discards nothing even at minute resolution, so no cash
figure in this kit is computed on a mark-filtered subsample.

---

## 3. Peak `|inventory|` at both resolutions

`fill_model=pessimistic`, `terminal=settled`, `max_inventory=20.0`, `quote_size=10.0`.
Peak is taken **after every fill**, not once per interval: an interval filled on both sides
moves inventory twice, and an interval-granular tracker would record a bid-then-ask round
trip as zero exposure — which is precisely the blindness this study exists to measure.

| statistic | interval=60 | interval=1 | change |
|---|---|---|---|
| mean peak `\|inventory\|` | 15.10 | **17.40** | **+2.30 contracts, +15.2%** |
| median peak | 20.0 | 20.0 | — |
| p90 / p95 / p99 peak | 20.0 | 20.0 | — |
| **max peak** | **20.0** | **20.0** | **0** |
| share of markets reaching `\|inv\| >= 20` | **51.0%** | **74.0%** | **+23.0 pp** |
| share of markets exceeding 20 | 0.0% | 0.0% | 0 |
| mean `\|terminal inventory\|` | 8.10 | 9.10 | +1.00, +12.3% |
| share with `\|terminal inv\| >= 20` | 12.0% | 17.7% | +5.7 pp |

Paired, market by market (300 pairs, both traded at both resolutions):

| | |
|---|---|
| 1m peak strictly higher | **86 markets (28.7%)** |
| equal | 197 (65.7%) |
| 60m peak strictly higher | 17 (5.7%) |
| mean delta | **+2.30 contracts** |
| median delta | 0.00 |
| max delta | **+10.0 contracts** |
| mean ratio 1m/60m | 1.258 |
| median / p90 / **max** ratio | 1.00 / 2.00 / **2.00** |

### 3.1 Time-weighted exposure — the figure T6 and T13 actually need

Peak says how big the position got. It cannot say how long it was there, and a capital
estimate needs the second. Within one resolution every quoting interval is the same length,
so a mean over quoting intervals *is* a time average. Inventory recorded is the position the
policy quoted against — the position exposed for that interval's duration.
`fill_model=pessimistic`, `terminal=settled`:

| statistic | interval=60 | interval=1 | understatement by the hourly replay |
|---|---|---|---|
| quoting intervals | 13,158 | 236,003 | — |
| **mean `\|inventory\|`, time-weighted** | **5.5054** | **6.9276** | **hourly is 20.5% low; 1m is 25.8% higher** |
| **median `\|inventory\|`** | **0.0** | **10.0** | hourly says "flat more than half the time"; 1m says "carrying 10 contracts more than half the time" |
| share of time at the inventory limit | 6.95% | **9.38%** | hourly is 25.9% low relative |
| share of time not flat | 48.10% | **59.90%** | hourly is 19.7% low relative |
| mean per-market share of time at limit | 6.74% | 11.02% | hourly is 38.8% low relative |

---

## 4. `held_into_settlement` across resolutions

`terminal=settled`, same 300 markets, `skew_strength=1.0`, `max_inventory=20.0`:

| | `fill_model=pessimistic` | | `fill_model=optimistic` | |
|---|---|---|---|---|
| | interval=60 | interval=1 | interval=60 | interval=1 |
| markets holding inventory into settlement | 207 / 300 | **220 / 300** | 202 / 300 | **233 / 300** |
| share | 69.0% | **73.3%** | 67.3% | **77.7%** |
| change | | **+13 markets, +4.3 pp** | | **+31 markets, +10.4 pp** |
| `settled_short_into_yes` | 79 | **88** | 76 | **89** |
| share of holders that are short into yes | 38.2% | 40.0% | 37.6% | 38.2% |

By train/test half, `fill_model=pessimistic`, `terminal=settled`: train 144 → 149, test
63 → 71.

**The direction is the opposite of the hypothesis in the brief.** Finer resolution does not
let the policy flatten more often. It flattens *less* often — 13 more markets carry a
position into settlement under the pessimistic model, 31 more under the optimistic one — and
carries a bigger position when it does (mean `|terminal inventory|` 8.10 → 9.10). More
opportunities to trade are, for this policy, more opportunities to *re-accumulate*, not to
get flat: at minute resolution the quote is re-posted 60x as often and the withdrawal logic
only removes the side that would make the position worse, so the flattening side keeps
resting and keeps being filled *and then refilled in the other direction*.

**So this does not explain the out-of-sample CI spanning zero — it deepens the problem.**
The Gate 1 NO-GO rests on inventory settling into a binary outcome, and the resolution a
live quoter actually experiences shows *more* of that exposure, not less. The kit's 78.5%
figure (3,390 of 4,320 trading markets holding into settlement, hourly) is a floor, not an
estimate: measured on these 300 markets the hourly replay understates the holding rate by
4.3 pp pessimistic and 10.4 pp optimistic.

**The tail is where the resolution difference is violent.** Worst single-market P&L,
`fill_model=pessimistic`, `terminal=settled`, `skew_strength=1.0`: **-$11.77 at interval=60,
-$46.80 at interval=1** — 3.98x worse. That is not a skew effect (it holds at every skew
level in §5) and it is not in any hourly table in this repo. `market_making.py`'s own
calibration comment quotes a worst of -6.90 at `max_inventory=20`; on the busiest 300
markets at the resolution a live quoter experiences, the worst market loses **-$46.80**.

---

## 5. Skew sweep at `max_inventory=20`, `interval=1`

`terminal=settled`. **Pessimistic first.** Cutoff 2026-09-04T09:11:15+00:00 (0.70 quantile,
210/300 train). `mean` is cash P&L per trading market; `5th pct` and `worst` are the 5th
percentile and minimum of per-market cash P&L across the 300 trading markets.

### 5.1 `fill_model=pessimistic`, `terminal=settled`, `interval=1` — the required table

| skew | mean | 5th pct | worst | total pnl | held into settlement | short into yes | mean peak `\|inv\|` | share at `\|inv\|>=20` | ci95 clustered by event (overall) |
|---|---|---|---|---|---|---|---|---|---|
| **0.0** | **+$8.4928** | -$8.69 | **-$33.95** | $2,547.85 | 246 (82.0%) | 98 | 19.77 | 97.7% | [+6.7967, +10.3197] |
| **0.5** | +$5.7968 | -$6.84 | -$45.10 | $1,739.04 | 233 (77.7%) | 92 | 18.87 | 88.7% | [+4.4059, +7.2818] |
| **1.0** *(current default)* | +$3.9447 | **-$6.47** | -$46.80 | $1,183.41 | 220 (73.3%) | 88 | 17.40 | 74.0% | [+2.7764, +5.3300] |
| **2.0** | +$0.6221 | -$9.05 | -$47.57 | $186.63 | 222 (74.0%) | 87 | 15.20 | 52.0% | **[-0.3600, +1.6630]** |

### 5.2 `fill_model=optimistic`, `terminal=settled`, `interval=1`

| skew | mean | 5th pct | worst | held into settlement | ci95 clustered by event (overall) |
|---|---|---|---|---|---|
| 0.0 | +$8.7688 | -$8.86 | -$39.21 | 245 | [+7.0700, +10.6655] |
| 0.5 | +$6.8938 | -$7.26 | -$41.72 | 242 | [+5.4853, +8.4778] |
| 1.0 | +$4.0256 | -$7.44 | -$46.80 | 233 | [+2.8146, +5.4674] |
| 2.0 | +$0.5921 | -$9.05 | -$47.13 | 229 | **[-0.3975, +1.6544]** |

### 5.3 Out-of-sample half only (`close_ts >= 1788513075`), n_trading=90, 61 events

`fill_model=pessimistic`, `terminal=settled`:

| skew | interval=60 mean | interval=60 ci95 | interval=1 mean | interval=1 ci95 |
|---|---|---|---|---|
| 0.0 | +$7.1641 | [+4.7552, +9.4171] | **+$9.6337** | [+6.1428, +13.6305] |
| 0.5 | +$5.6427 | [+3.6445, +7.4945] | +$6.7310 | [+3.8192, +10.1103] |
| 1.0 | +$4.5663 | [+2.6949, +6.2300] | +$4.6203 | [+2.1380, +7.6201] |
| 2.0 | +$2.6341 | [+1.3240, +3.9841] | +$1.0349 | **[-0.8574, +3.2505]** |

### 5.4 The resolution-controlled comparison — the point of the whole study

The same grid at interval=60 on the **same 300 markets and the same cutoff**, so the only
thing that differs is resolution. `fill_model=pessimistic`, `terminal=settled`, overall:

| skew | mean 60m | mean 1m | 5th pct 60m | 5th pct 1m | **worst 60m** | **worst 1m** | held 60m | held 1m | peak `\|inv\|` 60m | peak `\|inv\|` 1m |
|---|---|---|---|---|---|---|---|---|---|---|
| 0.0 | +$5.8659 | +$8.4928 | -$6.84 | -$8.69 | **-$14.76** | **-$33.95** | 253 | 246 | 18.13 | 19.77 |
| 0.5 | +$4.7172 | +$5.7968 | -$6.38 | -$6.84 | -$12.60 | -$45.10 | 225 | 233 | 16.87 | 18.87 |
| 1.0 | +$3.9502 | +$3.9447 | -$5.36 | -$6.47 | **-$11.77** | **-$46.80** | 207 | 220 | 15.10 | 17.40 |
| 2.0 | +$2.3547 | +$0.6221 | -$5.95 | -$9.05 | -$11.66 | -$47.57 | 184 | 222 | 12.97 | 15.20 |

Three things this isolates:

1. **The gain from dropping skew is *larger* at minute resolution, not smaller.** Going
   1.0 → 0.0 buys +$1.92/market at interval=60 (1.48x) and **+$4.55/market at interval=1
   (2.15x)**.
2. **The tail cost of dropping skew reverses sign.** At interval=60, skew 0.0 has the worse
   worst case (-$14.76 vs -$11.77 at skew 1.0) — which is what `market_making.py`'s
   docstring table records and what the decision to keep 1.0 was partly built on. At
   interval=1, **skew 0.0 has the *better* worst case (-$33.95 vs -$46.80)**. Leaning
   against inventory 60x more often means realising the lean 60x more often, and in a market
   trending against the position that is a sequence of progressively worse prices rather
   than a defence.
3. **Skew 2.0 is the only setting whose interval CI spans zero at 1-minute resolution**
   ([-0.3600, +1.6630] overall, [-0.8574, +3.2505] out of sample). At interval=60 on the
   same markets skew 2.0 still clears zero. More skew is strictly more harmful at the
   resolution a live quoter experiences.

**The single place skew earns anything is the 5th percentile**, and only barely: at
interval=1, `fill_model=pessimistic`, the 5th percentile is -$6.47 at skew 1.0 against
-$8.69 at skew 0.0 — $2.22/market of left-tail protection bought for $4.55/market of mean.
The 5th percentile is non-monotone (it worsens again to -$9.05 at skew 2.0), so its optimum
sits near 0.5–1.0, not at either end.

---

## 6. The one-paragraph answer

**Skew does not earn its keep at the resolution a live quoter experiences; the tight
inventory limit is doing the work, and at 1-minute resolution skew is actively expensive.**
At `interval=1`, `fill_model=pessimistic`, `terminal=settled`, on 300 markets / 201 events
with the 0.70 temporal cutoff at 2026-09-04T09:11:15+00:00, the current
`skew_strength=1.0` earns **+$3.9447** per trading market against **+$8.4928** at
`skew_strength=0.0` — skew is giving up **$4.55 per market, 53.6% of the available P&L** —
and it does not buy the protection it is charged for: the worst single market is
**-$46.80 at skew 1.0 against -$33.95 at skew 0.0**, so at minute resolution zero skew has
both the better mean *and* the better worst case, and the sign of that tail comparison is
**reversed** from the hourly table in `market_making.py` (-$11.77 at skew 1.0 vs -$14.76 at
skew 0.0 on these same 300 markets at `interval=60`). The only thing skew buys is 5th
percentile — -$6.47 at 1.0 against -$8.69 at 0.0, $2.22 of left-tail for $4.55 of mean — and
that purchase gets worse again at skew 2.0 (-$9.05), whose CI is the only one in the grid to
span zero at 1-minute resolution ([-0.3600, +1.6630]). What actually bounds exposure is
`max_inventory=20`: peak `|inventory|` **never exceeds 20.0 at either resolution, at any
skew** (share above 20 is 0.0% in all sixteen cells), because `quote_size=10.0` steps
inventory 0 → 10 → 20 and the limit then withdraws the adding side — the cap is structural
and the skew only changes *how often* the cap is reached (97.7% of markets at skew 0.0 vs
74.0% at skew 1.0, `interval=1`). The explicit hypothesis in `DEFAULT_SKEW_STRENGTH`'s own
docstring — "hourly candles cannot see intra-hour inventory swings, so the value of leaning
against them is understated here by construction" — is **measured false on this sample in
both directions**: finer resolution makes skew look worse on the mean (the 0.0-over-1.0
advantage grows from 1.48x to 2.15x) and worse on the tail (the worst-case advantage flips
in zero-skew's favour by $12.85).

---

## 7. Recommendation, and what would justify acting on it

**Recommendation: lower `DEFAULT_SKEW_STRENGTH` from 1.0 toward 0.0–0.5. Do not act on this
report alone.**

This study did not change the default, and the brief forbade it. It should stay forbidden
until the following is true, because three things here are genuinely insufficient:

1. **The sample is the top tail, not a draw.** 300 markets chosen as the highest-`n_fills`
   2.0% of the cache. Skew's cost is proportional to how often you re-lean, so the busiest
   markets are exactly where skew looks worst. The finding may not survive on the median
   market, and this study cannot say — it never looked at one.
2. **The grid was scored on both halves of one sample.** §5.3 reports the temporal test half,
   but the grid was not *tuned* on a held-out split. That is precisely T4's two-split
   challenger rule (tune on a random event-half, score on the other half and on the temporal
   test, 60 halves, win only at >=90% plus the temporal test), and it does not exist yet.
   GUARDRAILS.md §4.4 makes that rule the only route to a default change.
3. **A minute-resolution collection at Gate-1 scale does not exist.** 300 markets is 900
   GETs; 15,283 markets is ~46,000, which is feasible but has not been run.

**What would justify acting.** Run T4's two-split rule over
`skew_strength ∈ {0.0, 0.25, 0.5, 1.0}` at `max_inventory=20` on a **1-minute cache of a
random sample** of the T3 universe — not the `n_fills` top tail — with the same 0.70 temporal
cutoff. Lower the default if `skew_strength ∈ {0.0, 0.5}` wins the random-event-half
challenge at >=90% *and* beats 1.0 on the temporal test half's mean with a clustered CI whose
lower bound stays above the incumbent's point estimate. If it does, the change carries the
docstring table update and `tests/strategies/test_market_making.py`'s pin in the same commit
(GUARDRAILS.md §4.4).

**Do not read this as a Gate 1 reversal.** The mean P&L is essentially resolution-invariant
($3.9502 vs $3.9447) while the uncertainty grows 64%. Nothing here makes the pessimistic
cash test CI of [-0.1205, +0.4507] on 1,239 markets / 717 events look better; §3 and §4 make
the exposure behind it look worse.

---

## 8. Carry-forward for other tasks

- **T6 (taper).** The taper's premise is confirmed and its target is bigger than the hourly
  cache says: 73.3% of these markets hold into settlement at `interval=1`
  (`fill_model=pessimistic`) against 69.0% at `interval=60`, and time-weighted mean
  `|inventory|` is 25.8% higher (6.93 vs 5.51). **The taper should be swept at 1-minute
  resolution if it can be**, because a taper is a time-to-close rule and hourly candles
  quantise its trigger to the hour. Note also that skew and the taper are substitute levers
  on the same quantity and this study finds skew is the *worse* of the two at 1m — the taper
  reduces the position near close without paying the re-lean cost in every interval before it.
- **T13 (capital).** Use **6.93 contracts** mean time-weighted `|inventory|` and **9.38% of
  time at the `max_inventory=20` limit**, not the hourly 5.51 / 6.95%. Mean collateral per
  quoted market is resolution-stable at ~$7.32, so the capital-per-quote figure is safe; the
  *inventory risk* figure is not.
- **Every inventory figure in this kit measured on hourly candles is a floor.** On these 300
  markets the hourly replay understates mean peak `|inventory|` by 15.2%, the share of
  markets reaching the inventory ceiling by 23.0 pp (51.0% → 74.0%), the holding rate by
  4.3 pp, time-weighted mean `|inventory|` by 20.5%, and the **worst single-market loss by
  a factor of 3.98** (-$11.77 → -$46.80). The 78.5% held-into-settlement headline is a lower
  bound on true exposure, not an estimate of it.
- **T2's `n_unmarkable` carry-forward is discharged.** 0 of 601,216 intervals at
  `interval=1`. The mark gate discards nothing; no cash figure in this kit is computed on a
  mark-filtered subsample.
- **`fetch_candles` cannot serve `interval=1` beyond 5,000 minutes in one request.** Any
  future minute-resolution work needs the chunk.

---

## 9. Provenance and method

- Every number above is produced by `mm_backtest`'s own `replay()`, `_block()` and
  `_split()` — the same code that produced the Gate 1 report — driven from scratchpad
  scripts. **No file under `backend/app/` or `backend/tests/` was modified.**
- Peak and time-weighted `|inventory|` are not fields on `MarketRow`, so a copy of
  `replay()`'s inner loop records them. That copy is **proven identical to the harness**:
  `_assert_traced_matches_real` compares every field of every `MarketRow` it produces
  against the real `replay()` on the same inputs, for both resolutions and both fill models
  (1,200 row comparisons), and raises on any difference. It did not raise. The instrumented
  loop is therefore reporting the same replay, not a second one.
- The maker rebate is $0.00 on this schedule (`maker_rebate_rate=0.0`) and appears in no P&L
  figure (GUARDRAILS.md §2.3).
- Intervals are cluster-bootstrapped by `event`, 500 replicates, seed 20260906
  (GUARDRAILS.md §2.4). No naive interval is printed.
- Terminal inventory is settled at the venue's `result`; 168 markets with a non-binary
  `result` were excluded at universe construction and are counted (GUARDRAILS.md §4.2).
- **No selection threshold was widened or lowered to make n.** The 300 is the brief's
  number; the `n_fills` floor of 5 is where the top 300 happened to fall.

### Reproduction

```
# stage 1 — selection (reads the T3 60m cache, no network)
python3 t5_select.py

# stage 2 — 1-minute collection, 300 markets, chunked under the 5,000-period cap
python3 t5_collect_1m.py --cache backend/.cache/mm/kalshi-1m.json --interval 1 --days 10

# stage 3 — replay both resolutions, sweep skew
python3 t5_analyze.py      # 60m vs 1m, inventory, skew sweep at interval=1
python3 t5_sweep60.py      # the same skew grid at interval=60, same markets
python3 t5_exposure.py     # time-weighted inventory exposure
```

### Verify (run 2026-09-07, real output)

```
$ cd backend && test -f ../.claude/kits/mm-proveout/reports/kalshi-minute-study.md \
    && grep -q "interval=1" ../.claude/kits/mm-proveout/reports/kalshi-minute-study.md \
    && grep -q "fill_model=pessimistic" ../.claude/kits/mm-proveout/reports/kalshi-minute-study.md \
    && python3 -m pytest -q tests/strategies/test_market_making.py
```

Full suite alongside it: `python3 -m pytest -q` → **1355 passed in 16.40s**.
