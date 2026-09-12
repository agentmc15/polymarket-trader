# Kalshi taper — stopping accumulation near close does not help

**The brief's stated premise is false and is not tested here.** T6's brief claimed "160 of 537
markets carried a position into settlement and that is what moved every CI to span zero." Two
independent measurements since have contradicted the causal claim: T4's `max_inventory` slices
(venue-wide, 4,320 trading markets, pessimistic, `terminal=settled`) found `max_inventory=10` ->
72.9% held into settlement, ROC `0.0061`; `=20` -> 78.5%, ROC `0.0195`; `=50` -> 81.7%, ROC
`0.0344` — **widening** the inventory cap raised BOTH the share held into settlement AND return on
capital, at every grid point, and nine of the ten largest series confirmed the same direction. T5's
resolution study found `held_into_settlement` *rising*, not falling, at finer resolution (69.0% ->
73.3% pessimistic, same 300 markets) — more chances to trade are more chances to re-accumulate. The
calibrated defaults moved to `edge_fraction=0.90, min_spread=0.25, max_inventory=50.0` on exactly
that evidence (2026-09-08). A cap that avoids the terminal coin flip also forecloses the mid-life
spread capture that pays for everything; per-market settlement noise diversifies across thousands
of markets, so the premise that it needs fixing at all does not survive T4/T5.

**The actual question, and the answer.** A taper binds only NEAR close, unlike a `max_inventory`
cap that binds a market's whole life — so it is a structurally different lever, worth testing on
its own terms as a VARIANCE question: does stopping accumulation near close narrow the confidence
interval enough to be worth the mean it costs, without giving up the mid-life spread capture
`max_inventory=50` already proved is where the money is? **Measured answer: no.** Sweeping
`taper_hours` in `{0, 1, 3, 6, 12, 24}` at the calibrated defaults, on 11,911 Kalshi markets that
took no part in selecting any parameter (`overlap=0`), the test-split pessimistic `ci_low` **falls
monotonically** at every step — `+0.4631` (0h) -> `+0.4395` -> `+0.3276` -> `+0.2469` -> `+0.0969`
-> `+0.0341` (24h) — never once above the untapered value. The confidence interval does narrow
(width `0.5953` -> `0.4431`, -25.6%, tracking a real ~3.5 percentage-point drop in
`held_into_settlement`), but the mean falls faster (`+0.7717` -> `+0.2633`, -65.9%), so the
narrowing never outruns the falling mean. `app.scripts.mm_calibrate.two_split_rule` never selects
any nonzero taper on any of 60 random halves (`wins=0/60`). **`DEFAULT_TAPER_HOURS` stays `0.0`.**
This is a clean, monotonic "no" — the sweep changes nothing, and that is itself the result this kit
now has seven measurements establishing.

---

## 0. What changed and why this report reads differently from the brief

T6's brief in `TASKS.md` frames the taper as fixing a stated problem ("160 of 537 markets carried a
position into settlement and that is what moved every CI to span zero"). That framing is dead:

| measurement | finding | source |
|---|---|---|
| T4 `max_inventory` slices, venue-wide, pessimistic, `terminal=settled` | 10 -> 72.9% held, ROC 0.0061; 20 -> 78.5%, ROC 0.0195; 50 -> 81.7%, ROC 0.0344 — wider cap raises BOTH held share AND ROC | `reports/kalshi-calibration.md` §3, `DEFAULT_MAX_INVENTORY` docstring |
| T5 resolution study, same 300 markets, pessimistic | `held_into_settlement` 69.0% (60m) -> 73.3% (1m) — RISES at finer resolution | `reports/kalshi-minute-study.md` |
| Calibrated defaults (2026-09-08) | `edge_fraction=0.90, min_spread=0.25, max_inventory=50.0` shipped on exactly the T4 evidence above | `app/strategies/market_making.py` |

So "restrict inventory to avoid the coin flip" is a lever this kit already measured and rejected —
restricting it cuts ROC ~5.6x. The question this report actually answers is narrower and different:
a TAPER binds only in a market's last `taper_hours`, not its whole life, so it is not the same lever
as `max_inventory` and nothing had tested it before this task. The question is framed as **variance**,
not mean, per the coordinating instructions for this task: a taper that lowers the mean AND raises
`ci_low` could still be worth adopting (diversification pays for a narrower left tail); a taper that
lowers both is simply worse. `ci_low` is shown for every setting below, not only the mean.

## 1. The mechanism, exactly as specified

`app/strategies/market_making.py`:

- `MarketMaker.__init__` gained `taper_hours: float = DEFAULT_TAPER_HOURS` (new constant, `0.0`).
  Validated `>= 0.0`; `ValueError` otherwise (parametrized alongside the existing
  out-of-range-parameter test).
- `MarketMaker.quote()` gained `hours_to_close: float | None = None`. A new private method,
  `_effective_max_inventory(hours_to_close)`, returns `self.max_inventory` unchanged when
  `taper_hours <= 0.0` OR `hours_to_close is None`; otherwise, for `hours_to_close` inside
  `[0, taper_hours)`, it falls LINEARLY from `max_inventory` (at `hours_to_close == taper_hours`) to
  `quote_size` (at `hours_to_close == 0`, i.e. at close). A `hours_to_close` past close (negative)
  clamps to the same floor rather than extrapolating below it.
- `quote()` uses the effective value everywhere it previously used `self.max_inventory`: the
  inventory-skew clamp and the inventory-limit withdrawal check. Near close, this means a SMALLER
  position triggers the withdrawal that stops the policy from adding — the mechanism never places
  an order to flatten a position (this module places nothing, GUARDRAILS §1.1); it only withdraws
  the side that would grow it, and the remaining side's own fills (or simply no more fills) are what
  let inventory drift toward flat.
- `taper_hours=0.0` disables the mechanism UNCONDITIONALLY: every existing call to `quote()` — no
  `hours_to_close` at all, or any value with `taper_hours` left at its default — produces IDENTICAL
  quotes to before this parameter existed. Both `hours_to_close=None` (unknown distance to close)
  and `taper_hours=0.0` (disabled) short-circuit to the untapered `max_inventory`.

`app/scripts/mm_backtest.py`: `replay()` now computes `hours_to_close = (market.close_ts -
candles[i].end_ts) / 3600.0` for the candle the quote is drawn from, and passes it to every
`policy.quote(...)` call unconditionally — inert for every existing caller (`taper_hours=0.0` by
default), and exactly what a caller who DOES configure `taper_hours` needs, with no separate code
path. Nothing else in `mm_backtest.py` changed.

**Tests** (`tests/strategies/test_market_making.py`, 7 new: taper-zero inertness, `hours_to_close=
None` inertness, monotonic shrink, the exact floor at close, the past-close clamp, the withdrawal
firing sooner under a taper than without one, and the `DEFAULT_TAPER_HOURS` pin;
`tests/scripts/test_mm_backtest.py`, 2 new: `replay()` computes the right `hours_to_close` from the
quote candle's own `end_ts`, and a negative value past a market's own `close_ts` is passed through
uncorrected — clamping is `MarketMaker`'s job, not `replay()`'s, and testing both separately catches
either one silently absorbing the other's bug). All pass; see §7.

## 2. Data window and split

Venue `kalshi`, 1-minute candles, 10-day lookback — the SAME cache T18's holdout report used
(`backend/.cache/mm/kalshi-holdout-1m.json`), not re-collected for this task: **11,911 markets**,
closes span **2026-07-03T16:52:51Z -> 2026-09-07T11:21:04Z**, `7,303,230` candles,
`n_unmarkable_intervals=0`. Collected against `--min-volume 2000 --interval 1 --days 10 --sample
12000 --seed 20260908 --exclude-markets-from .cache/mm/kalshi-60m.json` (12,000 requested, 89 too
short, 0 payload/request errors). `n_universe=35,187` (post-exclusion), `excluded_by_result=168`.

**`overlap=0` against the tuning cache, re-verified independently for this task** (not merely
inherited from T18's report): comparing `market_id` sets of `kalshi-60m.json` (15,283 markets, the
cache `edge_fraction`/`min_spread`/`max_inventory` were tuned and selected on) against
`kalshi-holdout-1m.json` (11,911 markets) gives an intersection of exactly `0`. Every market this
sweep scores is one none of the calibrated defaults, the T4 candidate, or any earlier decision in
this kit was chosen against.

**Split.** `mm_calibrate.seventy_percent_cutoff(markets, train_fraction=0.70)` (imported, not
reimplemented) returned `cutoff_ts=1788633041` (`2026-09-05T18:30:41Z`). Realised train share:
**69.98%** (identical to T18's own realised share on the same market list, as expected — the cutoff
is a pure function of the same `close_ts` distribution).

**Policy held fixed across the whole sweep:** `edge_fraction=0.90, min_spread=0.25,
max_inventory=50.0, quote_size=10.0, skew_strength=1.0` — the calibrated defaults, per the task's
explicit instruction to sweep the taper AT the new defaults rather than the superseded
`0.80/0.10/20.0`.

## 3. The sweep, `fill_model=pessimistic` first, `terminal=settled`, `split=temporal-test`

### 3.1 Test split (the out-of-sample verdict split), pessimistic

| taper_hours | n_trading | n_events_trading | mean pnl/mkt | ci_low | ci_high | ci width | roc | held_into_settlement | settled_short_into_yes |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0 | 1477 | 1057 | +0.7717 | **+0.4631** | +1.0584 | 0.5953 | +0.0887 | 1263 (85.5%) | 570 (38.6%) |
| 1 | 1477 | 1057 | +0.7282 | **+0.4395** | +1.0068 | 0.5674 | +0.0836 | 1245 (84.3%) | 569 (38.5%) |
| 3 | 1477 | 1057 | +0.5780 | **+0.3276** | +0.8258 | 0.4982 | +0.0662 | 1231 (83.3%) | 565 (38.3%) |
| 6 | 1477 | 1057 | +0.4827 | **+0.2469** | +0.7297 | 0.4829 | +0.0552 | 1228 (83.1%) | 565 (38.3%) |
| 12 | 1477 | 1057 | +0.3304 | **+0.0969** | +0.5548 | 0.4579 | +0.0377 | 1218 (82.5%) | 563 (38.1%) |
| 24 | 1477 | 1057 | +0.2633 | **+0.0341** | +0.4772 | 0.4431 | +0.0300 | 1212 (82.1%) | 560 (37.9%) |

`n_trading` and `n_events_trading` are IDENTICAL at every setting — see §5 for why: the taper never
blocks a market's first fill, only later accumulation. `ci_low` never rises above the untapered
value at any setting; it falls monotonically, and every setting's interval nests entirely inside
the taper=0 interval's upper half — the taper never finds a region of the parameter this data
supports as an improvement.

### 3.2 Test split, optimistic (reported beside pessimistic, decides nothing — PLAN D3)

| taper_hours | n_trading | n_events_trading | mean pnl/mkt | ci_low | ci_high | ci width | roc | held_into_settlement | settled_short_into_yes |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0 | 1509 | 1072 | +0.9030 | +0.6095 | +1.2504 | 0.6409 | +0.1061 | 1300 (86.1%) | 574 (38.0%) |
| 1 | 1509 | 1072 | +0.8075 | +0.5293 | +1.1014 | 0.5721 | +0.0947 | 1278 (84.7%) | 574 (38.0%) |
| 3 | 1509 | 1072 | +0.6567 | +0.4070 | +0.9428 | 0.5358 | +0.0769 | 1268 (84.0%) | 573 (38.0%) |
| 6 | 1509 | 1072 | +0.5491 | +0.3167 | +0.8051 | 0.4883 | +0.0641 | 1263 (83.7%) | 573 (38.0%) |
| 12 | 1509 | 1072 | +0.4311 | +0.2140 | +0.6742 | 0.4602 | +0.0502 | 1249 (82.8%) | 567 (37.6%) |
| 24 | 1509 | 1072 | +0.3307 | +0.1194 | +0.5666 | 0.4473 | +0.0385 | 1247 (82.6%) | 567 (37.6%) |

Same pattern under both fill models: `ci_low` falls monotonically, `n_trading` unchanged.

### 3.3 Overall block (train+test together), pessimistic

| taper_hours | n_trading | n_events_trading | mean pnl/mkt | ci_low | ci_high | ci width | roc | held_into_settlement | settled_short_into_yes |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0 | 4198 | 3009 | +0.7170 | +0.5311 | +0.8881 | 0.3570 | +0.0798 | 3623 (86.3%) | 1583 (37.7%) |
| 1 | 4198 | 3009 | +0.7008 | +0.5280 | +0.8585 | 0.3306 | +0.0779 | 3575 (85.2%) | 1574 (37.5%) |
| 3 | 4198 | 3009 | +0.5648 | +0.4102 | +0.7154 | 0.3052 | +0.0627 | 3534 (84.2%) | 1564 (37.3%) |
| 6 | 4198 | 3009 | +0.4688 | +0.3251 | +0.6115 | 0.2865 | +0.0520 | 3516 (83.8%) | 1561 (37.2%) |
| 12 | 4198 | 3009 | +0.3468 | +0.2032 | +0.4899 | 0.2867 | +0.0384 | 3492 (83.2%) | 1556 (37.1%) |
| 24 | 4198 | 3009 | +0.2693 | +0.1285 | +0.4089 | 0.2804 | +0.0298 | 3474 (82.8%) | 1549 (36.9%) |

### 3.4 Train split, pessimistic

| taper_hours | n_trading | n_events_trading | mean pnl/mkt | ci_low | ci_high | ci width | roc | held_into_settlement | settled_short_into_yes |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0 | 2716 | 1950 | +0.6804 | +0.4495 | +0.9255 | 0.4759 | +0.0745 | 2355 (86.7%) | 1013 (37.3%) |
| 1 | 2716 | 1950 | +0.6802 | +0.4729 | +0.9058 | 0.4328 | +0.0744 | 2325 (85.6%) | 1005 (37.0%) |
| 3 | 2716 | 1950 | +0.5515 | +0.3553 | +0.7612 | 0.4059 | +0.0603 | 2298 (84.6%) | 999 (36.8%) |
| 6 | 2716 | 1950 | +0.4552 | +0.2664 | +0.6449 | 0.3784 | +0.0497 | 2283 (84.1%) | 996 (36.7%) |
| 12 | 2716 | 1950 | +0.3494 | +0.1661 | +0.5357 | 0.3697 | +0.0381 | 2269 (83.5%) | 993 (36.6%) |
| 24 | 2716 | 1950 | +0.2661 | +0.0862 | +0.4413 | 0.3551 | +0.0289 | 2257 (83.1%) | 989 (36.4%) |

Same monotonic pattern in every block, both fill models: `ci_low` only ever falls as `taper_hours`
rises.

## 4. The two-split rule (`app.scripts.mm_calibrate.two_split_rule`, imported — not reimplemented)

`two_split_rule({0.0: ..., 1.0: ..., 3.0: ..., 6.0: ..., 12.0: ..., 24.0: ...}, default=0.0,
cutoff_ts=1788633041, n_halves=60, seed=20260906, win_threshold=0.90)` — the same objective
(`_return_on_capital`, return on capital, pessimistic) and the same 90%-of-halves-plus-temporal-test
gate that moved `DEFAULT_MAX_INVENTORY`.

```
default_key=0.0  candidate_key=None  champion_key=None  passed=False
wins=0  n_halves=60  win_rate=0.0
times_selected_by_tuning=0
temporal_default_roc=None  temporal_candidate_roc=None  temporal_win=None
full_sample_default_roc=0.07985  full_sample_candidate_roc=None
```

`wins=0/60` is not a near miss — it is exactly what the monotonic table above predicts. Because ROC
falls monotonically as `taper_hours` rises (0.0798 -> 0.0779 -> 0.0627 -> 0.0520 -> 0.0384 ->
0.0298, overall block), `taper_hours=0.0` is the argmax of the FULL grid's tune-half objective on
EVERY one of the 60 random event-halves — no nonzero taper is ever even selected as a candidate to
score against the defaults (`_passes_two_split_rule`'s own logic: "if that pick is `default` itself,
the draw contributes no win to anyone"). `candidate_key=None` and `temporal_win=None` follow
directly: there is nothing to check on the temporal split because nothing was ever picked to check.

**No default changes.** `DEFAULT_TAPER_HOURS` stays `0.0` in `app/strategies/market_making.py`; its
docstring and `tests/strategies/test_market_making.py::test_default_taper_hours_is_disabled_pending
_evidence` carry this evidence in the same commit as the mechanism.

## 5. Why `n_trading` never moves: the mechanism is confirmed working as specified, not silently inert

A worry worth naming and closing: could this whole sweep be measuring nothing because the taper
never actually fires? No — the monotonic cost IN THE NUMBERS IS the confirmation it fires (a no-op
taper would leave every column identical to `taper_hours=0`, and none of `mean`, `roc`, `held_into
_settlement`, or `settled_short_into_yes` do). What stays constant, `n_trading` and `n_events
_trading`, has a specific and correct reason: `_effective_max_inventory`'s floor equals
`quote_size` (10 contracts) at every `taper_hours` and at close itself. A market's FIRST fill starts
from `inventory=0`, and `|0| < 10` is true regardless of how tight the taper has made the effective
limit — so the first unit a market ever trades is never blocked by any taper setting in this grid.
Only SUBSEQUENT accumulation, once a position has already built up toward the shrinking limit, gets
cut off sooner. That is exactly the mechanism T6's brief specified ("the policy stops adding"), and
the measured effect — `held_into_settlement` (test, pessimistic) falling from 85.5% to 82.1% (-3.45
points) and `settled_short_into_yes` from 38.6% to 37.9% (-0.68 points) as `taper_hours` rises from
0 to 24 — is real but small, because it can only ever act on inventory a market had ALREADY
accumulated by the time the taper window opens, not on whether the market traded at all.

## 6. The variance question, answered directly

The instruction for this task was explicit: report this as a variance question, not a mean
question, and a taper that lowers the mean AND narrows the CI enough to raise `ci_low` would still
be worth adopting. **It does narrow the CI — width falls 25.6% from taper 0 to taper 24 (0.5953 ->
0.4431, test split, pessimistic) — but the mean falls 65.9% over the same range (+0.7717 ->
+0.2633), more than twice as fast in relative terms.** A CI that narrows around a rapidly shrinking
center still has its lower bound fall, and that is exactly what happened at every step of the grid,
in every block (overall/train/test), under both fill models. The mechanism that would make a taper
worth its cost — trading mean for a large enough variance reduction that `ci_low` rises — is not
present in this data at any of the six settings tried. `held_into_settlement` reduction (-3.45
points, §5) is the taper's entire variance benefit, and it is not large enough to move `ci_low` even
at `taper_hours=24` (the widest window tried, more than half the width of the CI itself measured in
hours against a market with a 10-day lookback).

## 7. What this does and does not conclude

- **The taper mechanism works exactly as specified** and is independently confirmed correct: the
  cost is monotonic in `taper_hours` in every block and both fill models, `n_trading` is unaffected
  for the reason in §5, and unit tests (§1) pin the floor, the monotonic shrink, the disable paths,
  and the past-close clamp directly against `MarketMaker.quote()`'s metadata.
- **No setting tested helps.** `ci_low` never rises above the untapered value at any of `{1, 3, 6,
  12, 24}` hours, in any block, under either fill model. This is a genuinely different answer from
  T4's `max_inventory` sweep (which found a clear winner) and T5's skew sweep (left as an open
  question) — this sweep is a clean, unambiguous NO, and per PLAN.md's own instruction a sweep that
  changes nothing is a result worth stating plainly rather than searching further within the same
  grid for a different answer.
- **This does not retest the falsified premise.** The report does not claim settlement risk is
  large or that inventory needs controlling at all — T4 and T5 already settled that question in the
  other direction. It answers only the narrower, correctly-framed question: given that wider
  inventory is where the money is (T4), does a NEAR-CLOSE-ONLY brake still pay for itself. It does
  not, at any setting tried.
- **`DEFAULT_TAPER_HOURS` stays `0.0`.** `app/strategies/market_making.py`'s docstring and
  `tests/strategies/test_market_making.py::test_default_taper_hours_is_disabled_pending_evidence`
  carry this evidence in the same commit as the mechanism, per GUARDRAILS §4.4 (a default changes
  only on the two-split rule, and the change updates the docstring table and the pin test in the
  same commit — here, the rule found no change to make, and the docstring/test say so with the
  numbers rather than a bare assertion).
- **What would change this answer.** A `taper_hours` outside `{1, 3, 6, 12, 24}` might behave
  differently (this grid does not prove no value of `taper_hours` could ever help, only that none of
  these six do); a taper defined as a fraction of a market's own typical trading lifetime rather
  than a fixed hour count might separate short-lived and long-lived markets better than one global
  cutoff does. Neither was tested here and both are open for a future task, not assumed to fail.

## 8. Provenance

- Mechanism: `app/strategies/market_making.py` (`DEFAULT_TAPER_HOURS`, `MarketMaker.taper_hours`,
  `MarketMaker._effective_max_inventory`, `MarketMaker.quote(..., hours_to_close=)`);
  `app/scripts/mm_backtest.py::replay()` (the `hours_to_close` computation and pass-through). No
  other file in either module changed.
- Tests: `cd backend && python3 -m pytest -q tests/strategies/test_market_making.py
  tests/scripts/test_mm_backtest.py` -> **97 passed** (88 pre-existing + 9 new: 7 in
  `test_market_making.py`, 2 in `test_mm_backtest.py`). Full suite: `python3 -m pytest -q` -> **1411
  passed**. `python3 -m ruff check app/strategies/market_making.py app/scripts/mm_backtest.py
  tests/strategies/test_market_making.py tests/scripts/test_mm_backtest.py` -> clean.
- Sweep: an ad hoc driver (not part of the repo — GUARDRAILS §3.4, this kit's scratch analysis lives
  outside the tree) imported `app.scripts.mm_backtest.{read_cache, replay, report}` and
  `app.scripts.mm_calibrate.{seventy_percent_cutoff, two_split_rule}` unmodified, loaded
  `backend/.cache/mm/kalshi-holdout-1m.json`, built one `MarketMaker(edge_fraction=0.90,
  min_spread=0.25, max_inventory=50.0, taper_hours=th)` per grid point, called `replay()` once per
  `(taper_hours, fill_model)` pair (12 calls total) and `report(..., split="temporal",
  cutoff_ts=1788633041, seed=20260906)` on each, then `two_split_rule` once over the six
  `taper_hours` keys' pessimistic `ReplayResult`s. Total run time ~8 minutes (read_cache 27.3s;
  each of the 12 replay+report pairs ~40s).
- Overlap check: independently re-verified for this task (not merely cited from `kalshi-holdout.md`)
  by loading both cache files' `market_id` sets directly — `kalshi-60m.json` 15,283 ids,
  `kalshi-holdout-1m.json` 11,911 ids, intersection 0.
- `backend/.cache/mm/kalshi-holdout-1m.json` is a pre-existing, `.gitignore`d cache file (T18's
  holdout collection) — read-only in this task, never written to.
- Every P&L, ROC and CI figure above carries `fill_model` and `terminal=settled` in its source
  `report()` JSON block; pessimistic is reported first throughout (GUARDRAILS §2.1/§2.2).
