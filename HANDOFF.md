# HANDOFF — market-edge kit execution

**Last updated:** 2026-09-06
**Session:** `cbc400f8-2c7e-492a-9c9a-2f3550d6aae5`
**Resume with:** `/polytropos:execute market-edge` — the kit's 24 planned tasks and every
remediation task are `done`. The queue is empty; what remains is listed under "What is actually
left" below, and none of it is blocking.

---

## State in one paragraph

The `market-edge` kit is fully executed: **24/24 planned tasks, plus 22 unplanned remediation tasks**
(T21b–T21f, T25–T43) driven by two phase reviews, three red-team passes, a verification pass, and a
deployment audit. The backend suite went **654 → 841 passing**, migrations **001 → 007**, the
frontend typechecks and lints clean, and everything is committed and pushed to `main`. Nothing is
in flight and nothing is blocked.

The routing scorecard reads **34/40 first-try, 80% cheap-model review survival** (run it yourself —
see "Where things live"). Note that number counts only tasks present in `TASKS.md`; outcomes for
tasks dispatched without a `TASKS.md` entry are silently dropped, which happened twice in this run.
**Write the entry at dispatch time, not at close.**

---

## The defect worth remembering, and why it was invisible

**The headline ask was dead code for most of this build.** `scanner.near_resolution_pass()` — the
"events culminating soon" bucket — had **no production caller**. It was invoked from exactly one
place in the repo: its own test file. No beat, no route, no frontend. And the general `scan()` path
could not substitute, for two independent reasons: `settlement_edge` sits in the `"edge"` strategy
category so `ARBITRAGE_STRATEGIES` never ran it, and even forced via `?strategies=settlement_edge`,
`scan()` called `score()` without `allow_past_close=True`, which raises for every past-close market —
by construction, *every* settlement-edge intent.

So the entire bucket-cap apparatus built to close a severe exploit was correct, well-tested, and
**inert**. Every test passed. The UI answered a `near_resolution` filter with a confident empty list.

**Fixed (T25) and verified**: a `scan_near_resolution` beat runs the pass on its own interval, and the
chain was traced by reading it rather than trusting a test — beat → `run_near_resolution_scan()` →
`near_resolution_pass()` → the strategy stamps `metadata["bucket"]` → persisted to
`IntentRecord.extra_data` → `check_order_limits` enforces the cap, exercised end to end through the
real `OrderRouter.submit()`.

**The generalizable lesson, which recurred three more times after this:** a green suite says a unit
works, never that anything calls it. Three of four discovery surfaces in this repo were built,
tested, reviewed — and unwired. When you add a capability here, the last question is *what production
path invokes this*, and the answer must be a code path you traced, not a test that constructs it
directly.

---

## What is actually left

**The remediation queue is empty.** Everything found by the reviews, the red-team passes and the
deployment audit is fixed, each with a regression test proven to fail against the pre-fix code.

### The one gap I could not close

**Nobody has run this system end to end.** All 841 tests exercise units against fixtures, and the
deployment audit was deliberately static because GUARDRAILS §1.4 bars the venue network. So these
remain genuinely unknown:

- whether the adapters parse real Polymarket and Kalshi payloads (they parse recorded fixtures)
- whether Postgres/TimescaleDB accepts migration `001`–`007` against a live database (offline SQL
  only; **never** run `alembic upgrade head` from a kit task)
- whether the three beats behave under real venue latency and rate limits
- whether `Settings`' default `postgresql+asyncpg://postgres:postgres@…` fails an auth handshake
  against compose's `polymarket:polymarket` Postgres on a local non-Docker run — a plausible
  first-run trap, unverified

**Start in paper mode**, watch the scanner logs for `scan_book_fetch_complete` (it reports
`books_failed` and per-venue error types), and expect the first real payload to disagree with a
fixture somewhere.

### Small, deliberate, and documented rather than fixed

1. **Two rejections committing *concurrently* last-write-wins on the merged notes text.** Sequential
   rejections — what a review queue actually produces — are safe. The alternatives put a failure mode
   on the fail-safe direction or move the merge into SQL.
2. **Rejection is terminal**: no route returns a link to `proposed`, so a mistyped `link_id` on reject
   permanently bans a pair, correctable only in the database. The 409 says so plainly now rather than
   suggesting a path that does not exist.
3. **Two admitted gaps in the AST fence** over the matching package: a fully dynamic
   `setattr(obj, name_from_config, value)` (undecidable syntactically) and a mapping built on an
   earlier line then passed by name. Both are stated in the fence's own docstring with a test pinning
   that the first is still missed.
4. **`mypy`**: one untyped-celery-decorator finding on the new task module, identical to what all
   four existing task modules report. Left unsilenced deliberately.

---

## Money-safety constraints that MUST survive any future change

These are load-bearing. Two were violated by the repo itself and had to be fixed during this run.

- **Run exactly ONE order-routing process.** The bucket cap and position ledger are serialized by an
  `asyncio.Lock` scoped to one event loop in one process (`app/execution/router.py`). A second API or
  Celery worker sharing the database can breach the cap and lose filled positions to a
  concurrent-update overwrite — reproduced: the venue filled **1802 contracts while the ledger
  recorded 902**. The portable fix (optimistic concurrency, a `version` column on `positions`) is
  **not implemented**. `docker/backend/Dockerfile` was shipping `--workers 4` against this rule and
  now pins `--workers 1`; `celery-worker` pins `--concurrency=1` for the same reason, since the
  prefork default is one process per CPU. No Celery task routes orders today, so that pin is defense
  in depth — but it is the guard that makes adding one safe.
- **Configuration must actually reach the process.** `docker-compose.yml` forwarded 12 of 56 settings
  and had no `env_file`, so `.env.example`'s own "copy this to `.env`" instruction was false for 44
  values — including both Kalshi credentials, which meant the dockerized deployment could not
  authenticate to Kalshi at all. Fixed with `env_file` (topology-derived values stay pinned as
  explicit `environment:` entries so a local `.env` cannot redirect the container at its own
  database). `backend/tests/test_env_example_coverage.py` now fails if a `Settings` alias goes
  undocumented, because this same drift had already been fixed once and silently reopened.
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
