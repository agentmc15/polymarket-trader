"""Two-sided passive quoting, calibrated against measured adverse selection.

WHY THIS EXISTS. Every taker strategy this kit has measured loses to the
spread, and the losses are monotone in it: crossing the widest third of
Kalshi books costs -0.2580 per contract, the tightest -0.0217. The spread
is the product, and the taker pays it. This is the other side of that
trade — Kalshi charges takers 7% and makers nothing, so a resting quote
collects the spread with no fee drag.

THE QUESTION THAT DECIDES IT is not spread width but ADVERSE SELECTION:
you are filled precisely when someone better informed wants the other
side. Measured on 34,137 hourly candles across 537 settled Kalshi
markets, decomposing each passive fill into what was quoted and what
survived one hour:

    fill model         fills   quoted half   realized half   adverse
    front of queue     7,857     +0.0182       +0.0051        72%
    behind the queue   3,046     +0.0236       -0.0095       140%

So the whole thesis turns on queue position, which is exactly the thing a
newcomer does not control — and the honest conclusion is that quoting
indiscriminately is a coin flip on execution quality.

WHAT SURVIVES BOTH MODELS. Splitting by quoted spread (realized per fill,
95% CI clustered by event):

    spread      front of queue            behind the queue
    <= 0.02     +0.0000 [-.0016,+.0016]   -0.0156 [-.0205,-.0096]
    0.02-0.05   +0.0012 [-.0027,+.0044]   -0.0153 [-.0241,-.0066]
    0.05-0.10   +0.0095 [+.0006,+.0175]   -0.0064 [-.0214,+.0077]
    0.10-0.25   +0.0348 [+.0198,+.0476]   +0.0143 [-.0075,+.0358]
    >= 0.25     +0.1152 [+.0867,+.1465]   +0.0735 [+.0270,+.1225]

Only the widest bucket is profitable under BOTH. That bucket is
`CONSERVATIVE_MIN_SPREAD` (0.25) — the point below which profit requires
an execution assumption this repo cannot yet make good on. As of the
1-minute holdout below, `DEFAULT_MIN_SPREAD` has moved to that SAME
value: two names now name one number (see the note where each is
defined). Tight books are not a smaller opportunity here; they are a
losing one.

INVENTORY IS NOT SYMMETRIC, and that is a measured fact rather than a
modeling convenience. Sell fills outnumbered buy fills 1.5x to 2.0x in
EVERY price bucket, including 0.30-0.70 where a floor at zero cannot
explain it: takers on prediction markets are net BUYERS of YES. A
two-sided quoter therefore accumulates a short position by default, and
the skew below is what stops that drift from becoming an unhedged
directional bet against the crowd.

CALIBRATED DEFAULTS, UPDATED 2026-09-08 — NOT CERTIFIED BY THIS KIT'S OWN
RULE. Everything above is the ORIGINAL calibration (hourly, 537-market
Kalshi study) that shipped `min_spread=0.10`, `edge_fraction=0.80`,
`max_inventory=20.0`. A Phase 1 review of the holdout this change relied
on found the experiment was not what it claimed to be:

  - The holdout excludes tuning-cache markets by `market_id` ONLY.
    **51% of its 11,911 markets (6,073) share an EVENT with the tuning
    cache**, and every CI in this kit is event-clustered — events, not
    markets, are the unit of independence here. Restricted to the
    event-disjoint subset, the candidate's test CI is
    `[-0.1760, +0.9415]` — spans zero. Not a power artifact: 200 random
    same-size subsamples of the full pool clear zero 94.5% of the time;
    the event-disjoint result sits at the 0.5th percentile.
  - The "temporal holdout" is **1.70 days** (2026-09-05T18:30 Saturday
    to 2026-09-07T11:21 Monday, one US holiday weekend), because 89% of
    the window's closes fall in its final 7 days. NCAAF is 39.3% of
    trading markets and **75.3% of test P&L**; removing that one sport
    alone gives `[-0.0503, +0.7273]` — spans zero. 649 of the 807 NCAAF
    test markets also share an event with the TUNING cache's own test
    split.
  - The two-split rule that gates a default change (PLAN.md, "A default
    changes on thin evidence": >= 90% of 60 random event-halves AND the
    temporal hold-out) had not been run at 1-minute resolution for this
    parameter grid. The only 60-halves run performed on it was hourly,
    and it FAILED — 51/60 (`reports/kalshi-calibration.md`), three short
    of the floor. **This bullet is now superseded — see the honest
    holdout below, which runs exactly that rule and passes it.**

THE HONEST HOLDOUT — what these three constants now rest on
(`.claude/kits/mm-proveout/reports/kalshi-honest-holdout.json`, run
2026-09-12, reproduced twice to the digit). A fresh Kalshi sample chosen
to be EVENT-disjoint from the tuning cache and stratified by ISO close
week, scored at 1-minute resolution: 4,903 markets / 3,817 events,
`event_overlap=0` and `market_id_overlap=0` — verified by loading both
cache files directly rather than through the harness's own exclusion
filter, because a filter is not evidence about itself.

    policy                       test CI (pessimistic)   mean/mkt   n_trading   ROC
    old (0.80/0.10/20.0)         [-0.7607, -0.2473]       -0.5170     1,448     -6.20%
    shipped (0.90/0.25/50.0)     [+0.0365, +0.8615]       +0.4360       919     +5.10%

(That `-0.5170` was `-0.5507` until a Phase 3/4 review caught it: the
overall sample's mean, pool 1,546, sitting in a row of test values. It
was the FOURTH instance in this kit of one error — reading a number
without checking which sample produced it — and the third to reach
shipped text, in the same editing pass that fixed the sibling instance
eight lines above. The class is now the kit's most reliable defect.)

HOW PRECISE IS `+0.0365`? Not to four decimals. `cluster_bootstrap` runs
at `BOOTSTRAP_REPLICATES = 500`, and re-running it across 40 bootstrap
seeds on these same 919 rows gives the lower bound a Monte-Carlo
sd of **0.0221** (min -0.0150, max +0.0885): **2 of 40 seeds put it at
or below zero.** So "the CI clears zero" is itself seed-dependent at the
shipped replicate count. It is not a coin flip — raising replicates
converges the bound upward and away from zero (5,000 reps: mean +0.0414,
sd 0.0085, 0 of 40 below zero; 50,000 reps: mean +0.0396, sd 0.0028) —
so the substantive answer holds and `+0.0365` is an unlucky draw on the
right side of it. But any reading of this interval that leans on its
first two decimals is leaning on bootstrap noise.

Four criteria were fixed in advance; THREE were met. `event_overlap=0`
met; test window 17.18 days (>= 14) met; the two-split rule met — 59 of
60 random event-halves, 98.3% against a 90% floor, plus the temporal
test, the first time that rule has passed anywhere in this kit. The
fourth, `n_trading >= 1000`, was NOT met: 919. The criterion was a proxy
for power and the direct power measurement clears what it was protecting
(5th-percentile total P&L at a 1,000-market portfolio is +$93.24,
`p_profit` 0.998 — read from the TEST split's own power table, NOT the
overall sample's, which is a different pool of 990 markets and reads
+$89.21 / 0.986). But the criterion was fixed in advance at 1000 and the
sample delivered 919, so it failed, and moving a threshold after reading
the number is the error this whole kit exists to prevent. Note also that
this power table resamples the same 919-market pool whose five largest
markets carry 39.95% of P&L, so it is not independent corroboration.

Three further reasons the positive CI above is not a green light:

  - Its lower bound is series-fragile. Removing any ONE of `KXCS2GAME`,
    `KXITFWMATCH` or `KXNCAAFFIRSTTDTEAM` pushes it below zero.
  - Market-level concentration is worse than series-level dispersion
    makes it look. Series dispersion did improve over the contaminated
    run below — top series 14.4% of P&L across 401 series, against
    NCAAF's 75.3% — but the five largest single MARKETS still carry
    39.95% of test P&L, one market alone carries 10.73%, and half the
    test P&L closes on a single day.
  - **Much of that P&L is not spread capture.** Several of the largest
    contributing markets earned $22-28 on only 3-5 fills: that is
    inventory carried into a favourable settlement — a directional
    outcome — not the maker's spread. A market maker whose profit comes
    from where the inventory settled has not demonstrated a market
    making edge. On the SAME test split `markout_pnl` reads +$0.6281 per
    trading market against cash +$0.4360 (n_trading=919 for both), so
    the two do not disagree in sign and the mark-based number is the
    larger of the pair.
    **That gap does NOT prove settlement is a drag rather than the
    source, and an earlier version of this docstring wrongly said it
    did.** `markout_pnl` is not settlement-free in `terminal=settled`
    mode: `mm_backtest.replay()` adds `inventory * (settle - last_mid)`
    to the markout accumulator too. Cash receives the whole
    `inventory * settle`; markout receives only the move from
    `last_mid`. They differ in how much settlement they absorb, not in
    whether they absorb any — and 805 of these 919 markets held
    inventory into settlement, so the term is doing real work in both
    numbers.

    **Measured on 2026-09-13 by stripping that term**
    (`app/scripts/mm_markout_validation.py`,
    `reports/kalshi-markout-only.json`; interpretation fixed in the
    script's docstring before its first run). Same 919 test markets,
    same policy, pessimistic, 5,000-replicate event-clustered bootstrap
    across 20 seeds:

        statistic         terminal   mean/mkt   per contract   CI95                  seeds > 0
        cash              settled    +0.4360    +0.0168        [+0.0464, +0.8295]    20/20
        markout_settled   settled    +0.6281    +0.0241        [+0.2809, +0.9953]    20/20
        markout_only      excluded   +2.3671    +0.0909        [+2.1087, +2.6427]    20/20
        settlement term   —          -1.7390    (805 of 919 rows nonzero; total -$1,598)

    So the quoting captured +$2.37 per market of spread at a two-interval
    horizon, and carrying inventory into settlement gave back -$1.74 of
    it. The mechanism is real and it is not fragile: the top event is
    2.22% of markout_only across 789 events, against five MARKETS
    carrying 39.95% of cash P&L — the fragility in the cash interval was
    the settlement term's, not the spread capture's. And +0.0909 per
    contract sits between the two numbers the adverse-selection table at
    the top of this docstring measured for the >= 0.25 bucket on a
    different sample at a different resolution (front of queue +0.1152,
    behind +0.0735): two independent measurements agreeing.

    **What this does NOT say, stated because it is the tempting
    misreading.** Two-interval markout overstates the money by 5.4x here
    (2.37 vs 0.44): adverse selection keeps playing out after `mid(i+2)`
    — takers are net buyers of YES, and the YES they bought tends to
    settle higher than the last mid the quoter was marked at. A markout
    result on Polymarket (T23) therefore establishes that spread capture
    EXISTS there; it does not establish that money is made, and on
    Kalshi the settlement drag consumed ~73% of the captured spread.
    That ratio is a post-hoc observation from one venue at 1-minute
    resolution, not a pre-committed criterion, and Polymarket's ~7-minute
    collection cadence puts `mid(i+2)` ~14 minutes out rather than 2 —
    a different horizon, not directly comparable.

HOW MUCH THAT CI SHOULD BE BELIEVED, counted rather than waved at
(`reports/kalshi-density-gate.json`): **23 distinct policy variants and
33 event-clustered CI evaluations have now been run against this one
test split.** Under independence and a true null, 23 looks at a one-sided
2.5% give P(>= 1 spurious clear) = 44%; 33 give 56.6%. Those are upper
bounds — the variants are nested subsets of one row set — but the
ungated lower bound sits at 8.4% of its mean, which has no margin
against even a handful of looks.

A P&L-BLIND DENSITY GATE WAS TESTED AND REJECTED. T20's table showed the
per-market edge rising with series density, which suggested quoting only
dense series. Chosen the only honest way — N maximising ROC on train,
applied unchanged to test — the rule returns N=2, and N=2 destroys the
result: CI `[-0.2398, +0.6262]`, ROC +2.86%. The strict gate N>=20
admits exactly two series, `KXITFMATCH` and `KXITFWMATCH`, both ITF
tennis: a gate built to be blind to profit lands on precisely the set a
forbidden series-naming rule would have named. The only gate passing the
two-split rule is N>=1, which moves P&L by exactly $0.00 because a market
that fills makes its own series dense enough to admit itself. **Hence
`quote everything` is not a default nobody revisited — it is the measured
winner, and this module should keep it.**

Note also that the >= 14-day window was met in SPAN and not in density:
98.1% of test markets close in the final 8 days. That is Kalshi's close
calendar and its page-capped listing, not the sampler — excluding tuning
events leaves 17,257 markets of which only ~520 close before 2026-09-01.
And the two-split rule's pass is a TUNING gate run on this same sample;
it is not independent out-of-sample confirmation and must not be cited
as such.

**So: the kit's own two-split rule now certifies these values; the kit's
Gate 1 GO criteria do not.** The open keep-or-revert question this
docstring used to leave open is, however, ANSWERED — keep. The old
0.80/0.10/20.0 is measurably loss-making on an event-disjoint sample
(CI entirely below zero, and it stays below zero under removal of any
single series), and a 48-policy grid searched against the shipped values
produced no challenger meeting the 90% win floor. The change shipped:

    constant                 old     new
    DEFAULT_EDGE_FRACTION    0.80    0.90
    DEFAULT_MIN_SPREAD       0.10    0.25
    DEFAULT_MAX_INVENTORY    20.0    50.0

`DEFAULT_SKEW_STRENGTH` is unchanged at 1.0 — see its own docstring below
for why, and for an open question a separate study raised that this
change does not resolve.

THE CONTAMINATED HEADLINE NUMBER, for the record
(`.claude/kits/mm-proveout/reports/kalshi-holdout.md`): 11,911 Kalshi
markets, 1-minute resolution, 10-day lookback, `overlap=0` against
`kalshi-60m.json` by `market_id` (not by event — see above). 69.98%
train / temporal-test split. 7,303,230 candles,
`n_unmarkable_intervals=0` across 7,279,408 quote/fill/mark intervals.

    policy                       test CI (pessimistic)   mean/mkt   n_trading   n_events
    old (0.80/0.10/20.0)         [-0.1646, +0.3154]       +0.0661     2,150       1,402
    candidate (0.90/0.25/50.0)   [+0.4522, +1.0977]       +0.7717     1,477       1,057

The candidate's raw number clears zero at 1.48x Gate 1's 1,000-market
power floor; the old defaults' raw number spans zero at 2.15x the floor.
Neither number is out-of-sample in the sense this kit requires (D5):
event overlap and NCAAF's 75.3% P&L share (both above) apply to this
table exactly as they apply to the temporal split it is drawn from.

WHAT SURVIVES THE ABOVE. Two findings do not depend on the event overlap
or the holiday-weekend window, and are the reason anyone would still
pursue this:

  1. On IDENTICAL markets, the wide policy (0.90/0.25/50.0) earns
     **$0.2915/fill** against the tight policy's (0.80/0.10/20.0)
     **$0.0104/fill** — 28x. Split by sport, the tight policy's per-fill
     edge is NEGATIVE outside NCAAF (**-$0.0528**) while the wide
     policy's per-fill edge stays positive in BOTH buckets. Adverse
     selection at tight spreads is a measured effect here, not a fitted
     one; its MAGNITUDE — not its existence — is what the contamination
     above leaves unestablished.
  2. Resolution changes the SIGN, not just the size, independent of
     which parameter set is quoting. On the same 7,985 Kalshi markets,
     the OLD defaults (0.80/0.10/20.0) measure **+0.2038** mean P&L/mkt
     at hourly resolution and **-0.2258** at 1-minute resolution — a
     sign flip. Hourly candles average away intra-hour adverse
     selection; 1-minute resolution does not.

THE 64% CI-WIDENING CORRECTION DOES NOT GENERALISE, and this docstring
previously applied it as if it did. `reports/kalshi-minute-study.md`
(T5) measured hourly clustered CIs as 64% narrower than 1-minute ones
and reported a companion claim that mean P&L is "essentially
resolution-invariant" across the two. Both numbers came from ONE
300-market subsample selected as the TOP 2% BY `n_fills` — the busiest
markets in the pool, not a random draw. On a broad sample the companion
claim is FALSIFIED: mean P&L moves 0.43 and flips sign between
resolutions (finding 2 above). The 64% factor is cited elsewhere in this
kit with that provenance attached, per GUARDRAILS §7 — it is not a
general correction and must not be applied outside the sample it was
measured on.

THE MECHANISM, measured rather than inferred, is consistent with finding
1 above: at 1-minute resolution you observe the adverse selection hourly
averaging conceals. Quoting tight gets picked off by it; quoting wide
does not.

THE LIMIT. Queue position is unmodelled. Every fill in this replay
assumes the order rested where the model says it did; no replay can
establish that it actually would have — that is exactly what Gate 2 (a
live test, designed but not written or run by this kit) exists to
measure. This also inherits the kit's page-cap sampling bias: the
universe walk stops at Kalshi's listing page cap, so this generalises to
"markets like the ones Kalshi's listing shows," not to Kalshi as a
whole.

THIS MODULE DECIDES ONLY WHAT TO QUOTE. It places nothing (GUARDRAILS.md
§1.1), and it is a pure function of book, inventory and parameters, so
its policy can be replayed against history by
`app.execution.passive_fill` without touching a venue.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from app.venues.types import OrderBook, VenueId

#: Below this quoted spread, passive quoting did not pay under the
#: optimistic fill model either (+0.0012, CI spanning zero at 0.02-0.05).
#: The first bucket with a positive lower bound at the front of the queue
#: is 0.05-0.10; 0.10 is the first that is comfortably positive there and
#: not negative behind the queue. That was the ORIGINAL calibration.
#:
#: CALIBRATED to 0.25 (was 0.10). The bucket table above already showed
#: 0.10-0.25 positive only at the front of the queue and NOT behind it,
#: and >= 0.25 the only bucket positive under BOTH fill models — this
#: constant now equals `CONSERVATIVE_MIN_SPREAD` for that reason (see the
#: note on that constant). The 1-minute Kalshi holdout confirms it out of
#: sample and jointly with `edge_fraction`/`max_inventory`: the old 0.10,
#: replayed at 1-minute resolution on 11,911 holdout markets that took no
#: part in selecting either parameter set, has a temporal-test CI of
#: `[-0.1646, +0.3154]` (spans zero, n_trading=2,150) and an OVERALL CI
#: entirely below zero, `[-0.3143, -0.0434]` (n_trading=6,284) — quoting
#: that tight loses money once adverse selection is measured at the
#: resolution a live quoter experiences rather than the hourly candle
#: that hid it. 0.25 is the value that does not get picked off; see the
#: module docstring's "CALIBRATED DEFAULTS, UPDATED" section for the full
#: evidence and its limits.
DEFAULT_MIN_SPREAD = 0.25

#: The only bucket profitable under BOTH fill models. Use this when queue
#: priority cannot be assumed, which for a new participant is the honest
#: default.
#:
#: NOTE: as of 2026-09-08 this equals `DEFAULT_MIN_SPREAD` (also 0.25) —
#: the calibrated default caught up to the conservative one, not the
#: reverse. Kept as a separate name deliberately: this constant documents
#: the bucket-study reason for 0.25 on its own, independent of whatever
#: `DEFAULT_MIN_SPREAD` calibrates to next, and the two are free to
#: diverge again without either name changing meaning.
CONSERVATIVE_MIN_SPREAD = 0.25

#: Fraction of the spread to keep as edge when improving the touch. At
#: 1.0 the quote sits ON the touch; at 0.5, halfway between touch and
#: mid.
#:
#: ORIGINALLY CALIBRATED to 0.80, and it was the parameter that carried
#: the whole result. Sweeping it alone on 537 markets (min_spread 0.10,
#: max_inventory 20, hourly, in-sample, return on capital per quoted
#: market-hour):
#:
#:     edge   traded   mean P&L     ROC
#:     0.50      196    +0.2760   +0.0137
#:     0.70      190    +0.8718   +0.0474
#:     0.80      184    +1.0878   +0.0612   <- broad single-parameter optimum, hourly
#:     0.90      160    +1.1434   +0.0597
#:     1.00      133    +0.6395   +0.0287
#:
#: The optimum is interior for a reason worth keeping: quoting too close
#: to the mid (low values) fills often but hands most of the spread back
#: as adverse selection, while quoting AT the touch (1.0) earns no queue
#: priority and simply trades less — 133 markets against 184. That sweep
#: beat the earlier 0.5 default on 60 of 60 independent random halves,
#: which is why 0.80 shipped.
#:
#: CALIBRATED to 0.90 (was 0.80). The table above varies `edge_fraction`
#: alone, in-sample, at hourly resolution — it is not wrong, it is
#: superseded. The value that survives an out-of-sample replay at
#: 1-minute resolution (the interval a live quoter actually experiences),
#: jointly with `min_spread=0.25` and `max_inventory=50.0`, on 11,911
#: Kalshi markets that took no part in selecting it, is 0.90 — see the
#: module docstring's "CALIBRATED DEFAULTS, UPDATED" section for the full
#: evidence and its limits.
DEFAULT_EDGE_FRACTION = 0.90

#: Inventory at which one side is withdrawn entirely, in contracts.
#:
#: ORIGINALLY CALIBRATED to 20 (was 100): a tight limit is the primary
#: risk control, and it is a SUBSTITUTE for `skew_strength` rather than a
#: complement. Worst single-market P&L, edge_fraction 0.80:
#:
#:     max_inventory   skew 0.0   skew 1.0   skew 2.0
#:              20       -8.90      -6.90      -6.90
#:             100      -46.25     -13.25     -11.60
#:
#: At 100 the skew is what stands between the book and a -46 market; at
#: 20 the withdrawal does that job already.
#:
#: CALIBRATED to 50 (was 20). Varying `max_inventory` alone, holding the
#: ORIGINAL `edge_fraction=0.80`/`min_spread=0.10` fixed, venue-wide,
#: pessimistic, terminal=settled (`reports/kalshi-calibration.md` §3):
#:
#:     max_inventory   held into settlement   ROC      mean pnl/mkt
#:              10             72.9%          0.0061     +0.0882
#:              20             78.5%          0.0195     +0.2913
#:              50             81.7%          0.0344     +0.5151
#:
#: The measured direction is the opposite of "tighter is safer": widening
#: the cap raised BOTH the share of markets carrying inventory into
#: settlement AND return on capital, at every grid point tried. The
#: 1-minute Kalshi holdout (module docstring, "CALIBRATED DEFAULTS,
#: UPDATED") confirms 50 out of sample, jointly with the new
#: `edge_fraction`/`min_spread`, on markets that took no part in
#: selecting it.
#:
#: `skew_strength` stays at 1.0, unchanged, alongside this move — see
#: that constant's own docstring for why, and for an open question a
#: separate study raised about it that this change does NOT resolve. No
#: worst-single-market tail figure has been measured for max_inventory=50
#: at skew=1.0 specifically (the table above stops at 20 and 100); that
#: is a gap in the evidence, stated rather than papered over. If this
#: table's trend (wider cap, worse tail) continues past 50 the way it
#: continued from 20 to 100, Gate 2 is where it would show up.
DEFAULT_MAX_INVENTORY = 50.0

#: How hard inventory pushes the quote, as a fraction of the half-spread
#: at full inventory. At 1.0 a maxed-out book shifts its quotes by a full
#: half-spread toward getting flat.
#:
#: DELIBERATELY LEFT AT 1.0, against the grid search. At the ORIGINAL
#: `DEFAULT_MAX_INVENTORY` of 20 (hourly, in-sample) the measured
#: trade-off was:
#:
#:     skew   mean P&L      ROC   5th pct   worst
#:     0.0     +1.0878   +0.0612    -5.200   -8.90
#:     1.0     +0.6514   +0.0348    -4.700   -6.90
#:
#: Zero earns 1.7x more and gives up a slightly worse tail — a risk
#: appetite, not a fact, and not one this module should silently spend on
#: an operator's behalf. It is also the parameter this backtest measures
#: worst: hourly candles cannot see intra-hour inventory swings, so the
#: value of leaning against them is understated here by construction.
#: An operator running the diversified portfolio this strategy needs
#: (~2,500 simultaneous markets, where per-market tails average out) has
#: a good case for lowering it; that is their call to make explicitly.
#:
#: OPEN QUESTION, NOT ACTED ON: a later 1-minute sub-study
#: (`reports/kalshi-minute-study.md`, T5) measured skew as harmful in
#: BOTH directions this docstring hypothesized above — skew 1.0 earned
#: +$3.9447 against skew 0.0's +$8.4928 (skew gives up 53.6% of mean
#: P&L) AND had the WORSE tail (-$46.80 vs -$33.95 worst single market),
#: the opposite of "gives up a slightly worse tail" above. That study ran
#: on a 300-market TOP-`n_fills` subsample, not a random one, and it
#: explicitly declined to change this default on that basis: acting
#: needs T4's two-split rule (>= 90% of 60 random-event-halves plus the
#: temporal test) run over `skew_strength in {0.0, 0.25, 0.5, 1.0}` on a
#: random 1-minute sample, which has not been done.
#: `DEFAULT_MAX_INVENTORY` moving to 50.0 (this module, 2026-09-08) makes
#: this MORE consequential, not less — a wider cap gives inventory more
#: room to run before the hard withdrawal fires — but it is still a
#: properly-powered test away from being acted on, not a decision this
#: change makes.
DEFAULT_SKEW_STRENGTH = 1.0

#: Hours before close at which the effective `max_inventory` starts
#: shrinking linearly to a floor of one `quote_size`, reached exactly AT
#: close. `0.0` disables the taper unconditionally -- every existing
#: call to `quote()` (no `hours_to_close`, or `taper_hours` left at this
#: default) behaves exactly as it did before this parameter existed.
#:
#: WHY A TAPER AND NOT A TIGHTER `max_inventory`. T4's `max_inventory`
#: slices (`reports/kalshi-calibration.md`, `DEFAULT_MAX_INVENTORY`'s own
#: docstring) measured the opposite of the intuition that motivated this
#: parameter: widening the cap raised BOTH `held_into_settlement` and ROC
#: at every grid point tried (10/20/50 -> 0.0061/0.0195/0.0344), because a
#: cap binds a market's WHOLE life and forecloses the mid-life spread
#: capture along with the terminal coin flip. A taper is a different
#: lever: it binds only NEAR close, so it can (in principle) stop adding
#: to a position in its last hours without touching the cap that
#: applies for the other 99% of a market's life. Whether it actually pays
#: for the mean it costs, or merely trades a lower mean for a narrower
#: CI without moving `ci_low`, is an empirical question -- not assumed
#: here in either direction.
#:
#: LEFT AT 0.0. `.claude/kits/mm-proveout/reports/kalshi-taper.md` swept
#: `taper_hours` in {0, 1, 3, 6, 12, 24} at the calibrated
#: `0.90/0.25/50.0` defaults, on the 11,911-market 1-minute Kalshi
#: holdout (`overlap=0` against the tuning cache). The answer was a
#: MONOTONIC cost, not a wash: test-split pessimistic `ci_low` FELL at
#: every step -- `+0.4631` (0h) -> `+0.4395` -> `+0.3276` -> `+0.2469`
#: -> `+0.0969` -> `+0.0341` (24h), never once above the untapered
#: value, while the mean fell from `+0.7717` to `+0.2633` (-65.9%). The
#: CI DOES narrow (width 0.5953 -> 0.4431, -25.6%, from a real ~3.5
#: percentage-point drop in `held_into_settlement`), but not fast enough
#: to outrun the falling mean, so `ci_low` never rises. `two_split_rule`
#: (`app.scripts.mm_calibrate` -- the same gate that moved
#: `DEFAULT_MAX_INVENTORY`) picked `taper_hours=0.0` as the ROC-argmax on
#: all 60 random halves (`wins=0/60`, no candidate ever selected), so the
#: default stays 0.0. See the report for the full overall/train/test
#: tables and both fill models.
DEFAULT_TAPER_HOURS = 0.0


@dataclass(frozen=True)
class Quote:
    """One side of a two-sided quote.

    Attributes:
        side: `"buy"` or `"sell"`.
        price: Limit price, a probability in [0.0, 1.0], already rounded
            to the market's tick.
        size: Size in contracts, `> 0`.
    """

    side: str
    price: float
    size: float


@dataclass(frozen=True)
class QuotePair:
    """What the policy wants resting in one market right now.

    Either side may be `None` — that is a decision, not a failure: an
    inventory limit withdraws the side that would make it worse, and a
    book too tight to pay withdraws both.

    Attributes:
        venue: Venue the quote belongs to.
        market_id: Venue-native market identifier.
        outcome: Outcome being quoted.
        bid: The resting buy, or `None`.
        ask: The resting sell, or `None`.
        reason: Why the policy produced this, for the log and for a
            human reading a paper-trading run.
        metadata: Book state the decision was made from.
    """

    venue: VenueId
    market_id: str
    outcome: str
    bid: Quote | None
    ask: Quote | None
    reason: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_two_sided(self) -> bool:
        """Whether both sides are being quoted."""
        return self.bid is not None and self.ask is not None

    @property
    def quotes(self) -> tuple[Quote, ...]:
        """The sides actually being quoted, in (bid, ask) order."""
        return tuple(q for q in (self.bid, self.ask) if q is not None)


def round_to_tick(price: float, tick: float, *, side: str) -> float:
    """Round `price` to `tick` in the direction that is CONSERVATIVE.

    A bid rounds DOWN and an ask rounds UP, so rounding can only ever
    widen the quote. Rounding a bid up would pay more than the policy
    decided to pay, which is the direction that silently loses money.

    Args:
        price: Unrounded limit price.
        tick: The market's minimum price increment, `> 0`.
        side: `"buy"` or `"sell"`.

    Returns:
        float: The rounded price, clamped to [0.0, 1.0].

    Raises:
        ValueError: If `tick` is not finite and positive.
    """
    if not (math.isfinite(tick) and tick > 0.0):
        raise ValueError(f"tick must be finite and > 0, got {tick!r}")
    steps = price / tick
    rounded = (math.floor(steps) if side == "buy" else math.ceil(steps)) * tick
    # `floor`/`ceil` on a binary float can land a hair outside; the clamp
    # keeps the result a probability, which every downstream type demands.
    return min(max(round(rounded, 10), 0.0), 1.0)


class MarketMaker:
    """Decides the two-sided quote for one market.

    Stateless with respect to the venue: inventory is passed in, so the
    same policy object can be replayed over history or driven live
    without behaving differently.
    """

    def __init__(
        self,
        *,
        min_spread: float = DEFAULT_MIN_SPREAD,
        edge_fraction: float = DEFAULT_EDGE_FRACTION,
        max_inventory: float = DEFAULT_MAX_INVENTORY,
        quote_size: float = 10.0,
        skew_strength: float = DEFAULT_SKEW_STRENGTH,
        taper_hours: float = DEFAULT_TAPER_HOURS,
    ) -> None:
        """Configure the policy.

        Args:
            min_spread: Minimum quoted spread to quote into at all. See
                the module docstring: below 0.25, passive quoting is not
                established to pay under both fill models, and at the
                1-minute resolution a live quoter experiences the old
                0.10 default measured out-of-sample overall CI entirely
                below zero.
            edge_fraction: Fraction of the half-spread kept as edge when
                improving the touch, in (0.0, 1.0].
            max_inventory: Absolute contract position at which the side
                that would increase it is withdrawn. This is the FAR-
                from-close value; `taper_hours` (below) can shrink the
                effective value used inside `quote()` as a market nears
                its close.
            quote_size: Contracts per side. Also the FLOOR the taper
                shrinks the effective `max_inventory` toward at close.
            skew_strength: How hard inventory shifts the quote.
            taper_hours: Hours before close at which the effective
                `max_inventory` starts shrinking linearly to `quote_size`,
                reached exactly at close. `0.0` (`DEFAULT_TAPER_HOURS`)
                disables this unconditionally, regardless of what
                `hours_to_close` a caller passes to `quote()`. See
                `DEFAULT_TAPER_HOURS`'s own docstring for the evidence.

        Raises:
            ValueError: If any parameter is outside its documented range.
        """
        if not 0.0 < edge_fraction <= 1.0:
            raise ValueError(f"edge_fraction must be in (0, 1], got {edge_fraction!r}")
        if not min_spread > 0.0:
            raise ValueError(f"min_spread must be > 0, got {min_spread!r}")
        if not max_inventory > 0.0:
            raise ValueError(f"max_inventory must be > 0, got {max_inventory!r}")
        if not quote_size > 0.0:
            raise ValueError(f"quote_size must be > 0, got {quote_size!r}")
        if skew_strength < 0.0:
            raise ValueError(f"skew_strength must be >= 0, got {skew_strength!r}")
        if taper_hours < 0.0:
            raise ValueError(f"taper_hours must be >= 0, got {taper_hours!r}")
        self.min_spread = min_spread
        self.edge_fraction = edge_fraction
        self.max_inventory = max_inventory
        self.quote_size = quote_size
        self.skew_strength = skew_strength
        self.taper_hours = taper_hours

    def _effective_max_inventory(self, hours_to_close: float | None) -> float:
        """`max_inventory`, tapered toward one `quote_size` near close.

        Returns `self.max_inventory` unchanged when the taper is
        disabled (`taper_hours <= 0.0`) or when the caller does not know
        `hours_to_close` (`None`) -- a market with no known close-time
        distance gets no taper rather than an invented one. Otherwise,
        for `hours_to_close` inside `[0, taper_hours)`, the limit falls
        LINEARLY from `max_inventory` (at `hours_to_close ==
        taper_hours`) to `quote_size` (at `hours_to_close == 0`, i.e. at
        close). A `hours_to_close` past close (negative, from a candle
        that closed after the market's recorded `close_ts`) clamps to the
        same floor rather than extrapolating below it.

        This assumes `quote_size <= max_inventory`, true of every
        configuration this kit ships (`quote_size=10.0` against
        `max_inventory` in {10, 20, 50}); a caller that violates it gets
        a taper that grows toward close instead of shrinking, which is
        not a case this method guards against.
        """
        if self.taper_hours <= 0.0 or hours_to_close is None:
            return self.max_inventory
        if hours_to_close >= self.taper_hours:
            return self.max_inventory
        remaining = max(0.0, hours_to_close)
        floor = self.quote_size
        return floor + (self.max_inventory - floor) * (remaining / self.taper_hours)

    def quote(
        self,
        book: OrderBook,
        *,
        tick_size: float,
        inventory: float = 0.0,
        hours_to_close: float | None = None,
    ) -> QuotePair:
        """Return the quote this policy wants resting in `book`.

        Args:
            book: Current order book for one (market, outcome).
            tick_size: The market's minimum price increment.
            inventory: Signed current position in contracts — positive is
                long, negative short. Skews the quote toward flat.
            hours_to_close: Hours remaining until the market closes, or
                `None` when unknown. Only consulted when `self.
                taper_hours > 0.0`; see `_effective_max_inventory`. `None`
                means no taper is applied regardless of `taper_hours`.

        Returns:
            QuotePair: Possibly with one or both sides `None`; see
                `QuotePair` for why that is an answer rather than a
                failure.
        """
        best_bid, best_ask = book.best_bid(), book.best_ask()
        base = {
            "venue": book.venue,
            "market_id": book.market_id,
            "outcome": book.outcome,
        }
        if best_bid is None or best_ask is None:
            # A one-sided book gives no mid, and inventing one would be
            # inventing the fair value this whole policy prices against.
            return QuotePair(**base, bid=None, ask=None,
                             reason="one_sided_book", metadata={})

        effective_max_inventory = self._effective_max_inventory(hours_to_close)
        spread = best_ask.price - best_bid.price
        mid = (best_ask.price + best_bid.price) / 2.0
        meta: dict[str, Any] = {
            "best_bid": best_bid.price,
            "best_ask": best_ask.price,
            "spread": spread,
            "mid": mid,
            "inventory": inventory,
            "min_spread": self.min_spread,
            "hours_to_close": hours_to_close,
            "effective_max_inventory": effective_max_inventory,
        }
        if spread <= 0.0:
            # Crossed or locked: not a market to quote into.
            return QuotePair(**base, bid=None, ask=None,
                             reason="crossed_or_locked_book", metadata=meta)
        if spread < self.min_spread:
            return QuotePair(**base, bid=None, ask=None,
                             reason="spread_below_minimum", metadata=meta)

        # Improve the touch by keeping `edge_fraction` of the half-spread.
        half_edge = (spread / 2.0) * self.edge_fraction
        # Inventory skew: long inventory pushes BOTH quotes down, so the
        # ask is likelier to fill and the bid less so. Measured on live
        # data, sell fills outnumber buy fills roughly 1.5-2x, so without
        # this a two-sided quoter drifts short by construction.
        lean = 0.0
        if effective_max_inventory > 0.0:
            clamped = max(-1.0, min(1.0, inventory / effective_max_inventory))
            lean = clamped * half_edge * self.skew_strength
        bid_price = round_to_tick(mid - half_edge - lean, tick_size, side="buy")
        ask_price = round_to_tick(mid + half_edge - lean, tick_size, side="sell")
        meta.update({"half_edge": half_edge, "lean": lean,
                     "bid_price": bid_price, "ask_price": ask_price})

        bid: Quote | None = Quote("buy", bid_price, self.quote_size)
        ask: Quote | None = Quote("sell", ask_price, self.quote_size)
        reason = "quoting_two_sided"

        # An inventory limit withdraws the side that would breach it.
        # Near close (taper_hours > 0) this is the SHRUNKEN effective
        # limit, not the far-from-close max_inventory: the policy stops
        # ADDING to a position as the limit tightens toward it, and the
        # withdrawn side's absence is what drifts the position toward
        # flat -- this module places nothing, so "drift flat" means the
        # remaining side's own fills, not an active unwind.
        if inventory >= effective_max_inventory:
            bid, reason = None, "inventory_long_limit"
        elif inventory <= -effective_max_inventory:
            ask, reason = None, "inventory_short_limit"

        # Rounding, skew, or a one-tick market can invert the pair or
        # push a side outside the book. A quote that crosses the touch is
        # a TAKER order wearing a maker's clothes — it would pay the
        # spread instead of earning it, and pay the 7% taker fee too.
        if bid is not None and bid.price >= best_ask.price:
            bid, reason = None, "bid_would_cross"
        if ask is not None and ask.price <= best_bid.price:
            ask, reason = None, "ask_would_cross"
        if bid is not None and ask is not None and bid.price >= ask.price:
            bid = ask = None
            reason = "quotes_inverted_after_rounding"
        if bid is not None and bid.price <= 0.0:
            bid, reason = None, "bid_below_tick"
        if ask is not None and ask.price >= 1.0:
            ask, reason = None, "ask_above_one"

        return QuotePair(**base, bid=bid, ask=ask, reason=reason, metadata=meta)
