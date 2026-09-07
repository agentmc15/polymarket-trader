# mm-proveout tasks

Written against commit `12c93c7`. Run with `/polytropos:execute mm-proveout`. Paths are relative to
the repo root `/Users/michaelcave/Developer/reposV2/polymarket-trader`; `backend/` is the Python
package root and every `python3`/`pytest` command below runs from inside it.

Dispatch notes for the orchestrator:
- **T1 and T7 are independent of each other and of everything else** — fan them out together.
- **T2 → T3 → T4 are strictly serial on `backend/app/scripts/mm_backtest.py` and its cache.** T2 is
  opus; T3 and T4 are sonnet and may share one warm implementer.
- **T7 → T8 → T9 are strictly serial on `backend/app/services/data_collector.py`** and all sonnet —
  one warm implementer for the cluster.
- **T5 and T6 are independent of each other**, both depend on T3's cache.
- Phase 3 tasks build code now and run on whatever snapshot data exists; their reports must state the
  data window. They are not blocked on weeks of collection — the *verdict* is.
- Live network in verify commands is sanctioned (read-only public GETs, PLAN §Constraints). Tell the
  verifier so; it must not fail a task for making one.

---

## Phase 1 — Kalshi retrospective proof-out

### T1 — Kalshi candle client with a no-look-ahead selector
- status: pending
- model: sonnet
- independent: yes

**Brief.** Create `backend/app/venues/kalshi/candles.py`. The preceding session fetched candles ad
hoc with `adapter._get(f"/series/{series}/markets/{ticker}/candlesticks", params={"start_ts",
"end_ts", "period_interval"})`; make that a typed module the rest of the kit imports.

Provide:
- `@dataclass(frozen=True) Candle`: `end_ts: int`, `bid_close: float | None`,
  `ask_close: float | None`, `px_low: float | None`, `px_high: float | None`,
  `px_close: float | None`, `volume: float`, `open_interest: float | None`. Parse from the venue
  fields `end_period_ts`, `yes_bid.close_dollars`, `yes_ask.close_dollars`, `price.low_dollars`,
  `price.high_dollars`, `price.close_dollars`, `volume_fp`, `open_interest_fp`. `price.*` is ABSENT
  when `volume_fp` is `"0.00"` — that is a zero-volume candle, not an error.
- `async fetch_candles(adapter, *, series, ticker, start, end, interval_minutes) -> list[Candle]`
  with `interval_minutes ∈ {1, 60, 1440}`; sorted by `end_ts`; raises `ValueError` on any other
  interval.
- `series_for(market: VenueMarket) -> str` = `raw["event_ticker"].rsplit("-", 1)[0]`, falling back
  to `market_id.split("-")[0]` when `event_ticker` is absent.
- `candle_at_or_before(candles, cutoff_ts) -> Candle | None`: the LAST candle whose `end_ts <=
  cutoff_ts`. Because `end_ts` is the period END, that candle closed before the cutoff — no
  look-ahead. Document this in the function docstring and pin it with a test.

Also a live probe, `backend/app/scripts/probe_kalshi_candles.py`, that picks one settled market
with `volume_fp > 2000` closing ≥ 60 days ago (if none exists, the oldest available) and prints:
retention (candles returned at 60m and 1m for the day before close), whether `end_period_ts` of
consecutive hourly candles differ by exactly 3600, and whether `price.*` is absent on a zero-volume
candle. Exit 0 on success, 1 if fewer than 10 hourly candles came back.

**Acceptance.**
- `tests/venues/test_kalshi_candles.py` covers: parse of a full candle and of a zero-volume candle
  (no `price` key → `px_* is None`, `volume == 0.0`); sort order; `candle_at_or_before` returns the
  last candle with `end_ts <= cutoff` and `None` when every candle ends after the cutoff (the
  no-look-ahead property); `series_for` both branches; invalid interval raises. No network in tests
  (use `httpx.MockTransport` as the existing venue tests do).
- The probe prints the three facts and exits 0 against the live venue.
- Record retention depth and the `end_period_ts` semantics in `.claude/kits/mm-proveout/NOTES.md`.

**Verify.**
```bash
cd backend && python3 -m pytest -q tests/venues/test_kalshi_candles.py && python3 -m app.scripts.probe_kalshi_candles
```

---

### T2 — The replay harness: `mm_backtest.py`
- status: pending
- model: opus
- depends: T1

**Brief.** Create `backend/app/scripts/mm_backtest.py`, replacing the scratchpad scripts the
preceding session used (they are not in the repo). It replays `MarketMaker` through
`PassiveFillEngine` over settled Kalshi markets and produces the Gate 1 report. Read
`app/strategies/market_making.py`, `app/execution/passive_fill.py`, `app/scripts/calibration.py`
(the closest precedent for structure, CLI, cluster bootstrap and the `--cache` pattern) and
`app/venues/kalshi/candles.py` (T1) before writing.

Pipeline, as importable functions plus a CLI:
1. `settled_universe(adapter, min_volume)` — `list_markets(status="resolved")`, keep
   `result ∈ {"yes","no"}` and `venue_volume(m) >= min_volume` **or** `float(raw["volume_fp"])
   >= min_volume`; also return the count excluded by result (survivorship, PLAN §Risks).
2. `collect(adapter, markets, *, interval_minutes, lookback_days, concurrency=8)` — candles per
   market via T1; cache to JSON at `--cache`; skip markets with < 4 candles.
3. `replay(markets_with_candles, *, policy: MarketMaker, fill_model, tick_size=0.01) ->
   ReplayResult`. For each market, for each candle index i with candles i, i+1, i+2 available:
   quote from candle i's `bid_close`/`ask_close` (skip if either is None or not `0 < bid < ask <
   1`); fills from candle i+1's `px_low`/`px_high`/`volume` via `TradeRange` (skip fills if volume
   is 0 or `px_*` is None, but still count the quote-hour and the collateral); mark at candle i+2's
   mid. Track inventory per market and feed it back into `policy.quote(...)`. Record collateral
   locked per quote-hour (`bid.price*size + (1-ask.price)*size` over the sides quoted). **Settle
   terminal inventory at the venue result**: `pnl += inventory * (settle - last_mid)` where `settle
   ∈ {1.0, 0.0}` and `last_mid` is the last candle mid the inventory was marked at.
   `ReplayResult` holds per-market rows: `market_id, event, series, close_ts, n_fills, n_two_sided,
   pnl, collateral_mean, terminal_inventory, held_into_settlement: bool, settled_short_into_yes:
   bool`.
4. `report(result, *, split: "event"|"temporal", cutoff_ts=None) -> dict` — for BOTH fill models
   (the CLI runs `replay` twice): `n_quoted`, `n_trading`, `mean_pnl_per_trading_market`,
   `ci95_clustered_by_event` (bootstrap over events, 500 reps, seeded), `roc = total_pnl /
   (n_quoted * collateral_mean)`, `held_into_settlement`, `settled_short_into_yes`, and the power
   table (portfolio sizes 500/1000/2500/5000 → 5th percentile and P(profit) by resampling
   per-market P&L). Temporal split: `train = close_ts < cutoff`, `test = close_ts >= cutoff`; the
   verdict is computed on `test`, pessimistic.
5. Every numeric block in the JSON and in the printed table carries `"fill_model"` and
   `"terminal": "settled"` (GUARDRAILS §2.1). The printed report begins with the data window
   (venue, interval, first/last close, n quoted, n trading).

CLI: `python3 -m app.scripts.mm_backtest --sample N --min-volume 2000 --interval 60 --days 10
--cache PATH --seed 20260906 --temporal-cutoff YYYY-MM-DD --out REPORT.json [--min-spread
--edge-fraction --max-inventory --skew-strength --quote-size]` with policy args defaulting to
`MarketMaker`'s defaults. `--cache` reuses collected candles when present (as `calibration.py`
does). `--out` writes the JSON; the table prints to stdout.

**Acceptance.**
- `tests/scripts/test_mm_backtest.py`, no network, synthetic candles: (a) a buy fill at the bid
  when `px_low` reaches it under `optimistic` and NOT under `pessimistic` when it only touches;
  (b) the mark used is candle i+2's mid, never i+1's (construct candles where they differ and
  assert the P&L); (c) terminal inventory settles at the result — a short into `yes` loses
  `inventory * (1 - last_mid)`; (d) a market with `result` outside `{yes,no}` is excluded and
  counted; (e) collateral is accumulated only for quoted hours; (f) `report()` output has
  `fill_model` and `terminal` on every numeric block; (g) temporal split puts a market with
  `close_ts < cutoff` in train and `>=` in test; (h) the rebate is NOT in P&L (a schedule with
  `maker_rebate_rate=0.25` produces identical P&L to one with 0.0).
- `python3 -m app.scripts.mm_backtest --sample 30 --cache /tmp/mm_t2.json --out /tmp/mm_t2_report.json`
  exits 0 against the live venue and the JSON parses with the fields above.
- `ruff` clean on the new files.

**Verify.**
```bash
cd backend && python3 -m pytest -q tests/scripts/test_mm_backtest.py && python3 -m app.scripts.mm_backtest --sample 30 --cache /tmp/mm_t2.json --out /tmp/mm_t2_report.json && python3 -c "import json; d=json.load(open('/tmp/mm_t2_report.json')); assert d['pessimistic']['terminal']=='settled'; print('ok')"
```

---

### T3 — Kalshi Gate 1 at full power
- status: pending
- model: sonnet
- depends: T2

**Brief.** Run the harness at the scale the power analysis demands: sample 8,000 settled markets
(`--min-volume 2000`, hourly, `--days 10`) so that `n_trading ≥ 2,500` under the pessimistic model
(the preceding 537-market run had ~37% trading; if the rate is lower, raise `--sample` until
`n_trading ≥ 2,500` and say so). Cache to `backend/.cache/mm/kalshi-60m.json`; ensure
`backend/.cache/` is in the root `.gitignore` (add the line if absent — it is a one-line change).
Temporal cutoff: the date that puts ~70% of sampled closes in train. Expect ~25 minutes at
concurrency 8; if > 1% of requests 429, halve concurrency (PLAN §Risks).

Write `.claude/kits/mm-proveout/reports/kalshi-gate1.md`: the data window; the survivorship count;
for each fill model (pessimistic first) the report fields from T2 for `all`, `train`, `test`; the
power table; the terminal-inventory counts; and a **verdict line** of the exact form
`VERDICT kalshi fill_model=pessimistic terminal=settled split=temporal-test ci_low=<x> ci_high=<y>
n_trading=<n> -> GO|NO-GO` where GO iff `ci_low > 0`. Copy the JSON to
`.claude/kits/mm-proveout/reports/kalshi-gate1.json`. Append the verdict and the two most surprising
numbers to `NOTES.md`.

**Acceptance.**
- `reports/kalshi-gate1.md` exists, starts with the data window, contains `fill_model=pessimistic`
  before `fill_model=optimistic`, contains exactly one `VERDICT kalshi` line, and every numeric
  block carries `terminal=settled`.
- `n_trading` on the pessimistic test split ≥ 1,000 and on `all` ≥ 2,500 (or the report states why
  not, with the 429 rate and the sample size tried).
- `reports/kalshi-gate1.json` parses; `backend/.cache/` is gitignored; `git status --porcelain`
  shows no cache file.

**Verify.**
```bash
cd backend && test -f ../.claude/kits/mm-proveout/reports/kalshi-gate1.md && grep -c "^VERDICT kalshi fill_model=pessimistic terminal=settled" ../.claude/kits/mm-proveout/reports/kalshi-gate1.md | grep -qx 1 && python3 -c "import json; d=json.load(open('../.claude/kits/mm-proveout/reports/kalshi-gate1.json')); assert d['pessimistic']['all']['n_trading']>=2500, d['pessimistic']['all']['n_trading']; print('ok')" && git -C .. check-ignore -q backend/.cache/mm/kalshi-60m.json
```

---

### T4 — Per-series calibration, out of sample
- status: pending
- model: sonnet
- depends: T3

**Brief.** Add `backend/app/scripts/mm_calibrate.py` that loads a T2 cache and sweeps
`edge_fraction ∈ {0.6,0.7,0.8,0.9}`, `min_spread ∈ {0.05,0.10,0.15,0.25}`, `max_inventory ∈
{10,20,50}`, objective **return on capital**, under the two-split rule: tune on a random event-half,
score on the other half AND on the temporal test split; repeat over 60 random halves; a challenger
"wins" only if it beats the current defaults on ≥ 90% of halves and on the temporal test. Report the
winner per venue-wide sample and per each of the 10 largest `series` (by n_trading), pessimistic
first. Use T2's `replay()`/`report()` — do not reimplement them.

If a challenger wins venue-wide, change the corresponding default in
`app/strategies/market_making.py`, extend the docstring table with the new row and the evidence,
and update `tests/strategies/test_market_making.py::test_the_calibrated_defaults_are_the_measured_ones`
in the same commit. If not, the defaults stay and the report says so.

Write `.claude/kits/mm-proveout/reports/kalshi-calibration.md`.

**Acceptance.**
- `tests/scripts/test_mm_calibrate.py` (synthetic, no network): the 90%-of-halves rule is applied
  (a challenger winning 53/60 does not change a default; 55/60 with the temporal test also passing
  does); the temporal test is required (55/60 but temporal fail → no change).
- The report lists, per series, the tuned parameters with out-of-sample ROC and clustered CI, and
  states which split each number came from.
- Full pytest and ruff pass; if a default changed, the docstring table and the pin test changed with
  it.

**Verify.**
```bash
cd backend && python3 -m pytest -q tests/scripts/test_mm_calibrate.py tests/strategies/test_market_making.py && test -f ../.claude/kits/mm-proveout/reports/kalshi-calibration.md && grep -q "fill_model=pessimistic" ../.claude/kits/mm-proveout/reports/kalshi-calibration.md
```

---

### T5 — What hourly candles cannot see: the 1-minute sub-study
- status: pending
- model: opus
- depends: T3

**Brief.** The skew decision was left at 1.0 "against the grid search" because hourly candles cannot
see intra-hour inventory swings. Measure them. Take the 300 highest-`n_fills` markets from the T3
cache, fetch 1-minute candles for the same windows (`--interval 1`, T2 supports it) into
`backend/.cache/mm/kalshi-1m.json`, and replay the same policy at both resolutions. Report, per fill
model: P&L/trading market at 60m vs 1m for the SAME markets; peak `|inventory|` distribution at 1m
vs 60m; and the skew sweep `{0.0, 0.5, 1.0, 2.0}` at `max_inventory=20` at 1-minute resolution —
mean, 5th percentile, worst. Then answer, in one paragraph with the numbers: does skew earn its keep
at the resolution a live quoter experiences, or does the tight inventory limit do the work? If the
1-minute evidence supports lowering `DEFAULT_SKEW_STRENGTH`, apply the two-split rule from T4 before
changing it; otherwise record the recommendation and leave the default.

Write `.claude/kits/mm-proveout/reports/kalshi-minute-study.md`.

**Acceptance.**
- The report contains a side-by-side table (60m vs 1m, same markets, both fill models, pessimistic
  first, `terminal=settled`) and the skew table at 1m.
- The one-paragraph answer names the numbers it rests on.
- If any default changed, T4's rule was applied and the tests/docstring updated in the same commit.

**Verify.**
```bash
cd backend && test -f ../.claude/kits/mm-proveout/reports/kalshi-minute-study.md && grep -q "interval=1" ../.claude/kits/mm-proveout/reports/kalshi-minute-study.md && grep -q "fill_model=pessimistic" ../.claude/kits/mm-proveout/reports/kalshi-minute-study.md && python3 -m pytest -q tests/strategies/test_market_making.py
```

---

### T6 — Stop carrying inventory into a coin flip: the taper
- status: pending
- model: sonnet
- depends: T3

**Brief.** 160 of 537 markets carried a position into settlement and that is what moved every CI to
span zero. Add an optional `taper_hours: float = 0.0` to `MarketMaker.__init__` and a
`hours_to_close: float | None = None` keyword to `quote()`: when both are set and
`hours_to_close < taper_hours`, the effective `max_inventory` scales linearly to a floor of one
`quote_size` at close (so the policy stops adding, and the withdrawal logic drifts it flat). `0.0`
disables it and every existing call is unchanged. Extend T2's `replay()` to pass `hours_to_close`
from the candle `end_ts` and the market's `close_ts`. Sweep `taper_hours ∈ {0, 1, 3, 6, 12, 24}` on
the T3 cache under the two-split rule; report `held_into_settlement`, `settled_short_into_yes`,
P&L and ROC per setting, pessimistic first. If a nonzero taper wins by the rule, set
`DEFAULT_TAPER_HOURS` accordingly with the evidence in the docstring; otherwise leave 0.0 and record.

Write `.claude/kits/mm-proveout/reports/kalshi-taper.md`.

**Acceptance.**
- `tests/strategies/test_market_making.py` gains: taper 0 → identical quotes to before for any
  `hours_to_close`; inside the window the effective limit shrinks monotonically as
  `hours_to_close` falls; at the close the limit equals one `quote_size`; `hours_to_close=None`
  means no taper regardless of `taper_hours`.
- `tests/scripts/test_mm_backtest.py` gains: `replay()` passes a correct `hours_to_close`.
- Report exists with the sweep table; any default change followed T4's rule.

**Verify.**
```bash
cd backend && python3 -m pytest -q tests/strategies/test_market_making.py tests/scripts/test_mm_backtest.py && test -f ../.claude/kits/mm-proveout/reports/kalshi-taper.md && grep -q "taper_hours" ../.claude/kits/mm-proveout/reports/kalshi-taper.md
```

**— end of Phase 1 —**

---

## Phase 2 — Forward collection on both venues

### T7 — Collect the markets the policy would quote
- status: pending
- model: sonnet
- independent: yes

**Brief.** In `backend/app/services/data_collector.py`, `DataCollector.collect_books` currently
keeps the top `settings.book_collection_top_n` by `venue_volume` per venue — which yields 2 Kalshi
and 0 Polymarket markets with spread ≥ 0.10 out of 50. Change selection to: keep markets that are
two-sided with `ask - bid >= settings.book_collection_min_spread` (new, default 0.10) and
`venue_volume(m) >= settings.book_collection_min_volume` (new, default 100.0); then top
`book_collection_top_n` (raise the default to 500) by `venue_volume` *within* that set. Read bid/ask
from the LISTING payload so no book fetch is spent on rejects: Kalshi `raw["yes_bid_dollars"]` /
`raw["yes_ask_dollars"]`, Polymarket `raw["bestBid"]` / `raw["bestAsk"]`; put that in one helper
`quotable_spread(market) -> float | None` in `app/venues/types.py` beside `venue_volume`, returning
`None` when either side is missing, non-numeric, or the pair is not `0 < bid < ask < 1`. Log per
venue: listed, two-sided, quotable, selected. Add the three settings to `app/config.py` with
`.env.example` lines (the `test_env_example_coverage` guard will fail otherwise).

Add `backend/app/scripts/probe_quotable.py`: lists both venues live and prints those four counts per
venue; exits 1 if either venue's `quotable` is 0.

**Acceptance.**
- `tests/venues/test_quotable_spread.py`: Kalshi and Polymarket field names; one-sided → None;
  crossed → None; non-numeric → None.
- `tests/services/test_book_collection_selection.py`: with a mixed fixture, only quotable markets
  are selected; ordering is by volume within the quotable set; `min_spread`/`min_volume` are read
  from settings, not literals.
- The probe prints non-zero `quotable` for both venues live.
- `.env.example` documents the three new settings; full pytest passes.

**Verify.**
```bash
cd backend && python3 -m pytest -q tests/venues/test_quotable_spread.py tests/services/test_book_collection_selection.py tests/test_env_example_coverage.py && python3 -m app.scripts.probe_quotable
```

---

### T8 — Snapshots carry volume and the fee in force
- status: pending
- model: sonnet
- depends: T7

**Brief.** Add to `BookSnapshot` (`backend/app/models/book_snapshot.py`) three nullable `Float`
columns: `volume` (the listing's `venue_volume` at snapshot time), `taker_fee_rate` and
`maker_rebate_rate` (from `market.fee`). Generate Alembic migration
`backend/alembic/versions/<next>_book_snapshot_volume_fee.py` adding them (nullable, no default
backfill) and confirm it renders with `alembic upgrade head --sql` — **do not apply it**. In
`collect_books`, write the three fields. Why: volume deltas between snapshots are the only activity
signal Kalshi snapshots can carry (its `PriceHistory` path is Polymarket-only), which the replay
needs for the optimistic fill model; and Polymarket's per-market `feeSchedule` changes over time, so
the fee at the time of the quote must travel with the snapshot.

**Acceptance.**
- `tests/models/test_book_snapshot_volume_fee.py`: round-trip on SQLite of a row with the three
  fields and of a row with them `None`; the unique constraint on
  `(venue, market_id, outcome, ts)` is unchanged.
- `tests/test_migration_hypertables.py` (or its sibling pattern) still passes; a new test asserts
  the offline SQL for the new revision contains `ADD COLUMN volume`, `taker_fee_rate`,
  `maker_rebate_rate` and no `UPDATE`.
- `tests/services/test_book_collection_selection.py` extended: the snapshot written carries the
  listing's volume and the market's `fee.taker_rate`/`fee.maker_rebate_rate`.

**Verify.**
```bash
cd backend && python3 -m pytest -q tests/models/test_book_snapshot_volume_fee.py tests/services/test_book_collection_selection.py tests/test_migration_hypertables.py && python3 -m alembic upgrade head --sql 2>/dev/null | grep -q "maker_rebate_rate"
```

---

### T9 — A beat that runs it, and a preflight that checks the field names first
- status: pending
- model: sonnet
- depends: T8

**Brief.** Two things. (1) Schedule collection: add `settings.book_collection_interval_s` (default
60.0) and a `collect-books` entry in `celery_app.conf.beat_schedule` in `backend/app/tasks/__init__.py`
calling a new task `app.tasks.collection.collect_books` that runs `DataCollector.collect_books` via
`run_async_task` (the loop-safe helper in `app/database.py` — every other beat uses it; read
`app/tasks/scanner.py` for the shape). Both venues, per-venue isolation (one venue failing must not
abort the other — `collect_books` already does this; keep it). (2) Extend
`backend/app/scripts/preflight.py` with `--check-collection`: fetch 5 quotable markets per venue
live and assert, for each, that `quotable_spread(m)` is not None, `venue_volume(m) > 0`,
`m.fee.taker_rate` is a float, and one `get_book` returns a two-sided book. Print a table; exit 1 on
any failure. This is the fixture-versus-reality guard (PLAN D8): it runs before a collection run
starts and fails loudly if a venue renamed a field.

**Acceptance.**
- `tests/tasks/test_collect_books_beat.py`: the beat entry exists with the settings-sourced
  interval; the task calls `collect_books` through `run_async_task`; a venue raising `VenueError`
  does not prevent the other venue's snapshots (fake adapters).
- `tests/scripts/test_preflight_collection.py`: `--check-collection` fails when a fake adapter
  returns a market whose spread field is missing, and passes on a good one; no secret value in
  output (reuse the existing redaction test pattern in `tests/scripts/`).
- `python3 -m app.scripts.preflight --check-collection` exits 0 live.

**Verify.**
```bash
cd backend && python3 -m pytest -q tests/tasks/test_collect_books_beat.py tests/scripts/ && python3 -m app.scripts.preflight --check-collection
```

---

### T10 — Collection health
- status: pending
- model: sonnet
- depends: T8

**Brief.** Add `backend/app/scripts/collection_health.py`: from `book_snapshots`, for the last
`--hours` (default 24), per venue: snapshots, distinct markets, distinct (market, outcome), share of
snapshots with `volume` non-null, share two-sided, median seconds between consecutive snapshots of
the same (market, outcome), count of gaps > 2× `book_collection_interval_s`, first/last `ts`. Print a
table and write JSON to `--out`. Exit 1 if either venue has zero snapshots in the window. Query with
SQLAlchemy against `async_session_factory`; the tests use SQLite via the existing conftest pattern.

**Acceptance.**
- `tests/scripts/test_collection_health.py`: seeded rows produce the expected counts and gap
  detection; an empty venue exits 1; JSON schema stable.
- The script runs against the configured DB (empty is fine — exit 1 with a clear message, which is
  the correct answer before collection starts).

**Verify.**
```bash
cd backend && python3 -m pytest -q tests/scripts/test_collection_health.py
```

**— end of Phase 2 —**

---

## Phase 3 — Polymarket forward proof-out (build now, verdict when the data exists)

### T11 — Replay from snapshots, both venues, same report
- status: pending
- model: sonnet
- depends: T2, T8

**Brief.** Add `backend/app/scripts/mm_replay_snapshots.py`. Load `book_snapshots` for `--venue`
over `--from/--to`, group by (market, outcome=YES), sort by `ts`, and build the same
`(quote, fill, mark)` triples T2 uses from consecutive snapshots: quote from snapshot i's best
bid/ask; the pessimistic `TradeRange` from snapshot i+1 as `low = best_bid_{i+1}`, `high =
best_ask_{i+1}`, `volume = (volume_{i+1} - volume_i) if both non-null else 1.0` — so the
pessimistic model fills when the touch moved THROUGH the quote, and the optimistic model is only
computed when a real volume delta exists (report "optimistic: n/a — no volume field" otherwise);
mark at snapshot i+2's mid. Settle terminal inventory at the venue result fetched at analysis time
(`list_markets(status="resolved")` on the venue; a market not yet resolved is reported as
`unsettled` and excluded from the verdict). Reuse T2's `replay()`/`report()` by adapting rows into
its candle shape — do not write a second report. Fees per snapshot from T8's columns; rebate as the
separate line (GUARDRAILS §2.3). Report header states the data window and n.

**Acceptance.**
- `tests/scripts/test_mm_replay_snapshots.py` (SQLite, seeded snapshots): the through-model fills
  exactly when the next best bid < quote bid (buy) / next best ask > quote ask (sell); optimistic
  is `n/a` without volume and computed with it; unsettled markets are excluded and counted; rebate
  not in P&L; the report is produced by the same `report()` function as T2 (assert by identity of
  the imported callable).
- Runs against the configured DB and prints the data window (empty → clear exit 1).

**Verify.**
```bash
cd backend && python3 -m pytest -q tests/scripts/test_mm_replay_snapshots.py
```

---

### T12 — Per-venue calibration on forward data, rebate as its own line
- status: pending
- model: sonnet
- depends: T4, T11

**Brief.** Extend `mm_calibrate.py` (T4) with a `--source snapshots --venue V` mode that uses T11's
loader, and run it for both venues on whatever data exists at execution time. Same two-split rule.
Add to the report, per venue, the line `rebate would add $X per trading market if paid as
published` computed from `maker_rebate_rate * taker_fee_rate * p * (1-p) * size` over the fills
— beneath P&L, never in it — and a **power statement**: `n_trading`, per-market sd, and the
portfolio size at which the pessimistic 5th percentile crosses zero. If `n_trading < 300` for a
venue, the report says the calibration is underpowered and no default changes for that venue.

Write `.claude/kits/mm-proveout/reports/forward-calibration.md`.

**Acceptance.**
- Tests extend `test_mm_calibrate.py`: the rebate line is computed and is not inside P&L; the
  underpowered rule holds at `n_trading=299` and releases at 300.
- The report exists with both venues (or an explicit "no snapshots yet" block per venue with the
  data window it looked at), pessimistic first, `terminal=settled`.

**Verify.**
```bash
cd backend && python3 -m pytest -q tests/scripts/test_mm_calibrate.py && test -f ../.claude/kits/mm-proveout/reports/forward-calibration.md && grep -q "rebate would add" ../.claude/kits/mm-proveout/reports/forward-calibration.md
```

---

### T13 — Go / no-go
- status: pending
- model: opus
- depends: T3, T4, T5, T6, T12

**Brief.** Write `.claude/kits/mm-proveout/reports/go-no-go.md`. Read every report in
`reports/` and `NOTES.md` first. Structure: (1) the question; (2) per venue — data window, n,
pessimistic OOS temporal-test P&L/trading market with clustered CI, ROC, power (portfolio size for
5th percentile > 0), capital locked and sustained order rate at that portfolio (T2's collateral and
the 1.15 orders/market-hour measured previously, against ~10 req/s), the rebate line; (3) the two
things paper cannot prove — queue position and the rebate actually arriving — stated as the
residual risk; (4) a verdict per venue: `GO`, `NO-GO`, or `UNDERPOWERED — needs N more trading
markets`; (5) what would change the verdict. No number without `fill_model=` and `terminal=`. No
recommendation to trade real money appears in this document — that is Gate 2's design (T14) and
the user's decision.

**Acceptance.**
- The document exists, contains exactly one verdict line per venue of the form
  `VERDICT <venue> ... -> GO|NO-GO|UNDERPOWERED`, cites the report each number came from, and
  contains the residual-risk section.
- Every numeric claim in it can be found in a `reports/*.json` or `reports/*.md` produced by
  T3–T12 (the reviewer spot-checks five).

**Verify.**
```bash
cd backend && test -f ../.claude/kits/mm-proveout/reports/go-no-go.md && test "$(grep -c '^VERDICT ' ../.claude/kits/mm-proveout/reports/go-no-go.md)" = "2" && grep -q "queue position" ../.claude/kits/mm-proveout/reports/go-no-go.md
```

**— end of Phase 3 —**

---

## Phase 4 — Gate 2, designed and not executed

### T14 — The smallest real-money test that answers what paper cannot
- status: pending
- model: opus
- depends: T13

**Brief.** Write `.claude/kits/mm-proveout/reports/gate2-design.md`. It specifies, for whichever
venue(s) T13 marked GO or UNDERPOWERED-but-close: purpose (one statistic: realized fill rate and
realized half-spread versus the optimistic and pessimistic predictions from the replay); venue;
`quote_size` at the venue minimum; number of markets (enough for ≥ 100 fills at the predicted
pessimistic fill rate); capital cap as a research cost, derived from T2's collateral per market;
duration; the pass rule (`realized ≥ pessimistic prediction` with a CI) and the kill rule (rolling
P&L below the pessimistic 5th percentile → cancel all resting orders and stop); the exact code a
future kit must build — order placement, cancel/replace, resting-order inventory tracking,
reconciliation against venue fills — and the GUARDRAILS changes that kit would need (§1.1 narrowed
to one venue, one size, one strategy, behind `LIVE_TRADING_CONFIRMATION`); and a first line stating
that this kit built none of it. Include the measurement plan for the Polymarket rebate (does the
daily payout arrive, and at the published share).

**Acceptance.**
- The document exists; its first non-heading line states that no order-placing code exists in this
  kit; it names the statistic, the pass rule, the kill rule, the capital cap with its derivation,
  and the future-kit guardrail changes.
- `python3 -m pytest -q tests/test_fences.py` still passes (no order code was added anywhere).

**Verify.**
```bash
cd backend && test -f ../.claude/kits/mm-proveout/reports/gate2-design.md && grep -qi "kill rule" ../.claude/kits/mm-proveout/reports/gate2-design.md && grep -qi "no order" ../.claude/kits/mm-proveout/reports/gate2-design.md && python3 -m pytest -q tests/test_fences.py
```

**— end of Phase 4 —**
