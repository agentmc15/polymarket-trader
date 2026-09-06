# HANDOFF — market-edge kit execution

**Last updated:** 2026-09-05
**Session:** `cbc400f8-2c7e-492a-9c9a-2f3550d6aae5`
**Resume with:** `/polytropos:execute market-edge` (the kit's 24 planned tasks are all `done`; what
remains is post-review remediation, tracked below)

---

## State in one paragraph

The `market-edge` kit is fully executed: **24/24 planned tasks done, plus 7 unplanned remediation
tasks** (T21b–T21f, T26, T27) that came out of a Phase 3 review, a red-team pass, and a final
whole-kit review. The backend suite went **654 → 728 passing**, migrations **001 → 007**, and the
frontend typechecks and lints clean. Nothing is blocked. **Two remediation tasks were still running
when this session paused** — see "In flight" below, and check their state before doing anything else.

---

## What landed at the pause

Both remediation tasks in flight at the pause **completed their code changes and are committed**.
They were stopped mid-*verification* (both had reached their red-green proof step), so their fixes
are in the tree and green, but neither filed a final report.

| Task | What it did | Verify before building on it |
|---|---|---|
| **T25** | Added `scan_near_resolution` as a second Celery beat running `near_resolution_pass()` every `settings.near_resolution_scan_interval_s` (300s). Also fixed the `/opportunities` scan-id filter and reconciled fills erasing fence exposure. | `grep -n "scan_near_resolution" backend/app/tasks/scanner.py` |
| **T28** | Rewrote `test_sweep.py` to CHARACTERIZE the capital-cap confound rather than dodge it — three new tests drive the cap and the book independently. | `python3 -m app.scripts.sweep --synthetic` — check whether `pct_intents_downsized` is now > 0 at the top level (PLAN R4's tripwire) |

**Neither task's red-green proof was completed.** Re-run it before trusting the new tests:
revert each fix in a `$TMPDIR` copy, confirm the test fails, restore. This kit has already shipped a
test that passed vacuously.

State at the pause: **740 tests passing**, `alembic heads` = `007 (head)`, frontend `tsc` and
`npm run lint` both exit 0, working tree clean, all work merged to `main` and pushed.

---

## The one finding that matters most

**The user's headline ask was dead code, and T25 was dispatched to fix it.**

`scanner.near_resolution_pass()` — the "events culminating soon" bucket — had **no production
caller**. It was invoked from exactly one place in the repo: its own test file. No Celery beat entry,
no API route, no frontend caller. The general `scan()` path cannot substitute for two independent
reasons:

1. `settlement_edge` is in the `"edge"` strategy category, so `ARBITRAGE_STRATEGIES` (built from
   `STRATEGY_CATEGORIES["arbitrage"]`) never runs it.
2. Even forced via `?strategies=settlement_edge`, `scan()` calls `score()` **without**
   `allow_past_close=True`, so `scoring.py` raises `UnscorableIntent` for every past-close market —
   which is, by construction, *every* settlement-edge intent. 100% silently skipped.

Consequence: the entire bucket-cap apparatus (the `check_order_limits` bucket cap,
`warn_unknown_bucket`, `_bucket_open_notional`, and the `Position.extra_data["bucket_notional"]`
ledger built to close a severe exploit) is correct, well-tested, and **inert in production**. The UI
answers a `near_resolution` filter with a confident empty list.

**If T25 did not land, this is the first thing to finish.** Its brief is recorded in NOTES.md.

---

## Remaining known defects

The queue from the final review is **cleared**. What was item 1-7 is now:

| Was | Status |
|---|---|
| `unmarked_positions` never reaching the API | Fixed (T29) — persisted unconditionally, badge fires |
| Fee-basis divergence | Fixed (T29) — cross-venue prices per fill; worst case was 59% of its own edge gate |
| `composite` ranking incomparable values | Fixed (T31) — strategies publish pre-risk edge, scoring applies every haircut once |
| Cross-venue producing nothing out of the box | Fixed (T30) — hourly link-proposal beat, still human-gated |
| `POST /backtests/sweep` with no UI producer | Fixed (T32) |
| Frontend/backend contract drift | Fixed (T32) — 12 mismatches, incl. a silent slippage no-op |
| Stale `types/index.ts` comment | Fixed (T32) |

### Open, lower priority

1. **`extra="ignore"` hides client mistakes.** The slippage bug (client posted `slippage_bps`, backend
   wanted `slippage_value`, FastAPI silently discarded it, so every backtest used default slippage)
   and the earlier `TRADING_KILL_SWITCH_PATH` bug are the same shape. **Every request model with
   pydantic's default `extra="ignore"` is a place a client can be wrong without being told.** Worth a
   systematic sweep.
2. **`edge_basis` is persisted but not a first-class `OpportunityOut` column.** It reaches
   `/opportunities` inside the payload's `metadata`, not as a typed field.
3. **`favorite_compounder` / `no_bias_exploit` publish an `"edge"` that is a directional mispricing.**
   Unscored today, but it would mean the wrong thing under T31's new contract if either joins a
   scanner pass.
4. **Only 4 of 15 `TradeMetrics`/`RiskMetrics` fields are populated** by `get_backtest_status`. Typed
   but deliberately unrendered, so the UI never shows an unpopulated metric as a real `0.00`.
5. **Scan request volume** — ~800 sequential `get_book` calls per scan pass every 120s.
6. **`mypy`**: one untyped-celery-decorator finding on the new task module, identical to what all four
   existing task modules report. Left unsilenced deliberately.

---

## Money-safety constraints that MUST survive any future change

These are load-bearing. Two were violated by the repo itself and had to be fixed during this run.

- **Run exactly ONE order-routing process.** The bucket cap and position ledger are serialized by an
  `asyncio.Lock` scoped to one event loop in one process (`app/execution/router.py`). A second API or
  Celery worker sharing the database can breach the cap and lose filled positions to a
  concurrent-update overwrite — reproduced: the venue filled **1802 contracts while the ledger
  recorded 902**. The portable fix (optimistic concurrency, a `version` column on `positions`) is
  **not implemented**. `docker/backend/Dockerfile` was shipping `--workers 4` against this rule and
  now pins `--workers 1`.
- **`TRADING_MODE` defaults to `paper`** and must stay `paper` in every test process. Live requires
  `LIVE_TRADING_CONFIRMATION=I_UNDERSTAND_REAL_MONEY` *and* no kill-switch file.
- **The kill-switch variable is `KILL_SWITCH_PATH`**, not `TRADING_KILL_SWITCH_PATH`. `CLAUDE.md`
  documented the wrong name; with pydantic's `extra="ignore"` the wrong name fails **silently**, so
  an operator setting it during an incident would halt nothing. Fixed.
- **Only three modules may contain order-placement calls**: `app/venues/polymarket/live.py`,
  `app/venues/kalshi/live.py`, and `app/services/polymarket/client.py`.
  `backend/tests/test_fences.py` enforces this by AST walk, with a positive control (it injects a
  call into a copy of a real module and asserts detection) and a paired negative control — so
  "passes because it found nothing" is distinguishable from "passes because it looked nowhere".
- **No backtest number is quoted without its `depth_source` and `fill_at` labels** (GUARDRAILS §1.7).

---

## Economics established during this run (verified against the real fee models)

Useful context; do not re-derive.

- **Kalshi's fee ceiling is charged per FILL, not per order.** A 100-contract order at p=0.98 costs
  **$0.14 as one block but $1.00 across 100 single-contract fills** — half the gross edge consumed by
  the floor alone. Polymarket's fee is linear in size and **flat in fill count**
  (`fee(0.995, 100) == 100 × fee(0.995, 1)` exactly).
- Therefore **on Kalshi, how an order fills matters as much as the price it fills at.** A
  depth-walking engine that fragments across thin levels is quietly expensive there and never on
  Polymarket. This is why recorded depth (T21) changes what Kalshi actually charges.
- The venue asymmetry is **fragmentation-driven, not a flat per-contract penalty**. It is ~40× at 1
  contract and ~1.2× at 100 contracts in a single fill.
- Settlement-edge trades clear at realistic sizes but the absolute profit is **under two cents per
  contract**; the high annualized figures are a capital-efficiency number, not a margin of safety.

---

## How to verify the current state

```bash
cd backend && python3 -m pytest -q          # expect 728+ passed, 0 failed
cd backend && alembic heads                  # expect 007 (head)
cd frontend && npx tsc -p tsconfig.app.json --noEmit && npm run lint   # both exit 0
cd backend && python3 -m app.scripts.sweep --synthetic --levels 500,5000,50000 --out /tmp/s.json
```

**Never run `alembic upgrade head` against a real database** from a kit task — verify offline with
`alembic upgrade head --sql` only.

---

## Process lessons worth carrying to the next kit

Measured, not asserted. Full per-role table in NOTES.md under "Routing scorecard".

- **`red-team` was by far the highest-value role**: 5 dispatches, 32 findings, 28 confirmed, 88%
  precision, **425% marginal catch rate** — including both severe money bugs, found on code that had
  *already* passed its verifier and a phase reviewer. The **plain `verifier` was the weakest**: 67%
  precision, 50% marginal. Next kit: keep red-team and reviewer, spend the verifier's budget on a
  second red-team pass.
- **The architect's own recurring defect** (9 instances): task verify commands used *directory*-scoped
  `ruff check` and whole-project `tsc`/`npm run lint` gates, which sweep in untouched legacy files —
  contradicting PLAN §2's own "untouched legacy files are not a gate". Scope lint gates to the files
  a task writes, or drive the baseline to zero first.
- **Verify commands must be able to fail for the reason they appear to check.**
  `test -z "$(grep -n 'x' README.md)"` passes when `README.md` does not exist at all.
- **Prove every new test red-green** (revert the fix in a `$TMPDIR` copy, confirm the test fails,
  restore). This kit shipped one test that passed vacuously by proving `0 == 0`, and one acceptance
  criterion satisfied by score keys being "present and non-null" while the value was structurally
  zero.
- **A canonicalization added at one layer changes which inputs reach the layers below it.** T21d
  widened a gate to strip whitespace; the consumer one line down still compared unstripped and raised,
  aborting whole backtests. The fix relocated the failure instead of removing it until T21f caught it.
- **The ledger in NOTES.md is execute-owned.** Implementers wrote their own `agent:`/`outcome:` lines
  three times, producing malformed entries. Future briefs should say: prose only, never a line
  beginning `outcome:`, `agent:`, `reviewer:`, `defect:`, `reroute:` or `session:`.

---

## Where things live

| What | Where |
|---|---|
| Kit plan, decisions D1–D13, pinned venue API facts | `.claude/kits/market-edge/PLAN.md` |
| Task list + the 5 remediation tasks (Phase 3R) | `.claude/kits/market-edge/TASKS.md` |
| Absolute money rules, conventions | `.claude/kits/market-edge/GUARDRAILS.md` |
| **Full execution ledger, every finding and adjudication** | `.claude/kits/market-edge/NOTES.md` |
| Project structure, money invariants, env vars | `CLAUDE.md` |
| Routing scorecard | `cd ../polytropos && python3 bin/routing_scorecard.py market-edge --kits-dir <this repo>/.claude/kits` |
