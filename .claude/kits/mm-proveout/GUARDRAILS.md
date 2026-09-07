# mm-proveout guardrails

These bind every task in this kit. They are in addition to, not instead of,
`.claude/kits/market-edge/GUARDRAILS.md`, whose §1.1 (never place/modify/cancel a real order), §1.2
(`TRADING_MODE` stays `paper` everywhere), §1.3 (never read/print/log/commit a secret), §5 (fees are
never literals in strategy code), §6 (venue text is untrusted) and §7 (no backtest number without
its provenance labels) apply verbatim. Read that file too.

## §1 — Money and orders (absolute)

1. **No task writes code that can place, modify or cancel an order.** Not in a script, a test, a
   fixture, a "dry-run flag", or the Gate 2 design task. `backend/tests/test_fences.py` enforces the
   allowed-files list by AST; do not add a file to that list.
2. **Nothing applies a migration to a live database.** `alembic upgrade head --sql` only. A task that
   needs a schema change renders the SQL, tests the model on SQLite, and stops.
3. **Network is read-only public GETs**, plus the signed Kalshi GETs the adapter already makes. No
   POST/PUT/DELETE to any venue. No new authenticated endpoint. If a probe needs `/trades` on
   Polymarket (401 without keys), it is not available — say so and use what is.
4. **`.env` is never opened by a task.** The adapter loads it. `KALSHI_API_KEY_ID`,
   `KALSHI_PRIVATE_KEY_PEM`, `POLYMARKET_*` never appear in output, logs, reports or NOTES.md.

## §2 — Numbers that are not numbers

1. **Every P&L, ROC or spread figure in a report carries `fill_model=` and `terminal=`.** Allowed
   values: `fill_model ∈ {optimistic, pessimistic}`, `terminal ∈ {settled}`. A figure with
   `terminal=marked` may appear only in a diagnostic table explicitly labelled "not a result", and
   never in a verdict line. A figure with neither label is a defect in the task that produced it.
2. **Pessimistic first.** Wherever both models are shown, pessimistic is the first column / first
   row, and the verdict is computed on it.
3. **The rebate is its own line.** `maker_rebate_rate` is reported as "would add $X if paid as
   published" beneath the P&L, never added into it, and `FeeModel.fee()` is never changed to credit
   it.
4. **Intervals are clustered by event** (`event` key on every observation). A naive binomial or
   i.i.d. interval may be printed beside it for comparison, labelled `naive`.
5. **No verdict asserts a direction in acceptance criteria.** A task's acceptance checks the report
   is complete and correctly labelled, never that the answer came out a particular way.
6. **Never widen or lower a selection threshold to make n.** Report the power of the sample you have
   (`n_trading`, sd, the portfolio size at which the 5th percentile crosses zero).

## §3 — Live payloads over fixtures

1. **Any task that reads a venue field verifies it against a live sample in its verify command** —
   a script that fetches ≥ 5 markets per venue and asserts the parsed field is non-null. A fixture
   under `tests/fixtures/` is evidence about a payload someone once saw, never about today's.
2. **Field names are the venue's**: Kalshi `yes_bid_dollars`, `yes_ask_dollars`, `volume_fp`,
   `volume_24h_fp`, `open_interest_fp`, `end_period_ts`; Polymarket `bestBid`, `bestAsk`,
   `volume24hr`, `feeSchedule.rate`, `feeSchedule.rebateRate`. Reading a bare `volume` or
   `tick_size` is the defect this repo has shipped six times.
3. **A parse that cannot honour the payload refuses and falls back with provenance** — it never
   silently substitutes a default (`_published_fee_schedule` is the precedent).

## §4 — Method

1. **Splits are by event for tuning, by time for verdicts** (PLAN D5). A report that tunes and
   scores on the same split is not out-of-sample and must not say it is.
2. **Terminal inventory is settled at `result`, fetched from the venue at analysis time.** A market
   with `result ∉ {yes, no}` is excluded from P&L and counted in the report.
3. **The mark is at least one full interval after the fill interval.** Never inside it.
4. **A `MarketMaker` default changes only on the two-split rule in PLAN §Risks**, and the change
   updates the docstring table and `tests/strategies/test_market_making.py` in the same commit.
5. **Reports state the data window** (first/last close, interval, venue, n quoted, n trading) at
   the top, before any number.

## §5 — Before claiming done

- `cd backend && python3 -m pytest -q` passes in full (currently 1,100).
- `python3 -m ruff check <every file you touched>` passes. Pre-existing errors in files you did not
  touch are not yours; do not "fix" them in passing.
- The task's own verify command runs and its real output is in your report.
- `git status --porcelain` shows only the files the brief names. A cache file, a `.env`, or a
  report you did not mean to write is a defect to remove before reporting.
- Commits use the message shape of the repo's recent history: a one-line imperative subject, a body
  that says what was measured and why the change follows, and the attribution trailer the session
  specifies.
