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

## In flight when the session paused — CHECK THIS FIRST

Two implementer subagents were mid-write. If their work did not land, the tree may hold partial
edits; if it did, verify before building on it.

| Task | Scope | Files |
|---|---|---|
| **T25** | Wire `near_resolution_pass` into production; fix the `/opportunities` scan-id filter; fix reconciled fills erasing fence exposure | `app/tasks/__init__.py`, `app/tasks/scanner.py`, `app/services/scanner.py`, `app/api/routes/arbitrage.py`, `app/execution/reconcile.py` |
| **T28** | Fix `pct_intents_downsized` (contaminated by the capital cap; fails PLAN R4's tripwire on the shipped demo) | `app/services/backtesting/sweep.py`, `app/services/backtesting/engine.py`, `tests/backtesting/test_sweep.py` |

**To check:** `cd backend && python3 -m pytest -q` (expect ≥728, zero failures) and
`git log --oneline -3`. If either task's files are half-edited, the suite will say so.

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

## Remaining known defects (from the final review, not yet fixed)

Ranked. All are recorded in full detail in `.claude/kits/market-edge/NOTES.md` under
"Final review (Phase 4 + whole kit) — adjudication".

1. **`unmarked_positions` never reaches the API.** T21d added `BacktestResult.unmarked_positions` so
   a partly-fictional equity curve announces itself. `build_report()` in `app/tasks/backtesting.py`
   writes only `depth_source` and `fill_at`, so the field is computed, logged at WARNING, then
   dropped at the persistence boundary. **One line.** Batched, not yet dispatched.
2. **Fee-basis divergence across strategies.** Four strategies price fees on three different bases.
   Zero divergence on Polymarket (linear, no per-fill rounding); real on Kalshi only. The two
   size-1.0 strategies *overstate* the fee (safe — they reject genuine edges);
   `cross_venue_arbitrage.py` **understates** it by up to 0.53¢/contract, ~35% of its own
   `min_net_edge` gate of 0.015. **Fix `cross_venue_arbitrage` first** — it is the one erring toward
   admitting marginal trades *and* the one carrying real cross-venue settlement risk.
3. **`composite` ranks values the code says not to compare.** `multi_outcome_bundle_arbitrage`'s own
   comment says "Do not compare this value to `cross_venue_arbitrage`'s `net_edge` as if they
   measured the same kind of risk" — while `composite` is the sole consumer and does exactly that,
   in one sorted list.
4. **Cross-venue arbitrage produces nothing out of the box.** `event_links` is empty on a fresh
   install and stays empty: `propose_links` is reachable only via `POST /links/propose`, with no beat
   and no frontend. All five `/links` routes are uncalled by the UI. Approval is *correctly*
   human-gated (PLAN D9) — but there is nothing to approve.
5. **`POST /backtests/sweep` has no UI producer.** `BacktestForm` posts to `POST /backtests`, so the
   EdgeDecayTable built in T23 is reachable only through History.
6. **Pre-existing frontend/backend contract drift** (not introduced by this kit): `POST /backtests`
   returns `id` while `backtestApi.ts` reads `backtest_id`, so the Results tab never activates after
   a run; `metrics` vs `trade_metrics`/`risk_metrics`; `equity_curve`/`final_capital` vs
   `points`/`final_value`.
7. **Scan request volume.** ~800 sequential `get_book` calls per scan pass every 120s
   (`scan_top_n=200` × 2 outcomes × 2 venues). A rate-limit and latency risk, not a correctness one.

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
