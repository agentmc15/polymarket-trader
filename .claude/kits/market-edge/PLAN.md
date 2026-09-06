# market-edge — multi-venue prediction-market inefficiency engine

autonomy: advisory
budget: max-dispatches=110 max-escalations=8 max-consults=10
roles: test-author red-team second-verifier security-auditor

Architected 2026-09-04 by Fable 5.1 against commit `2397b8a` (branch `main`, clean tree).
Every executor reads this file first. It has no access to the conversation that produced it;
everything it needs to make a consistent micro-decision is here or in TASKS.md/GUARDRAILS.md.

---

## 1. Goal

Turn `polymarket-trader` into the single repository for finding and (eventually) capturing
genuine pricing inefficiencies across real-API prediction-market exchanges — Polymarket and
Kalshi today, more later through a venue-adapter seam — with time-to-resolution as a
first-class ranking axis, a paper-trade execution path that is the *same* code as the live
path, and a backtest whose numbers can be trusted because it was audited for look-ahead and
survivorship bias and because it shows, per strategy, the capital level at which each edge dies.

Trader mimicry (copy-trading "whales") is removed entirely.

### "Done" is checkable

1. `cd backend && python3 -m pytest -q` passes with ≥ 120 tests, on SQLite, with no network.
2. `python3 -c "import app.main"` succeeds; `alembic upgrade head --sql` emits DDL for every
   mapped table with no error (offline mode, no DB needed).
3. `grep -rniE "whale|TrackedTrader|copy_trad|leaderboard|TraderFollow" backend/app frontend/src .claude CLAUDE.md` returns nothing.
4. `app/venues/` contains `VenueAdapter` + `polymarket` + `kalshi` adapters; both pass the shared
   adapter contract test suite on recorded fixtures.
5. `Settings.trading_mode` defaults to `"paper"`; `tests/test_fences.py` proves (by AST walk)
   that no module outside `app/venues/*/live.py` can reach a venue's order-placement call, and
   that live adapters refuse to construct unless `trading_mode == "live"` AND
   `live_trading_confirmation == "I_UNDERSTAND_REAL_MONEY"`.
6. A backtest of `binary_complement_arbitrage` on the synthetic fixture executes BOTH legs,
   settles positions at resolution, and fills at the *next* snapshot's book by default.
7. `POST /api/v1/backtests/sweep` (and `python3 -m app.scripts.sweep`) returns an
   `EdgeDecayReport` with one row per capital level and an explicit `edge_dies_at` value.
8. `event_links` table exists; cross-venue arbitrage consumes only `approved` links; there is an
   API to propose/approve/reject links with the two venues' resolution texts side by side.
9. `GET /api/v1/arbitrage/opportunities` returns intents ranked by a score whose components
   (net edge, annualized return, fill confidence, resolution risk, hours-to-resolution) are all
   present in the payload.

---

## 2. Constraints and out-of-scope (executors must NOT do these)

- **No sportsbooks.** DraftKings/FanDuel/etc. are excluded: no public API, ToS forbids automation,
  accounts get limited. Do not write scrapers. Do not add them to the venue registry.
- **No new strategy stubs.** The repo has nine strategy files; the job is to make the
  inefficiency-based ones REAL and MEASURED, not to add a tenth. `catalyst_momentum` and
  `correlation_hedging` are kept (they compile and run under the new base) but receive no new
  work in this kit.
- **No real orders, ever, from tests, verifiers, red-team, or CI.** See GUARDRAILS.md §1.
- **No rewrite of the backtesting engine.** `engine.py`, `metrics.py`, `data_replay.py` are
  extended and corrected in place. Preserve public names in `app/services/backtesting/__init__.py`
  (`Backtester`, `BacktestConfig`, `BacktestResult`, `DataReplayer`, `InMemoryDataReplayer`,
  `calculate_metrics`, …) — the API route and Celery task depend on them.
- **No LLM-in-the-loop event matching.** The matcher is deterministic and human-reviewed.
  An LLM assist hook is a later kit.
- **No Kalshi WebSocket in this kit.** REST polling only. The adapter interface leaves a
  `stream_books()` slot unimplemented (`NotImplementedError`), not faked.
- **No frontend redesign.** Two frontend tasks only: delete the traders surface, add an
  Opportunities tab and an edge-decay table. Node/npm work is confined to those tasks.
- **Do not "fix" ruff/mypy across the whole repo.** Baseline is 139 ruff findings. New/changed
  modules must be clean under `ruff check <path>` and `mypy <path>`; untouched legacy files
  are not a gate.
- `polymarket-backtesting-complete-guide.md` at the repo root is a reference document; leave it.

---

## 3. Repo reality (verified 2026-09-04 — do not re-derive, DO read the files before editing)

| Fact | Evidence |
|---|---|
| `import app.models` FAILS: `InvalidRequestError: Attribute name 'metadata' is reserved` | 9 `metadata: Mapped[dict]` columns in `models/{market,trade,position,strategy,backtest,trader}.py`; `models/__init__.py` imports all, so even `app.models.base` fails via the package |
| Therefore `app.main`, `alembic/env.py`, and `tests/conftest.py` cannot import | `pytest --collect-only` errors |
| `conftest.py` is doubly broken: `sqlite+aiosqlite` but `aiosqlite` not installed; `AsyncClient(app=app)` removed in httpx ≥ 0.27 (installed 0.27.0); custom `event_loop` fixture unsupported in pytest-asyncio 1.x (installed 1.3.0); JSONB columns cannot create on SQLite | read `backend/tests/conftest.py` |
| `MarketPrice.market_id` has no `ForeignKey` but `Market.prices` declares a relationship → mapper configuration will raise once import is fixed | `models/market.py` |
| `database.init_db` executes a bare string `"SELECT 1"` — SQLAlchemy 2.0 requires `text()` | `database.py` |
| Only ONE alembic migration exists (`001`): `price_history`, `trade_history`, `tracked_traders`, `backtest_runs`. `markets`, `market_prices`, `orders`, `trades`, `positions`, `strategies`, `backtests`, `backtest_trades`, `traders`, `trader_follows` have NO migration | `alembic/versions/` |
| `OrderExecutor.execute_signal` reads `signal.side` — `Signal` has `type`, not `side` → the live path raises `AttributeError` before any order | `bots/executor.py` |
| `ClobClientWrapper` methods are `async def` but call the synchronous `py_clob_client` → they block the event loop | `services/polymarket/client.py` |
| Arbitrage strategies emit ONE leg (`outcome="YES"`) with a comment "executor should buy both"; the engine buys only that leg | `binary_complement_arbitrage.py`, `multi_outcome_bundle_arbitrage.py`, `engine._execute_signal` |
| The engine never settles positions at resolution; `_close_all_positions` marks to last price. `Market.is_resolved/resolution_outcome/resolved_at` exist but the replayer ignores them | `engine.py`, `data_replay.py` |
| Fills happen at the SAME snapshot the signal was generated on, at `signal.price ± fixed slippage`; no depth | `engine._open_position` |
| Naive/aware datetime mixing: `PriceHistory.timestamp` is tz-aware; `BacktestConfig` dates and `datetime.utcnow()` are naive → comparisons will raise on real data | `engine.py`, `tasks/backtesting.py` |
| Every strategy hardcodes `fee_rate: 0.0` / `polymarket_fee_rate: 0.0` | all `DEFAULT_CONFIG`s |
| `cross_platform_arbitrage._find_arbitrage` "sells" on the higher venue — impossible without an existing long; neither venue supports naked shorts | `cross_platform_arbitrage.py` |
| Python: `/opt/anaconda3/bin/python3` 3.12.7; sqlalchemy 2.0.34, fastapi, pytest, numpy, scipy, httpx 0.27.0, pytest-asyncio 1.3.0, py_clob_client, celery, websockets present; `aiosqlite` MISSING | `python3 -c "import …"` |
| Tools: ruff 0.14.10, mypy 1.11.2, alembic 1.17.2, black 24.8. Baseline `ruff check app/` = 139 findings; `mypy app/services/backtesting app/strategies/base.py` = 2 errors (`metrics.py` date-vs-datetime dict key) | measured |
| Node 26.3, npm present; `frontend/node_modules` NOT installed; `package.json` has no `typecheck`/`test` scripts (`build` = `tsc -b && vite build`). React 19 + Tailwind 4 (CLAUDE.md says React 18 — CLAUDE.md is stale) | `frontend/package.json` |
| `data_collector.py` (1177 lines) holds `GammaAPIClient`, `CLOBDataClient`, `PolymarketDataClient` (leaderboard/trader methods AND `get_market_trades`), `DataCollector` (sync_markets, price snapshots, backfill via TradeHistory, `update_trader_leaderboard`, `get_whale_trades`) | outline grep |
| `TradeHistory` is the public trade TAPE (used by `data_replay._get_recent_trades` and `backfill_data.generate_price_snapshots`) — it is NOT mimicry and is kept | grep |

### Venue facts (fetched from vendor docs 2026-09-04; pin these, cite them in docstrings)

**Polymarket**
- Fee: `fee = C × feeRate × p × (1 − p)`; **takers only, makers never pay**; charged in USDC.
  Category taker rates: Crypto 0.07; Sports/Economics/Culture/Weather/Other 0.05;
  Finance/Politics/Mentions/Tech 0.04; Geopolitics 0. (docs.polymarket.com/trading/fees)
  The per-market rate may also be carried on the CLOB market payload (historically
  `maker_base_fee` / `taker_base_fee`); when present, it is authoritative over the category table.
  Fixture the actual payload at implementation time and record which source was used on each fee.
- Book: `GET /book?token_id=…` → `{bids:[{price,size}], asks:[…], market, asset_id, timestamp(ms),
  hash, min_order_size, tick_size, neg_risk, last_trade_price}`; price/size are decimal strings;
  one book per outcome token.
- CLOB trades are relayed by the operator; the user pays no gas per trade. Gas IS paid on
  Polygon for USDC deposit/withdrawal and for redeeming winning shares after resolution
  (small, ~$0.01–0.05). Model as `redemption_gas_usd` per settled position.
- Resolution: UMA optimistic oracle; a proposed outcome has a challenge window (~2h) and can be
  disputed; disputed markets can take days. This is *settlement risk*, not price risk.

**Kalshi** (docs.kalshi.com, Trade API v2)
- Base URLs — production `https://external-api.kalshi.com/trade-api/v2` (alt
  `https://api.elections.kalshi.com/trade-api/v2`); demo
  `https://external-api.demo.kalshi.co/trade-api/v2`. WS (NOT used this kit)
  `wss://external-api-ws.kalshi.com/trade-api/ws/v2`.
- Auth: headers `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP` (ms), `KALSHI-ACCESS-SIGNATURE`;
  signature = RSA-PSS(SHA256, MGF1-SHA256, salt=digest length) over
  `f"{timestamp_ms}{METHOD}{path}"` where `path` is the URL path WITHOUT query string
  (e.g. sign `/trade-api/v2/portfolio/orders`, not `…?status=open`). Private key is a PEM.
- Market payload: `yes_bid_dollars`, `yes_ask_dollars`, `no_bid_dollars`, `no_ask_dollars`,
  `last_price_dollars` (fixed-point dollar strings), `*_fp` sizes, `rules_primary`,
  `rules_secondary`, `close_time`, `expected_expiration_time`, `latest_expiration_time`,
  `settlement_timer_seconds`, `settlement_ts`, `result` ∈ {`yes`,`no`,`scalar`,``},
  `fee_waiver_expiration_time`, `price_level_structure` (tick sizes by price range).
- Orderbook: `GET /markets/{ticker}/orderbook` → `{"orderbook_fp": {"yes_dollars": [[price,qty],…],
  "no_dollars": [[price,qty],…]}}` — **bids only**. A NO bid at price q is a YES ask at 1−q.
  Derive `yes_asks` from `no_dollars` and `no_asks` from `yes_dollars`. Older payloads may carry
  `orderbook.yes/no` in integer cents; parse both.
- Orders (V2): `POST /portfolio/events/orders` with `ticker`, `side` ∈ {`bid`,`ask`}, `count`
  (fixed-point string), `price` (dollar string, 2–4 dp), `time_in_force` ∈
  {`fill_or_kill`,`good_till_canceled`,`immediate_or_cancel`}, `self_trade_prevention_type`,
  optional `client_order_id` (idempotency), `post_only`, `expiration_time`. Response:
  `order_id`, `client_order_id`, `fill_count`, `remaining_count`, `average_fill_price`,
  `average_fee_paid`, `ts_ms`.
- Fees: Kalshi's published retail formula is `fee = rate × C × P × (1 − P)` with standard taker
  rate 0.07 and maker 0 (some series carry maker fees; some markets carry a fee waiver until
  `fee_waiver_expiration_time`). Rounding per docs: `trade_fee = ceil_6dp(model_fee)` per fill,
  then the fill's net is floored to $0.01 for non-direct members. **The implementer must
  re-confirm the 0.07 default against the live fee page at implementation time and record the
  URL + date in the docstring; the rate is a `Settings` override, never a literal in strategy code.**
- Capital: USD in a CFTC-regulated FCM account; deposits/withdrawals via ACH/wire (days).
  Capital is NOT fungible with Polymarket USDC in real time. Default `transfer_latency_hours=72`.

---

## 4. Architecture and key decisions (with rationale)

### D1. Repair before build (Phase 0 is not optional)
The models package must import, tests must run on SQLite without network, and the mimicry
surface must be gone before any venue or execution code lands. Rationale: every later verify
command runs `pytest`; a repo whose models can't import turns every task into "blocked".
JSON columns become `JSON().with_variant(JSONB, "postgresql")` (one alias `JSONDict`/`JSONList`
in `models/base.py`) so Postgres keeps JSONB and SQLite tests work. The reserved attribute is
renamed `extra_data` (column name `extra_data`) — no table for these models has ever been
migrated, so there is no data to preserve.

### D2. Mimicry is deleted outright, with one migration — not excised gradually
Rationale (preserve this — it is the user's reasoning and it is correct): on prediction markets a
single wallet's realized trades are far too few, too correlated (same events, same news), and
too survivorship-selected (leaderboards show winners after the fact) to distinguish skill from
variance; a copier also pays the latency and slippage the leader did not. It is not sustainable.
Repo state makes deletion cheap: only `tracked_traders` ever got a migration; `traders` /
`trader_follows` never existed in any DB. Migration `002` drops `tracked_traders` and issues
`DROP TABLE IF EXISTS` for the two never-created tables so a stray dev DB is left clean.
`TradeHistory` (public tape, used by replay/backfill) is kept.

### D3. One `VenueAdapter` seam; venues are data, strategies are venue-agnostic
`app/venues/base.py` defines the Protocol; `app/venues/types.py` the normalized types:
- `VenueId = Literal["polymarket","kalshi"]`
- `VenueMarket(venue, market_id, event_id|None, question, outcomes: list[str],
  outcome_ids: dict[str,str], rules_text, resolution_source|None, close_time, expected_settle_time|None,
  status: open|closed|resolved, result|None, tick_size, min_size, fee: FeeSchedule, raw: dict)`
- `BookLevel(price: float, size: float)`; `OrderBook(venue, market_id, outcome, bids, asks, ts)` —
  prices are probabilities in [0,1] on every venue (Kalshi dollars→float, cents/100), sizes are
  contracts (1 contract pays $1.00 at resolution).
- `OrderRequest(venue, market_id, outcome, side: BUY|SELL, price, size, tif, client_order_id,
  post_only)`, `OrderAck`, `Fill(venue, order_id, price, size, fee, ts, liquidity: maker|taker)`,
  `Balance(venue, available, locked)`, `Position(venue, market_id, outcome, size, avg_price)`.
- Methods: `list_markets(status, updated_since)`, `get_market(id)`, `get_book(market_id, outcome)`,
  `get_balance()`, `get_positions()`, `get_open_orders()`, `get_fills(since)`, `place_order()`,
  `cancel_order()`, `fee_model() -> FeeModel`, `stream_books()` (NotImplemented this kit).
Registry: `app/venues/registry.py` `get_adapter(venue, mode)`. Rationale: the user asked "if others
exist as well" — the honest answer is that the liquid, real-API set is Kalshi + Polymarket today;
the seam is the answer, not a pile of adapters. Candidates that could slot in later (NOT tasks):
PredictIt (legacy, illiquid, no trading API), Manifold (play money; API exists — useful as a
sentiment source, never a venue), Metaculus (no money), Betfair Exchange (real API but
geo-restricted for US users and sports-centric), Limitless/Opinion-style on-chain venues
(thin, contract-level integration). None change the design.

### D4. Paper and live share one execution path; only the last hop differs
`app/execution/router.py::OrderRouter` is the ONLY object that turns an `Intent` into venue
orders. It receives adapters from the registry; in `paper` mode the registry hands it a
`PaperVenueAdapter` that proxies market-data reads to the real adapter (or a fixture replay)
and answers `place_order` from `SimulatedFillEngine`. Idempotency (`client_order_id` =
`f"{intent_id}:{leg_index}:{attempt}"`), partial fills, leg-failure policy, capital ledger, and
reconciliation are exercised in paper mode exactly as they would be live. Rationale: the point
of paper trading is to test the execution logic, not just the signal; a separate "simulator"
code path would validate nothing.

### D5. The backtester and the paper trader use the SAME fill engine and fee models
`app/execution/fill_engine.py::SimulatedFillEngine.fill(order, book, now) -> Fill|None` walks
the book level by level, honors `min_size`/`tick_size`, applies the venue `FeeModel`, and
returns partial fills. `engine.py` replaces `_apply_slippage` with it. Rationale: an edge that
survives backtest but not paper (or vice versa) should be a data problem, never a "the two
simulators disagree" problem.

### D6. Backtest integrity fixes are prerequisites for trusting ANY result
- **Fill at next snapshot** (`BacktestConfig.fill_at: Literal["same","next"] = "next"`):
  the signal computed on snapshot N is filled against snapshot N+1's book for that market.
  Same-snapshot fill is available for diagnostics but is not the default.
- **Resolution settlement**: the replayer emits `ResolutionEvent(market_id, outcome, ts)` from
  `Market.is_resolved/resolution_outcome/resolved_at`; the engine settles positions at 1.00/0.00
  minus `redemption_gas_usd`, after `settlement_delay_hours` (capital stays locked). Unresolved
  positions at the end are marked to last price AND reported separately as `unrealized_at_end`.
- **Survivorship coverage report**: `BacktestResult.coverage` = markets seen, markets with
  resolution data, markets closed-without-resolution, snapshot density per market. The API
  surfaces it. A result with resolution coverage < 80% is labeled `low_resolution_coverage`.
- **Look-ahead guard**: snapshots carry only fields known at `ts`; `resolution_outcome`,
  `outcome_prices` (current), `is_resolved` are never attached to a `MarketSnapshot`. A
  regression test constructs a market whose future price is extreme and asserts the fill/PnL
  at N is invariant to what happens at N+2.
- **tz-aware everywhere**: `app/utils/time.py::utcnow()` returns aware UTC; naive datetimes are
  rejected at `BacktestConfig` and `MarketSnapshot` construction.
- **Depth**: `PriceHistory` stores only top-of-book. Until `book_snapshots` (D10) accumulate,
  the replayer synthesizes a one-level book with size = `liquidity_fraction × volume_24h`
  (config, default 0.02) and the result is tagged `depth_source="synthetic"`. Results tagged
  synthetic are labeled as such in every report. This is honest, not hidden.

### D7. Multi-leg `Intent` replaces the one-leg `Signal` as the execution unit
`app/strategies/base.py` gains `Leg(venue, market_id, outcome, side, limit_price, size_contracts|None,
size_usd|None)` and `Intent(kind: single|complement|bundle|cross_venue, legs, hold_to_resolution,
atomicity: all_or_none|best_effort, expected_resolution_ts, confidence, metadata)`. `Signal` stays
(back-compat) and the engine normalizes it to a one-leg `Intent`. `on_market_data` may return
`Signal | Intent | None`. `binary_complement_arbitrage` emits YES+NO legs on the same venue;
`multi_outcome_bundle_arbitrage` emits N legs; cross-venue emits YES on venue A + NO on venue B.
Position ids become `f"{venue}:{market_id}:{outcome}"`. Rationale: an "arbitrage" with one leg
is a directional bet; the current backtest numbers for the arb strategies are meaningless.

### D8. Cross-venue arbitrage is complement-to-resolution only, and consumes approved links only
The riskless form on prediction markets is: buy YES on A at `ask_A`, buy NO on B at `ask_B`,
hold both to resolution; profit iff `ask_A + ask_B + fee_A + fee_B + 2×redemption_gas < 1.00`
AND both contracts resolve on the same fact. "Buy on A, sell on B" is not available (no naked
shorts). Sizing is bounded by `min(available_A, available_B)` in each venue's own ledger —
capital is not moved to chase an edge. Link confidence < 1 applies a resolution-mismatch
haircut: `expected_profit ×= p_same_resolution` and the worst case (one leg loses, the other
does not pay) is reported as `max_loss`. Rationale: see §5 R1.

### D9. Event equivalence is a reviewed, persisted, confidence-scored subsystem
`event_links(id, venue_a, market_a, venue_b, market_b, outcome_map JSON, confidence, evidence JSON,
status: proposed|approved|rejected, reviewed_by, reviewed_at, notes)` + migration. Candidate
generation: normalized-title token overlap (Jaccard on stemmed tokens after date/number
removal), close-time proximity (|Δ| ≤ 48h scores high), numeric-threshold agreement ("≥ 100k"
vs "above 100,000"), and resolution-source agreement (both cite the same source string). A
deterministic score in [0,1]; anything ≥ 0.5 is written as `proposed`. The review API returns
both `rules_text`s side by side. **Nothing trades on a `proposed` link.** In paper mode a
`proposed` link with confidence ≥ 0.85 may be *scored* (shown in opportunities with
`link_status="proposed"`) but the router refuses to execute it.

### D10. Time-to-resolution is a scoring axis everywhere, plus a dedicated near-resolution path
`app/services/scoring.py::score(intent, context) -> OpportunityScore(net_edge, annualized_return,
hours_to_resolution, fill_confidence, resolution_risk, capital_lockup_usd, composite)`.
`annualized_return = net_edge / max(hours_to_resolution, min_hours) × 8760`. The near-resolution
scanner (`hours_to_resolution ≤ near_resolution_hours`, default 72) runs the same strategies
but with distinct risk handling: (a) liquidity-collapse check (spread widening vs 24h median),
(b) dispute-window awareness (Polymarket: a proposed-but-challengeable outcome; Kalshi:
`settlement_timer_seconds` and `expected_expiration_time`), (c) "outcome already determined"
detection = price ≥ 0.97 or ≤ 0.03 with the underlying event date passed → treated as a
capital-lockup trade whose return is the residual minus fees, ranked by annualized return, and
(d) a hard cap on capital in the near-resolution bucket. `settlement_edge.py` is rewritten around
(c); its current keyword heuristics for "ambiguity" are dropped.

### D11. Costs are configuration, not literals
`Settings` gains: `polymarket_taker_fee_overrides: dict[str,float]`, `kalshi_taker_fee_rate`
(default 0.07), `kalshi_maker_fee_rate` (0.0), `redemption_gas_usd` (0.05),
`transfer_latency_hours` (72), `transfer_cost_usd` (5.0), `liquidity_fraction` (0.02),
`near_resolution_hours` (72), `settlement_delay_hours` (24), `min_hours_for_annualization` (6).
Every fee is computed by `app/venues/fees.py::FeeModel` from the venue + market + side +
liquidity role; strategies never carry a fee number.

### D12. Capital sweep is a first-class output
`app/services/backtesting/sweep.py::run_sweep(strategy, config, capital_levels)` runs the
backtest once per level (default `[500, 2_000, 10_000, 50_000, 250_000]`) with depth-walking
fills and returns `EdgeDecayReport(rows: [CapitalRow(capital, net_return, annualized,
fill_rate, avg_slippage_bps, pct_intents_downsized, capital_utilization, trades)], edge_dies_at:
float|None, depth_source)`. `edge_dies_at` = the smallest level at which annualized net return
falls below `min_viable_annualized` (config, default 0.05) — `None` if it never dies within the
sweep, with the caveat printed that the sweep ceiling is not proof. Rationale: an edge that
lives at $500 and dies at $50k is a different product; the user wants that visible, not buried.

### D13. Live fence is structural
`Settings.trading_mode: Literal["paper","live"] = "paper"`; `live_trading_confirmation: str = ""`;
`kill_switch_path: str = "TRADING_KILL_SWITCH"` (file presence halts all order placement);
`max_order_notional_usd` (250), `max_daily_loss_usd` (100), `max_open_notional_usd` (1000).
`app/execution/fences.py::assert_live_allowed()` is called in every live adapter constructor.
`tests/test_fences.py` walks the AST of `backend/app` and fails if any module other than
`app/venues/polymarket/live.py` and `app/venues/kalshi/live.py` references
`post_order`, `create_order`, `/portfolio/events/orders`, or `ClobClient(` — the fence is a test,
so it cannot rot silently.

### D14. Roster and model pins — from the routing ledger, cited
Cross-kit evidence (`python3 bin/routing_scorecard.py --history/--roles`, 33 kits): sonnet 216
pins / 90% first-try / 0% escalation; opus 59 / 91%; haiku 42 / 91%. **Verifier precision: haiku
60% (28 events) vs sonnet 89% (73 events) → the verifier is pinned to sonnet.** Reviewer on opus:
81% precision, 262 findings — kept on opus. test-author: 19 dispatches, 86% precision, **89%
marginal-catch rate — the highest measured marginal value of any role**, on a repo with zero
runnable tests → declared. red-team declared because arbitrage math and fill assumptions need
attack beyond the acceptance lines. second-verifier declared with a stated DIFFERENT lens:
**financial correctness** — fee sign and rounding direction, probability-vs-cents units,
per-leg vs per-intent sizing, settlement off-by-one (who is paid when), tz-aware comparisons.
security-auditor declared because the tree holds a private key, exchange API secrets, and a
path to real money. docs-editor NOT declared (4 dispatches, 17% precision — insufficient and
weak); documentation is an explicit haiku task instead. scout NOT declared — this PLAN.md is the
map. synthesizer NOT declared — the run's lessons go through the reviewer's phase notes.
Implementer pins: opus on the seven tasks where a wrong micro-decision silently books a loss
(fill engine, engine multi-leg/next-fill, resolution+survivorship, Kalshi adapter, order router,
event matcher, cross-venue arb); sonnet elsewhere; haiku for pure deletion/doc edits.

---

## 5. Risks and tripwires

**R1 — Event equivalence is the core technical problem, and most apparent cross-venue edges are
not real.** Kalshi's "Will X happen by D?" and Polymarket's nearest market differ in resolution
source, wording ("official announcement" vs "credible reporting"), timezone cutoff (ET midnight vs
UTC), and edge cases (ties, postponements, "annulled" handling). Two contracts that look
identical can settle differently — then the "arb" is a pair of naked bets. *Tripwire:* if any
task finds itself writing an automatic string-match that feeds the router, STOP; the matcher
proposes, a human approves (D9). *Tripwire:* if the opportunity scanner reports a cross-venue
edge > 8% net, treat it as a probable link error first and surface the two rules texts.

**R2 — Settlement/resolution risk is not price risk.** Being right and being paid are different
events (UMA disputes, Kalshi determination timing, market voided/annulled, venue insolvency).
*Tripwire:* every intent with `hold_to_resolution=True` must carry `expected_resolution_ts` and a
`resolution_risk` component; a scorer that returns `resolution_risk=0` for a real market is a bug.

**R3 — Real costs kill paper arbs.** Polymarket taker fees are no longer zero (0.04–0.07 ×
p(1−p)); Kalshi's fee is nonlinear in price and peaks at p=0.5; redemption gas; transfer latency
means capital on the wrong venue is dead capital. *Tripwire:* any strategy `DEFAULT_CONFIG` that
still contains a fee literal after Phase 1 is a defect. *Tripwire:* an arb whose edge disappears
when `liquidity_role="taker"` on both legs is not an arb — report it as `maker_only`.

**R4 — Depth, not top-of-book.** Best bid/ask is often a few hundred dollars deep. *Tripwire:*
`SimulatedFillEngine` must return a partial fill, never silently fill the whole size at the top
level; the sweep's `pct_intents_downsized` must be > 0 at the top capital level on the synthetic
fixture or the fixture is too deep.

**R5 — Backtest integrity.** A strategy that "works" in a biased backtest is worse than none.
*Tripwire:* until T08/T09 land, no backtest number from this repo is quoted anywhere. *Tripwire:*
`fill_at="same"` results are always labeled `diagnostic`.

**R6 — Capital non-fungibility.** *Tripwire:* the router's `CapitalLedger` is per venue; any code
path that sums balances across venues to size an order is a defect.

**R7 — The live path.** *Tripwire:* GUARDRAILS §1. If a test needs a "real" adapter, it uses a
fixture transport (`httpx.MockTransport`) — never the network.

**R8 — Kalshi API drift.** The V2 order shape and fixed-point dollar strings are recent; fields
may move again. *Tripwire:* the adapter parses BOTH `orderbook_fp` (dollars) and legacy
`orderbook` (cents); every parser has a fixture; on an unrecognized payload it raises
`VenuePayloadError` with the raw body attached rather than guessing.

**R9 — Naive datetimes.** *Tripwire:* a `TypeError: can't compare offset-naive and offset-aware`
anywhere is a task defect, not an environment quirk.

**R10 — Scope creep into the frontend or into new strategies.** *Tripwire:* §2.

**R11 — Alembic without a database.** Executors verify migrations with `alembic upgrade head --sql`
(offline). *Tripwire:* a migration that uses `op.execute` with Postgres-only DDL must be wrapped so
offline SQL generation still succeeds (the existing hypertable block is the pattern).

---

## 6. Phase map (details in TASKS.md)

- **Phase 0 — Foundation repair**: models import, tests run on SQLite, mimicry deleted, migration 002.
- **Phase 1 — Costs, venue seam, backtest integrity**: types+Protocol, fee models, fill engine,
  engine multi-leg + next-fill, resolution + coverage, strategy refactor, Polymarket adapter, Kalshi adapter.
- **Phase 2 — Execution (paper first)**: fences + settings, persisted order/trade/position with venue
  columns (migration 003), OrderRouter + PaperVenueAdapter + reconciliation, API wiring.
- **Phase 3 — Edge discovery**: event links (migration 004), cross-venue complement arb, scoring +
  opportunity scanner, near-resolution path, book-snapshot collection (migration 005).
- **Phase 4 — Measurement and surface**: capital sweep + EdgeDecayReport, frontend opportunities +
  edge-decay table, docs.

The repo is never left broken between tasks: every task's verify command runs the full test
suite, and no task deletes a symbol another still-live task imports without replacing it in the
same task.
