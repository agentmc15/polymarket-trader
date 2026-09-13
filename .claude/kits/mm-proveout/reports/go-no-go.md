# mm-proveout — go / no-go

**This document states verdicts and what would move them. It does not recommend
trading real money, at any size, on either venue. That decision is the user's and
is out of this kit's scope by construction.**

Every number below is cited to the file it came from. Citations of the form
`kalshi-honest-holdout.json → path.to.field` are keys in that JSON; citations of
the form `file.md §N` are sections; `NOTES.md:NNNN` is a line number in
`.claude/kits/mm-proveout/NOTES.md`. Three numbers are cited outside `reports/`
and are flagged as such where they appear (§6). Two numbers the brief for this
task asked for could not be located anywhere and are named in §6 rather than
reproduced from memory.

Per GUARDRAILS §2.1/§2.2 every P&L, ROC and spread figure carries `fill_model=`
and `terminal=`, and pessimistic is stated first. Per §2.3 the rebate is its own
line and is in no P&L figure. Per §2.4 every interval is clustered by event.

---

## 1. The question

**Does this repo have measured, out-of-sample evidence that its `MarketMaker`
policy makes money on either venue — evidence strong enough that the next step
is a decision about real capital rather than a decision about more measurement?**

The kit's own pre-committed bar, fixed before any of the results below were
seen, is **FOUR** conditions. `TASKS.md` T20 states them verbatim -- "Four
criteria, fixed before the run: `event_overlap=0`; test window >= 14 days;
`n_trading >= 1000`; the two-split rule passes at 1-minute on event-disjoint
halves" -- and `NOTES.md:1351-1365` agrees ("Four criteria were fixed in
advance... Result: THREE of four met"):

1. an event-disjoint sample (`event_overlap=0`);
2. a test window of at least 14 days;
3. `n_trading >= 1000`;
4. the two-split tuning rule passes.

**An earlier version of this document listed FIVE**, adding "the candidate's
pessimistic test CI clears zero" as condition 1 while citing the passage that
says four. That is a defect of exactly the class this kit exists to police, and
it ran in the flattering direction twice over: the added condition is the one
that PASSED, it is an outcome rather than a precondition (a bar you can only
score after the run is not a bar), and it converted a 3-of-4 record into
4-of-5 in the most-read line of the kit. The verdict is unchanged; how
disciplined and how close it looks is not.

The document answers that question per venue, states the residual risk paper
cannot retire, and says what would change each verdict.

**The kit rejected its own positive result twice** — once at the Phase 1 review
(`NOTES.md:1196-1240`) and once at T22 (`kalshi-density-gate.json → interpretation`).
That is the shape of the record, and this document is written to preserve it, not
to smooth it.

---

## 2. Which reports are authoritative, and which are superseded

| report | status |
|---|---|
| `kalshi-honest-holdout.json` / `.md` (T20) | **Authoritative** for the Kalshi verdict. |
| `kalshi-density-gate.json` (T22) | **Authoritative** for multiplicity, capital scale, and the density-gate question. |
| `polymarket-feasibility.md` | **Authoritative** for Polymarket. It is a feasibility study; it contains no P&L and estimates none. |
| `kalshi-holdout.md` (T18) | **SUPERSEDED. Its GO was rejected and must not be cited as a result.** Retained only for the comparison in §3.6. |
| `kalshi-gate1.md/.json`, `kalshi-calibration.md`, `kalshi-minute-study.md`, `kalshi-taper.md` | Earlier, narrower studies. Not contradicted, but not the basis of any verdict here. |

Where `kalshi-holdout.md` conflicts with `kalshi-honest-holdout.md`, **the honest
holdout wins**, and this document says so explicitly rather than averaging them.
The conflict is stated in full in §3.6.

**`reports/polymarket-*.json` does not exist, and neither do forward snapshots.**
That is stated in §4, and it is a fence working, not an oversight.

---

## 3. Kalshi

### 3.1 Data window

| | |
|---|---|
| venue | Kalshi |
| instrument | 1-minute candles (`--interval 1`), `--days 10` lookback per market |
| cache | `.cache/mm/kalshi-honest-1m.json`, 4,903 markets / 3,817 events (`kalshi-honest-holdout.md` §2.1) |
| sample closes span | 2026-07-07T23:52:22Z → 2026-09-07T11:20:53Z = 61.48 days (`kalshi-honest-holdout.md` §2.3) |
| temporal cutoff | 2026-08-21T00:00:00Z, `cutoff_source=argument` (`kalshi-honest-holdout.md` §2.3) |
| **test window** | 2026-08-21T07:00:06Z → 2026-09-07T11:20:53Z = **17.18 days** (`kalshi-honest-holdout.md` §2.3; `kalshi-density-gate.json → split.test_window_days_computed_here` = 17.18109953703704, independently recomputed there) |
| event disjointness | `event_overlap=0`, `market_id_overlap=0` against the tuning cache, verified by raw `json.load` of both cache files outside the harness (`kalshi-honest-holdout.md` §2.1; `kalshi-honest-holdout.json → overlap`) |
| policy scored | shipped defaults `edge_fraction=0.90, min_spread=0.25, max_inventory=50.0, quote_size=10.0, skew_strength=1.0` (`kalshi-density-gate.json → provenance.policy`) |

**Span is not density.** The >=14-day criterion was met in span and not in
density: **98.1% of test markets close in the final 8 days of the 17.18-day
window** (`kalshi-honest-holdout.md` §7; `NOTES.md:1420`). Within the test split,
3,217 of 4,579 markets close in ISO week 2026-W36 and 1,276 on the single day
2026-09-07 (`kalshi-honest-holdout.md` §2.3, §4.3). **Half the candidate's test
P&L — 49.05% — closes on that one day** (`kalshi-honest-holdout.md` §4.3).

This is a property of Kalshi's close calendar and its page-capped settled
listing, not of the sampler. After removing every event in the tuning cache,
33,213 of 50,470 universe markets (65.8%) are gone, and of the 17,257 that remain
only ~520 close before 2026-09-01 against 16,737 in the seven days after it
(`kalshi-honest-holdout.md` §2.2, §7). Stratifying by close week moved the
per-week coefficient of variation from 2.8009 to 2.1194 — materially flatter,
nowhere near flat — and drew ten of eleven weeks to exhaustion
(`kalshi-honest-holdout.md` §2.2).

### 3.2 n

| | shipped candidate (0.90/0.25/50.0) | old defaults (0.80/0.10/20.0) |
|---|---:|---:|
| test markets in split | 4,569 | 4,569 |
| `n_quoted` | 1,581 | 1,771 |
| **`n_trading`** | **919** | 1,448 |
| `n_events_trading` | **789** | 1,202 |
| `n_fills` | 2,392 | 6,239 |
| `held_into_settlement` | 805 / 919 = 87.6% | 1,172 / 1,448 = 80.9% |

Source: `kalshi-honest-holdout.json → policies.*.pessimistic.test`;
tabulated in `kalshi-honest-holdout.md` §3.1.

**`n_trading=919` against a pre-committed floor of 1,000 is the failed
criterion.** It is the only one of the five that failed
(`kalshi-honest-holdout.md` §6).

The miss was not engineered and was not repaired after the fact. `--sample 5000`
was fixed before any result was seen, projected from T18's measured 35% trading
rate; the realized rate was 20%, because this sample averages 274 candles per
market against T18's 613 (`kalshi-honest-holdout.md` §3.2). **~12,000 unsampled
W36 markets remain and collecting ~2,000 of them would very likely have pushed
`n_trading` past 1,000. T20 declined**, because adding data after seeing a
marginal positive is the widen-to-make-`n` move GUARDRAILS §2.6 forbids
(`kalshi-honest-holdout.md` §3.2; `NOTES.md:1430-1434`).

### 3.3 Pessimistic OOS temporal-test P&L per trading market, with clustered CI

`fill_model=pessimistic`, `terminal=settled`, `split=temporal-test`,
`pnl_basis=cash_settled`, CI clustered by event (500 bootstrap replicates,
whole events resampled with replacement).

| policy | mean P&L / trading market | ci95 clustered by event | total P&L | sd / trading market |
|---|---:|---|---:|---:|
| **shipped candidate** (0.90/0.25/50.0) | **+$0.4360** | **`[+0.0365, +0.8615]`** | **+$400.67** | 6.0941 |
| old defaults (0.80/0.10/20.0) | −$0.5170 | `[-0.7607, -0.2473]` | −$748.56 | — |

Source: `kalshi-honest-holdout.json → policies.candidate(0.90/0.25/50.0).pessimistic.test`
(`ci95_clustered_by_event` = `[0.03651241534988742, 0.8615392781316351]`,
`total_pnl` = 400.67, `sd_pnl_per_trading_market` = 6.094081460683052) and
`…defaults(0.80/0.10/20.0).pessimistic.test`; tabulated in
`kalshi-honest-holdout.md` §3. The density-gate study reproduced the candidate
row to the full float independently (`kalshi-density-gate.json →
anchor_ungated_vs_t20.pessimistic_test`; `NOTES.md:1486-1489`).

Optimistic agrees in sign and is reported second per GUARDRAILS §2.2:
`[+0.0457, +0.8595]` at `n_trading=935` (`fill_model=optimistic`,
`terminal=settled`) for the candidate; `[-0.6816, -0.1345]` at `n_trading=1465`
for the old defaults (`kalshi-honest-holdout.md` §3).

**THE MULTIPLICITY, STATED ADJACENT TO THE NUMBER AND NOT IN A FOOTNOTE.**
`[+0.0365, +0.8615]` is one of **33 event-clustered CI evaluations of this single
test split, across 23 distinct policy variants** (T20 + T22 combined:
`kalshi-density-gate.json → multiplicity.counted`, which enumerates 2 T20 policy
variants + 21 T22 gate variants = 23, and 33 total CI evaluations; `NOTES.md:1511-1517`).
Under independence and a true null, 23 looks give
`P(>=1 spurious clear) = 0.4414` and 33 looks give `0.5663`
(`kalshi-density-gate.json → multiplicity.what_it_does_to_a_hairline_ci`). Those
are **upper bounds**, because the variants are nested subsets of one row set and
so are strongly positively correlated — the effective number of independent looks
is lower, and the JSON says so. What is not in doubt is that the number of looks
exceeded one, and that **the lower bound sits at 8.37% of the mean**
(`multiplicity.what_it_does_to_a_hairline_ci.lower_bound_as_share_of_mean.ungated_shipped`
= 0.08374699804464152). There is no margin here against even a handful of looks.

**And the interval does not survive a leave-one-out.** Recomputed through the
harness's own `_block` with the cluster bootstrap included
(`kalshi-honest-holdout.md` §4.1), `fill_model=pessimistic`, `terminal=settled`:

  removed                      share   n_trading   CI                       verdict
  KXCS2GAME (largest)         14.44%         900   [-0.0088, +0.7768]       NO-GO
  KXITFWMATCH (2nd)           12.11%         894   [-0.0225, +0.7653]       NO-GO
  KXNCAAFFIRSTTDTEAM (3rd)    10.73%         918   [-0.0091, +0.8214]       NO-GO
  top two cumulatively        26.55%         875   [-0.0908, +0.7900]       NO-GO
  top three cumulatively      37.28%         874   [-0.0990, +0.6838]       NO-GO

**The five largest single markets carry 39.95% of the entire test P&L, and one
market carries 10.73%** (`kalshi-honest-holdout.md` §4.2). Two of the top three
series are single-market series: `KXNCAAFFIRSTTDTEAM` at `n_trading=1` earned
$43.00 and `KXPGAPLAYOFF` at `n_trading=1` earned $42.75 — 21.4% of all test P&L
from two markets (`NOTES.md:1382-1384`). Several of the largest contributors
earned $22–28 on three to five fills, which is inventory carried into a
favourable settlement, not spread capture (`kalshi-honest-holdout.md` §4.2).

The aggregate moderates that last point without retiring it, and the correction
is on the record: on the same 919 test markets the (NOT settlement-independent -- `mm_backtest.replay()` adds
`inventory * (settle - last_mid)` to markout too, and 805 of these 919
markets held inventory into settlement)
`markout_pnl` mean is **+$0.6281** against the cash mean **+$0.4360**
(`kalshi-honest-holdout.json → …pessimistic.test.mean_markout_pnl_per_trading_market`
= 0.6280957562568009, `markout_basis=marked_at_i_plus_2`). So carrying inventory
into settlement is in aggregate a drag of about $0.19/market on what the quoting
itself earned — the biggest individual winners are settlement outcomes while the
aggregate statistic survives without them (`NOTES.md:1466-1474`).

The whole +$400.67 is the residual of **+$5,820.67 of fill cash against
−$5,420.00 paid out at settlement** — a 6.9% net between two opposing flows each
about fourteen times larger than it (`kalshi-honest-holdout.md` §4.2).

### 3.4 ROC

`fill_model=pessimistic`, `terminal=settled`, temporal-test split:

| policy | ROC |
|---|---:|
| **shipped candidate** | **+5.10%** (`roc` = 0.05098737607563484) |
| old defaults | −6.20% |

Source: `kalshi-honest-holdout.json → policies.*.pessimistic.test.roc`;
`kalshi-honest-holdout.md` §3.1. The harness's ROC denominator is
`n_quoted × collateral_mean` (`kalshi-density-gate.json → capital_picture.note`).

Optimistic, second: +5.39% for the candidate
(`kalshi-density-gate.json → anchor_ungated_vs_t20.optimistic_test.roc` =
0.053890536177893744).

### 3.5 Power, capital, order rate, rebate

**Power — portfolio size at which the 5th percentile of total P&L exceeds zero.**
`fill_model=pessimistic`, `terminal=settled`, resampling whole events with
replacement from the test pool (`kalshi-honest-holdout.json →
policies.*.pessimistic.test.power`; tabulated `kalshi-honest-holdout.md` §3.3):

| portfolio (markets) | events drawn | candidate 5th pct total | candidate P(profit) | defaults 5th pct total | defaults P(profit) |
|---:|---:|---:|---:|---:|---:|
| 500 | 429 | **+$6.47** | 0.954 | −$436.75 | 0.012 |
| 1,000 | 859 | +$93.24 | 0.998 | −$761.58 | 0.000 |
| 2,500 | 2,146 | +$615.25 | 1.000 | −$1,704.01 | 0.000 |
| 5,000 | 4,293 | +$1,494.99 | 1.000 | −$3,166.99 | 0.000 |

**Candidate: the 5th percentile is already positive at the smallest portfolio the
report measures — 500 markets, +$6.47. No smaller portfolio was evaluated, so the
crossing point is "at or below 500" and is not pinned by this evidence.**
**Old defaults: it never crosses; every portfolio size is negative.**

Two limits on this table, both load-bearing. First, the pool it resamples is the
same 919 markets whose five largest carry 39.95% of the P&L, so the power
statistic inherits that concentration and is not independent corroboration of the
CI. Second, `n_trading=919` was the pre-committed criterion and 919 < 1,000; T20
recorded that the direct power measurement clears what the `n_trading` proxy was
protecting **and explicitly declined to re-score on it**, because moving a
threshold after seeing the number is the error this kit exists to prevent
(`NOTES.md:1390-1396`). This document honours that: the criterion failed.

**Capital locked at that portfolio, in dollars.** At the measured portfolio of
1,581 quoted markets (`fill_model=pessimistic`, `terminal=settled`,
temporal-test), from `kalshi-density-gate.json → capital_picture.blocks.ungated`
and `→ scale.ungated`:

| | |
|---|---:|
| quoted markets | 1,581 |
| collateral per quoted market | $4.9704 |
| **total collateral implied** | **$7,858.22** |
| P&L over the 17.18-day window | **+$400.67** |
| **P&L per day** | **$23.32** |
| fills per day | 139.2 |
| ROC | +5.10% |
| annualised point estimate (assumes the window repeats) | **$8,511.94/yr** |
| **annualised event-clustered interval** | **[$712.85, $16,820.25]/yr** |

**The percentage alone misleads. The business measured here is roughly $23 a day
on about $7,900 of tied capital, with an event-clustered annual range from $713 to
$16,820.** The JSON's own framing: "total_collateral_implied_usd = n_quoted ×
collateral_mean … It is the capital tied up by a quoted market on average, not a
simultaneous peak" (`capital_picture.note`), and the annual interval "assumes the
window repeats; it is a scale statement, not a forecast" (`scale.ungated.ci_note`).
NOTES records the same conclusion in one line: "a rounding error, not a job. The
percentage is respectable; the dollars are not" (`NOTES.md:1533`).

Scaling to the 1,000-trading-market power floor: the measured ratio
`n_quoted / n_trading` = 1,581 / 919 = 1.72, so ~1,720 quoted markets, implying
~$8,551 of collateral at the same $4.9704 per quoted market. That is a derived
scaling of the two cited figures, not a separate measurement.

**Sustained order rate at that portfolio.** Derived from cited fields plus the
replay's own structure; the arithmetic is shown so it can be checked.

- `quote_hours` = 163,219 and `n_two_sided` = 159,713
  (`kalshi-honest-holdout.json → …candidate…pessimistic.test`). `quote_hours` is
  documented as "Intervals the policy actually rested a quote in"
  (`backend/app/scripts/mm_backtest.py:533`), and at `--interval 1` those are
  1-minute intervals → **2,720.3 market-hours** of resting quotes, of which 97.9%
  were two-sided.
- `replay()` calls `policy.quote(...)` once per candle interval
  (`backend/app/scripts/mm_backtest.py:1783-1808`), i.e. the modelled behaviour is
  a fresh quote each minute. Orders placed over the window =
  `2 × 159,713 + 1 × (163,219 − 159,713)` = **322,932**.
- Over 17.18109953703704 days (`kalshi-density-gate.json → split.test_window_days_computed_here`)
  = 1,484,447 s → **0.218 orders/s sustained average**, or **118.7 orders per
  market-hour**.
- Mean simultaneity: 2,720.3 market-hours / 412.35 wall-clock hours = **6.60
  markets quoting at once on average**.
- Fills, for contrast: 2,392 fills / 2,720.3 market-hours = **0.88 fills per
  market-hour**; 139.2 fills/day (`capital_picture.blocks.ungated.fills_per_day`).

Against Kalshi's documented unauthenticated limit of **~10 requests per second**
(`.claude/kits/mm-proveout/PLAN.md:83`; `backend/app/venues/kalshi/adapter.py:341`;
measured in the market-edge kit as 17 requests in 1.69s with a 429 on the 18th,
`.claude/kits/market-edge/NOTES.md:6880`) the sustained average of 0.218 orders/s
has roughly 46x headroom. **The headroom is in the average, not the peak**: if all
1,581 quoted markets ever rested two-sided quotes simultaneously on a 1-minute
refresh, that is 52.7 orders/s and about 5x over the limit. The measured window
never reaches that concurrency (mean 6.60 markets), so the rate limit is not a
binding constraint on anything actually measured — but nothing here measures a
peak, and no report in this kit does.

**THE REBATE LINE (GUARDRAILS §2.3 — its own line, never added into P&L).**
`rebate_if_paid_not_in_pnl` = **$0.0000**
(`kalshi-honest-holdout.json → …pessimistic.test.rebate_if_paid_not_in_pnl`).
The fee model is `KalshiFeeModel maker_rate=0.0175 taker_rate=0.07
maker_rebate_rate=0.0 source=settings_default` (`kalshi-honest-holdout.md` §2.3),
so **the rebate would add $0.00 if paid as published, and this is trivially zero
because the configured schedule carries no maker rebate at all**
(`kalshi-gate1.md` §149-151). Note what that means precisely: `source=settings_default`
is this repo's default, not a schedule read from Kalshi. **No task in this kit
ever queried a Kalshi maker rebate from the venue.** The Kalshi P&L above neither
gains nor loses anything from a rebate; whether one exists is unmeasured.

### 3.6 The conflict with `kalshi-holdout.md`, stated rather than averaged

`kalshi-holdout.md` (T18) reported a **GO** for this same candidate at 1-minute
resolution: `[+0.4522, +1.0977]` at `n_trading=1477`. **That GO was rejected by
the Phase 1 review and the rejection was independently confirmed. It is not a
result and must not be cited as one.**

| | `kalshi-holdout.md` (T18, REJECTED) | `kalshi-honest-holdout.md` (T20, authoritative) |
|---|---|---|
| candidate pessimistic test CI | `[+0.4522, +1.0977]` | **`[+0.0365, +0.8615]`** |
| `n_trading` | 1,477 | **919** |
| old-defaults test CI | `[-0.1646, +0.3154]`, n=2,150 ("near zero, well powered") | **`[-0.7607, -0.2473]`, n=1,448 — a sign reversal** |

Why T18 was rejected (`NOTES.md:1200-1219`, verified by the orchestrator directly):

- **Event-level contamination.** Market-id overlap was genuinely 0, but tuning and
  holdout shared **3,183 events**, and **6,073 of 11,911 holdout markets (51.0%)
  belonged to an event that also appears in the tuning cache.** The kit's CI is
  event-clustered, so the event is the unit of independence. Re-scored:
  event-disjoint n=447 → `[-0.1760, +0.9415]` NO-GO; event-shared n=1,030 →
  `[+0.5757, +1.3093]` GO. Proven not a power artifact — 200 random 389-event
  subsamples of the full pool clear zero 94.5% of the time, and the event-disjoint
  result sits at the 0.5th percentile.
- **The "temporal holdout" was 1.70 days over one holiday weekend** (2026-09-05
  18:30 Sat → 2026-09-07 11:21 Mon) against a 65.8-day span, because 89% of closes
  fall in the final 7 days.
- **NCAAF carried 75.3% of all test P&L**; removing that one sport gave
  `[-0.0503, +0.7273]` → NO-GO. And 649 of the holdout's 807 NCAAF test markets
  belonged to an event with markets in the tuning cache's test split — different
  lines on the same football games, the same afternoon.

T20's honest sample directionally reproduces the Phase 1 reviewer's event-disjoint
finding on independently collected data (`kalshi-honest-holdout.md` §7). What
genuinely improved is dispersion: top series 14.4% here against NCAAF's 75.3%
there, across 401 series (`NOTES.md:1386-1387`). What replaced it is worse in a
different way: market-level concentration, three independent leave-one-outs each
removing the result (§3.3).

### 3.7 The two positive Kalshi findings, and their limits

**The two-split tuning rule passes at 1-minute, 59 of 60 event-disjoint halves
(98.3%, threshold 90%)** — the first time this gate has been cleared for these
parameters, against the 51/60 failure measured on hourly candles
(`kalshi-honest-holdout.md` §5; `kalshi-calibration.md` §1). **It is a tuning gate
evaluated on the same sample being scored** (GUARDRAILS §4.1), so it establishes
that `0.90/0.25/50.0` is robustly the better of the two policies on this data and
not that the better of two policies makes money. `kalshi-honest-holdout.md` §5
says this in its own words, and `NOTES.md:1428-1429` records it so nobody later
cites 59/60 as out-of-sample confirmation.

**Quote-everything is the measured winner, and a P&L-blind density gate was
tested and rejected** (T22, `kalshi-density-gate.json`). N* = 2 was chosen on the
train split by a rule declared before the numbers; applied unchanged to test it
**destroys** the result — CI `[-0.2398, +0.6262]`, spanning zero, and ROC falls
from +5.10% to +2.86% (`headline.n_star_test`, `fill_model=pessimistic`,
`terminal=settled`). The strict N>=20 gate admits exactly two series,
`KXITFMATCH` and `KXITFWMATCH`, both ITF tennis — a gate built to be P&L-blind
landed on precisely the set a forbidden series-naming rule would have named
(`headline.the_density_structure_was_not_a_signal`). The only gate that passes
the two-split rule head-to-head is N>=1 at 55/60, and it moves P&L by exactly
$0.00, because a market that fills makes its own series dense enough to admit
itself (`headline.the_one_gate_that_passes_changes_no_pnl`). Its prettier ROC
buys a better percentage of a smaller business: $6,941 of collateral against $7,858 (the $959 figure is N>=20's, not N>=1's)
(`capital_picture.blocks`). **T22 shipped nothing** (`NOTES.md:1535-1536`).

---

## 4. Polymarket

### 4.1 There is no P&L, and that is a fence working

**No forward order-book snapshots exist. Collection has never started.** Kalshi is
backtestable because it publishes candles; Polymarket is not — no historical
bid/ask, no public tape, so every Polymarket number this project will ever produce
must come from forward-collected snapshots (`NOTES.md:1098-1100`).
`app/scripts/mm_replay_snapshots.py` exists and is tested, and against the empty
table today it exits 1 with a clear message, which is the correct answer until
collection starts (`NOTES.md:1101-1103`).

Migrations 008 and 009 are rendered to `reports/pending-migrations-008-009.sql`
and reviewed as purely additive — 7 nullable columns on `book_snapshots` plus a
`selection_membership` table, zero DROP/TRUNCATE/DELETE/ALTER COLUMN statements.
**Applying them is where GUARDRAILS §2 stops this kit**, verbatim: nothing applies
a migration to a live database. So the SQL is rendered and it stops
(`NOTES.md:1448-1456`). **This is blocked by design, not by oversight.**

Consequently **every entry below that Kalshi fills with a measurement,
Polymarket fills with "not measured"**, and the honest verdict form is
`UNDERPOWERED`, not `NO-GO`. Those are different claims: NO-GO says a measurement
was made and failed; Polymarket's says no measurement exists.

### 4.2 Data window

There is none. What exists is a live feasibility study, `polymarket-feasibility.md`,
built from three read-only public-GET snapshots taken on one day:

| probe | timestamp (UTC) |
|---|---|
| quotable grid + fee/rebate distribution | 2026-09-12T15:55:42Z – 15:55:51Z |
| book dwell time (16 polls at 60s, 480 `get_book` reads, 0 errors) | 2026-09-12T16:00:43Z – 16:15:44Z |
| close-time distribution | 2026-09-12T16:32:24Z |

Source: `polymarket-feasibility.md` §1, §4, §5.1. Every figure is one day's live
reading of a universe that visibly moved between two snapshots 37 minutes apart.

### 4.3 n — the quotable cohort

| threshold (`volume >= 100`) | quotable markets | distinct events | largest event share | markets/event |
|---|---:|---:|---:|---:|
| `min_spread >= 0.10` (the collection floor) | **130** | **53** | 12.3% | 2.45 |
| `min_spread >= 0.25` (the shipped `MarketMaker` default) | **75** | **32** | 17.3% | 2.34 |

Source: `polymarket-feasibility.md` §1 grid and §2. Measured again 37 minutes
later the market counts were 141 and 76 (`polymarket-feasibility.md` §5.1) —
evidence the boundary is noisy at the margin even though the ballpark is stable.

**Scale.** Kalshi's honest-holdout test split had `n_trading=919` across
**789 events** (`kalshi-honest-holdout.json`). The feasibility study's own
comparison used Kalshi's 1,477 trading markets / **1,057 events** — but that
figure comes from `kalshi-holdout.md`, **the rejected T18 report**
(`polymarket-feasibility.md` §2 cites it explicitly). Against either baseline the
conclusion is unchanged in direction: Polymarket's event count is roughly
**15-33x smaller**. And Polymarket bundles *more* candidates per event than
Kalshi (2.34-2.45 vs Kalshi's 1.16 `mean_markets_per_event`,
`kalshi-honest-holdout.json → …power.500.mean_markets_per_event` = 1.164765525982256),
so a naive per-market interval there would overstate effective sample size by
that factor — the exact error GUARDRAILS §2.4 exists to correct.

Polymarket clears this kit's `>=30 distinct events` floor in a single snapshot at
both thresholds (53 and 32). **Event count is not the binding constraint. Market
count is** (`polymarket-feasibility.md` §2).

### 4.4 Pessimistic OOS temporal-test P&L per trading market, ROC, power, capital, order rate

**All five: NOT MEASURED.** No P&L, no ROC, no CI, no power table, no collateral
figure, no order rate exists for Polymarket in any report in this kit.
`polymarket-feasibility.md` states in its own first paragraph that it models no
P&L and estimates no profit anywhere. **No figure is offered here in their place,
and none should be inferred from Kalshi's.**

What *is* measured, and is the reason the gap cannot be closed quickly:

**Time-to-power** (`polymarket-feasibility.md` §5.2, §5.3). Under Scenario A —
the fastest defensible and explicitly optimistic assumption, that the whole
quotable cohort turns over completely and independently every ~110 days with zero
event overlap between cohorts:

| threshold | pool/cohort | cohorts to 1,000 markets | **time to power** |
|---|---:|---:|---|
| `min_spread >= 0.10` | ~135 | 8 | **~880 days ≈ 2.4 years** |
| `min_spread >= 0.25` | 75 | 14 | **~1,540 days ≈ 4.2 years** |

Scenario B — the more realistic one, a continuous trickle of shorter-dated
listings with recurring event families reappearing release over release — is not
bounded above, and the report says Scenario A's rate is an upper bound on speed,
not a typical one.

**The structural fact that sets that pace**: today's quotable cohort is bimodal.
About 9% (7 of 76 at `>=0.25`, 12 of 141 at `>=0.10`) is already past its nominal
close and could resolve at any time; **essentially all the rest — 69 of 76 at
`>=0.25`, ~127 of 141 at `>=0.10` — cluster on almost exactly one date,
2026-12-31, ~110 days out, with a hard gap and zero markets closing between
roughly day 8 and day 109** (`polymarket-feasibility.md` §5.1). A market cannot
resolve before its close date, so absent new entrants this cohort supplies almost
no newly-resolved markets before around January 2027. **The specific date is
calendar weather and will not recur in this form; the existence of clustering
around salient dates is plausibly structural** (`polymarket-feasibility.md` §6).

**The largest unmeasured unknown is the new-market entry rate into quotability**,
which a single day's snapshot cannot supply (`polymarket-feasibility.md` §5.2).

**Book dwell time, for completeness** (`polymarket-feasibility.md` §4): at the
production 180-second collection beat, 68.0% of windows capture a genuinely moved
book (102 of 150); at 60s, 48.7% (219 of 450 intervals). 3 of 30 markets never
moved over the full 15 minutes; 10 of 30 changed on every 60s poll. The beat is
not badly mismatched to how fast Polymarket books move. This says how much depth
history accrues per tracked market per day; it says nothing about how many
calendar days pass before a tracked market resolves.

### 4.5 THE REBATE LINE (GUARDRAILS §2.3)

Sampled over the raw `feeSchedule` object on the listing payload
(`polymarket-feasibility.md` §3), 2026-09-12T15:55Z:

| | `min_spread >= 0.10` (n=130) | `min_spread >= 0.25` (n=75) |
|---|---|---|
| carries a raw `feeSchedule` | 128 (98.5%) | 73 (97.3%) |
| `feesEnabled: false` (explicit zero fee, zero rebate) | 2 (1.5%) | 2 (2.7%) |
| `rebateRate` = 0.25 | 79 (60.8%) | 47 (62.7%) |
| `rebateRate` = 0.20 | 49 (37.7%) | 26 (34.7%) |
| `takerOnly` | `true` on all 128 with a schedule | `true` on all 73 |
| parsed `FeeSchedule.source` | `venue_schedule`: 130/130 | `venue_schedule`: 75/75 |

**So 97-99% of the markets this collection would actually quote publish a nonzero
maker rebate of 20% or 25% of the taker fee, split roughly 60/40 between the two
rates, and every one of them parses to `source="venue_schedule"` rather than
falling back to the hand-maintained category table.**

**What that establishes and what it does not.** It proves the rate is *published*
by the venue on those markets on that day. **It proves nothing about whether a
rebate is ever *paid*.** Polymarket settles rebates separately, daily, in pUSD,
under terms the venue can change, and no public GET can observe a settlement —
that needs a live fill and a later balance check, which is out of scope for this
kit (`polymarket-feasibility.md` §3). The exact `rate`/`rebateRate` value mix is
explicitly classified as weather, not structure
(`polymarket-feasibility.md` §6).

Since no Polymarket P&L exists, there is no figure for the rebate to be added
to or withheld from. It is recorded here as a published rate, unpaid and
unverified.

---

## 5. What paper cannot prove — the residual risk

Everything in §3 and §4 is a replay. Two specific things the replay cannot
observe, and they are the two that decide whether any of it survives contact with
a live venue.

### 5.1 Queue position

**This is the larger of the two.** The backtest's fill model is an assumption
about **queue position**, not a measurement of it. It has two settings and the
kit reports both:

- `fill_model=optimistic` assumes the resting quote is at the **front of the
  queue** — every trade that touches the price fills it.
- `fill_model=pessimistic` assumes it is **behind** the queue.

Every verdict in this document is computed on `pessimistic` (GUARDRAILS §2.2),
which is the honest default for a new participant with no queue standing. But
**pessimistic is still a model of queue position, not an observation of one.**
Nothing in this kit has ever observed where a real resting order sat in a real
Kalshi or Polymarket book, how long it waited, or how often it was stepped in
front of. There is no public data that would let it: the candle tape gives price
and volume per interval, never per-order queue depth or arrival order.

Three concrete ways this matters, each visible in the measurements above:

1. **The two fill models already disagree about significance on identical rows.**
   At the N>=20 gate the pessimistic interval clears zero while the optimistic one
   does not — `[-0.1923, +2.5620]` optimistic
   (`NOTES.md:1499-1502`; `kalshi-density-gate.json →
   test_split_by_N_optimistic_crosscheck`). Two assumptions about queue position,
   two different answers about whether an edge exists.
2. **`min_spread` was calibrated on a queue-position argument.** The bucket study
   found 0.10-0.25 positive **only at the front of the queue and not behind it**,
   and `>= 0.25` the only bucket positive under **both** fill models — which is
   why `DEFAULT_MIN_SPREAD` and `CONSERVATIVE_MIN_SPREAD` are both 0.25
   (`backend/app/strategies/market_making.py:256-283`). The shipped parameter
   exists because of an assumption about queue position.
3. **`edge_fraction`'s interior optimum is a queue-position story.** Quoting at
   the touch (1.0) "earns no queue priority and simply trades less — 133 markets
   against 184" (`backend/app/strategies/market_making.py:301-306`). The parameter
   that carried the whole original result is tuned against a modelled queue.

**Residual risk, stated plainly: a live quoter's realized queue position could be
worse than `pessimistic` assumes — a market maker with no standing on a venue is
not merely behind the queue on average, it may be behind it systematically, and on
exactly the fast-moving markets where the spread is widest.** On a lower bound
sitting at 8.37% of its mean (§3.3), a small adverse shift in realized fill
probability is enough to move `[+0.0365, +0.8615]` below zero. **No paper study
this kit can run will retire this.** Only resting real orders and measuring what
happens to them will.

### 5.2 The rebate actually arriving

Both venues, for different reasons:

- **Kalshi**: the configured schedule carries `maker_rebate_rate=0.0` with
  `source=settings_default`, so the rebate line is $0.00 and the P&L is unaffected
  either way (§3.5). But that is this repo's default, not a schedule read from the
  venue. **Whether Kalshi pays a maker rebate at all is unmeasured by this kit.**
  If one exists and is not in the model, the Kalshi figures understate; if the
  model were ever changed to credit one that is not in fact paid, they would
  overstate. GUARDRAILS §2.3 forbids `FeeModel.fee()` crediting it, and nothing
  here proposes changing that.
- **Polymarket**: 97-99% of quotable markets **publish** a 20% or 25% maker
  rebate, parsed from the venue's own schedule (§4.5). **A public GET can see the
  published rate and can never see a payment.** Rebates settle separately, daily,
  in pUSD, under terms the venue controls.

**Residual risk, stated plainly: a published rebate rate is a venue's statement of
intent, not a receipt.** Confirming one requires a live fill followed by a balance
check on a later day — a real order on a real venue. That is the same instrument
§5.1 needs, and it is outside this kit's fences (`market-edge/GUARDRAILS.md` §1.1,
`mm-proveout/GUARDRAILS.md` §1.1).

### 5.3 The third thing, named because omitting it would be dishonest

Neither of the above is the reason Kalshi is a NO-GO. **The measured reasons are
`n_trading=919` against a floor of 1,000, an interval that fails on removal of any
of three series, 39.95% of P&L in five markets, half of it closing on one day, and
33 CI evaluations of one split.** Queue position and rebate settlement are what
would *remain* unproven even if all of those were fixed.

---

## 6. Numbers that could not be sourced, and numbers cited outside `reports/`

Recorded here rather than reproduced from memory, because this document's rule is
that every number is locatable.

**Could not be located anywhere in `reports/*`, `NOTES.md`, `TASKS.md`, `PLAN.md`
or the source tree, and therefore appears nowhere above:**

- **"1.15 orders per market-hour."** This task's brief asked for it as "measured
  previously". `grep` across the entire repo finds the string only in the brief
  itself (`TASKS.md:763`). No report or NOTES entry contains it. The only
  "market-hour" in the source tree is "return on capital per quoted market-hour"
  (`backend/app/strategies/market_making.py:292`), a different quantity. §3.5
  therefore derives an order rate from cited fields and shows the arithmetic
  instead. (The nearest numeric neighbour in the data is `mean_markets_per_event`
  = 1.1648, which is not an order rate; noted as a possible transcription, not
  asserted as one.)
- **Polymarket cohort figures "136 markets / 57 events" at `min_spread >= 0.10`
  and "78 / 34" at `>= 0.25`.** The brief for this task gave these. The
  authoritative measurements in `polymarket-feasibility.md` §1/§2 are
  **130 / 53** and **75 / 32** (2026-09-12T15:55Z), drifting to market counts of
  141 and 76 at 16:32Z with event counts not re-reported. §4.3 uses the report's
  numbers.

**Cited above but sourced outside `reports/` and `NOTES.md`, flagged so a
spot-check knows where to look:**

- `~10 requests per second`, Kalshi's unauthenticated limit —
  `.claude/kits/mm-proveout/PLAN.md:83`,
  `backend/app/venues/kalshi/adapter.py:341`, and measured as 17 requests in
  1.69s with a 429 on the 18th at `.claude/kits/market-edge/NOTES.md:6880`.
- `quote_hours` semantics and the one-quote-per-interval replay structure used in
  §3.5's derivation — `backend/app/scripts/mm_backtest.py:533` and `:1783-1808`.
- The queue-position rationale behind `min_spread` and `edge_fraction` in §5.1 —
  `backend/app/strategies/market_making.py:256-283` and `:301-306`.

**Two conflicts found between `NOTES.md` and the authoritative JSON. The report
wins in both, and §3 uses the report's figures:**

- Power at a 1,000-market portfolio. `NOTES.md:1388` records
  `5th-percentile total P&L = +$89.21, p_profit 0.986` and `p_profit 0.974` at
  500. `kalshi-honest-holdout.json → …power` gives **+$93.24 / 0.998** at 1,000
  and **+$6.47 / 0.954** at 500.
  **This is NOT bootstrap noise, and an earlier version of this document
  misfiled it as "most likely bootstrap re-runs".** The three NOTES values are
  exactly `policies.candidate.pessimistic.**overall**.power.{500,1000}` — the
  OVERALL sample's table, pool 990 — quoted inside a section about the TEST
  split, pool 919. It is the kit's signature defect (reading a number without
  checking which sample produced it), not a reproducibility wobble, and calling
  it noise disarms the next reader. The correct test figures appear five lines
  later at `NOTES.md:1393`; `:1388` is the uncorrected residue. The JSON is the
  artifact and §3 uses the test table.
- Multiplicity. `NOTES.md:1511-1514` says 33 looks give 58%.
  `kalshi-density-gate.json → multiplicity.what_it_does_to_a_hairline_ci.p_at_least_one_spurious_clear_over_all_ci_looks`
  = **0.5663**. §3.3 uses 0.5663.

---

## 7. Verdicts

One line per venue. `fill_model=` and `terminal=` labels per GUARDRAILS §2.1 on
every figure that carries P&L. **The Polymarket line carries no `fill_model` or
`terminal` label because it quotes no P&L, ROC or measured spread figure — none
was ever measured on that venue.**

VERDICT kalshi market-making fill_model=pessimistic terminal=settled split=temporal-test policy=shipped(0.90/0.25/50.0) window=17.18d ci95_clustered_by_event=[+0.0365,+0.8615] mean=+$0.4360/mkt roc=+5.10% n_trading=919 n_events_trading=789 vs_precommitted_floor=1000 (criterion 3 of 4 FAILED; CI fails on removal of any one of three series; 39.95% of P&L in five markets; 33 CI evaluations of this one split) -> NO-GO

VERDICT polymarket market-making no_pnl_ever_measured (zero forward snapshots; migrations 008/009 deliberately unapplied per GUARDRAILS §2) quotable_cohort=130mkt/53events at min_spread>=0.10 and 75mkt/32events at min_spread>=0.25 (2026-09-12) needs 1000 more trading markets to reach this kit's own power floor, time_to_power ~2.4yr at 0.10 / ~4.2yr at 0.25 under the optimistic scenario -> UNDERPOWERED

Read literally:

- **Kalshi is a NO-GO by the kit's own pre-committed rule**, and not by a close
  call about it. THREE of the four pre-committed conditions were met; the fourth,
  `n_trading >= 1000`,
  was missed at 919, and the condition that *was* met — the CI clearing zero — is
  itself fragile to three independent single-series removals. The kit declined to
  collect the ~2,000 additional markets that would very likely have made `n`
  (§3.2). That restraint is why this NO-GO means something.
- **Polymarket is UNDERPOWERED, not NO-GO, and the distinction is the point.**
  NO-GO would assert that a measurement was made and came out against. **No P&L
  was ever measured on Polymarket.** What was measured is that reaching the power
  to make one takes years (§4.4).

---

## 8. What would change the verdict

Stated as conditions, not as recommendations. Nothing here is advice to trade.

### 8.1 Kalshi — what would change `NO-GO`

**The criterion that failed, honestly repaired.** A fresh sample with
`n_trading >= 1000`, its size fixed in advance and whatever it produces accepted
(GUARDRAILS §2.6). The specific trap is named in `kalshi-honest-holdout.md` §3.2:
~12,000 unsampled W36 markets exist, and topping up after seeing a marginal
positive is the move the kit forbids. **A larger W36 draw would buy `n_trading`
and buy nothing at all against the concentration in §3.3** — that is the report's
own judgement, not this document's.

**The concentration, which matters more than `n`.** The result would have to
survive removal of its largest contributors. As measured it does not: any one of
`KXCS2GAME`, `KXITFWMATCH` or `KXNCAAFFIRSTTDTEAM` removes it (§3.3). A verdict
worth acting on would clear zero after each of those removals, not only before.

**Density, not span.** 98.1% of test markets closing in the final 8 days is the
binding structural fact (§3.1). `kalshi-honest-holdout.md` §7 states what it would
take: *a test window whose density is multi-week, not only its span — and on this
venue's visible listing that is not obtainable by sampling.* It needs either a
listing that is not page-capped (the cap over-represents recent closes by
construction) or **a forward-collected sample accumulated over weeks**. The
existing settled listing shows ~38% of the tradeable universe with 2026-08 ~94%
missing (`NOTES.md:58`, `NOTES.md:508`, `NOTES.md:607`), so a verdict from it generalises
to "markets like the ones the listing shows", not to Kalshi.

**The multiplicity, spent down rather than re-spent.** 33 CI evaluations have
already been made against this one test split (§3.3). Any further variant scored
on it adds to that count. A verdict that changes on the *same* split is worth less
than the count implies; a verdict on a *new* split starts the count at one.

**The mechanism question, which `markout_pnl` can discriminate.** If the edge is
settlement luck rather than spread capture, the (NOT settlement-independent -- `mm_backtest.replay()` adds
`inventory * (settle - last_mid)` to markout too, and 805 of these 919
markets held inventory into settlement)
`markout_pnl` is the statistic that separates them (`NOTES.md:1435-1439`). On the
test split it currently reads **+$0.6281/market against cash's +$0.4360**
(`fill_model=pessimistic`, `markout_basis=marked_at_i_plus_2`), which is the
aggregate surviving without settlement (§3.3). Reproducing that on an independent
sample would be the strongest available evidence short of a live fill.

**What would move it the other way, to a firmer NO-GO**: the same measurement on
a second event-disjoint, density-multi-week sample returning an interval that
spans zero. The old defaults `0.80/0.10/20.0` are already there —
`[-0.7607, -0.2473]` at `n_trading=1448` (`fill_model=pessimistic`,
`terminal=settled`) is the best-powered negative result this kit has produced, and
a sign reversal from T18's reading of the same policy (§3.6).

**And the scale question stands regardless of the statistics.** Even taking
+$400.67 at face value, the measured business is ~$23.32/day on ~$7,858 of tied
capital, annualising to a point estimate of $8,512 with an event-clustered range
of **[$713, $16,820]** (§3.5). A statistically clean version of this result would
still be a business of that size at that capital. Nothing about a better CI
changes those dollars.

### 8.2 Polymarket — what would change `UNDERPOWERED`

**The blocked step, named exactly.** Cash-settled P&L needs forward snapshots,
which need migrations 008/009 applied to a live database, **which GUARDRAILS §2
forbids this kit from doing** (`NOTES.md:1448-1456`). The SQL is rendered and
reviewed as purely additive at `reports/pending-migrations-008-009.sql`. Applying
it is a decision only the user can make. **This document does not ask for it.**

**The years-long path.** Even with collection running, reaching 1,000 trading
markets is ~2.4 years at `min_spread >= 0.10` and ~4.2 years at `>= 0.25` under
the explicitly optimistic Scenario A, and longer under Scenario B
(`polymarket-feasibility.md` §5.3). **The verdict would not change for years on
that path.** Lowering the spread threshold to make the cohort bigger is the move
GUARDRAILS §2.6 forbids, and `polymarket-feasibility.md` explicitly declines to
recommend a threshold.

**The weeks-long path, and what it can and cannot answer.** This is the one fact
about measurability worth stating precisely. `mm_replay_snapshots.py` gained a
`--markout-only` mode (T21, `backend/app/scripts/mm_replay_snapshots.py:124-215`).
**`markout_pnl` requires no settlement for its fill component** — it marks each
fill forward at mid(i+2) — so the *maker-quality* question on Polymarket is
answerable in weeks rather than the years cash-settled P&L needs
(`NOTES.md:1435-1439`). **That is a statement about what is measurable, not a
recommendation to measure it, and not a claim that a markout result would be a
GO.** A markout verdict would answer "does quoting this venue capture spread net
of adverse selection"; it would not answer "does the strategy make money after
terminal inventory settles", which is a different question and the one the Kalshi
verdict is computed on.

**The cheaper prerequisite the report itself names.** The single largest
unmeasured unknown is the **new-market entry rate into quotability**, which no
single-day snapshot can supply. `polymarket-feasibility.md` §5.3 observes that a
short 1-2 week pilot aimed specifically at measuring how many new markets enter
the quotable set per day would replace Scenario A/B's assumption with a measured
number — before any larger decision. That would not change the verdict; it would
put a measured number where an assumption currently sits.

**What would move it to `NO-GO` instead**: a measured new-market entry rate low
enough that the cohort provably cannot reach 1,000 trading markets on any horizon,
or a markout study on real forward snapshots returning an event-clustered interval
below zero. Neither measurement exists today.

---

## 9. Standing constraints this document operated under

- **No recommendation to trade real money appears above.** Verdicts and
  conditions only.
- **No order was placed, modified or cancelled**, and no code here can place one.
- **`.env` was never opened**; no secret appears in this file.
- **No migration was applied to any database.** `reports/pending-migrations-008-009.sql`
  is rendered SQL and stays that way.
- Every P&L, ROC and spread figure carries `fill_model=` and `terminal=`;
  pessimistic is first and every verdict is computed on it; the rebate is its own
  line and is in no P&L figure; every interval is clustered by event.
