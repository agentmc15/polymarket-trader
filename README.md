# Prediction Market Edge Engine

A multi-venue inefficiency detection, backtesting, and execution engine for prediction markets.
It scans **Polymarket** and **Kalshi** for pricing dislocations, ranks them by a
time-to-resolution-aware score, backtests them against recorded depth with honest labeling, and
executes through a single order path that is shared by paper and live trading.

**Status: paper-first research tool.** Live trading is fenced behind two environment variables and a
kill switch, and is off by default. Read [Money safety](#money-safety) before changing that.

---

## Table of contents

- [What it does](#what-it-does)
- [Quick start](#quick-start)
- [Money safety](#money-safety)
- [Architecture](#architecture)
- [Venue economics you should know](#venue-economics-you-should-know)
- [Backtesting: what is trustworthy and what is not](#backtesting-what-is-trustworthy-and-what-is-not)
- [Known limitations](#known-limitations)
- [Development](#development)
- [Project layout](#project-layout)
- [Documentation map](#documentation-map)
- [Disclaimer](#disclaimer)

---

## What it does

### Inefficiency detection

Four strategies, all fee-aware and all sourcing rates from `app/venues/fees.py` rather than literals:

| Strategy | What it looks for | Venue scope |
|---|---|---|
| `binary_complement_arbitrage` | YES + NO priced below \$1.00 on the same market | single venue |
| `cross_venue_arbitrage` | The same event priced differently on Polymarket vs Kalshi | cross venue |
| `multi_outcome_bundle_arbitrage` | All outcomes of an N-way market summing below \$1.00 | single venue |
| `settlement_edge` | Near-certain outcomes trading below \$1.00 with a short lockup | single venue |

Trader-mimicry strategies were **deliberately removed** — copying other accounts is not sustainable
with the data available, and the surface (whale tracking, copy trading, trader models and routes) was
deleted outright rather than left dormant.

### Opportunity ranking

Every opportunity carries a seven-component score:

```
composite = annualized_return × fill_confidence × (1 − resolution_risk)
```

with `net_edge`, `hours_to_resolution`, `capital_lockup_usd`, `link_status` and `depth_source`
alongside. **Time-to-resolution is structurally unskippable** — `scoring.py` raises
`UnscorableIntent` when a market has no resolution timestamp, so there is no code path that produces
a score without it.

### Backtesting

Replays market snapshots through the **same fill engine the paper trader uses**, walking real order
book depth level by level, applying per-venue fee models per fill, and settling positions at
resolution. Produces a **capital sweep**: the same strategy run at multiple capital levels so you can
see where an edge dies under size.

### Execution

One `OrderRouter` drives both paper and live. Multi-leg intents, per-venue capital ledgers,
crash-safe pending rows, an unwind path that records its realized loss, and a structural fence that
makes it impossible for order placement to live outside three named modules.

---

## Quick start

### Install

```bash
cd backend && pip install -r requirements.txt
cd ../frontend && npm ci
```

Python 3.12. No virtualenv is assumed.

### Run the tests

```bash
cd backend && python3 -m pytest -q
```

**738 tests**, SQLite in-memory via `aiosqlite`, **no network access** — every venue interaction in
the suite goes through recorded fixtures or `httpx.MockTransport`.

### Run a capital sweep with no database

```bash
cd backend
python3 -m app.scripts.sweep --synthetic --levels 500,5000,50000 --out sweep.json
```

Output looks like this — note that every row is labeled with the depth it was computed on:

```
     capital | net_return | annualized | trades | downsized |   util | depth_source | fill_at
         500 |      4.53% |     71.49% |     12 |     16.7% |  96.1% |    synthetic |    next
       5,000 |      4.18% |     64.67% |    104 |      0.0% |  91.0% |    synthetic |    next
      50,000 |      2.45% |     34.20% |    582 |      0.0% |  32.1% |    synthetic |    next

NOTE: No tested level pushed the annualized return below min_viable_annualized (5%); the top
level tested was $50,000. The sweep ceiling is NOT proof the edge survives above that size.
```

That closing caveat is printed, not buried in a field. An edge that lives at \$500 and dies at \$50k
is a different product, and the sweep exists to make that visible.

### Run the stack

```bash
docker compose up
```

Postgres + TimescaleDB, Redis, the FastAPI backend, a Celery worker and beat, and the Vite frontend.

---

## Money safety

These are not style preferences. Two of them were violated by this repo's own configuration and had
to be corrected.

### Run exactly ONE order-routing process

The near-resolution bucket cap and the position ledger are serialized by an `asyncio.Lock` scoped to
**one event loop in one process** (`app/execution/router.py`). A second API worker or Celery worker
sharing the database can breach the cap *and* lose filled positions to a concurrent-update overwrite.

This is measured, not hypothetical: a reproduction had the venue fill **1,802 contracts while the
ledger recorded 902**. The portable fix — optimistic concurrency with a `version` column on
`positions` — **is not implemented**. `docker/backend/Dockerfile` pins `--workers 1` for this reason;
do not raise it.

### Live trading requires two variables and the absence of a file

```bash
TRADING_MODE=live                                    # default: paper
LIVE_TRADING_CONFIRMATION=I_UNDERSTAND_REAL_MONEY    # default: empty
```

Neither alone is sufficient. Additionally the kill-switch file must not exist — its path comes from
`KILL_SWITCH_PATH` (default `TRADING_KILL_SWITCH`). Creating that file refuses all order placement
until it is deleted.

> The variable is `KILL_SWITCH_PATH`, **not** `TRADING_KILL_SWITCH_PATH`. Settings use
> `extra="ignore"`, so a misspelled name fails *silently* — an operator setting the wrong one during
> an incident would halt nothing.

### Order placement is structurally fenced

Exactly three modules may contain order-placement calls:

- `backend/app/venues/polymarket/live.py`
- `backend/app/venues/kalshi/live.py`
- `backend/app/services/polymarket/client.py`

`backend/tests/test_fences.py` enforces this with an **AST walk**, and — importantly — it carries a
**positive control** that injects a placement call into a copy of a real module and asserts the
walker catches it, plus a paired negative control. That is what makes "the fence passed" mean "it
looked and found nothing" rather than "it looked nowhere."

### Never run migrations against a live database from tooling

Verify offline only:

```bash
cd backend && alembic upgrade head --sql
```

---

## Architecture

### The venue seam

`app/venues/base.py` defines a `VenueAdapter` protocol; `app/venues/polymarket/` and
`app/venues/kalshi/` implement it. **Strategies are venue-agnostic** — they receive normalized types
and never branch on venue identity.

Normalization happens at the adapter boundary and nowhere else:

- Prices are probabilities in `[0, 1]`. Kalshi's integer cents and dollar-strings are converted once,
  at the adapter.
- Sizes are contracts; each pays \$1.00 at resolution.
- Fees and cash are USD floats.
- Datetimes are timezone-aware UTC, via `app/utils/time.py`.

`tests/venues/test_adapter_contract.py` runs the same contract suite against both adapters from
recorded fixtures, including a test asserting that the unimplemented Kalshi WebSocket raises
`NotImplementedError` rather than being faked.

### One fill engine, two consumers

`app/execution/fill_engine.py::SimulatedFillEngine` walks book depth level by level, honors tick size
and minimum order size, charges **one fee call per level** (which matters enormously on Kalshi — see
below), and returns partial fills with a typed decline reason.

Both the backtester and the paper adapter use it. A backtest and a paper trade of the same
opportunity price identically, because it is the same code.

### Capital is per venue

`app/execution/ledger.py::CapitalLedger` tracks reserve/release/settle/credit/debit **per venue**.
There is no `transfer()`. Summing available balances across venues to size an order is a defect —
funds cannot move between Polymarket and Kalshi inside a trade, and the sizing code consumes
`available_by_venue()` as a mapping and only ever takes a minimum.

### Event linking is human-gated

Cross-venue arbitrage requires knowing that two markets describe the same event. The matcher
(`app/services/matching/`) is deterministic — a vendored Porter stemmer, negation and comparison
tokens preserved as content — and it **only ever writes `status="proposed"`**. A human approves via
the `/links` API, which surfaces both venues' resolution text side by side. `LinkBook` raises on any
non-approved link, and the scanner filters to approved before building strategies.

There is deliberately **no LLM in the matching loop**.

---

## Venue economics you should know

These were measured against the repo's own fee models, not assumed.

### Kalshi charges its fee ceiling per FILL; Polymarket does not

A 100-contract order at p=0.98:

| Fill shape | Kalshi total fee | Polymarket total fee |
|---|---:|---:|
| One block of 100 | **\$0.14** | \$0.098 |
| 100 fills of 1 | **\$1.00** | \$0.098 |

Polymarket's fee is `size × rate × p × (1−p)` — linear in size and **flat in fill count**. Kalshi
applies a whole-cent ceiling **once per fill**, so fragmenting a 100-lot across 100 thin levels
consumes half the gross edge in fees alone.

**The practical consequence: on Kalshi, *how* an order fills matters as much as the price it fills
at.** A depth-walking engine that fragments across thin levels is quietly expensive there and never
on Polymarket. This is why recorded depth changes what Kalshi actually costs.

The often-quoted "~40× venue asymmetry" is **fragmentation-driven**, not a flat per-contract penalty:
it is ~40× at one contract and ~1.2× at 100 contracts in a single fill.

### Polymarket's fee collapses at the tails

`p × (1−p)` goes to zero as price approaches 0 or 1, which is exactly where near-resolution trades
live. At the default 0.05 category rate, 100 contracts cost \$1.25 at p=0.50 and \$0.098 at p=0.98.

### Settlement-edge returns look better than they are

A near-certain outcome at 0.98 with 30 hours to resolution annualizes to several hundred percent —
but the absolute profit is **under two cents per contract**, you are locking up 98¢ to earn it, and
you are short a small, rare, total loss if "determined" turns out wrong. High annualized return on a
short lockup is a *capital-efficiency* number, not a margin of safety.

---

## Backtesting: what is trustworthy and what is not

### Integrity properties that are enforced

- **No look-ahead.** Fills happen at the *next* snapshot by default (`fill_at="next"`). A recorded
  book is attached only if its timestamp is at or within the match window *before* the price row —
  a book one microsecond later is rejected.
- **Real depth when it exists.** `book_snapshots` stores recorded books; the replayer attaches them
  and the engine synthesizes only when none exists.
- **Settlement at resolution**, with redemption gas charged per position.
- **Every result is labeled.** `depth_source` (`recorded` / `synthetic` / `mixed`) and `fill_at`
  travel with every number, to the CLI, the JSON, and the UI.

### The labeling rule

> Any metric computed on synthetic depth is labeled as such **wherever it is shown**.

This is a standing project rule, not a footnote. A `synthetic` badge appears on the results view and
on every sweep row, as visible text rather than a tooltip. A result with no label renders no badge
rather than defaulting to `recorded` — an unlabeled synthetic run showing a confident "recorded"
badge would be worse than showing nothing.

### Distinguishing "no edge" from "not measurable"

A strategy that cannot obtain a book produces zero trades at every capital level — the identical
signature to a strategy with genuinely no edge. `CapitalRow.zero_trades_cause` separates them
(`"no_signal"` vs `"structural: …"`), and `EdgeDecayReport.unmeasurable_note` fires when
`edge_dies_at` is anchored to a structural row. **A structural row is not evidence about edge.**

---

## Known limitations

Stated plainly, because a limitation you cannot see is worse than one you can.

1. **Multi-outcome bundle strategies cannot be backtested.** `MarketSnapshot.book` holds a single
   order book and every leg of a bundle shares one market snapshot, so an N-outcome bundle can carry
   at most one book; the remaining legs get no depth and the all-or-none intent never executes. The
   **live scanning path handles arbitrary outcome labels correctly** — this is backtest-specific.
2. **A binary complement's NO leg fills against synthesized depth** even when a recorded NO book
   exists, for the same single-`book`-field reason. Runs are honestly labeled `mixed`, but "mixed"
   here means "every complement intent is half-recorded by construction."
3. **`PriceHistory` has no outcome column**, so the DB-backed replayer can only attach a `"YES"`
   book, and every recorded Kalshi book is currently dead data for DB-backed backtests.
4. **Cross-venue arbitrage produces nothing out of the box.** `event_links` starts empty and
   `propose_links` is reachable only via `POST /links/propose` — there is no scheduled job and no UI.
   Approval is correctly human-gated; there is simply nothing to approve until you propose links.
5. **Strategies do not price fees on a uniform basis.** Realized P&L is unaffected (the fill engine
   charges the true per-level fee), but the *emission gates* differ, so which opportunities exist at
   all is not calibrated identically across strategies.
6. **Frontend/backend contract drift predating this work** means the Results tab may not activate
   after a run (`POST /backtests` returns `id`; the client reads `backtest_id`).

See `HANDOFF.md` for the current remediation queue.

---

## Development

```bash
cd backend
python3 -m pytest -q                    # full suite
ruff check app/services/backtesting     # scope lint to what you changed
mypy app/services/backtesting
alembic heads                           # expect 007 (head)
alembic upgrade head --sql              # offline DDL, never against a live DB
```

```bash
cd frontend
npx tsc -p tsconfig.app.json --noEmit
npm run lint
```

**Scope lint gates to the files you touch.** The repo carries a legacy baseline of ~139 ruff
findings; new and changed modules must be clean, untouched legacy files are not a gate.

### Testing conventions

- Every money-math test states its expected number **by hand in a comment**. A test that computes its
  expectation with the code under test is not a test.
- Prove new tests **red-green**: revert the fix in a scratch copy, confirm the test fails, restore.
  This repo has shipped a test that passed vacuously by proving `0 == 0`, and an acceptance criterion
  satisfied by score keys being "present and non-null" while the value was structurally zero.
- Tests never touch the network, never place orders, and never set `TRADING_MODE=live`.

---

## Project layout

```
backend/app/
├── venues/              # VenueAdapter protocol, types, fee models, registry
│   ├── polymarket/      #   adapter.py, live.py (order placement allowed)
│   ├── kalshi/          #   adapter.py, live.py (order placement allowed)
│   └── paper.py         #   PaperVenueAdapter — simulated fills
├── execution/           # router.py, ledger.py, fences.py, fill_engine.py, reconcile.py
├── strategies/          # four inefficiency strategies + base types
├── services/
│   ├── matching/        # deterministic event matcher (normalize.py, matcher.py)
│   ├── backtesting/     # engine.py, data_replay.py, metrics.py, sweep.py
│   ├── scoring.py       # the seven-component opportunity score
│   ├── scanner.py       # scan() and near_resolution_pass()
│   └── data_collector.py
├── models/              # SQLAlchemy 2.0 async models
├── api/routes/          # FastAPI routes
└── tasks/               # Celery tasks and beat schedule

frontend/src/
├── components/opportunities/    # OpportunitiesTable
├── components/backtesting/      # EdgeDecayTable, BacktestResults, DepthBadges
├── hooks/                       # useOpportunities, useEdgeDecay, useTradingMode
└── services/api.ts
```

---

## Documentation map

| Document | What is in it |
|---|---|
| `CLAUDE.md` | Project structure, tech stack, **money invariants**, environment variables |
| `HANDOFF.md` | Current state, in-flight work, remediation queue, process lessons |
| `.claude/kits/market-edge/PLAN.md` | Architecture decisions D1–D13 with rationale, pinned venue API facts |
| `.claude/kits/market-edge/GUARDRAILS.md` | Absolute money rules (§1), conventions, testing rules |
| `.claude/kits/market-edge/NOTES.md` | Full execution ledger — every finding, defect and adjudication |
| `.claude/skills/kalshi-api/SKILL.md` | Kalshi auth, payloads, order book encoding, fees |
| `.claude/skills/polymarket-api/SKILL.md` | Polymarket endpoints, fee formula, category rate table |

---

## Disclaimer

This is a research tool, not investment advice.

Backtest results assume execution at recorded or synthesized prices and will not match live results.
Synthetic depth is invented depth — a number computed on it is a hypothesis, not a measurement. Paper
trade first, and understand the regulatory position of prediction markets in your jurisdiction before
deploying capital. The authors accept no responsibility for trading losses.
