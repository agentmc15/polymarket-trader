# mm-proveout — take market making from "positive in one backtest" to a go/no-go

autonomy: advisory
budget: max-dispatches=70 max-escalations=4 max-consults=6
roles: test-author red-team

Written against commit `12c93c7` of `polymarket-trader`, 2026-09-07. Every number below was
measured live in the preceding session and is recorded with its date in
`.claude/kits/market-edge/NOTES.md`; nothing here is assumed.

## Goal

Decide, with numbers that survive scrutiny, whether passive two-sided quoting is a business on
Kalshi and/or Polymarket — and if so, produce the design of the smallest real-money test that can
answer the one question paper trading cannot.

**Done looks like:**

1. `reports/kalshi-gate1.md` — the calibrated `MarketMaker` replayed through `PassiveFillEngine`
   over **≥ 2,500 trading markets** of settled Kalshi history, terminal inventory settled at the
   venue's real result, both fill models reported, confidence intervals clustered by event, scored on
   a **temporal** hold-out (train on earlier closes, test on later). A verdict line: pessimistic-model
   P&L lower CI bound above zero, or not.
2. Forward collection running on both venues, selecting markets the policy would actually quote,
   recording what the replay needs (both sides with sizes, volume, fee in force), with a health
   report and a preflight that checks live field names before every run.
3. `reports/go-no-go.md` — the two venues compared on the same statistic, with the power of each
   sample stated, the rebate reported as a separate line that is never credited, capital and order
   rate at the target portfolio, and an explicit list of what paper cannot prove.
4. `reports/gate2-design.md` — the measurement-sized live test, fully specified and **not
   executed**: this kit writes no code that places, modifies or cancels an order.

## Constraints and out of scope

- `.claude/kits/market-edge/GUARDRAILS.md` §1.1, §1.2, §1.3, §5, §6, §7 bind every task verbatim.
  §1.4 (no venue network) is **lifted for read-only public GETs only** — the user lifted it in the
  preceding session. Signed Kalshi GETs using the existing `.env` credentials are read-only and
  allowed; no task reads, prints or copies a secret.
- **Gate 2 is designed here and executed nowhere.** No order placement code, no `TRADING_MODE`
  other than `paper`, no `LIVE_TRADING_CONFIRMATION`. A future kit with its own guardrails owns
  execution; this one owns the evidence.
- Alembic migrations are rendered **offline** (`alembic upgrade head --sql`) and never applied to a
  real database by any task.
- Out of scope: new strategies, cross-venue arbitrage, the near-resolution beat, UI. This kit is
  market making only.
- Out of scope: lowering `min_spread` on any venue to manufacture sample size. Report the power
  you have; calibrating `min_spread` per venue on out-of-sample data is a task, forcing n is not.

## Verified repo facts an executor must not re-derive

- `app/strategies/market_making.py` — `MarketMaker.quote(book, tick_size=, inventory=)` returns a
  `QuotePair`; calibrated defaults `min_spread=0.10`, `edge_fraction=0.80`, `max_inventory=20`,
  `skew_strength=1.0`. Calibration evidence is in its docstrings.
- `app/execution/passive_fill.py` — `PassiveFillEngine(fee_model, schedule, fill_model=)`,
  `TradeRange(low, high, volume)`, `mark_to_market(fill, mark)`. Default fill model is
  `"pessimistic"`; every `PassiveFill` carries `fill_model`.
- Fees: Kalshi taker 0.07 / maker **0.0175** (confirmed 2026-09-06, `tests/test_fee_rates.py`).
  Polymarket per-market rate from `feeSchedule.rate` (`source="venue_schedule"`); makers pay 0;
  `FeeSchedule.maker_rebate_rate` carries the rebate and **`fee()` never credits it**.
- Kalshi history: `GET /series/{series}/markets/{ticker}/candlesticks?start_ts&end_ts&period_interval`
  with `period_interval` in `{1, 60, 1440}` minutes. Candle fields: `end_period_ts`,
  `yes_bid.{open,high,low,close}_dollars`, `yes_ask.{...}_dollars`, `price.{open,high,low,close,
  mean,previous}_dollars` (OHLC absent when `volume_fp` is 0), `volume_fp`, `open_interest_fp`.
  Measured 2026-09-07: **50,704 settled markets with `volume_fp` > 2000, closes 2026-06-30 →
  2026-09-07**, hourly and 1-minute candles available at the oldest.
- `KalshiAdapter.list_markets(status="resolved")` walks `/events?status=settled` to exhaustion
  (427k markets; shards excluded). `VenueMarket.result` is `"yes"`/`"no"`/`"scalar"`/`""`.
  The `series` for the candle URL is `raw["event_ticker"].rsplit("-", 1)[0]`.
- Polymarket history: `/prices-history` returns `{t, p}` only — **no bid/ask, no volume**; YES and
  NO series share zero timestamps; `/trades` needs credentials. **Polymarket cannot be backtested
  retrospectively.** Forward collection is the only path.
- Collection: `DataCollector.collect_books` (`app/services/data_collector.py`) writes
  `BookSnapshot` (`app/models/book_snapshot.py`: venue, market_id, outcome, ts, bids, asks,
  tick_size, min_size, depth_source — **no volume, no fee**) for the top
  `settings.book_collection_top_n` (default 50) by `venue_volume`. It is invoked only by
  `python -m app.scripts.collect_prices --books`; **no beat schedules it**.
- Selection is the blocker: top-50 by volume yields **2 Kalshi and 0 Polymarket** markets with
  spread ≥ 0.10. Venue-wide: Kalshi 17,079 and Polymarket 169 with spread ≥ 0.10.
- `venue_volume(market)` in `app/venues/types.py` is the one ranking key (prefers `volume_24h_fp`
  / `volume24hr`). Live Kalshi bid/ask are `yes_bid_dollars`/`yes_ask_dollars`; Polymarket
  `bestBid`/`bestAsk`. Fixtures under `tests/fixtures/` do NOT match live field names in several
  places — a fixture is never evidence about the live payload.
- Rate limits: Kalshi ≈ 10 req/s unauthenticated, adapter paces at `kalshi_min_request_interval_s`
  (0.10) with 429 retry. Kalshi `get_book` is one request per market; Polymarket two (YES/NO
  tokens). Candle fetches at concurrency 8 ran 700 markets in 134s with zero 429s.
- Preceding measurements to reproduce, not re-argue: 537-market hourly study — realized half-spread
  +0.0051 (optimistic) / −0.0095 (pessimistic); only spread ≥ 0.25 positive under both; sell fills
  1.5–2.0× buy fills at every price; settling terminal inventory moved every CI to span zero;
  per-market Sharpe ≈ 0.05; ~2,500 trading markets for P(profit) > 99%.

## Architecture and decisions

- **D1 — Select by quotability, not volume.** A market enters collection iff two-sided, spread ≥
  `book_collection_min_spread`, and `venue_volume` ≥ a floor; then top-N by volume *within* that set.
  Rationale: volume alone selects the tightest books, which the policy refuses (2/0 quotable in the
  top 50).
- **D2 — Kalshi proves out retrospectively, Polymarket forward.** Same policy, same engine, same
  report; different data source. Rationale: Kalshi candles carry bid/ask/trade/volume history;
  Polymarket exposes none.
- **D3 — Pessimistic is the reporting default; optimistic is reported beside it, never alone.** A
  number without `fill_model` is not a number (GUARDRAILS §3). Rationale: the two models differ in
  sign on the same data; queue position is the variable and history cannot see it.
- **D4 — Terminal inventory is always settled at the venue's real result, fetched at analysis
  time.** Never marked. Rationale: marking turned a null result into a significant one; settled
  results are available after the fact (427k), so nothing needs collecting for this.
- **D5 — Two splits, two purposes.** Random split by *event* for parameter tuning (events share
  outcomes); *temporal* split (earlier closes → later closes) for the go/no-go. Rationale: a random
  split cannot detect regime change; the go/no-go must.
- **D6 — The rebate is a separate line, never inside P&L.** Reported as "would add X if paid as
  published". Rationale: it is a daily programme payout under changeable terms; crediting it in the
  fee model lets projected revenue leak into every cost calculation.
- **D7 — Per-venue calibration of `min_spread` and `edge_fraction`.** Rationale: Polymarket pays
  makers and Kalshi charges them, a +0.0069/contract swing at p=0.50 that moves the breakeven
  spread; one default cannot serve both.
- **D8 — Every parsed field is verified against a live payload, not only a fixture.** Each task that
  reads a venue field has a live probe in its verify command. Rationale: six defects in the
  preceding session were fixtures agreeing with code while the venue did not.
- **D9 — Snapshots carry volume and the fee in force.** Rationale: volume deltas let the replay use
  the optimistic model and detect activity; fee schedules change per market over time.
- **D10 — Reports live in the kit, data lives in `backend/.cache/mm/` (gitignored).** Rationale:
  reports are the deliverable and belong in version control; multi-hundred-MB candle caches do not.
- **D11 — Shared report code.** `app/scripts/mm_backtest.py` (T2) exposes `replay()` and `report()`
  as importable functions; the snapshot replay (T11) imports them so both venues are scored by
  byte-identical logic. Rationale: two report implementations would drift exactly as the two volume
  helpers did.

## Risks and tripwires

- **Candle retention or semantics differ from the probe.** T1 verifies `end_period_ts` is the
  period END (so "last candle with end ≤ cutoff" has no look-ahead) and retention ≥ 60 days. If
  retention is shorter, T3 samples only from closes inside it and the report states the reduced
  temporal-split power; do not silently widen `--days`.
- **429s at scale.** If > 1% of candle requests return 429 in T3, halve concurrency and continue;
  never disable pacing.
- **Polymarket quotable count (169) cannot reach significance.** Report power; do not lower
  `min_spread` to make n. Per-venue `min_spread` calibration (T12) is the sanctioned route and it is
  out-of-sample or it is nothing.
- **Survivorship.** `status=settled` omits voided/delisted markets. T3 reports the share of sampled
  markets with `result ∈ {yes,no}` vs other and excludes the rest from P&L, stating the count.
- **Look-ahead through the mark.** The mark is the mid ≥ one full interval after the fill interval,
  never inside it. T2's tests pin this.
- **A default changes on thin evidence.** No `MarketMaker` default moves unless the challenger wins
  on ≥ 90% of 60 random event-halves AND on the temporal hold-out. Otherwise the report records the
  candidate and the default stays.
- **Brief vs repo drift.** Earlier tasks change files later tasks name. Every implementer reads the
  named files before editing; a brief that contradicts the tree is reported, not improvised around.
- **Secrets.** `.env` exists and holds Kalshi credentials. No task opens it. The adapter reads it.
