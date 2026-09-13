# mm-proveout — execution notes

Run id `2026-09-07-7465`. Kit dials: autonomy `advisory`, budget `max-dispatches=70 max-escalations=4
max-consults=6`, roles `test-author red-team`.

## Dispatch decisions

- **T1 (Phase 1) and T7 (Phase 2) dispatched in parallel at run start**, per the TASKS.md dispatch
  preamble ("T1 and T7 are independent of each other and of everything else — fan them out
  together"). T7 starts the forward-collection clock, which is the time-sensitive path; the two
  touch disjoint files (`venues/kalshi/candles.py` vs `services/data_collector.py`). Phase
  reviewers still run at each phase end.

- **Kit agents were not registered this session.** `.claude/agents/mm-proveout-*.md` were authored in
  this same session, and agent definitions load at session start, so `subagent_type:
  mm-proveout-implementer` errored. Dispatches use the generic agent with the task's `model` pin and
  a first instruction to read the kit agent file from disk as its operating instructions — the
  skill's documented fallback ("if the kit has no implementer agent, use the Agent tool directly with
  that model"). A future session picks the agents up normally.

## Findings carried forward

**T1 — Kalshi candle history is sound, and the three facts PLAN §Risks wanted checked all hold.**
Probed a market that closed exactly 60 days before the run (`volume_fp` 4413):
- **Retention >= 60 days at both granularities**: 24 of 24 hourly candles returned for the 24h
  window before close, and 346 one-minute candles. (346 < 1440 is the market's actual trading
  window inside that day, not a history cap — the 24/24 hourly count is the clean retention
  signal.) The PLAN risk "retention shorter than 60 days" does NOT fire; T3 may sample the full
  2026-06-30 -> 2026-09-07 range and the temporal split keeps its power.
- **`end_period_ts` is the period END**: consecutive hourly candles differ by exactly 3600s in
  every sampled case. This is what makes `candle_at_or_before` look-ahead-free, and it is now
  verified live rather than assumed.
- **Zero-volume candles omit `price` entirely** (confirmed on a live candle): parsed as
  `px_* is None`, never an error.

**CORRECTION — the settled listing IS truncated, and my earlier check was broken.** I recorded that
`list_markets(status="resolved")` reaches 427,063 markets "with the page cap NOT reached". T2's live
run contradicted it (`kalshi_event_page_cap_reached pages=150 markets=432171`) and T2 was right.

My check captured the adapter's WARNING records into a buffer and grepped the text for
`page_cap_reached` — but the adapter logs `logger.warning("kalshi", extra={...})`, and a plain
handler never renders `extra`. **I grepped for a string the formatter does not emit**, so the guard
could only ever report "not hit". Same shape as the backticked-ledger error: a check that cannot
fail, reporting success.

Measured properly by walking `/events?status=settled` past the cap: the listing does not exhaust
even at **400 pages / 1,241,961 markets**, against `_MAX_EVENT_PAGES = 150`.

Impact on tradeable settled markets (`result` in yes/no, `volume_fp > 2000`):

| month | inside the cap | beyond it (invisible) |
|---|---|---|
| 2026-07 | 2,125 | 480 |
| 2026-08 | 4,613 | **77,602** |
| 2026-09 | 44,087 | 5,069 |
| **total** | **50,826** | **83,151** |

So the visible universe is 38% of the tradeable one, and August is **94% missing**. The listing is
not chronologically ordered (page 0 spans 2025-2031), so this is not a clean "recent tail is gone" —
it is an uneven, month-correlated bias.

**What this does and does not invalidate.** A temporal split WITHIN the visible set is still
internally valid: it holds out later closes from earlier ones on data actually observed. What is
damaged is EXTERNAL validity — the visible set is not a representative sample of Kalshi's settled
universe, so a Gate 1 verdict generalises to "markets like the ones we can see", not "Kalshi". T3
must state this in `reports/kalshi-gate1.md` rather than reporting `n` as if it were a random sample.
Raising `_MAX_EVENT_PAGES` is NOT the fix: the listing exceeds 400 pages, so any cap truncates; a
complete walk is a different, much longer job than T3's brief describes.

**T7 — quotability selection, measured live after the change.** Kalshi 96,072 listed -> 60,332
two-sided -> **643 quotable -> 500 selected**; Polymarket 1,918 -> 1,655 -> **130 quotable -> 130
selected**. Two notes for later phases:
- Polymarket's 130 is below PLAN's stated 169 with spread >= 0.10, because quotability now also
  requires `venue_volume >= 100`. The PLAN risk "Polymarket quotable count cannot reach
  significance" is therefore *tighter* than written, not looser — T12 must report power, and
  GUARDRAILS §2.6 forbids lowering the threshold to make n.
- Kalshi's 643 quotable exceeds `book_collection_top_n` (500), so the cap binds on Kalshi and does
  not bind on Polymarket. Collection covers ~78% of Kalshi's quotable set.

**T7 red-team — two confirmed breaks, one of them a carry-forward that invalidates a later brief.**

1. **Negative `BOOK_COLLECTION_TOP_N` is a silent collection blackout** (T7's own code; being fixed on
   retry). `select_quotable_markets` slices `[:top_n]` with no validation and pydantic declares no
   constraint, so Python's negative-slice semantics apply: `top_n=-1` drops the last market,
   `top_n=-10` selects NOTHING while `quotable` stays 5. `probe_quotable` still exits 0, because it
   keys on `quotable` rather than `selected` — so a typo of `-500` for `500` collects zero books from
   both venues indefinitely with every health signal green. Confirmed by the orchestrator directly.

2. **`collect_books`' per-venue isolation does not exist for non-`VenueError` exceptions.** The
   docstring claims isolation; it is implemented as `except VenueError` around `get_market`/`get_book`
   only. A plain `RuntimeError` (unwrapped payload bug, DB `IntegrityError` from
   `_upsert_book_snapshot`) in the FIRST venue propagates out of `collect_books`, so a healthy second
   venue is never reached — and because the method commits once at the end, that venue's rows are
   lost too. Structural to the pre-existing loop/commit design, NOT introduced by T7.

   **This makes T9's brief false where it says "per-venue isolation (one venue failing must not abort
   the other — `collect_books` already does this; keep it)".** It does not already do this. T9 must
   ESTABLISH the guarantee, not preserve it — recorded as a brief defect below, and T9's dispatch
   carries the correction.

3. Not confirmed, carried as context for T9: `collect_books` is unsafe on a shared `AsyncSession`
   under `asyncio.gather` (documented SQLAlchemy behaviour, not a defect in this code). It matters
   only because T9 schedules it on a 60s beat — if a pass ever exceeds the interval, Celery may
   overlap it. T9's design should not assume overlap is safe.

**Carry-forward for T3 (from T1's hardening).** `Candle` now validates prices into `[0,1]` and
volume/open-interest as finite `>= 0`, refusing anything else with `VenuePayloadError`. The live
probe still exits 0, but that samples ONE market. T3 fetches candles for ~8,000. **If T3 sees
`VenuePayloadError` at scale, the first hypothesis is that a bound is too strict for some real
market, not that the venue is corrupt** — report the offending payload rather than loosening the
bound blindly, and never catch-and-skip it silently, which would reintroduce exactly the
"corrupt reads as empty" defect this hardening removed.

**Ledger-format defect, mine, caught by the scorecard.** I wrote every `outcome:`/`agent:`/`defect:`
line wrapped in backticks. The backtick rule applies to quoting ledger GRAMMAR IN PROSE; an actual
data line must be plain or it does not parse. `--live` read "no finished tasks" for every tier with
two outcomes recorded. Un-backticked 13 lines; the signal now reads. Worth carrying: the failure was
silent in exactly the way this kit keeps finding elsewhere — a well-formed-looking record that no
consumer reads.

**On sonnet's live first-try rate (0/2).** Both retries were triggered by declared-roster roles
(test-author on T1, red-team on T1 and T7) catching defects that would otherwise have reached
`done`. Per the roster caveat, a declared R5 roster structurally raises `attempts=` on exactly those
tasks, so this is the roster working rather than the tier regressing. `--live` recommends no
re-route and I am not overriding that; no tier is re-pinned.

**Carry-forward for T11 (from T8's test-author).** `app/services/backtesting/data_replay.py:454`
contains `volume=row.volume or 0.0`. That row is a `PriceHistory` row, so it is NOT a live defect —
but T8 has just added three NULLABLE columns (`volume`, `taker_fee_rate`, `maker_rebate_rate`) with
no backfill, so every pre-existing `book_snapshots` row reads `None` on them. T11's brief requires a
volume delta of `(volume_i+1 - volume_i) if both non-null else 1.0`; copying the `x or 0.0` idiom
onto the new columns would turn a MISSING volume into a real zero, silently changing which fills the
optimistic model computes. Treat `None` as missing, never as zero, and say so in the docstring.

**T8's implementer wrote a test that could not fail.** Its
`test_snapshot_written_carries_listing_volume_and_fee_in_force` asserted `taker_rate=0.07` and
`maker_rebate_rate=0.0175` — exactly `Settings`' Kalshi defaults — so it passed whether the code read
`market.fee` or silently fell back to the venue default. The implementation turned out to be correct;
the evidence for it was hollow. The kit's test-author replaced it with rates differing from every
plausible default. Pattern worth naming for later tasks: **a fixture whose expected value equals the
default it is meant to rule out proves nothing.**

**T8 red-team — a confirmed defect in the one thing the task is named for.** `_upsert_book_snapshot`
is check-then-insert on `(venue, market_id, outcome, ts)` and a hit is a total no-op, so it never
refreshes `volume`/`taker_fee_rate`/`maker_rebate_rate` on an existing row. That is safe only if an
unchanged `ts` means nothing changed — and for Polymarket it does not: `_parse_book` sets
`OrderBook.ts` from the CLOB payload's own `timestamp` (when the BOOK last moved), while Kalshi
stamps `utcnow()`. A quiet Polymarket book therefore reports an identical `ts` across many 60s polls
while its `feeSchedule` changes underneath — the exact scenario T8's own docstring gives as the
reason the column exists. Every poll after the first is dropped and the row keeps its first-seen fee
forever: the opposite of "the fee in force", on the only venue whose fee actually moves. Verified the
crux directly (`_parse_book` ts source vs Kalshi's `utcnow()`).

Also confirmed: migration `008`'s `downgrade()` drops the only copy of this data with no docstring,
against the kit's own precedent one revision earlier (`007` marks its downgrade
`"Irreversible: see the module docstring."`).

**T10/T9 coupling — a fallback that will quietly outlive its reason.** T10 reads the gap
threshold via `getattr(settings, "book_collection_interval_s", None)` with a `_DEFAULT_INTERVAL_S =
60.0` fallback, because T9 (which owns that setting) had not landed when T10 ran. The fallback path
is the one actually exercised today. Two consequences for whoever verifies T9: (a) once T9 lands the
setting, the fallback becomes dead code that no test reaches unless a test deletes the attribute, and
(b) the fallback value equals T9's planned default, so a test that seeds gaps against 60.0 cannot
distinguish "read the setting" from "fell back" — the same shape as T8's hollow fee test. T10's
test-author has been asked to check exactly this.

**T10 environment limit, not a defect.** `python3 -m app.scripts.collection_health` cannot be run
end-to-end here: no Postgres container is running, so it fails with a connection error rather than
the brief's "empty DB -> exit 1". The empty-DB path is covered under SQLite. T10's second acceptance
line ("runs against the configured DB") is therefore satisfied only in test, and the report should
not claim a live run happened.


**T2 test-author — the replay's P&L is not the money, and my brief is why.** Confirmed, and it is
the most consequential finding of the run so far.

`replay()` computes `pnl = sum over fills of mark_to_market(fill, mid(i+2)) + terminal_inventory *
(settle - last_mid)`. That marks each fill ONCE, at the mark following it, and never re-marks
inventory carried across later intervals. The module docstring names the residual itself
(`mm_backtest.py:55`): `sum over intervals of inventory_before_interval * (mark_i - mark_prev)`,
and dismisses it as having "zero expectation under a martingale".

Demonstrated divergence (`test_a_multi_fill_position_diverges_from_cash_settled_pnl_by_a_real
_amount`, passing): buy 10 @ 0.34, sell 20 @ 0.67, terminal short 10 settling into `yes`. Cash is
`-3.40 + 13.40 - 10.00 = 0.00` exactly. The harness reports **-$2.45**. The algebra generalises --
the total reduces to `10 * (m1 - m2 - m3 + m_last)`, so the reported P&L is a function of which
candles happened to be the marks, not of the trades.

Two reasons the docstring's defence fails, in increasing order of importance:
1. **The martingale premise is the one thing this project has already measured false.** Maker fills
   here are adversely selected -- sell fills outnumber buy fills 1.5-2.0x, which is what "adverse
   selection = quoted - realized half-spread" measures. Flow that picks the maker off is not a
   martingale conditional on the fill, and the sign of the drift is against the maker.
2. **Even granting zero expectation, the term is pure added variance, and the Gate 1 verdict is
   `ci_low > 0` on a clustered CI.** Zero-mean noise on every market's P&L widens every CI, so it
   pushes the gate toward NO-GO regardless of the truth. The prior measurement was +$0.15-0.26 per
   market with a CI already spanning zero -- this convention is a live candidate for why.

**The brief is at fault, not just the implementation.** Acceptance (b) demands "the mark used is
candle i+2's mid, never i+1's -- construct candles where they differ and assert the P&L". Correct
cash-settled P&L is mark-INDEPENDENT, so (b) as written is unsatisfiable against `pnl`; the
implementer satisfied it the only way it can be satisfied, by adopting a mark-dependent convention.
The fix keeps both numbers and moves the assertion: `pnl` becomes cash-settled (telescoping,
mark-independent, what the gate uses), and the mark-dependent markout at i+2 becomes its own reported
field -- which is the market-maker's real quality statistic and the right target for (b).

**T2 test-author -- power table confirmed, as raised.** `P(profit) = 1.0000` at every portfolio size
was reproduced live (`--sample 30`, test half `n_trading=4`). Pinned as forced arithmetic: i.i.d.
resampling with replacement from an all-positive pool can never sum negative at any size, and
flipping one market's sign swings it to < 0.05 with `n_trading` unchanged. The power block also
carries no pool-size field, so it cannot be read in isolation. Fix mirrors the CI's existing
`n_events_trading < 5 -> None` convention.


**T9 — isolation established, and it creates a new silent-failure mode that nothing yet detects.**
The correction landed: each venue's whole body now runs in its own `try/except Exception` at venue
granularity (matching `app.tasks.execution.reconcile_venues`' precedent rather than `scanner.scan`'s
narrower book-level catch), and `session.commit()` moved from once-at-the-end to once per successful
venue with `rollback()` on failure. T9 confirmed the defect by reverting its own fix and watching the
`RuntimeError` propagate, then restored it — the right way to establish a guarantee.

But note what the fix buys and what it costs. Before: one venue's unwrapped bug killed both venues,
loudly. After: one venue's unwrapped bug is caught, logged, and the beat reports success forever
while that venue contributes zero rows. **The failure is now silent and indefinite**, and the beat's
own exit status will never show it. T10's `collection_health.py` is the only detector, and nothing
schedules it. That gap belongs in T13's residual-risk section at the latest; flagged to T9's roster
roles now.

**Verify-command defect, mine.** `python3 -m app.scripts.preflight --check-collection` cannot exit 0
in any environment without a live head-migrated Postgres and a reachable Redis, because preflight
runs those checks unconditionally — plain `preflight` with no flags exits 1 here too. Confirmed
directly: the collection group itself is `[PASS]` for both venues live (5 quotable markets each,
`quotable_spread`/`venue_volume`/`fee.taker_rate` parsed, `get_book` two-sided), and the only
failures are Database and Redis. The brief should have scoped the verify to the collection group.

**T9's full-suite number was measured against a moving tree** and should not be treated as a
baseline: `tests/scripts/test_mm_backtest.py` was being rewritten by T2's retry at the time (T9
attributed this to a "T3/T4 agent" — there is none; T3 and T4 are still pending). Re-measure the
suite once T2's retry lands.

**T9's overlap decision: documented, not locked.** Rationale given — each tick opens a fresh
`AsyncSession` so the shared-session hazard cannot occur between ticks, and `_upsert_book_snapshot`'s
natural key means an overlap costs at worst one extra snapshot, never corruption. Stated explicitly
with what to do if a pass is ever measured to exceed the interval. That is a defensible answer to the
hazard rather than a silent one, which is what was asked.


**T10 verifier — accepted, 7/7 mutants red, and two limits worth carrying.**
Mutation evidence (all against scratchpad copies swapped into `sys.modules`, never the file on disk):
`volume is not None` -> truthiness, gap `>` -> `>=`, median -> mean, group key `(market,outcome)` ->
market only, exit-code OR -> AND, `bids and asks` -> `or`, and `_ensure_utc` -> no-op. Every one
turned the suite red. No statistic in the brief's list survives a plausible mutation unnoticed.

1. **Unreachable DB and empty DB both exit 1.** Confirmed live: with no Postgres listening, the CLI
   raises an unhandled `OSError` out of `session.execute` before any table or JSON is produced, so in
   TEXT the two are unmistakable — the unreachable case never prints the "zero snapshots" message.
   But the exit CODE is 1 either way (once from `report.exit_code`, once from Python's default for an
   uncaught exception). Automation keying on exit status alone cannot tell "collection stopped" from
   "I could not connect". The script does the safer of the two things — it crashes rather than
   reporting a false all-clear — so this is a limit, not a defect. It belongs in T13's residual risk
   next to the silent-venue-failure mode T9 introduced, because this script is that mode's only
   detector.

2. **T10's fallback is now dead code in production.** `config.py:711` makes
   `book_collection_interval_s` always present, so `_interval_s_and_source`'s `getattr(..., None)`
   branch can never fire through the real singleton; it survives only under the synthetic
   stand-in in tests. The test that matters keys on the `source` LABEL (`"settings"` vs `"default"`),
   not on the numeric value, so it does distinguish the two paths despite both being 60.0 — the
   hazard was checked and does not apply. The module docstring is now stale, however: it still frames
   the fallback as live conditional behaviour. Whoever next edits `collection_health.py` should fix
   that sentence.

Also confirmed: one `select(` in the whole file, so tests and the CLI share the single query path;
no `x or 0.0` idiom in code (the only occurrence is the docstring naming the anti-pattern); a real
`0.0` volume counts as present.


**T2 retry — the replay now reports the money, and one live reading reversed because of it.**
`pnl` is computed as direct cash (`sum(-direction * price * size) - fees + terminal_inventory *
settle`) rather than by adding the missing re-marking term to a marking loop. Both are algebraically
identical; cash was chosen because it contains no mark at all, so mark-independence is structural
rather than a cancellation a reader has to trust. `markout_pnl` carries the old marked number
byte-identically, so every previously-correct figure moved rather than changed, and criterion (b)
now asserts against it. The CI, `roc`, the power table and the verdict all read cash.

Divergence case: `pnl` -$2.45 -> **$0.00 exact**; `markout_pnl` -$2.45; the gap +$2.45 is exactly
`10 * (0.75 - 0.505)`, the `inventory_before * (mark_i - mark_prev)` term the docstring had dismissed.

**On the 27-market live sample, the optimistic test half changed sign: +0.6700 marked -> -0.1200
cash.** "Optimistic is positive out of sample" on this sample was an artifact of the marking
convention, not a finding. No verdict direction changed -- both remain "not above 0".

**Honest limit, volunteered by the implementer and worth preserving.** My variance argument (that
the noise term widens every CI and biases the gate toward NO-GO) is NOT measured by this sample: 3
of 4 blocks got a narrower CI and a lower sd, but pessimistic-overall got *wider*, at `n_trading`
9 and 5. That is noise in both directions. The argument stands on reasoning; T3 at full power is
what would settle it. Do not quote the variance claim as measured.

Power floor: `MIN_POWER_POOL_MARKETS = 30`, justified as (a) strictly above what
`cluster_bootstrap` already refuses (5 events) since the power table assumes MORE independence than
the CI does, and (b) the floor at which an all-positive pool stops being plausible luck -- at
p~0.8, 0.8^6 = 26% versus 0.8^30 = 0.12%. It is a refusal threshold, so moving it cannot manufacture
`n` or a verdict (GUARDRAILS 2.6). Below it, `p_profit` and `pct5_total_pnl` are null and stdout
prints the refusal explicitly.

**Two things T3 must know.**
1. `report()`'s block key is `overall`, NOT `all`. T3's verify command in TASKS.md asserted
   `d['pessimistic']['all']['n_trading']` and would have died on `KeyError`. Corrected in TASKS.md;
   recorded as a brief defect. Fail-closed, so it would have cost a dispatch, not a wrong verdict.
2. `total_pnl` is now the CASH figure under an unchanged key name -- a semantic change T3 inherits
   silently. Any comparison against a number produced before this retry is comparing two different
   statistics.

**Process note: a mid-flight guardrail does not reach an agent already dispatched.** T2's retry
mutated `app/scripts/mm_backtest.py` in place, which GUARDRAILS 3.4 forbids -- but 3.4 was appended
AFTER that agent had read the file. It snapshotted byte-for-byte first, restored after each
mutation, and disclosed the whole thing unprompted; `diff` shows only its two deliberate edits.
The lesson is mine, not the agent's: an amendment written mid-run binds only dispatches made after
it, so a fence added during a run has to be repeated in the next dispatch's prompt to take effect.


**T10 red-team — CONFIRMED BREAK. The health tool reports healthy while the collector is dead.**
`compute_venue_health` computes gaps only between consecutive STORED rows (`zip(ts, ts[1:])`) and
never compares the newest `ts` to report-generation time. Two reproductions against seeded SQLite:

1. Kalshi every 60s from `now-3h` to `now-1h` (121 rows), then silence for the final hour -- 60
   missed polls. Output: `median 60.0`, `n_gaps_over_2x_interval 0`, `empty_venues []`,
   `exit_code 0`, and the render prints `OK -- both venues have snapshots in the window.`
2. One Kalshi row, 23 hours old, in a 24-hour window: `n_snapshots 1`, `median None`, `n_gaps 0`,
   `exit_code 0`.

This lands on Kalshi -- the venue whose own `gap_semantics` string says *"a gap here means the
collector was not polling this (market, outcome)... Trust this gap metric directly."* The metric
asking to be trusted never looks at the interval that matters.

**Why the verifier's clean 7/7 sweep and this break are both true.** It is a MISSING comparison, not
a wrong one. No mutation of existing arithmetic can produce a line that does not exist. Worth keeping
as a general lesson for this kit: mutation testing measures whether the tests pin the code that is
there, and says nothing about code that should be there and isn't.

Scope: T10's written acceptance is silent on staleness, so this is outside the criteria -- but it is
squarely the failure T10 exists to catch, and T9's per-venue `except Exception` just made silent
indefinite venue death the dominant failure mode. Adjudicated as a real break; T10 returns to its
implementer. Fix aggregates staleness at VENUE level (not per market, because a quiet Polymarket book
legitimately writes no row) and wires it into `exit_code`.

Declined from the same report: a baseline/expected-count check on `n_distinct_markets`. The right
expected count moves with venue conditions, and a wrong baseline produces false alarms that train a
reader to ignore the tool.


**T9 test-author — the D8 guard cannot fail, which is the fourth instance of this shape in one kit.**
`check_collection_for_venue` samples from `select_quotable_markets(markets)`'s `quotable` set, then
asserts `quotable_spread(m) is not None` and `venue_volume(m) > 0` on those same markets. But
`quotable` is DEFINED by those predicates over the same immutable `market.raw`, so both are
unconditionally unreachable. Proven directly against `select_quotable_markets`.

Mitigating fact, worth stating so the severity is not overread: a TOTAL rename still shrinks
`quotable` toward zero and the `sampled == 0` path does fire. The damage is the `[PASS]` line, which
prints "quotable_spread()/venue_volume()/fee.taker_rate all parsed" when two of the three were
guaranteed by construction. A guard whose purpose is to fail loudly reports evidence it never
gathered. T9 retried to make the two checks derive from an unfiltered population.

**The recurring shape, now named.** Four times in this kit a well-formed check has been unable to
fail: my `page_cap_reached` grep against a string the formatter never emits; T8's fee test asserting
the Settings default it was meant to rule out; the power table's `P(profit)=1.0000` forced by an
all-positive pool; and now D8's tautological guard. In every case the artifact looked like evidence
and no consumer read anything real. **The generalisable check is to ask what INPUT would make this
fail, and then construct it** -- not to ask whether the code looks right.

**Confirmed as genuinely exercisable, and previously untested:** `fee.taker_rate` is-a-float.
`FeeSchedule` is a plain dataclass with no coercion, so `FeeSchedule(taker_rate=0, ...)` yields a
valid quotable market whose rate is an `int`. That check is real and now has coverage.

**Also confirmed, no defect:** isolation tests do raise a plain `RuntimeError` (not `VenueError`), so
they would have failed against the old broken code; `asyncio.CancelledError` propagates rather than
being swallowed, since the code catches `Exception` not `BaseException`; and failures are recorded
via `logger.exception`, so a per-tick failure leaves a traceback rather than a bare warning. Per-venue
commit durability is now pinned from a FRESH session in production venue order, which the previous
tests did not cover.

**Silent-failure surfacing — confirmed by grep, nothing exists.** No counter, metric, alarm, or
threshold anywhere in `app/` fires on repeated per-venue collection failure; no Prometheus, statsd or
Sentry integration exists in the repo. `collection_health.py` is referenced only in docstrings and is
scheduled by nothing. After T9's isolation fix, a venue failing every tick forever yields a beat
reporting success with a per-tick log line as the sole evidence. Goes in T13's residual risk.

**Out-of-kit observation, not actioned:** the same "assert the setting equals its own default"
methodology gap exists in `tests/matching/test_link_proposal_beat.py` and
`tests/services/test_near_resolution.py`. Not this kit's scope; recorded so it is not lost.


**T2 verifier — ACCEPT, with two items that must land in T2's final retry (do not lose these).**

*(i) The rebate guard covers only half the money.* `test_a_paying_rebate_does_not_change_a_single_
pnl_figure` -- the exact test GUARDRAILS 2.3 names -- asserts `.pnl` equality and never
`.markout_pnl`. The CODE is correct: calling `replay()` with `maker_rebate_rate=0.25` vs `0.0`
returned `pnl 6.56 == 6.56` AND `markout_pnl 6.56 == 6.56`. Only the regression guard is missing.

*(ii) `MIN_POWER_POOL_MARKETS` silently double-purposes as a crash guard.* Removing the floor
entirely produced 11 failures, not the 3 claimed -- the 4 targeted floor tests plus 7 unrelated ones
crashing with `IndexError`, because the same constant also prevents `rng.choices` from sampling an
empty pool. Weakening it 30 -> 1 (preserving the crash guard) produced only 2. The floor is
load-bearing in both experiments, but the claimed count reproduces neither. One constant serving a
statistical threshold AND a crash guard means a future change to the evidence floor for statistical
reasons silently changes crash behaviour. Separate them.

**The `n_unmarkable` question is answered, and the answer is "latent, not biasing" -- for now.**
`n_unmarkable_intervals = 0` on the 27-market/1,552-interval verify sample, and 0 on an independent
freshly-fetched 224-market/11,636-interval live sample cross-tabbed by decile of market history,
days-to-close, tape thinness and series -- zero in every bucket. So gating cash fills on the
availability of a markout mark discards nothing today. The structural criticism stands (cash P&L
needs no mark, so the gate is unnecessary), but there is no incidence to correlate with anything.
**Carry-forward for T3 and T5: re-run this probe.** T3's 8,000-market sample and especially T5's
1-minute resolution have far more thin/zero-quote intervals, and that is where a correlation would
first appear. If `n_unmarkable_intervals` is materially non-zero there, the cash number the verdict
rests on is computed on a filtered subsample and must be reported as such.

Independently confirmed rather than read: `pnl` bit-identical (0.0) across two mark paths while
`markout_pnl` diverges -2.45 vs +2.55; `markout_pnl` reproduces the old convention exactly by hand
reconstruction; fees subtracted once per accumulator, never summed across them; nothing outside the
module reads `markout`; `total_pnl` is cash everywhere.


**T10 retry — staleness landed, and the venue asymmetry is deliberate.** `VenueHealth` gained
`seconds_since_last_snapshot`, `staleness_threshold_s` and `is_stale`, computed venue-wide (max `ts`
across all markets) against the same `generated_at` the report already carries -- no second
`utcnow()` call, so the table and the JSON cannot drift. `stale_venues` feeds `exit_code` beside
`empty_venues`. Both red-team reproductions are now tests, proven red before the fix (`exit_code 0`
on 60 missed Kalshi polls and on a 23-hour-old single row) and green after
(`seconds_since_last_snapshot` 3600.0 and 82800.0, `is_stale=True`, `exit_code 1`).

Thresholds are asymmetric by design, `_STALENESS_MULTIPLIER = {"kalshi": 2.0, "polymarket": 15.0}`:
Kalshi stamps `utcnow()` per poll so a live collector's venue-wide max should lag by about one beat;
Polymarket's `ts` is book-move time, so a quiet market legitimately writes nothing and only all ~130
going silent at once is a failure. Both sit far below the 3600s the first reproduction measures, so
neither can pass it.

**Empirical tuning item for the first days of collection.** Polymarket's 900s is a reasoned guess,
not a measurement. Overnight quiet across all 130 tracked markets is plausible, and a threshold that
fires on healthy collection trains a reader to ignore the tool -- the same reason the
expected-market-count baseline was declined. Once real snapshots exist, read the actual distribution
of `seconds_since_last_snapshot` for Polymarket and move the multiplier to fit it. Kalshi's 120s
carries the opposite risk (a stalled pass rather than a quiet book) and is safe as written.

Also fixed in the same pass: a latent wall-clock flake in the pre-existing
`test_run_returns_0_when_both_venues_have_snapshots`, which seeded rows at the fixed `NOW` constant
while `_run` measures staleness against real `utcnow()` -- it was passing only because this session's
clock happens to fall before `NOW`'s time-of-day. Now seeded from real `utcnow()`.


**T9 retry — the D8 guard can fail now, and the thresholds are measured rather than guessed.**
The two checks re-derive as fractions over the FULL unfiltered listing (never `two_sided` or
`quotable`, which are already filtered by these very predicates): `two_sided / listed >= 0.20` and
`venue_volume_positive / listed >= 0.05`.

Measured live 2026-09-07 over `list_markets(status="open")`:

| venue | two-sided | volume > 0 |
|---|---|---|
| kalshi | 62.1% (61,347/98,817) | **16.8% (16,582/98,817)** |
| polymarket | 86.2% (1,654/1,918) | 97.7% |

Both floors sit below the tightest measured venue with margin, and a renamed field collapses either
fraction to exactly 0.0, tripping unconditionally. Proven red against a byte-faithful scratchpad copy
of the pre-fix file loaded under a separate module name (§3.4 honoured): with 2 genuinely quotable
markets plus 18 whose `bestBid`/`bestAsk` were renamed away, OLD passes with `failures=[]` and NEW
fails with "only 2/20 (10.0%) ... below the 20% floor". Both scenarios are now permanent tests.

**Kalshi's 16.8% volume-positive rate is worth carrying independently of this guard.** Five of every
six open Kalshi markets have never traded. That is not a parsing failure -- they are correctly parsed
freshly-listed markets -- but it is the denominator behind T7's finding that only 643 of 96,072
Kalshi markets are quotable, and T3 should not be surprised by it.

**Known limit of a fraction floor, accepted deliberately.** A PARTIAL rename degrades the fraction
gradually rather than to zero, so a rename affecting, say, 70% of Kalshi's listing would still clear
the 5% floor. Raising the floor to catch that would put it above Kalshi's measured 16.8% in
plausible market conditions and produce false alarms, which is the failure mode that trains a reader
to ignore the tool. The guard catches total renames unconditionally and partial ones only past a
large margin; that trade is intentional and is what the printed counts are for -- a reader who sees
17% become 6% learns something the pass/fail line alone does not say.


**T2 red-team — CONFIRMED BREAK. The power floor counts markets, not events, and the concentration
it fails to refuse exists in the live universe today.**

`MIN_POWER_POOL_MARKETS = 30` gates `_power_table()` on `len(pnls)`, a flat list carrying no event
key, while `cluster_bootstrap` requires 5 distinct EVENTS. So the power table accepts 30 markets from
any number of events, including one. Reproduced with 40 rows all `event="EVENT-SINGLE"`:
`ci95_clustered_by_event [None, None]` (correctly refused at 1 event) sitting in the SAME JSON object
as `p_profit 1.0` and `pct5 5962.09` at portfolio 5000.

The floor's own `p**n` justification assumes n independent draws and nothing enforces it. **This is
the same defect as the earlier power-table finding, along a second axis:** the first fix made the
floor refuse SMALL pools and never made it refuse CONCENTRATED ones. Worth generalising -- fixing an
instance of a flaw is not fixing the flaw, and the question to ask after any fix is "what other input
shape produces the same wrong output?"

Live confirmation, not hypothetical: `settled_universe(min_volume=2000)` returns 50,796 markets
across 18,457 events, largest being `KXDPWORLDTOUR-OMEM26` (155), `KXWORLDCUPHALFTIME-26` (130),
`KXDPWORLDTOURR1LEAD-OMEM26` (74), `KXPGATOP20-BMC26` (50), `KXBTCD-26SEP0417` (49) -- golf brackets,
halftime prop families, daily BTC threshold ladders, each ONE correlated real-world outcome. At the
harness's own median-close default cutoff, all 49 of `KXBTCD-26SEP0417` land entirely in the TEST
split, which is where the verdict and the power table print.

**Confirmed, and adjudicated as T2's job rather than T3's: the universe carries no provenance for its
own truncation.** `survivorship_share` covers exclusion-by-`result` only -- a different thing from the
`_MAX_EVENT_PAGES=150` page cap that makes ~38% of the tradeable settled universe visible with August
~94% missing. T3's brief says to write "the survivorship count" from T2's JSON, and there is no field
to hang the caveat on, so it survives only if T3's author independently reads this file. That is the
hand-off that fails. GUARDRAILS 7 forbids a number without provenance, so the caveat goes in the JSON
and the printed header structurally.

**Confirmed and being fixed: 16 of 18,457 events (128 of 50,796 markets) straddle the median cutoff**,
so a test-split market can share one correlated outcome with a train-split market. Small (0.25%) and
not an acceptance violation -- GUARDRAILS 4.1 mandates splitting by time -- but it is exactly what an
out-of-sample test exists to prevent. Straddling events are excluded from the TEST split, counts
reported either way.

**Checked and found correct, so later tasks need not re-examine:** fee sign is right in both
directions and independent of `settle` (`fee()` is non-negative and always subtracted); all four
terminal-inventory sign combinations; inventory and collateral reset per market (`MarketMaker` holds
no mutable state, so reusing one policy object across the run leaks nothing); `random.seed` runs
before `random.sample` with no intervening consumer of the global RNG, so `--seed` reproduces the
universe; and `n_short_history` is excluded from `failure_rate` by design but printed as its own
field, so a large silent-skip rate is visible.


## PHASE 2 REVIEW: REJECT

The Phase 2 reviewer returned **reject** with 8 findings, 3 irreversible. I re-verified the two most
decisive claims directly in the code before accepting. Phase 2b (T15-T17) is the remediation.

**The one-sentence version: Kalshi would have recorded nothing, Polymarket would have recorded
something T11 cannot use, and the beat would have reported success the entire time.**

**F1 (irreversible) — the stored `volume` cannot produce a delta.** `_upsert_book_snapshot` writes
`venue_volume(market)`, which tries the 24-HOUR fields first by design (correct for ranking, which is
what it was written for). Measured live: Kalshi's `volume_24h_fp` did not move for **1 of 6,058
actively-traded markets** over 245s, while lifetime `volume_fp` moved for 25 of the same set.
Polymarket's `volume24hr` went DOWN for 62 of 255 (24.3%). Verified downstream by me:
`passive_fill.py:142` returns no fills when `volume <= 0.0` -- **gating BOTH fill models** -- and `:97`
raises `ValueError` on a negative. So three weeks of Kalshi collection replays to zero fills, and a
quarter of Polymarket's intervals raise. Fault is the PLAN's (D9 says "volume"; T8's brief said
"`venue_volume` at snapshot time"; the implementer wrote exactly that). **Nobody asked what
`venue_volume` IS.**

**F2 — one Kalshi pass takes ~3 hours on a 60s beat.** `tasks/collection.py` discards the
`VenueMarket` objects `list_markets` just built and passes only ids, so `collect_books` spends one
`get_market` per candidate at 0.108s each: ~101,045 markets = **3.03 hours per tick**. A full nested
listing walk returns all 101,045 in **15 seconds**, and `quotable_spread`/`venue_volume` compute
straight off it -- which is what T7 built them for. The per-market fetches cancel T7's entire stated
saving. **This also falsifies T10's Kalshi staleness threshold**: 120s is justified in-file as "about
one beat" and is off by ~90x, so `collection_health` would scream `is_stale` at a WORKING collector
from day one -- the exact false-alarm mode T10 declined the market-count baseline to avoid. T9 and T10
disagreed about what the system does and neither measured it.

**F3 — a routine 404 kills the whole venue, silently and forever.** VERIFIED BY ME:
`raise_for_venue_error` maps 429/401/403 to `VenueError` and lets everything else through as
`httpx.HTTPStatusError` (deliberately, per its docstring), while `collect_books:1075` catches only
`VenueError` -- so a 404 escapes to the venue-level `except Exception` at :1119. **3 of 5 real tickers
sampled from the open listing 404 on `/markets/{ticker}`.** This is the ordinary case. NOTES had
framed this failure mode as a hypothetical "unwrapped payload bug"; it is routine, and F2 makes it
certain every tick.

**F4 (irreversible) — no observation time.** Polymarket's `ts` is book-move time and T8's same-`ts`
path refreshes rather than inserts, so "quiet book" and "dead collector" are permanently
indistinguishable. Worse: a refreshed row's `volume` comes from the LAST poll that saw that `ts`, so
the volume channel is shifted forward by one dwell and partly measures activity AFTER the mark.

**F5 (irreversible) — fee provenance discarded.** `FeeSchedule.source` distinguishes a published
schedule from the category table that is wrong on 82% of live Polymarket markets. The snapshot writes
the rates and drops `source`, and preflight cannot catch it because a wrong default is a good float.
`maker_fee_rate` -- the rate a passive quoter actually pays -- is not stored at all.

**F6 — nothing runs the guards.** `grep -rn preflight app/` outside the script returns two comments.
`collection_health` appears only in docstrings. PLAN's "Done looks like #2" requires a preflight
before every run. **This one is mine**: I wrote the acceptance as "`--check-collection` exits 0 live",
which tests the CLI rather than the gate. A deliverable deferred because it was nobody's task.

**F7 — selection churns and membership is unrecorded.** Kalshi over 5 minutes: quotable 684->684,
selected 500->500, but **20 markets (4.0%) left and 20 entered**, all departures from lost quotability
rather than the cap. Three weeks of holes, each indistinguishable from a collector fault.

**F8 — migration 008 unapplied and ungated.** Starting the beat against 007 makes every insert raise
`UndefinedColumn`, swallowed per-venue, both venues dead every tick. `preflight` already checks head
revision; F6 is why it never runs.

**The reviewer verified three mutations red** (T8's same-`ts` refresh, T9's per-venue commit, T7's
settings-sourced spread floor), so the pinning is real. Its closing line is the lesson: *"The tests
pin the code that is there. Every finding above except F7 is code that isn't there."* Third time this
kit has learned that, and the first time it cost a whole phase.


**T2 DONE — the event-aware floor, and the number that shows why it mattered.**
`_block` now passes `[(r.event, r.pnl)]` -- the event key stopped being dropped at the boundary -- and
`_power_table` resamples **whole events with replacement** rather than markets i.i.d.
`MIN_POWER_POOL_EVENTS = 30`, pinned at 29/30, with `CI_FLOOR_EVENTS = 5` derived from
`cluster_bootstrap`'s real behaviour in the test rather than hard-coded, so it trips if calibration's
floor ever moves.

**The resampling change is not cosmetic.** On 40 events x 25 same-signed markets, the i.i.d. market
draw gives a 500-market portfolio sd of **22.4**; the whole-event draw gives **111.8**. The old power
table described a portfolio five times safer than the data supports. That is now a test.

Verified in the live JSON by me: `universe_provenance` carries `listing_is_page_capped: true`,
`sample_is_random_draw_from_venue: false`, `visible_share_of_tradeable_universe: 0.379`, the full
month table (2026-08: 4,613 visible / 77,602 invisible) and a `consequence` sentence saying a verdict
generalises to "markets like the ones the listing shows", not to Kalshi. `split_exclusions` carries
the straddler rule and its counts. The power block carries `pool_n_events`, `pool_floor_events`,
`mean_markets_per_event` and `resample: whole_events_with_replacement`, and refuses correctly at 4
events against the floor of 30 beside a verdict whose CI is unavailable -- **the two blocks now
agree**, which was the original complaint.

Note for T3: the event-level power table costs ~2s per report at T3 scale, negligible against a
25-minute run.


**T15 DONE — the four irreversible columns landed while 008 is still unapplied.** Verified by me:
32 tests pass and the offline SQL renders all seven `book_snapshot` columns (`volume`,
`taker_fee_rate`, `maker_rebate_rate` from T8; `volume_lifetime`, `observed_at`, `fee_source`,
`maker_fee_rate` from T15) with no `UPDATE`.

`volume_lifetime` deliberately does NOT reuse `venue_volume()` -- a separate `_LIFETIME_VOLUME_KEY`
reads the lifetime-only key per venue, so T7's ranking is untouched. The monotonicity guard returns
`None` on a decrease (never the smaller value, never 0.0, never negative), and accepts outright when
`previous is None` so one restatement resets the baseline rather than vetoing every later poll. It
runs on BOTH paths -- the same-`ts` refresh (Polymarket's normal path) and the insert, via a
`_last_known_lifetime_volume()` lookup, because Kalshi's `ts=utcnow()` means Kalshi essentially always
inserts and a refresh-only guard would never fire for it. `observed_at` is written unconditionally on
both branches rather than gated behind "did content change", since its job is "when did we last
confirm this row is alive" -- which a content-gated write cannot answer for a quiet market.

**T15 exposed the same F1 defect one layer up, in T11's brief -- corrected before T11 runs.** T11 said
`volume = (volume_{i+1} - volume_i) if both non-null else 1.0`, reading the `volume` column. Both
halves were wrong: it named the 24h column whose delta is always zero, and `else 1.0` fabricates a
trade where the truth is unknown. Because `passive_fill.py:142` gates BOTH fill models on
`volume <= 0.0`, that default is the difference between a fill and no fill -- an optimistic bias in
the number meant to be the conservative one. Corrected to `volume_lifetime`, with unknown intervals
counted and excluded rather than defaulted, and with a note to use `observed_at` rather than `ts`
when reasoning about interval length.

**Worth naming: this is the fifth `None`-collapsed-into-a-number in this kit** (`row.volume or 0.0`;
T8's stale fee; T10's share denominator; T15's restatement guard; now T11's `else 1.0`). The pattern
is always the same -- a missing measurement is replaced by a plausible constant, and the constant is
indistinguishable downstream from a real observation. Later tasks should treat any `or <literal>` or
`else <literal>` on a measured quantity as suspect by default.


**T3's first Gate 1 numbers (sample 8,000) — underpowered, but the first positive signal in the
project.** Pessimistic, cash, `terminal=settled`:

| block | n_trading | n_events | mean/market | CI |
|---|---|---|---|---|
| overall | 1,767 | 1,293 | +0.3074 | **[+0.0872, +0.5344]** |
| temporal test | 770 | 576 | +0.1507 | [-0.1590, +0.4777] |

Optimistic: overall [+0.2004, +0.5864], test [-0.0763, +0.5300]. **The overall CI is entirely above
zero -- the first measurement in this project that has been.** The out-of-sample CI spans zero, which
is a NO-GO by the rule set before any of it was seen. Both facts go in the report; neither may stand
in for the other.

Underpowered against its own acceptance (needs `n_trading >= 2500` overall, `>= 1000` test): the
trading rate is 28.3% of collected, not the ~37% my brief assumed. 429 rate is 0.0 and the universe
is 50,780, so sample size is the binding constraint and is free to raise -- the honest fix is to buy
the power, never to restate the threshold (GUARDRAILS 2.6).

Two supporting measurements. `n_unmarkable_intervals = 0` across **377,494 intervals**, which retires
T2's verifier's carry-forward: the harness discards no fills at scale, so the cash figure is not
computed on a filtered subsample. And event concentration is LOW -- 1,767 markets across 1,293 events,
~1.37 markets/event -- so after the red team found the power table treating one 49-market BTC ladder
as 49 draws, the clustering is doing honest work on this sample. Separately, **21.8% of sampled
markets (1,748/8,000) were skipped for <4 candles**, a selection effect stacked on the page-cap bias.

**A THIRD instance of the catch-too-narrow pattern, and this one cost 43 minutes of live collection.**
T3's 20,000-market run crashed after ~27 minutes: `fetch_candles` -> `adapter._get` -> `httpx.ReadError`.
`mm_backtest.py:820` catches `(VenueError, OSError, ValueError)`, and `httpx.ReadError` is an
`httpx.TransportError`, NOT an `OSError`, so one transport blip terminated a run the 2% `failure_rate`
budget was designed to absorb. The same shape as `collect_books`' `except VenueError` (F3) and
distinct from Kalshi's deliberate `raise_for_venue_error` pass-through.

**Why four review roles missed it: every test for this file uses `httpx.MockTransport`, and a mock
transport cannot produce a transport error.** It was only reachable against the live venue, at scale,
after 27 minutes. Seventh instance of fixtures disagreeing with live payloads in this kit -- and the
first where the gap was in the *error* path rather than the data path. Worth generalising: a mocked
transport tests the venue's answers, never the network's failures.

**Compounding defect: the cache is written only at the end.** After a 27-minute run,
`.cache/mm/kalshi-60m.json` was absent -- the crash discarded everything. `--cache` exists so work is
not repeated, and as built it only helps a run that already succeeded. Being fixed with an atomic
incremental flush so an interrupted run resumes rather than restarts.


**Three consecutive large runs crashed on `httpx.ReadError` — so T3 was unachievable as specified,
not unlucky.** Samples 20,000 and 19,000 both died mid-collection; the 8,000 run survived but came in
underpowered at `n_trading` 1,767 against an acceptance of 2,500. Reaching 2,500 needs roughly a
12,000+ sample, which is ~25+ minutes and ~12,000 HTTP requests, and at that scale a transport error
is not a tail risk — it is three for three.

The consequence is worth stating precisely: **no amount of retrying would have produced a passing T3.**
Every path to the acceptance threshold ran through a duration in which the crash was effectively
certain, and because the cache only persisted on success, each attempt started from zero. A task whose
acceptance requires a scale its own tooling cannot survive is not a flaky task, it is an impossible
one, and the loop would have burned retries indefinitely without the crash being diagnosed. Recorded
as a brief defect against T3: the acceptance named a sample size without anyone checking that the
harness could run that long.

This also sharpens the mock-transport lesson. The gap was not "a rare live condition our fixtures
miss" — it was "the single most common live failure mode, which our fixtures cannot express at all."
`httpx.MockTransport` answers every request; the network does not. Any future task in this repo that
runs thousands of live requests should be assumed to hit a transport error, and its error path tested
with a real `httpx` exception rather than a mocked response.


**CORRECTION — the crash is in the universe walk, not the per-market fetch. My first diagnosis was
incomplete and cost a fifth run.** Run 4's stack:

    mm_backtest.py:2266 main -> :2176 _main -> :2130 _collect_or_load -> :787 settled_universe
      -> KalshiAdapter.list_markets -> adapter._get -> httpx -> anyio.BrokenResourceError -> httpx.ReadError

`settled_universe` walks up to `_MAX_EVENT_PAGES = 150` pages of `/events` with **no transport-error
handling anywhere**, so one dropped TLS read in ~150 sequential requests discards the entire walk.
The per-market `httpx.HTTPError` fix was a real defect and is genuinely fixed -- it simply does not
protect this path, which runs before collection starts.

**The evidence was in front of me and I misread it.** Runs 2 and 3 printed
`universe: 50774 settled markets`; run 4 printed no such line. I had already noticed that this line
appeared AFTER the traceback in runs 2-3 and explained it away as stdout/stderr buffering, instead of
asking why one log had it and another did not. That single missing line distinguishes "crashed during
collection" from "crashed before collection", which is exactly the distinction that determines which
catch matters. I then told T3 to re-run on the strength of the wrong diagnosis, buying an 80-minute
crash. Lesson worth keeping: **when two runs of the same command produce different LOG PREFIXES, that
difference is the diagnosis** -- reconcile it before acting on a theory about the suffix.

**The larger finding underneath it: the universe walk is repeated in full on every invocation.** It is
the most expensive step in the script, it is identical across runs at a given `--min-volume`, and five
runs have each paid for it and thrown it away. Being fixed with an on-disk universe cache keyed on
`min_volume`, atomic-written like the candle cache, plus a bounded 3-attempt retry on `httpx.HTTPError`
only (never on `VenuePayloadError`, which must stay loud). Staleness is handled by printing the
cache's age and offering `--refresh-universe`, deliberately without an automatic expiry: a universe
cached an hour ago is correct for re-running the same experiment and wrong for a fresh measurement,
and that is the caller's judgment to make.

**Running tally of the catch-too-narrow pattern in this codebase: four sites.** `collect_books`
per-market (`except VenueError`, F3); `collect_books` venue-level (established by T9);
`mm_backtest._collect_one` (`except (VenueError, OSError, ValueError)`); and now
`settled_universe`/`list_markets` (no catch at all). Only the last two were reachable exclusively
against the live venue at scale. Any future task in this repo that issues thousands of live requests
should be assumed to hit a transport error in EVERY loop it contains, not just the innermost one.


## PHASE 2b COMPLETE — remediation of the rejected Phase 2

**T16 — the pass, the 404, and selection membership.** Measured live: Kalshi `list_markets` returns
99,588 markets in 10.46s while `get_market` averages 0.096s/call, confirming the reviewer's ~2.66h
figure. The beat now carries the listing's own `VenueMarket` objects through, so a tick costs **zero**
`get_market` calls. The per-candidate catch widened to `(VenueError, httpx.HTTPError)`, with tests
building a real 404 `httpx.HTTPStatusError` proving it costs one candidate, not the venue. New
`SelectionMembership` table (migration 009), one row per `(venue, market_id)` -- bounded by distinct
markets ever selected, not by elapsed ticks.

**T16's honest caveat, which became T17's requirement.** The pass is NOT ~15s. `get_book` has no
caching between outcomes and Kalshi markets always carry `("YES","NO")`, so 500 selected markets cost
~1,000 paced calls: a realistic tick is **~110-125s**. T16 declined to fix `get_book`'s per-outcome
duplication -- correctly, as a pre-existing adapter-level inefficiency outside its brief -- and flagged
it as the binding constraint instead of quietly absorbing it.

**T17 — the gates, and an interval derived rather than assumed.** `book_collection_interval_s`
60 -> **180.0** (125s worst case + ~44% margin); `collection_health_interval_s` 1800.0 with its own
beat entry; `run_collect_books` refuses to start when the collection preflight or the DB head check
fails (head is now **009**, two unapplied migrations); and 5 consecutive zero-write ticks (15 min)
raises so the Celery task registers as a FAILED result -- queryable via `AsyncResult`, which is a real
signal in a repo with no Prometheus, statsd or Sentry.

T17 discovered that `default_check_database`/`default_check_collection` call `asyncio.run()`
internally and therefore cannot be invoked from inside the loop `run_async_task` already opened; it
awaited the underlying primitives instead of restructuring either side.

**T10's staleness threshold survives the interval change** because it reads
`2 * book_collection_interval_s` from settings dynamically rather than hard-coding 120s -- it
auto-scales to 360s and keeps its "tolerate one full missed tick" meaning. The F2 finding that it was
"off by ~90x" is resolved by T16's fix, not by a new constant.

**A defect T10's retry introduced and its own tests missed.** T17's suite run surfaced
`test_run_returns_1_when_only_one_venue_is_empty` failing on `assert "kalshi" not in err`. T17 called
it a pre-existing time-of-day flake; that framing was wrong. T10's staleness feature now flags a venue
whose newest snapshot is older than the threshold, the fixture seeded a hardcoded
`2026-09-07T12:00:00Z`, and wall clock had moved ~6,000s past it -- so Kalshi was reported stale and
its name appeared in stderr. **T10's retry fixed exactly this pattern in a sibling test and never
swept the file.**

Fixed by seeding from real `utcnow()`, assertion untouched. The sweep established precisely why only
one test was exposed rather than guessing: `compute_venue_health` takes an explicit `now=`, so every
test calling it directly is deterministic by construction, and only the four `_run()` tests touch the
real clock -- three of which were already correct. Time-independence was then PROVEN, not asserted:
3 scenarios x 5 clock offsets (+0s, +6000s, +30d, +400d crossing a leap boundary, -400d), 15/15
passing.

Worth keeping as a rule: **a test that passes only because the current hour falls on the right side
of a constant is broken whether or not it is red today.** Fixing the instance you can see and not
sweeping for siblings is how this one survived.


## T3 — KALSHI GATE 1: NO-GO

`VERDICT kalshi fill_model=pessimistic terminal=settled split=temporal-test ci_low=-0.1205
ci_high=+0.4507 n_trading=1239 -> NO-GO`

**MY ERROR, caught by T3.** I read the `test` block of the run JSON and announced GO without checking
`cutoff_ts`. The harness defaults `--temporal-cutoff` to the MEDIAN close, a ~50/50 split; T3's brief
requires "the date that puts ~70% of sampled closes in train". Re-scoring the identical cached
candles at the compliant cutoff (71.2% train, offline, seconds, no new network) reverses the verdict.
Verified myself against the written report: `cutoff_ts` 1788637226 = 2026-09-05T19:40:26.

| pessimistic | n_trading | n_events | mean | CI |
|---|---|---|---|---|
| overall | 4,320 | 2,479 | +0.2913 | [+0.1622, +0.4314] |
| train | 3,066 | 1,758 | +0.3490 | [+0.1785, +0.5320] |
| **test** | **1,239** | **717** | **+0.1501** | **[-0.1205, +0.4507]** |

Optimistic test: n_trading 1,283 / 741 events / mean +0.1868 / CI [-0.0959, +0.4928]. Both fill models
span zero at the compliant split, so the NO-GO does not depend on the queue-position assumption.

**The finding is not "no effect" -- it is that the effect halves out of sample.** Train mean +0.349
with a CI well clear of zero; test mean +0.150 with a CI spanning it. That is a temporal split doing
precisely the job it exists for, and it is why the 50/50 default flattered the result: a later, shorter
test window performs materially worse than the earlier data the policy was calibrated on.

**Not underpowered.** Test `n_trading` 1,239 clears the acceptance floor of 1,000 and 717 events clears
both the CI's 5-event floor and the power table's 30-event floor. Overall 4,320 clears 2,500. This is a
NO-GO measured at adequate power, not a shrug.

**Event concentration is far healthier than the earlier sample.** Largest event overall
`KXVANCEMENTION-26SEP03` at 14 markets (0.32%); largest on test `KXNASCARTOP10-COOOS26` at 12 markets
(0.97%) -- against the 155-market golf bracket that motivated the event-aware power floor. The
clustering has little work to do here, which makes the CI more credible, not less.

`n_unmarkable_intervals` = **0 of 921,222** on both models: the cash figure is computed on the full
sample. 429 rate 0% across all six collection attempts; 11 request errors (0.055%) absorbed by the
widened catch -- the first live evidence that fix works, where each would previously have killed the run.

**Second time this session I announced a conclusion from evidence I had not fully checked** (the first
was the crash misdiagnosis, where a missing log line was the tell). Both were caught by agents. The
generalisable habit: when a result arrives, verify the CONDITIONS it was computed under -- the cutoff,
the split, the filter -- before reading the number. A number is only as good as the block it came from.


**The taper hypothesis is far stronger in the full sample than when PLAN was written.** T3's
pessimistic run: **3,390 of 4,320 trading markets (78.5%) carried inventory into settlement**, 1,414
of them settling short into `yes`. On the test split, 967 of 1,239 (78.0%), 406 short into yes.
PLAN/T6 were written against the earlier 537-market run's 160 (29.8%). Terminal inventory settles at
the venue's real binary result, so nearly four in five markets end with `inventory * (settle -
last_mid)` -- a coin flip carrying no edge, added to every market's P&L. That is the leading
explanation for why the out-of-sample CI spans zero while the point estimate stays positive, and it
makes T6 the highest-value remaining lever.

**Dispatch ordering, and why T6 is not first despite that.** T6's acceptance requires "T4's rule" --
the two-split challenger rule (tune on a random event-half, score on the other half AND the temporal
test, 60 halves, win only at >=90% plus the temporal test). That rule does not exist until T4 builds
it in `mm_calibrate.py`, and T4 also sweeps `max_inventory`, which is a direct substitute lever on the
same held-into-settlement quantity. So T4 first, T6 after. T5 runs in parallel because its expensive
half is live 1-minute collection, which touches nothing T4 touches -- but T5 is explicitly forbidden
from changing any default, since its own brief also defers to T4's rule and T4 may be editing
`market_making.py` concurrently.

**Standing instruction added to both dispatches: use the compliant temporal cutoff explicitly.** The
harness default is the median close (~50/50) and the brief requires ~70% train. The gap is not
cosmetic -- [+0.081, +0.542] versus [-0.121, +0.451] on the same cached candles. Every table in every
report must state which cutoff produced it.


## T4 — CALIBRATION SWEEP: no default changes, and the strongest near-miss in the kit

Sweep run by the orchestrator against the completed hourly cache (offline, no new collection),
`--train-fraction 0.70`, cutoff 2026-09-05T19:40:26, 60 halves, 90% win threshold.

**Every scope failed the rule.** Venue-wide 51/60 (85%); the ten per-series scopes ranged 8/60 to
42/60. `final_params_is_default: true` everywhere. **Defaults do not change** -- 51/60 is not 54/60,
and GUARDRAILS 2.6 exists exactly for the moment a threshold stands between me and a result I would
like to have. The threshold was set before any of this was seen.

**The venue-wide candidate is nonetheless the strongest signal this kit has produced.**
`edge_fraction=0.9, min_spread=0.25, max_inventory=50` against defaults `0.8 / 0.10 / 20`:
temporal-test ROC **0.0373 vs 0.0090 (4.1x)**, full-sample ROC **0.0531 vs 0.0195 (2.7x)**,
`temporal_win: true`, and 51 of 60 random halves.

**The `max_inventory` slices falsify the taper premise T6 was written to test.**

| max_inventory | held into settlement | ROC | mean P&L |
|---|---|---|---|
| 10 | 72.9% | 0.0061 | +0.088 |
| 20 | 78.5% | 0.0195 | +0.291 |
| 50 | 81.7% | 0.0344 | +0.515 |

More inventory means MORE markets held into settlement and monotonically BETTER returns. PLAN said
carrying inventory into a coin flip "is what moved every CI to span zero"; the data says restricting
it cuts ROC ~5.6x while only moving settlement exposure 82% -> 73%. Per-market settlement is a coin
flip, but its variance diversifies across thousands of markets, and the cap that avoids it also
forecloses the spread capture that pays for everything. **T6's question is now real rather than
presumed: can a taper narrow the INTERVAL enough to clear zero while lowering the MEAN?**

## The candidate's Gate 1 verdict — and why it is NOT a GO

The sweep optimises ROC; Gate 1 turns on `ci_low > 0`. I scored the candidate through the verdict
path directly. Pessimistic test: n_trading 884 / 618 events / mean +0.8928 / ROC 0.04335 /
**CI [+0.4983, +1.3061]**, `lower_bound_above_zero: True`. Optimistic test CI [+0.6198, +1.3442].

**Three reasons this does not overturn T3's NO-GO, all of which must appear in the report:**

1. **The result is circular.** The two-split rule uses the temporal test as one of its gates -- the
   candidate carries `temporal_win: true` because it was *selected* partly for winning that test.
   Scoring it on the same temporal split and reporting the CI as an independent verdict is exactly
   the shape of defect this kit keeps finding: a number produced by a process that guarantees it.
   This is in-sample for the selection, not out of sample.
2. **It is underpowered against T3's own acceptance.** `min_spread=0.25` quotes far fewer markets:
   2,300 trading overall and 884 on test, against floors of 2,500 and 1,000. The wider interval is
   partly just less data.
3. **The split is not the compliant one.** `--temporal-cutoff 2026-09-05` (midnight) yields a
   **61.4%** train share, not ~70%. Different cutoff, different split, not comparable to T3's verdict
   line without saying so.

**What would settle it:** scoring these parameters on data that played no part in selecting them --
a fresh Kalshi sample outside the current cache, or the forward Polymarket/Kalshi collection Phase 2b
just made safe to start. That is a clean test and it is worth running. Until then the honest statement
is: *a promising parameter direction, selected on this cache, not yet validated off it.*


## T5 — 1-MINUTE SUB-STUDY: the hourly P&L is right and its UNCERTAINTY is not

300 markets (top `n_fills`, range [5,52]) collected at 1-minute into `.cache/mm/kalshi-1m.json`,
0 failures, 601,816 candles. Cutoff 2026-09-04T09:11:15Z, realised train share exactly 210/300.

**Blocker the brief did not anticipate, and a live harness defect.** Kalshi's candlestick endpoint
caps at **5,000 periods** (measured: 5,000 OK, 5,040 -> HTTP 400), so `--interval 1 --days 10`
(14,400 minutes) is impossible in one request and failed 3/3. T5 chunked at 4,800 in its own scratch
driver and correctly did NOT edit `candles.py`. **`fetch_candles` does not chunk, so any minute-scale
window over ~3.5 days fails.** T6 and any future minute work hit this; it needs a task.

**Mean P&L is resolution-invariant; the confidence interval is not.**

| | 60m | 1m |
|---|---|---|
| mean P&L | $3.9502 | $3.9447 |
| clustered CI | [+3.1799, +4.7370] | [+2.7764, +5.3300] |
| fills | 2,583 | 5,031 |
| per-fill edge | $0.4588 | $0.2352 |

Same money, **1.95x the fills, half the per-fill edge, sd +44% and CI +64% wider**. This is the most
consequential number T5 produced: **every hourly CI in this kit is optimistically narrow.** T3's
NO-GO therefore stands more firmly, not less. It also bears on T4's candidate: its test CI
[+0.4983, +1.3061] has half-width 0.404, which at +64% becomes ~0.66 -> roughly [+0.24, +1.55]. Still
clear of zero, but the margin is smaller than it looks and must be stated that way.

`n_unmarkable` = **0 of 601,216** at 1m, discharging T2's carry-forward at the finer resolution too.

**Skew is measured harmful, in both directions the docstring claimed.** At `interval=1`, skew 1.0
earns +$3.9447 against skew 0.0's **+$8.4928** -- skew gives up **53.6% of available P&L** -- and buys
no tail protection: worst market **-$46.80 at skew 1.0 vs -$33.95 at skew 0.0**, which is
SIGN-REVERSED from the hourly table on the same markets (-$11.77 vs -$14.76). The
`DEFAULT_SKEW_STRENGTH` docstring's hypothesis that "hourly candles understate the value of leaning
against inventory" is false at both the mean (advantage grows 1.48x -> 2.15x) and the tail. Skew's
only win is the 5th percentile: $2.22 of left tail bought for $4.55 of mean.

**No default changed, correctly.** The 300 markets are the `n_fills` top tail, not a random sample.
Acting needs T4's two-split rule over `skew in {0.0,0.25,0.5,1.0}` on a 1-minute cache of a RANDOM
sample at the same 0.70 cutoff.

**What the hourly replay understates -- exposure, not P&L.** Peak `|inventory|` never exceeds 20.0 at
either resolution across all 16 sweep cells, so `max_inventory=20` with `quote_size=10` is a
structural cap and the LIMIT, not skew, bounds exposure (agreeing with T4's slices). What hourly hides
is how often and how long: share reaching the ceiling 51.0% -> **74.0%**; median `|inv|` **0.0 -> 10.0**
(flat versus carrying ten contracts); time-weighted mean `|inv|` +20.5%; **worst single market -$11.77
-> -$46.80, 3.98x**. The 78.5% held-into-settlement headline is a **floor, not an estimate**.

**And holding gets WORSE at finer resolution, opposite to the hypothesis.** `held_into_settlement`
207 -> 220 of 300 pessimistic (69.0% -> 73.3%), 202 -> 233 optimistic. More chances to trade are more
chances to re-accumulate. This does not explain the out-of-sample CI spanning zero -- it deepens the
problem, and it is the second independent result (with T4's slices) pointing away from PLAN's stated
taper rationale.

T5 also proved its instrumented replay identical to the harness across 1,200 `MarketRow` comparisons
at both resolutions and both fill models -- verifying its own instrument before trusting its numbers.


## T18 — THE CLEAN HOLDOUT: promising, unconfirmed

Fresh 12,000-market sample (seed 20260908) drawn from the ~35,187 markets remaining after excluding
all 15,283 ids in `.cache/mm/kalshi-60m.json`. 7,985 had usable history. **`overlap=0`, verified
twice** -- inside the harness before sampling, and independently by intersecting the two caches' id
sets. Cutoff from `seventy_percent_cutoff()`: realised train share **70.01%** (5,590 of 7,985).

| policy | n_trading (test) | n_events | raw CI | +64% widened (T5) |
|---|---|---|---|---|
| defaults 0.80/0.10/20 | 630 | 441 | [-0.3298, +0.4315] | -- |
| candidate 0.90/0.25/50 | 325 | 268 | **[+0.1105, +1.2075]** | [-0.2405, +1.5585] |

**The 4.1x ROC is NOT pure selection artifact.** The candidate's sign and rough magnitude carried
over to markets that played no part in tuning it, and its raw CI clears zero despite far less data
(325 trading markets against 884 in the contaminated scoring). That is a real, informative result:
the direction -- quote fewer and wider, cap inventory loosely -- survived an honest test.

**It is also not a GO, for two reasons that both have to be stated.** T5's measured 64% CI-widening
correction takes it to [-0.2405, +1.5585], which spans zero; and `n_trading` 325 is well below Gate
1's own 1,000-market power floor. Defaults remain NO-GO on holdout too, consistent with T3.

**Why the power is low, and what it costs to fix.** `min_spread=0.25` quotes few markets by design:
12,000 sampled -> 7,985 usable -> ~2,400 on the test split -> 325 trading at candidate params (13.5%).
Reaching 1,000 would need a test split near 7,400 markets, i.e. ~37,000 sampled -- but only ~23,000
of the visible universe remain unsampled. **Pure holdout at this `min_spread` cannot reach the floor
on Kalshi's visible universe at `--days 10`.** The honest levers are more history per market
(`--days 30` gives more quote intervals on the same markets, changing neither the universe nor any
threshold) or a lower `--min-volume` (which changes the population and must be reported as such).
Shrinking the test fraction is not a lever -- that is threshold-fiddling and GUARDRAILS 2.6 forbids it.

**Standing lesson: the pre-committed reading did its job.** The interpretation was fixed before the
run -- clears zero on held-out markets means real, spans zero means artifact, both reported with equal
prominence. The answer landed between the two, and having written the rule first is what makes
"promising but unconfirmed" a finding rather than a hedge.


## T18b — THE 30-DAY HOLDOUT: my power hypothesis was wrong, and the venue is why

Re-ran the identical 12,000 draw (same seed, universe, exclusion) at `--days 30`. `overlap=0`
re-verified independently (15,283 vs 8,080 ids, zero intersection); `seventy_percent_cutoff()` gave
train share 70.00%. The 30-day market set is a strict superset of the 10-day one (+95 markets),
confirming the same sample rather than a fresh draw.

| 30-day, pessimistic test | n_trading | raw CI | widened +64% |
|---|---|---|---|
| defaults 0.80/0.10/20 | 668 | [-0.1828, +0.5746] NO-GO | [-0.4252, +0.8170] NO-GO |
| candidate 0.90/0.25/50 | 354 | [+0.3672, +1.4040] GO | **[+0.0354, +1.7357]** clears by 3.5 cents |

**MY RECOMMENDATION WAS WRONG ON THE FACTS.** I proposed `--days 30` to triple the quote intervals
per market. Measured: `n_quoted` rose **5-7.5%**, not ~3x, because **84.4% of the 7,985 markets got an
IDENTICAL candle count at 30 days as at 10.** Median Kalshi market lifetime is under **2 days** of
hourly candles; only 0.2% were near even the 10-day cap and none approached the 30-day cap. There is
no history to buy -- the lookback was never the binding constraint. `n_trading` moved 325 -> 354.

**The structural consequence, which matters more than the run.** Kalshi retrospective power is bounded
by the NUMBER OF MARKETS, and the visible universe is nearly exhausted: ~23,000 unsampled against the
~37,000 a 1,000-market test split would need at `min_spread=0.25`. Neither more lookback nor more
sampling can reach the floor. **Hourly Kalshi replay cannot answer this question at the required
power, and no amount of re-running changes that.**

`held_into_settlement` essentially unchanged (77.8% -> 78.0% defaults, 85.8% -> 86.2% candidate),
mechanistically explained: a longer lookback extends history into the PAST, not closer to settlement.

**The remaining lever is resolution, not lookback -- and it is now available for the first time.**
At `--interval 1`, a market with a 2-day lifetime contributes ~2,880 intervals instead of ~48, roughly
60x more quote/fill/mark triples from the SAME markets and the SAME universe. It also removes the need
for T5's 64% correction entirely: the 1-minute CI is the honest interval measured directly at the
resolution a live quoter experiences, rather than an hourly interval adjusted by a factor. This was
impossible until the candle-chunking fix landed an hour ago -- `fetch_candles` failed on any minute
window past ~3.5 days against Kalshi's 5,000-period cap.

Cost estimate: T5 collected 300 markets at 1-minute in 900 GETs; ~8,000 markets with median lifetime
under 2 days (2,880 minutes, inside one 4,800-period chunk) is on the order of 8,000-16,000 GETs.


## T11 — SNAPSHOT REPLAY BUILT (Polymarket's only possible instrument)

`app/scripts/mm_replay_snapshots.py` + 38 tests. Full suite 1,402. Verified by me:
`m.report is b.report` and `m.replay is b.replay` both **True** -- there is exactly one report
implementation in the kit, not two that agree today and drift tomorrow. The only `or 0.0` / `else 1.0`
strings in the file are in the docstring naming the rejected anti-pattern, same as
`collection_health.py`.

**Three things it did better than the brief asked.**

1. **The unknown-volume exclusion is structural rather than parallel.** Instead of re-checking
   `passive_fill`'s volume gate, a `None` delta forces that candle's `px_low`/`px_high` to `None`, so
   T2's own `_trades()` refuses the interval regardless of volume. Both fill models therefore exclude
   identically **by construction** -- no second code path that could drift from the first.
2. **Exclusion counts are computed only over the range `replay()` actually reads as a fill interval**
   (`j` in `[1, len-2]`). An unknown reading at a series' first or last snapshot is never consulted
   by the replay, so counting it would have overstated exclusions. Subtle, and the kind of thing that
   quietly inflates a caveat until it looks like a problem.
3. **Two exclusion reasons kept distinct**: `n_unknown_volume_intervals` (delta unknown) vs
   `n_incomplete_fill_book_intervals` (delta known, fill snapshot's book one-sided or crossed). Both
   in the header beside `n`.

**A real repo-reality gap, resolved by design instead of by editing the harness.** T2's `replay()`
takes one scalar `tick_size` and one `FeeSchedule` per market, but Polymarket's tick size varies per
market. T11 grouped markets by observed tick size, called `replay()` once per bucket, and merged the
`ReplayResult`s -- documenting both as limitations inherited from reusing `replay()` unmodified,
rather than editing a file another agent was mid-run against. That is the right call under the
constraint and it is written down where the next reader will find it.

Also uses `observed_at` rather than `ts` for dwell reasoning (`median_observed_dwell_s` reported
beside the `ts`-derived window), and requires all four fee columns non-null together for a row to
count as a complete fee reading, falling back to the venue schedule otherwise -- so T15's `fee_source`
does real work here.

Honest flag it volunteered: a raw `python3 -c` invocation of the CLI against in-memory aiosqlite hung
before any of its own code ran, while the identical pattern via pytest's fixture runs fine. Judged a
sandbox artifact of raw asyncio+aiosqlite under `-c`, reported rather than dropped.

**What this unlocks.** Kalshi is backtestable because it publishes candles; **Polymarket is not** --
no historical bid/ask, no public tape. Every Polymarket number this project will ever produce must
come from forward-collected snapshots, and this is the script that turns them into the same verdict
Kalshi got, through the same `report()`. It runs against an empty table today and exits 1 with a clear
message, which is the correct answer until collection starts.


## T18c — THE 1-MINUTE HOLDOUT: the result that settles Kalshi

Third holdout window: same 12,000 draw, same seed, same universe, same exclusion, 10-day lookback,
**`--interval 1`**. 11,911 of 12,000 markets collected (89 too short, **0 errors**), 7,303,230 candles
against 502,052 at hourly on the same markets. ~36,000 requests -- the largest collection of the kit,
possible only because `fetch_candles` gained chunking hours earlier. **`overlap=0` verified a third
time** (15,283 vs 11,911, zero intersection), and the 60-minute run's 7,985 markets are an exact
subset of this run's 11,911. Train share 69.98%. `n_unmarkable_intervals=0` across 7,279,408 intervals.

### Pessimistic, terminal=settled, temporal-test. No widened column -- this IS the honest resolution.

| policy | n_trading | n_events | mean/mkt | CI | vs floor | verdict |
|---|---|---|---|---|---|---|
| defaults 0.80/0.10/20 | 2,150 | 1,402 | +0.0661 | [-0.1646, +0.3154] | 2.15x | **NO-GO** |
| candidate 0.90/0.25/50 | 1,477 | 1,057 | +0.7717 | **[+0.4522, +1.0977]** | 1.48x | **GO** |

Optimistic agrees: defaults [-0.0471, +0.4387] spans zero; candidate [+0.5940, +1.1983] clears it.

### The finding hourly resolution was hiding: THE DEFAULTS LOSE MONEY

At 1-minute the default policy's **overall** CI is `[-0.3143, -0.0434]` -- **entirely below zero** at
6,284 trading markets -- and train is `[-0.4603, -0.1264]`, also entirely below. The hourly replay
reported +0.2913/market overall for the same policy. **That is a sign flip, not a magnitude
adjustment.** Hourly candles were not merely optimistic about the defaults; they were wrong about
which side of zero they sit on.

### Why the candidate strengthens where the defaults collapse

The candidate's test CI *improved and tightened* from 60m to 1m: `[+0.1105, +1.2075]` at n=325 ->
`[+0.4522, +1.0977]` at n=1,477. Coherent mechanism: at 1-minute you observe the adverse selection
that hourly averaging conceals. Quoting tight (`min_spread=0.10`) gets picked off by it; quoting wide
(`min_spread=0.25`) does not. Per-fill edge shrank 48.4% ($0.5528 -> $0.2853) against T5's independent
48.7% on a disjoint 300-market sample -- an almost exact reproduction of a number measured by a
different agent on different markets.

Candidate ROC on test: **8.87%** per 10-day window, against 4.34% at hourly.

### What this establishes, and what it does not

Established: on markets that played no part in selecting the parameters, at the resolution a live
quoter experiences, with a compliant 70/30 temporal split, at power 1.48x the floor, with both fill
models agreeing -- **the candidate policy's out-of-sample edge is real, and the shipped defaults lose
money.** The same run returns NO-GO for the defaults at 2.15x the floor, so the instrument
discriminates rather than blessing everything.

Not established, and no amount of replay can establish it: **queue position.** Every fill in this
study assumes the order rested where the model says it did. That is the single largest unmodelled
cost and it is exactly what Gate 2 exists to measure. Also unchanged: the page-cap bias means this
generalises to "markets like the ones the listing shows", not to Kalshi.

**The defaults must not ship as they are.** They are calibrated on hourly data that gets their sign
wrong. Changing them is T4's rule's business, not a verdict I make here -- but the evidence for
revisiting them is now much stronger than the 51/60 near-miss that left them standing.


## T6 — THE TAPER: the mechanism works and still does not pay

Swept on the 1-minute holdout (11,911 markets, `overlap=0` re-verified, 69.98% train) at the new
defaults `0.90/0.25/50.0`, pessimistic:

| taper_hours | mean | **ci_low** | ROC | held |
|---|---|---|---|---|
| 0 | +0.7717 | **+0.4631** | 0.0887 | 85.5% |
| 1 | +0.7282 | +0.4395 | 0.0836 | 84.3% |
| 3 | +0.5780 | +0.3276 | 0.0662 | 83.3% |
| 6 | +0.4827 | +0.2469 | 0.0552 | 83.1% |
| 12 | +0.3304 | +0.0969 | 0.0377 | 82.5% |
| 24 | +0.2633 | +0.0341 | 0.0300 | 82.1% |

**`ci_low` falls monotonically and is never once above the untapered value.** `two_split_rule`
(imported from `mm_calibrate`, not reimplemented) selects no nonzero taper on any of 60 halves --
`wins=0/60`, `candidate_key=None`. `DEFAULT_TAPER_HOURS` stays **0.0**.

**Why this is a better null than "no effect".** The mechanism demonstrably works: the CI narrows
(width -25.6%) and `held_into_settlement` genuinely falls (-3.45pp), both in the intended direction.
The mean simply falls faster (-65.9%), so the narrowing never pays for itself. Reframing T6 from a
mean question to a variance question was what made that visible -- had it reported only means, the
result would have read as "taper is bad" rather than "the trade is real and priced against you".

This closes the loop opened by PLAN's dead premise. Three independent measurements now agree that
terminal inventory is not the thing to fix: T4's `max_inventory` slices (tighter caps cost 5.6x ROC),
T5's resolution study (holding RISES at finer resolution), and T6's sweep (reducing holding costs more
mean than it buys variance). **Carrying inventory into settlement is a cost this strategy is paid to
bear, not a leak to plug.**

## PHASE 1 COMPLETE

T1 candle client, T2 harness, T3 Gate 1 (NO-GO at old defaults), T4 calibration (no default won on
hourly), T5 minute study, T6 taper (no taper won), T18 clean holdout (**GO at candidate params, and
the old defaults measured NEGATIVE at honest resolution**). Defaults updated 2026-09-08 to
`0.90 / 0.25 / 50.0`. Full suite 1,411.


## PHASE 1 REVIEW: REJECT — the GO does not survive, and I verified the decisive facts myself

The reviewer reproduced every headline figure to 4 dp from the caches through the unmodified harness
and could not make it lie under five mutations. **The arithmetic is honest. The experiment is not.**

**F1 — event-level contamination. VERIFIED BY ME.** Market-id overlap is genuinely 0, but
`tuning events 7,297 | holdout events 7,817 | SHARED 3,183`, and **6,073 of 11,911 holdout markets
(51.0%) belong to an event that also appears in the tuning cache.** The kit's CI is event-clustered,
so the event is the unit of independence -- and `_split()` already removes straddling events WITHIN a
cache for exactly this reason. No equivalent check exists ACROSS caches; `--exclude-markets-from`
excludes by `market_id` only. Re-scored: event-disjoint n=447 -> **[-0.1760, +0.9415] NO-GO**;
event-shared n=1,030 -> [+0.5757, +1.3093] GO. Proven not a power artifact: 200 random 389-event
subsamples of the full pool clear zero **94.5%** of the time; the event-disjoint result sits at the
**0.5th percentile**.

**F2 — the "temporal holdout" is 41 hours of a holiday weekend. VERIFIED BY ME.**
`70% cutoff 2026-09-05 18:30 Sat -> 2026-09-07 11:21 Mon = 1.70 days` against a 65.8-day span, because
**89% of closes fall in the final 7 days**. Test weekdays: Sun 2,192 / Sat 714 / Mon 670. NCAAF is
39.3% of trading markets and **75.3% of all test P&L**; removing that one sport gives
[-0.0503, +0.7273] -> NO-GO. And the circularity closes: **649 of the holdout's 807 NCAAF test markets
belong to an event that also has markets in the TUNING cache's test split** -- different lines on the
same football games, the same afternoon, as the gate that selected the parameters.

**F3 — the two-split rule was bypassed, not outgrown.** The only 60-halves run on this grid is hourly
and it FAILED (51/60, `passed: NO`). No 60-halves run was ever done at 1-minute for this triple -- and
**T6 ran `two_split_rule` on the very same 1-minute cache hours later for `taper_hours`, got 0/60, and
honoured it.** Both reports producing the evidence explicitly decline to change the default; a later
report attributes the change to "T4 evidence" whose recorded verdict is `passed: NO`.

**F5 — the new defaults have zero behavioural coverage.** Deselect the two pin tests and every
mutation (0.25->0.10, 0.90->0.80, 50.0->20.0) leaves **1,409 passing**. No test exercises a book with
spread between 0.10 and 0.25 -- the exact band the change is about.

**F6 — no machine-readable artifact exists for the run the defaults changed on.** `reports/` holds one
JSON. The holdout report asserts its figures carry `fill_model`/`terminal` "in its source JSON"; that
JSON does not exist and the caches are gitignored. GUARDRAILS 7.

**F7 — T5's headline does not generalise, and I generalised it.** Same 7,985 markets, hourly vs
1-minute: defaults +0.2038 -> **-0.2258** (sign flip); candidate +0.8227 -> +0.6210 (-24.5%). T5's
"mean is resolution-invariant" held only on its 300-market top-2%-by-fills subsample -- and **the 64%
CI-widening factor came from that same unrepresentative sample** and was applied as a universal
correction.

**F8 — the MECHANISM survives everything.** On identical markets, candidate per-fill +$0.2915 vs
defaults +$0.0104 (28x), and the defaults' per-fill edge is **negative outside NCAAF** (-$0.0528)
while the candidate stays positive in both buckets. Adverse selection at tight spreads is measured,
not fitted. Its MAGNITUDE and significance are what come 75% from one sport on one weekend.

### Mine, beyond the four already recorded

1. Never asked whether the holdout was event-disjoint -- though `_split()` implements that exact rule
   within a cache, and the red team had already found the 155-market golf bracket that motivated the
   event-aware power floor. The reasoning was in the kit; I did not apply it across the boundary.
2. Never printed the calendar width of the test split. The kit twice noticed the cutoff mattered and
   never asked what 30%-of-markets buys in days. It buys 1.7.
3. Ran the rule at 1-minute for `taper_hours` and not for the triple I actually changed.
4. Changed a shipped default on two reports that both decline to change it.
5. Generalised a 300-market study into a universal correction without checking whether its companion
   finding transferred. My own holdout data disproves it.
6. Let the one artifact that would make the change reproducible go unwritten.

**Nothing is burning:** `MarketMaker` is absent from the `STRATEGIES` registry and from all five beat
entries, so blast radius is backtest tooling only.


## Ledger

agent: T1 id=a660a1a00c94ea27c role=implementer model=sonnet
agent: T7 id=a6ff81fe523801b53 role=implementer model=sonnet
agent: T7 id=a9cdf7b2754e5ddb9 role=test-author model=sonnet findings=1 confirmed=0 marginal=0 result=accepted
agent: T1 id=ad7dfd92683eea6b4 role=test-author model=sonnet findings=1 confirmed=1 marginal=1 result=accepted
agent: T7 id=a578f36b3c4d2eaf5 role=verifier model=sonnet findings=0 confirmed=0 result=accepted
agent: T7 id=ae3e25cb8e3c193e7 role=red-team model=sonnet findings=3 confirmed=2 marginal=2 result=accepted
defect: T9 kind=stale-plan-decision
agent: T1 id=a7b7fc5cf7a922838 role=verifier model=sonnet findings=0 confirmed=0 result=accepted
agent: T1 id=ae7e9954a9ed62ce8 role=red-team model=sonnet findings=8 confirmed=6 marginal=6 result=accepted
agent: T7 id=ad3a16691d7a21711 role=implementer model=sonnet
outcome: T7 model=sonnet attempts=2 result=retry-pass review=revised run=2026-09-07-7465
- **T8 dispatched FRESH, not as a warm continuation of the T7 cluster.** TASKS.md's preamble flags
  T7 -> T8 -> T9 as a cohesive same-file cluster, which normally argues for one warm implementer.
  Declined here on the skill's own caveat that "a warm agent accumulates context and eventually needs
  compaction, which destroys the cache advantage": T7's two implementers finished at ~218k and ~137k
  subagent tokens, already near that point, and T8's brief is self-contained. T9 will be reassessed
  against T8's finishing context.
agent: T1 id=a36241fca4d6ee749 role=implementer model=sonnet
outcome: T1 model=sonnet attempts=3 result=retry-pass review=revised run=2026-09-07-7465
agent: T8 id=a45c1cfc9c834f33c role=implementer model=sonnet
agent: T8 id=aad6cd9fbf01cce77 role=test-author model=sonnet findings=2 confirmed=2 marginal=2 result=accepted
agent: T8 id=af4992995f2ecc596 role=verifier model=sonnet findings=0 confirmed=0 result=accepted
agent: T8 id=a057dd77a044f5d08 role=red-team model=sonnet findings=5 confirmed=2 marginal=2 result=accepted
defect: - kind=stale-plan-decision
agent: T8 id=a31186773a56211e1 role=implementer model=sonnet
outcome: T8 model=sonnet attempts=2 result=retry-pass review=revised run=2026-09-07-7465
agent: T10 id=af5d11299227681f4 role=implementer model=sonnet
agent: T2 id=ac0cbacfcb55001cb role=test-author model=sonnet findings=2 confirmed=2 marginal=2 result=accepted
agent: T10 id=ad7dbd6dc2e40ca01 role=test-author model=sonnet
defect: T2 kind=unsatisfiable-acceptance
agent: T10 id=ad7dbd6dc2e40ca01 role=test-author model=sonnet findings=3 confirmed=3 marginal=3 result=accepted
agent: T9 id=a93631a4fb8d4f0ea role=implementer model=sonnet
defect: T9 kind=unrunnable-verify
agent: T10 id=a67a566d967d9fbf5 role=verifier model=sonnet findings=2 confirmed=2 marginal=2 result=accepted
agent: T2 id=ab7cb41deda1652ee role=implementer model=opus
defect: T3 kind=stale-key-in-verify
agent: T10 id=ad9ab436a7952245c role=red-team model=sonnet findings=1 confirmed=1 marginal=1 result=accepted
agent: T2 id=a38dcdd6b06aa7c02 role=verifier model=sonnet
agent: T10 id=a0b068a973d3d5169 role=implementer model=sonnet
agent: T9 id=a32f1f5c970f00a7c role=test-author model=sonnet findings=5 confirmed=5 marginal=4 result=accepted
agent: T9 id=a0551b313116240c2 role=implementer model=sonnet
agent: T2 id=a38dcdd6b06aa7c02 role=verifier model=sonnet findings=2 confirmed=2 marginal=2 result=accepted
agent: T2 id=a7ff7eb4dc2c040c4 role=red-team model=sonnet
outcome: T10 model=sonnet attempts=2 result=retry-pass review=revised run=2026-09-07-7465
outcome: T9 model=sonnet attempts=2 result=retry-pass review=revised run=2026-09-07-7465
agent: T2 id=a7ff7eb4dc2c040c4 role=red-team model=sonnet findings=3 confirmed=3 marginal=3 result=accepted
agent: T2 id=aa6a69c7d55296b02 role=implementer model=opus
reviewer: P2 model=opus findings=8 confirmed=8 result=accepted
defect: T8 kind=wrong-field-semantics
defect: T9 kind=unmeasured-performance-assumption
defect: - kind=deferred-deliverable
agent: T15 id=af12a2b27a628197d role=implementer model=sonnet
outcome: T2 model=opus attempts=3 result=retry-pass review=revised run=2026-09-07-7465
outcome: T15 model=sonnet attempts=1 result=pass review=none run=2026-09-07-7465
defect: T11 kind=wrong-field-semantics
agent: T16 id=a313a78b4ca0e57ea role=implementer model=sonnet
agent: T2 id=a42118774217d52b6 role=implementer model=sonnet
defect: T2 kind=mock-only-error-path
defect: T3 kind=unachievable-acceptance
agent: T2 id=af24bc76569de6002 role=implementer model=sonnet
defect: T2 kind=unprotected-listing-walk
agent: T17 id=a71447e2e6afae23d role=implementer model=sonnet
agent: T10 id=a3407d578235a2530 role=implementer model=sonnet
outcome: T16 model=sonnet attempts=1 result=pass review=none run=2026-09-07-7465
outcome: T17 model=sonnet attempts=2 result=retry-pass review=none run=2026-09-07-7465
defect: T10 kind=wall-clock-dependent-fixture
outcome: T3 model=sonnet attempts=1 result=pass review=none run=2026-09-07-7465
defect: - kind=orchestrator-misread-split
agent: T4 id=af9cb39711caf0e80 role=implementer model=sonnet
agent: T5 id=abd11956f749dead5 role=implementer model=opus
agent: T5 id=a8e072ea13b808aee role=implementer model=opus
outcome: T5 model=opus attempts=2 result=retry-pass review=none run=2026-09-07-7465
defect: T5 kind=uncapped-candle-window
outcome: T4 model=sonnet attempts=2 result=retry-pass review=none run=2026-09-07-7465
agent: T18 id=aa9005674205b5f57 role=implementer model=sonnet
outcome: T18 model=sonnet attempts=1 result=pass review=none run=2026-09-07-7465
defect: - kind=orchestrator-unchecked-premise
agent: T11 id=aaf4088f9f502a5e6 role=implementer model=sonnet
outcome: T11 model=sonnet attempts=1 result=pass review=none run=2026-09-07-7465
outcome: T18 model=sonnet attempts=3 result=pass review=none run=2026-09-07-7465
agent: T6 id=a0fddc3dd556fa781 role=implementer model=sonnet
outcome: T6 model=sonnet attempts=1 result=pass review=none run=2026-09-07-7465
reviewer: P1 model=opus findings=8 confirmed=8 result=accepted

## T20 — The honest holdout (the measurement that decides market making)

Scored the candidate params on a Kalshi sample that is EVENT-disjoint from the tuning
cache, stratified by ISO close week, at 1-minute resolution. Four criteria were fixed in
advance. Reproduced twice (runs 3 and 4 identical to the digit).

Report: `reports/kalshi-honest-holdout.json`.

Result: THREE of four met. Not a GO.

  1. event_overlap = 0                       MET   (0 shared events, 0 shared market ids;
                                                    verified by raw json.load of BOTH cache
                                                    files, not through the harness's own
                                                    exclusion filter)
  2. test window >= 14 days                  MET   17.18 days, 2026-08-21 -> 2026-09-07
  3. n_trading >= 1000                       NOT MET   919
  4. two-split rule passes                   MET   59/60 halves (98.3%, threshold 90%) plus
                                                    the temporal test; first pass in this kit

The candidate's pessimistic test CI does clear zero: [+0.0365, +0.8615], n_trading=919 /
789 events, mean +$0.436 per trading market, total +$400.67, ROC +5.10%, collateral_mean
$4.97, 2,392 fills over 17.18 days. Optimistic agrees: [+0.0457, +0.8595].

The old defaults 0.80/0.10/20.0 are decisively NEGATIVE on the same honest sample:
[-0.7607, -0.2473], n_trading=1448, ROC -6.20%. That is the clearest measurement in the
kit: the pre-calibration defaults lose money, and the loss survives removing any single
series.

Two things keep this from being a GO beyond the n_trading miss:

- The lower bound is razor-thin and series-fragile. Removing ANY ONE of three series flips
  it: minus KXCS2GAME [-0.0088,+0.7768]; minus KXITFWMATCH [-0.0225,+0.7653]; minus
  KXNCAAFFIRSTTDTEAM [-0.0091,+0.8214]. All NO-GO.
- Two SINGLE-MARKET series carry 21.4% of test P&L: KXNCAAFFIRSTTDTEAM n_trading=1 -> $43.00
  and KXPGAPLAYOFF n_trading=1 -> $42.75. A one-market series contributing a tenth of all
  profit is an outlier, not a business.

What genuinely improved over the rejected Phase 1 holdout: dispersion. Top series is 14.4%
of P&L here versus NCAAF's 75.3% there, across 401 series. And the power table is not
forced: p_profit is 0.954 at a 500-market portfolio and 0.998 at 1000 (TEST split; this line read 0.974/0.986 until a Phase 3/4 review caught them as the OVERALL sample's values, pool 990 -- the fourth instance of that error class in this kit), not 1.0000 — the
pool is not all-positive, so the statistic can fail and did not.

Note on criterion 3, recorded as an observation and NOT as a re-score: n_trading was a
PROXY for power, and the direct power measurement clears what the proxy was protecting
(5th-percentile total P&L at a 1,000-market portfolio is +$93.24, p_profit 0.998). The
criterion was still fixed in advance at 1000 and the sample delivered 919, so it FAILED.
Moving a threshold after seeing the number is the exact error this kit exists to prevent.

Falsified my own pending recommendation: I had recommended reverting the shipped defaults
to 0.80/0.10/20.0 after the Phase 1 rejection removed their basis. That recommendation is
WITHDRAWN — 0.80/0.10/20.0 is measurably loss-making on an event-disjoint sample, and the
grid of 48 policies produced no challenger that beats the shipped 0.90/0.25/50.0 at the 90%
threshold (`candidate=None wins=0/60` means no challenger qualified, NOT that the shipped
params lost). Keep the shipped defaults.

Sampling honesty: stratifying by close week improved the per-week CV from 2.80 (natural
draw, same size, same seed) to 2.12, but could not flatten it — the post-exclusion universe
holds 15,475 markets closing in W36 and under 100 in each earlier week. The stratifier
cannot manufacture supply that Kalshi's close calendar does not have.

outcome: T20 model=opus attempts=1 result=pass review=none run=2026-09-12-3e71
agent: T20 id=afb55ab2c38c253b5 role=implementer model=opus

### T20 addenda from the final report (these change the interpretation)

- **The P&L is largely NOT spread capture.** Several of the biggest contributing markets
  earned $22-28 on only 3-5 fills — that is inventory carried into a favourable settlement,
  a directional outcome, not the maker's spread. The five largest single MARKETS carry
  39.95% of the candidate's entire test P&L and one market alone carries 10.73%. Half the
  test P&L closes on a single day. Series-level dispersion (top series 14.4%) looked much
  healthier than market-level concentration actually is.
- **The >=14-day window was met in SPAN, not in density.** 98.1% of test markets close in
  the final 8 days of the 17.18-day window. Criterion 2 is honestly met as written, but the
  concern that motivated it — one weekend deciding everything — is only partly retired. This
  is a property of Kalshi's close calendar and the page-capped listing, not of the sampler:
  excluding tuning events removes 33,213 of 50,470 universe markets (65.8%), and of the
  17,257 that remain only ~520 close before 2026-09-01 against 16,737 in the next 7 days.
- Train share fell to 6.61% (324 markets) — the arithmetic price of demanding a long test
  window from that pool.
- **The two-split rule's pass is a tuning gate on the same sample, not independent
  evidence.** Recorded so nobody later cites 59/60 as out-of-sample confirmation.
- T20 explicitly DECLINED to collect ~2,000 more W36 markets to push n_trading past 1,000:
  adding data after seeing a marginal positive is the widen-to-make-n move GUARDRAILS §2.6
  forbids. `--sample 5000` was fixed before any result. Correct call, recorded so the
  restraint is visible.

**Consequence for what to measure next.** If the Kalshi edge is settlement luck rather than
spread capture, then `markout_pnl` — which is mark-based and mark-independent of settlement —
is the statistic that DISCRIMINATES between the two. That is exactly what T21's
`--markout-only` computes, and exactly what Polymarket forward snapshots can deliver in weeks
rather than the years cash-settled P&L would need there.

## Infrastructure state — the database, and where the fence stops

The repo has its own `polymarket-postgres` service in `docker-compose.yml` (TimescaleDB
pg15, port 5432). Port 5432 was free — the other Postgres containers on this machine belong
to unrelated projects (`stock-ai` on 127.0.0.1:54329, `personalos` unpublished). Started it;
healthy. Starting a database is not applying a migration.

Migrations 008 and 009 are rendered to `reports/pending-migrations-008-009.sql` and reviewed:
**purely additive** — 7 nullable columns on `book_snapshots` (volume, taker_fee_rate,
maker_rebate_rate, volume_lifetime, observed_at, fee_source, maker_fee_rate) plus the new
`selection_membership` table. Zero DROP / TRUNCATE / DELETE / ALTER COLUMN statements.

**Applying them is where GUARDRAILS §2 stops this kit**, verbatim: "Nothing applies a
migration to a live database. `alembic upgrade head --sql` only. A task that needs a schema
change renders the SQL, tests the model on SQLite, and stops." So the SQL is rendered and
this stops. T12, T13 and the whole Polymarket markout path stay blocked behind a decision
only the user can make — the fence did its job and is not mine to lift.

### Correction to the T20 addendum above — the aggregate moderates the market-level story

T20 reports several LARGE individual markets earning $22-28 on 3-5 fills, which is settlement
luck rather than spread capture. That observation is correct at the level of those markets.
It does NOT generalize to the sample, and I checked before letting it stand:

    split   n_trading   cash mean/mkt   markout mean/mkt
    test          919        +0.4360           +0.6281
    overall       990        +0.4471           +0.6063
    train          71        +0.5906           +0.3244

On the test split the mark-based statistic is HIGHER than cash (+0.6281 vs +0.4360, same 919
markets). Markout is settlement-independent — it marks each fill forward at mid(i+2). So in
aggregate, carrying inventory into settlement is a net DRAG of about $0.19/market on what the
quoting itself earned, not the source of the profit. The correct statement is: the biggest
individual winners are settlement outcomes, while the aggregate edge survives without them.

I nearly recorded this wrong. The first version of the `market_making.py` docstring paired the
OVERALL markout mean (+0.6063) with the TEST cash mean (+0.4360) — two different samples, read
as if they were one. That is the identical error logged earlier in this file as
`defect: - kind=orchestrator-misread-split`: reading a number without checking the conditions
it was computed under. Caught before it shipped this time; recorded because catching it twice
is not the same as not making it.

## T22 — The density gate is noise. Quote-everything wins.

Report: `reports/kalshi-density-gate.json`. Branch 3 of the pre-committed interpretation fired:
the gate does not beat ungated. Verified the anchor myself — the report's ungated row
reproduces T20 to the full float (`ci95=[0.03651241534988742, 0.8615392781316351]`,
n_trading=919, total +400.67, markout +0.6281), which proves both studies measured the same
rows through the same inference path.

Chosen honestly: N* = the admissible N maximising ROC on TRAIN, ties to the larger N,
admissible = >=30 trading markets and >=5 trading events. Rule returned **N*=2**. Applied
unchanged to test, N*=2 **destroys the result**: CI [-0.2398, +0.6262] (spans zero, against
ungated [+0.0365, +0.8615]) and ROC falls +5.10% -> +2.86%.

**The falsification is the elegant part.** The strict gate N>=20 admits exactly TWO series:
`KXITFMATCH` (33 markets) and `KXITFWMATCH` (25) — both ITF tennis. A gate built to be
P&L-blind lands on precisely the set a forbidden series-naming rule would have named. The
"densest series are strongest" structure in T20's table was two tennis series wearing a
density costume. Partly confirms it: removing KXITFWMATCH stops it clearing zero ([-0.8469, +2.7565]) but removing KXITFMATCH does NOT ([+0.1742, +3.9633]) -- 'remove either' overstates it, and the density-gate JSON's own headline says 'either one' against its own fragility rows, and the optimistic
fill model does NOT clear zero at N>=20 ([-0.1923, +2.5620]) while pessimistic does — two fill
models disagreeing about significance on identical rows.

Two-split rule at 1-minute, event-disjoint halves: full grid FAILS (winner N>=20 at 11/60).
Head-to-head, the only gate that passes is **N>=1 at 55/60 — which moves P&L by exactly
$0.00**, because a market that fills makes its own series dense enough to admit itself. Its
entire "gain" is a smaller denominator. It also has no causal form: the strictly causal
variant (density counted on train only) admits 20 trading markets in the whole test split,
CI [-1.382, +2.685]. There is no causal version of this gate with power.

**Multiplicity, counted rather than waved at: 23 distinct policy variants and 33 CI
evaluations against this one test split** across T20+T22. Under independence and a true null,
23 looks give P(>=1 spurious clear) = 44%; 33 give 56.6% (the JSON's 0.5663; NOTES first said 58%) — upper bounds, since the variants are
nested subsets of one row set. The ungated lower bound sits at 8.4% of its mean. Neither it
nor N>=20's has margin against even a handful of looks. This is the number to quote when
anyone asks how much the +$400.67 should be believed.

### The capital picture — the answer to "is there a possible profit here?"

                        ungated      N>=1       N>=20
    quoted markets        1,581      1,383         205
    total collateral     $7,858     $6,941        $959
    P&L over 17.18d     +$400.67   +$400.67     +$83.42
    P&L per day          $23.32     $23.32       $4.86
    fills per day         139.2      139.2         6.3
    ROC                  +5.10%     +5.77%      +8.70%

Scaled to a year on the assumption the window repeats: **~$8,512/yr on ~$7,900 of tied
capital, event-clustered interval [$713, $16,820]/yr.** Ungated is the only configuration
with real throughput. The density gate's prettier ROC is bought entirely by shrinking the
denominator below $1,000 — a better percentage of a smaller business.

Plainly: a rounding error, not a job. The percentage is respectable; the dollars are not.

Decision: **ship nothing.** `MarketMaker`'s shipped defaults and its quote-everything
behaviour stand unchanged. No repo source file was modified by T22.

outcome: T22 model=opus attempts=1 result=pass review=none run=2026-09-12-3e71
agent: T22 id=ad58d8ca189b2d7a7 role=implementer model=opus findings=1 confirmed=1 result=accepted

### The migration precondition is complete — GUARDRAILS §2 was satisfied in full, not just obeyed

§2 prescribes three things: render the SQL, test the model on SQLite, stop. All three are done.

- **Rendered**: `reports/pending-migrations-008-009.sql`, from `alembic upgrade 007:head --sql`
  (offline mode — `alembic/env.py::run_migrations_offline` never builds an engine).
- **Verified non-destructive**: `tests/test_migration_book_snapshot_volume_fee.py` renders the
  ACTUAL SQL Alembic emits for the 007->008 step and asserts no backfill, rather than re-reading
  the migration's Python source — so an accidental `server_default` that becomes a backfilling
  UPDATE under the hood would be caught. 0 DROP / TRUNCATE / DELETE / ALTER COLUMN statements.
- **Model tested on SQLite**: `tests/models/test_book_snapshot_volume_fee.py` uses conftest's
  `test_session`, which runs `Base.metadata.create_all` on an in-memory SQLite engine — the same
  schema `008` must produce, not a drifted second definition. Both a fully-populated row and an
  all-`None` row round-trip; the pre-existing unique constraint on
  `(venue, market_id, outcome, ts)` is untouched. `selection_membership` (009) is exercised
  through `tests/services/test_book_collection_selection.py`.
- **Stopped**: nothing was applied.

So the decision in front of the user is now a clean one — apply or do not — with no engineering
precondition left outstanding.

### Repo hygiene — flagged, not acted on

Full suite: **1,433 passed**, run from `backend/` by the orchestrator. `ruff check .` reports 69
errors, ALL in pre-existing tracked files (`app/api/routes/bots.py`, `app/scripts/backfill_data.py`,
alembic revisions 001-006 and old tests). **Zero are in any file this kit created** — mm_backtest,
mm_calibrate, mm_replay_snapshots, collection_health, candles, the probes, 008/009. That debt
predates the kit and is not its to fix silently.

The kit's ENTIRE output is uncommitted: 25 untracked paths (including `mm_backtest.py`,
`reports/`, `NOTES.md`, migrations 008/009) plus 14 modified files, sitting on the branch
`fix/frontend-dependency-advisories` — named for an unrelated frontend dependency commit.
Surfaced to the user rather than committed, since committing was not asked for.

## T13 — Go / no-go, and four errors it caught in my own briefing

`reports/go-no-go.md` written; verify command exit 0 (exactly 2 `VERDICT ` lines, "queue
position" present). The two verdicts:

- **Kalshi -> NO-GO.** Criterion 4 of 5 failed (n_trading 919 vs a pre-committed 1000); the CI
  fails on removal of any one of three series; 39.95% of P&L in five markets; 33 CI evaluations
  of this one split.
- **Polymarket -> UNDERPOWERED**, not NO-GO — and the distinction is the point: no P&L has EVER
  been measured there, which is a different claim from measuring it and finding none. Needs
  1,000 more trading markets to reach this kit's own power floor; time-to-power ~2.4yr at
  min_spread>=0.10 / ~4.2yr at >=0.25.

I briefed that agent from memory and notes. It checked the reports instead and found I was
wrong four times. The reports win; all four are now corrected at source:

1. **Polymarket cohort figures were fabricated by compaction.** I briefed 136 markets / 57
   events at >=0.10 and 78 / 34 at >=0.25. Verified myself: `136`, `57` and `78` appear ZERO
   times in `polymarket-feasibility.md`. The measured figures are **130 / 53** and **75 / 32**.
2. **"1.15 orders/market-hour measured previously" was never measured.** Verified: the string
   exists in exactly two places in the entire repo — `TASKS.md:763`, which is the architect's
   own brief, and the go-no-go report where T13 flags it. Nothing measured it; the brief cited
   a number into existence. T13 derived a real order rate from cited fields instead
   (`quote_hours=163219`, `n_two_sided=159713`) and showed the arithmetic: 322,932 orders over
   the window = 0.218 orders/s sustained, 118.7 per market-hour, mean concurrency 6.60 markets.
3. **I quoted the OVERALL power table while arguing about the TEST criterion.** Verified: there
   are two tables. Overall (990 markets) reads +$89.21 / p_profit 0.986 at a 1,000-market
   portfolio; TEST (919 markets) reads **+$93.24 / 0.998**. Corrected in NOTES and in
   `market_making.py`. This is the THIRD instance of the same error in this kit —
   `defect: - kind=orchestrator-misread-split` logged it, then I caught the markout version
   before it shipped, and this one got all the way into a module docstring. The pattern is not
   carelessness about arithmetic; it is reading a number without re-checking which sample it
   was computed over. Two power tables sitting under sibling keys is exactly the shape that
   defeats it.
4. **Multiplicity 58% was my rounding of the JSON's 0.5663 (56.6%).** Corrected both places.

T13 also declined to answer a question it could not answer cleanly: the candidate's 5th
percentile is ALREADY positive at the smallest portfolio measured (500 markets, +$6.47 on the
test split), so the crossing point is "at or below 500" and is NOT pinned. It said so rather
than implying 500 is the threshold. It further noted the power table resamples the same
919-market pool whose five largest carry 39.95% of P&L, so it is not independent
corroboration — now recorded in the docstring too.

defect: T13 kind=unmeasured-performance-assumption
outcome: T13 model=opus attempts=1 result=pass review=none run=2026-09-12-3e71
agent: T13 id=a0926cd2053530af7 role=implementer model=opus findings=4 confirmed=4 result=accepted

## T14 — Gate 2 designed, and the correction that matters most in this kit

`reports/gate2-design.md` written. Verify exit 0; `tests/test_fences.py` 31 passed; full suite
1,433. No order-placing code was written, stubbed or fixtured, and the fences' allowed-file
lists are unchanged.

**Scoping, honestly:** neither venue qualifies. Kalshi is NO-GO (not a flavour of
UNDERPOWERED); Polymarket is UNDERPOWERED but 2.4-4.2 years from power with zero snapshots —
the opposite of "close". So the document is written in the conditional, with a four-part entry
condition that is explicitly NOT met today. The queue arm is scoped to Kalshi (the only venue
with replay predictions to score `realized >= predicted` against); the rebate arm is
Polymarket-only and much smaller (10 fills, no replay prediction needed).

### CORRECTION — `markout_pnl` is NOT settlement-independent, and I asserted that it was

I wrote, in NOTES above and in `market_making.py`'s docstring, that markout is
settlement-independent and therefore the +$0.6281-vs-+$0.4360 gap proves settlement is a net
drag rather than the source of profit. **That is wrong.** Verified by reading
`mm_backtest.py` `replay()` directly:

    cash    += inventory * market.settle
    markout += inventory * (market.settle - last_mid)

Both accumulators take a settlement term. They differ in HOW MUCH they absorb — cash receives
the whole settlement value, markout only the move from `last_mid` — not in whether they absorb
any. And it is not a rounding detail: **805 of the 919 test markets (87.6%) held inventory into
settlement.**

So the honest position is weaker than what I recorded: the two accounting bases differ, and
that gap is not evidence about spread capture versus settlement luck. Separating those two
requires a genuinely settlement-free measurement — `mm_replay_snapshots.py --markout-only`,
which reverses the terminal term via `_strip_terminal_settlement` and stamps
`terminal="excluded"`. **That has never been run on Kalshi.** It is the cheapest remaining
measurement in this kit and it bears directly on whether the Kalshi edge is a maker edge at
all. Corrected at all three sites: NOTES, `market_making.py`, `mm_replay_snapshots.py`.

### A fabricated number was live in a shipped docstring

`mm_replay_snapshots.py` asserted "Polymarket's OWN measured quotable universe is 136 markets
across 57 events at spread >= 0.10 (78 across 34 events at >= 0.25)". Those are the same
compaction-fabricated figures T13 caught in my briefing — and they were not only in my prose,
they were in code, labelled as measured. Corrected to the report's **130 / 53** and **75 / 32**
with the report section and measurement timestamp cited. Lesson: when a fabricated number is
found in one place, grep the tree for it; it propagates.

### Two design findings worth keeping

- **The venue minimum triggers a fee cliff.** `KalshiFeeModel` rounds the maker fee up to a
  whole cent, so at `quote_size=1` it is a flat $0.01/fill at any price versus $0.002-$0.005
  per contract at size 10 — **21-33% of the entire +$0.024131 predicted per-contract edge**.
  The replay's `pnl` already subtracts `fill.fee`, so the prediction embeds the size-10 fee and
  does NOT transfer to size 1. The pass rule's right-hand side must be re-derived at
  `--quote-size 1 --max-inventory 5` (read-only, existing cache). The fill-RATE prediction is
  size-invariant and does transfer, but only with `max_inventory` rescaled 50->5 to preserve
  the ratio the policy actually uses.
- **`OrderRouter._risk_context` cannot cap a market maker.** It sums `remaining_size * price`
  with no side awareness, so a resting SELL at 0.10 counts $0.10 against the cap while locking
  $0.90. `MAX_OPEN_NOTIONAL_USD` systematically undercounts exactly the side a two-sided quoter
  is short on every quote. Latent today (nothing places orders); load-bearing the moment
  anything does.

Also: `--markout-only` is attributed in places to "T21", which does not exist in this kit's
TASKS.md — the file was created by T11. Provenance label only; the code is as described.

defect: T14 kind=false-independence-claim
outcome: T14 model=opus attempts=1 result=pass review=none run=2026-09-12-3e71
agent: T14 id=abb0ea98e1573f15e role=implementer model=opus findings=5 confirmed=5 result=accepted

## Phase 3/4 review — REJECT, and it was right

Final reviewer verdict on Phases 3 and 4 as a unit: **REJECT**. The substance held —
the reviewer reproduced the entire T20 headline bit-for-bit through the committed harness
(n_trading 919, ci [0.03651241534988742, 0.8615392781316351], window 17.18109953703704d,
held 805, markout 0.6280957562568009) and independently confirmed `event_overlap=0` by raw
`json.load` of both caches. Nothing was fabricated. The rejection is about the correction
chain and one post-hoc criterion, both of which reached the verdict line.

### The four findings I verified myself and fixed

1. **A FOURTH instance of the kit's signature error, in shipped code.** `market_making.py`'s
   holdout table gave the old defaults' mean as `-0.5507` — the OVERALL sample's value, pool
   1,546 — in a row whose other three cells are test values. Test is **-0.5170**, pool 1,448.
   Verified against the JSON. It was written in the SAME editing pass that corrected the
   sibling power-table instance eight lines above. Fixed.
2. **A post-hoc criterion in the verdict line.** `TASKS.md` T20 pre-committed FOUR criteria;
   `NOTES.md` says "Four criteria were fixed in advance... THREE of four met".
   `go-no-go.md` listed FIVE, adding "the CI clears zero" as condition 1 — while citing the
   passage that says four — and carried "criterion 4 of 5 FAILED" into the VERDICT line. The
   added condition is the one that PASSED, and it is an outcome rather than a precondition.
   3-of-4 became 4-of-5 in the most-read line in the kit. Fixed at all three sites.
3. **The same error class misfiled as bootstrap noise, inside the newest report.**
   `go-no-go.md` reported the +$89.21/0.986/0.974 figures as a NOTES-vs-JSON conflict and
   concluded "most likely bootstrap re-runs". They are exactly the OVERALL power table.
   Calling it reproducibility noise disarms the next reader. Fixed, and the citation
   corrected (`:1388` is the residue, `:1393` holds the correct test values).
4. **$959 attributed to the wrong gate.** go-no-go said N>=1 ties "$959 of collateral"; N>=1
   is **$6,941.21** and $958.75 is N>=20's — wrong by 7x about the gate it names. Fixed.

Also fixed: both surviving "settlement-independent `markout_pnl`" claims in go-no-go (the
correction had been made in three other files but never here), and the density-gate claim
that removing "either one" of the two ITF series breaks the N>=20 interval — removing
`KXITFWMATCH` does ([-0.8469, +2.7565]), removing `KXITFMATCH` does NOT
([+0.1742, +3.9633], lower bound above zero). The JSON's own `strict_gate_fragility` rows
contradict its own headline.

### The finding that most changes what the headline is worth

The reviewer measured the BOOTSTRAP's own Monte-Carlo error, which no report had stated. I
reproduced it independently on the same 919 rows through the committed harness:

    replicates   mean lower bound     sd      min       max      seeds with low <= 0
    500 (shipped)     +0.0359       0.0221   -0.0150   +0.0885        2 of 40
    5,000             +0.0414       0.0085   +0.0227   +0.0553        0 of 40
    50,000            +0.0396       0.0028   +0.0363   +0.0428        0 of 5

The shipped seed reproduces `[0.03651241534988742, 0.8615392781316351]` exactly. But at
`BOOTSTRAP_REPLICATES = 500`, **"the CI clears zero" flips on ~5% of bootstrap seeds.** The
headline is quoted to four decimals with an MC sd of 0.022 — a precision two orders of
magnitude finer than the statistic supports. Raising replicates converges the bound upward
to roughly +0.040 and away from zero, so the substantive answer HOLDS and +0.0365 is an
unlucky draw on the right side of it. Recorded in `market_making.py` too.

This also retires §8's "reproducibility, measured rather than asserted" claim: `_block`
calls `random.seed(seed)` immediately before each `cluster_bootstrap`, so four identical
runs of a seeded pipeline could not have come out otherwise. That was a determinism check
wearing a reproducibility label — an eighth "check that cannot fail".

### Accepted and NOT fixed, recorded instead

- **The multiplicity count of 33 is an UNDERCOUNT** (~45 raw / ~28 distinct): 12
  leave-one-out and cumulative-removal CIs against this split where the JSON allots 8, 8
  optimistic-fill gated CIs enumerated nowhere, and 5 counted "evaluations" that admit zero
  markets and produce no interval. It errs AGAINST the author's own interest — it understates
  the warning — so the conclusion it supports only gets stronger. Left as-is with this note
  rather than rewritten, because re-deriving it needs the generator that does not exist.
- **"P&L-blind" is blind to DOLLARS only.** T22's primary density window is the scored split,
  which the JSON itself concedes is "CONTEMPORANEOUS, not causal". Density counts TRADING
  markets — an output of the simulation being scored. The causal variant is empty (0 markets
  at N>=20), so the two-ITF-series finding exists only under test-window density. The JSON
  says this; go-no-go's unqualified "P&L-blind" does not.
- **T22 is unreproducible.** Its generator script was never committed — no
  `choosing_N_on_train` code exists anywhere in the repo — so a 145 KB artifact carrying ~45
  bootstrap CIs cannot be re-derived or audited. T20 has the same gap but discloses it;
  T22 does not. T22's verify command (`assert 'provenance' in d`) asserts a file the task
  wrote contains a key the task wrote, and touches none of its acceptance criteria: a ninth
  check that cannot fail.

reviewer: P3-P4 model=opus findings=19 confirmed=19 result=accepted
defect: T22 kind=tautological-verify
defect: T13 kind=post-hoc-criterion

## 2026-09-12/13 — Forward collection is running

The user authorised the remedy for Polymarket's UNDERPOWERED verdict: apply the migrations and
start the collector. Done in this order, deliberately — criteria first, fence lift on record,
apply, hand-run one pass, daemonize — so that no bar was written after a number existed.

### 1. Criteria pre-committed before any data (TASKS.md Phase 3b)

T23 (Polymarket markout verdict: seven criteria, all required, interpretation fixed), T24 (the
entry rate that replaces feasibility §5.2's Scenario A/B assumption), T25 (the Dec-31 batch
settlement — a sanity check, labelled so on its first line), T26 (the loop). Two criteria come
straight from this session's review: bootstrap at >= 5,000 replicates, and the lower bound must
survive 20 seeds. The 500-replicate default gave the Kalshi bound an MC sd of 0.022.

### 2. GUARDRAILS §1.2 lifted by the user — scope recorded in the file

For exactly one target: the local `polymarket-postgres` container on a fresh volume. Lifted AFTER
the rule had been satisfied in full (SQL rendered, reviewed additive, models tested on SQLite),
not instead of it. The orchestrator applied the migrations under that authorisation; no task did.

### 3. Applied: `alembic upgrade head`, 001 -> 009, exit 0

A fresh volume has no schema, so all NINE revisions ran, not just 008/009. Full base->head render
saved to `reports/applied-migrations-001-009.sql` (89 DDL statements). The DROPs in it are 002
removing tables 001 creates (trader mimicry), and 007's DELETE/UPDATE ran against an empty
`book_snapshots` — nothing destructive to any data, because there was no data. `alembic current`
= `009 (head)`. 16 tables present; all seven 008 columns present on `book_snapshots`.

### 4. Preflight, live: database PASS, both venues PASS, broker FAIL (unused)

`preflight --check-collection`: schema at head; Kalshi 5 quotable sampled live, fee parsed,
two-sided book returned, 80,010/124,159 listed parse a quotable spread (Kalshi's listing is
124,159 today vs 96,072 on 09-07); Polymarket 5 sampled, 1,642/1,919 two-sided. The one FAIL is
`CELERY_BROKER_URL` unreachable — Redis is not running, and the loop does not use it.
`run_collect_books()`'s internal preflight checks only DB head and live field names, so the
broker FAIL cannot block a tick.

### 5. One pass by hand (`collection_loop --once`): both venues wrote

    collect  ok  elapsed 239.0s  written polymarket=256 kalshi=1000
    health   ok  exit 0

1,763 HTTP requests (1,143 Kalshi, 387 CLOB, 233 Gamma). Rows verified in the database:

    venue       outcome  rows  markets  volume  taker_fee  rebate  rebate>0  fee_source
    kalshi      YES/NO   500/500  500    all     all        all       0       settings
    polymarket  YES/NO   128/128  128    all     all        all     127       venue_schedule

Kalshi = 500 markets x 2 outcomes = `book_collection_top_n`; Polymarket = 128 x 2, matching the
feasibility grid's 130 at the production floor to within same-day drift. **127 of 128 Polymarket
markets publish a rebate > 0 (99.2%)** — the 96-99% figure, now in the database rather than a
probe. `selection_membership`: 500 + 128 selected.

**The pass took 239s against a 180s configured interval.** The 110-125s that derived 180s is
stale — Kalshi's listing grew ~30%. Not a problem for correctness: the loop's interval is a GAP
between passes, so ticks never overlap; a start-to-start schedule at 180s would have run at 100%
duty, ~7 req/s sustained against Kalshi's ~10/s measured ceiling. It IS a measurement fact every
report on this data must carry: the real cadence is ~7 minutes, not 3, and the markout mark at
`mid(i+2)` is therefore ~14 minutes after the fill, not 6. Recorded in the module docstring.

Two Polymarket warnings on every pass, pre-existing and structural: `gamma_market_page_cap_reached`
at 2,100 (the venue's offset ceiling, feasibility §1) and 181 listed markets skipped for a missing
`endDate`. Kalshi logs `kalshi_unknown_market_status: inactive, assumed open` for a handful of
markets — adapter behaviour that predates this kit.

### 6. Daemonized

`nohup python3 -m app.scripts.collection_loop`, detached, PID in `backend/.cache/collection-loop.pid`,
JSON-lines log at `backend/.cache/collection-loop.log` (both gitignored). Start line logged at
2026-09-13T01:43:50Z, `mode=paper`, `collect_interval_s=180`, `health_interval_s=1800`. httpx
INFO is silenced in the loop (measured ~1,700 lines per pass); venue WARNING/ERROR still logs.

**Stop it with:** `kill $(cat backend/.cache/collection-loop.pid)` — SIGTERM sets a flag the
loop honours within a second and it exits after the current tick, logging a `stop` line.
**Check it with:** `grep '"tick"' backend/.cache/collection-loop.log | tail` or
`python3 -m app.scripts.collection_health --hours 1` from `backend/`.

What is NOT exercised: the Celery task wrappers and the Redis broker. The loop calls the same
two coroutines through the same `run_async_task`; the wrapper is one line, and it is untested in
production. Said here so nobody reads "collection is running" as "the beat is running".

outcome: T26 model=opus attempts=1 result=pass review=none run=2026-09-12-3e71

### 7. First daemon tick, measured

    2026-09-13T01:47:44Z  collect  ok  elapsed 234.0s  written polymarket=144 kalshi=1000

Second pass in ~4 minutes; 234s against the hand-run's 239s, so ~235s is the steady-state pass
time on today's listings and the real cadence is ~415s (pass + 180s gap). Polymarket wrote 144
rows against the hand-run's 256: an unchanged book carries the same venue `ts` and upserts into
its existing row (`observed_at` refreshed, no new row — T15's design), and feasibility §4 measured
48.7-68.0% of Polymarket books changing inside a 15-minute window. 144/256 = 56% is inside that
range. Consistent with the measurement, not a defect; T24 will measure it properly over weeks.
Kalshi wrote 1,000 again — every selected book was new at its poll `ts`.

### 8. Handed off to launchd — survives logout, sleep and reboot

The nohup process would have died with the session. Installed
`~/Library/LaunchAgents/com.polymarket-trader.collection-loop.plist` (outside the repo; its
content is reproduced below so it can be rebuilt): `/opt/anaconda3/bin/python3 -m
app.scripts.collection_loop`, `WorkingDirectory` = `backend/`, stdout+stderr appended to
`backend/.cache/collection-loop.log`, `RunAtLoad` + `KeepAlive` true, `ThrottleInterval` 60s,
minimal explicit PATH (launchd sources no shell profile). `.env` is found by `Settings` via
`_REPO_ROOT`, not the cwd, so the working directory only matters for the module path.

Handoff sequence, measured: SIGTERM to the nohup PID 33372 -> it finished its in-flight tick and
logged `{"tick": "stop", "collect_ticks": 5, "signalled": true}` at 02:17:08Z -> `launchctl load`
-> launchd PID 56443 logged `start` at 02:17:20Z. The `ps` check fired inside the old process's
12-second teardown and reported it alive; re-checked after: gone, exactly one loop running. The
log was backed up to `.cache/collection-loop.nohup.log` before the swap in case launchd
truncated it; it appended (1,444 lines, both `start` lines present), so the backup is redundant
and harmless.

**Stop for real:** `launchctl unload ~/Library/LaunchAgents/com.polymarket-trader.collection-loop.plist`.
A plain `kill` is honoured (clean stop line) and then undone by `KeepAlive`, which restarts it —
that is the point of the agent, and it is the trap for anyone who expects `kill` to be final.
**Status:** `launchctl list | grep polymarket-trader` (PID, last exit code).

Plist, verbatim minus comments:

    Label                com.polymarket-trader.collection-loop
    ProgramArguments     /opt/anaconda3/bin/python3 -m app.scripts.collection_loop
    WorkingDirectory     /Users/michaelcave/Developer/reposV2/polymarket-trader/backend
    EnvironmentVariables PATH=/opt/anaconda3/bin:/usr/local/bin:/usr/bin:/bin  PYTHONUNBUFFERED=1
    StandardOutPath      .../backend/.cache/collection-loop.log   (StandardErrorPath the same)
    RunAtLoad true   KeepAlive true   ThrottleInterval 60   ProcessType Background

## T27 — Markout-only on Kalshi: the maker mechanism is real, and settlement is where it went

The Phase 3/4 review established that `markout_pnl` in settled mode is not settlement-free, and I
called running the settlement-free version on Kalshi "the cheapest remaining measurement" because
Kalshi is the venue where cash is already known. Ran it: `app/scripts/mm_markout_validation.py`,
committed (unlike the T20/T22 generators), interpretation fixed in its docstring BEFORE the first
run, at T23's own bootstrap standard (5,000 replicates, 20 seeds). T20's exact test split.

    pessimistic, 919 markets / 789 events / 2,392 fills / 805 held into settlement

    statistic         terminal   mean/mkt   per contract   CI95 (seed 0)         min low   seeds>0
    cash              settled    +0.4360    +0.01675       [+0.0464, +0.8295]    +0.0277   20/20
    markout_settled   settled    +0.6281    +0.02413       [+0.2809, +0.9953]    +0.2666   20/20
    markout_only      excluded   +2.3671    +0.09094       [+2.1087, +2.6427]    +2.0933   20/20
    settlement term              -1.7390                   total -$1,598.10, nonzero on 805 rows

    optimistic agrees: markout_only +2.4964 [+2.2381, +2.7779] 20/20; settlement term -1.7883.

**Branch A fired, all three conditions met.** Concentration is the opposite of the cash picture:
top event `KXNCAAFFIRSTTDTEAM-26SEP03COLOGT` is **2.22%** of markout_only across 789 events, and
the CI without it is [+2.07, +2.57]. Recall five MARKETS carried 39.95% of CASH P&L. So the
fragility the reviewer and I found in the cash interval was the settlement term's fragility — a
handful of positions that happened to settle well — not the spread capture's, which is spread
almost uniformly across events.

**Independent agreement.** +0.0909 per contract sits between the two numbers the original
adverse-selection study (537 markets, hourly, in `market_making.py`'s header) measured for the
>= 0.25 bucket: +0.1152 front-of-queue, +0.0735 behind. Different sample, different resolution,
different code path, same answer to within the queue-position uncertainty.

**Cash at 5,000 replicates: [+0.0464, +0.8295], 20/20 seeds.** The review predicted the bound would
converge upward from +0.0365 and it did. This does NOT reopen the Kalshi NO-GO — n_trading is
still 919 and the multiplicity is still 33 looks — but the seed-fragility finding is now closed at
the standard T23 uses.

### The finding that matters most, and it is post-hoc

**Two-interval markout overstates the money 5.4x** (2.37 vs 0.44). The quoting earns the spread at
`mid(i+2)`; then adverse selection keeps playing out — takers are net buyers of YES (the header's
own measurement), and the YES they bought settles higher than the quoter's last mid more often
than not. The settlement term consumed ~73% of the captured spread. Three consequences:

1. **For T23:** a Polymarket markout pass means spread capture EXISTS there, never that money is
   made. Written into T23 as a labelled post-hoc note; the seven criteria are unchanged.
2. **For the taper question (T6):** the settlement drag is real and large, yet T4/T6 found
   `taper_hours=0` optimal on ROC. Not a contradiction — the taper stops QUOTING, it does not exit
   inventory, and exiting means crossing a >= 0.25 spread plus the 7% taker fee, which costs more
   than -$1.74/market. The lead this opens: a policy that reduces terminal inventory WITHOUT
   crossing (lean the quotes harder as close approaches, so the crowd takes you out on your side)
   — that is what `skew_strength` does, and T5 measured skew 1.0 WORSE than 0.0. Unresolved, noted.
3. **For the ratio itself:** one venue, 1-minute resolution, 919 markets. Polymarket's ~7-minute
   cadence puts `mid(i+2)` ~14 minutes out, so its markout already contains more of the drift.
   Not transferable as a number; transferable as a direction.


outcome: T27 model=opus attempts=1 result=pass review=none run=2026-09-13-4c1d
outcome: T26 model=opus attempts=1 result=pass review=none run=2026-09-13-4c1d

## 2026-09-13 — Dependency, safety, secrets and hardening pass

Every secret scan below printed counts and filenames only, never matched content.

### Secrets: clean

- Tracked tree at HEAD: 0 hits for private-key blocks, 64-hex keys, AWS/GitHub/OpenAI token
  shapes. 20 hits for `KALSHI_API_KEY_ID=`/`POLYMARKET_*=` with a value — all in tests, CLAUDE.md
  and the market-edge NOTES; inspected by SHAPE (alphanumerics masked, length shown): every one is
  a short snake_case fixture (11-19 chars with underscores). A real Kalshi key id is a 36-char UUID;
  a real Polymarket key is 66 chars of hex. None present.
- `.env` never entered git history (`git log --all --diff-filter=A -- .env` empty); mode 600;
  ignored, as are `backend/.env`, `.cache`, `*.pem`.
- `.env.example`: placeholders only, except the compose-default Postgres credentials and a
  localhost URL — expected for local dev, and see hardening below.
- Daemon logs (4,334 lines, both files): 0 hits for `Authorization`, `KALSHI-ACCESS-*`,
  `PRIVATE KEY`, 64-hex, `POLY_*`, passphrase, api_key, or any 40+ char base64 run.
- launchd plist: `EnvironmentVariables` holds only `PATH` and `PYTHONUNBUFFERED`.

### Safety: clean

- `tests/test_fences.py` 31 passed. `Settings(trading_mode="live").trading_mode -> 'paper'`
  without `LIVE_TRADING_CONFIRMATION`. The daemon's own two `start` lines both say `mode=paper`.
- No `privileged`, no docker socket mount, no `network_mode: host` in compose.
- CORS default is localhost origins only. **The API has no authentication** (0 auth-guarded route
  dependencies) — acceptable only because it is now loopback-bound (below); recorded as the first
  thing to add before any non-local deployment.

### Hardening: one real finding, fixed

**Postgres was published on `0.0.0.0:5432` with the compose-default password, and the macOS
firewall is disabled.** A writable database — the one T23's verdict will be computed from —
reachable from anything on the same network with a known credential. Not a data-secrecy problem
(it holds public market data); a data-INTEGRITY problem.

Fixed in `docker-compose.yml`: every published port (`5432`, `6379`, `8000`, `5173`) now binds
`127.0.0.1:` explicitly, with a comment saying why. Recreated the Postgres container; the data
volume persisted (12,976 rows, revision 009, both checked after); `lsof` now shows
`127.0.0.1:5432` only; the app reaches it via `localhost` (`select 1 -> 1`).

**The recreate cost one Kalshi pass, and that was me.** The container restart landed inside
Kalshi's write phase on the 03:07-03:12Z tick: `InterfaceError` (asyncpg connection closed) at
03:12:46Z, the venue's uncommitted rows rolled back, Polymarket's 178 rows (already committed)
untouched, and the next tick wrote both venues in full (178 / 1,000). The database shows the
10-minute bucket at 03:10Z empty for Kalshi and 1,000 again at 03:20Z. The per-venue isolation
T16 built behaved exactly as tested, under a fault it was not warned about. Net: a ~7-minute gap
on one venue, inside T23 criterion 1's 10% allowance by a wide margin.

Recommended, NOT done (system-level or credential changes are the user's):
- Enable the macOS application firewall (`socketfilterfw --setglobalstate on`). With every port
  now on loopback this is defence in depth, not the fix.
- Change `POSTGRES_PASSWORD` from the compose default. Requires editing `.env` and
  `DATABASE_URL`, which this kit's fences keep out of a task's hands.
- 62% of the daemon log is `kalshi_unknown_market_status: inactive, assumed open` (2,687 lines).
  Pre-existing adapter behaviour; a once-per-pass dedupe in the adapter would cut the log by
  half. Hygiene, not safety.

### Dependencies

- Frontend: this branch was cut from `main` and so still carried the vulnerable lockfile — the
  axios fix lived only on `fix/frontend-dependency-advisories`. `npm audit` here read 20 (14 high)
  again. Cherry-picked `41da3ed` (verified by `git diff-tree` to touch `package-lock.json` alone)
  -> `npm audit` 0, axios 1.20.0. A branch merged alone must not ship what another branch fixed.
- Backend: `pip-audit` was not installed. The first attempt did not run at all — zsh does not
  word-split an unquoted variable, so `$RUN -r ...` became one command named
  `"python3 -m pip_audit"`; caught because the "exit code" line printed empty. Installed and
  re-run explicitly; result recorded below when it lands. `pip list --outdated` = 367 is the
  whole Anaconda base environment, not this project, and is not a signal.
- Backend result: `pip-audit -r requirements.txt` -> **No known vulnerabilities found**, exit 0.
