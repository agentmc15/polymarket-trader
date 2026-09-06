# market-edge — TASKS

Read `PLAN.md` and `GUARDRAILS.md` before any task. Paths below are relative to the repo root
`/Users/michaelcave/Developer/reposV2/polymarket-trader` unless they start with `backend/` or
`frontend/`, in which case they are exactly that. All Python verify commands run from
`backend/` with the `python3` on PATH (`/opt/anaconda3/bin/python3`, 3.12).

## Dispatch preamble

- Status vocabulary: `pending` / `in-progress` / `done` / `blocked`.
- `model:` is authoritative at dispatch. `depends:` lists ids that must be `done` first.
  `independent: yes` means the task may run in parallel with anything else not depending on it.
- **Warm-cluster candidates** (serial chains sharing a primary file and model pin — one continued
  implementer may serve the chain):
  - T08 → T09 (both opus, both primarily `backend/app/services/backtesting/engine.py` +
    `data_replay.py`).
  - T13 → T15 (both sonnet; `config.py` then `models/`). T14 and T16 are separate.
  - T19 → T20 (both sonnet; `services/scoring.py` / `services/scanner.py`).
- The full suite `python3 -m pytest -q` is part of every verify command from T02 onward. A task
  that makes an unrelated existing test fail is not done.
- Every task's verify must be able to FAIL. If you find one that cannot (tautological) or cannot
  run (missing helper, missing dep), record `defect: <id> kind=<tautological-verify|unrunnable-verify>`
  in NOTES.md and stop rather than declaring pass.
- Fee/price/size units: prices are probabilities in [0,1]; sizes are contracts (each pays $1.00 if
  it wins); dollars are floats rounded only at the venue boundary. State units in every docstring.
- Datetimes are tz-aware UTC. Use `app.utils.time.utcnow()`; never `datetime.utcnow()`.

---

## Phase 0 — Foundation repair

### T01 — Make `app.models` importable; portable JSON; FK/relationship fixes; `utcnow`
- status: done
- model: sonnet
- depends: —
- independent: no (everything depends on this)

**Brief.** `python3 -c "import app.models"` currently raises
`InvalidRequestError: Attribute name 'metadata' is reserved when using the Declarative API`.
Nine columns named `metadata` exist: `models/market.py` (Market), `models/trade.py` (Order, Trade),
`models/position.py`, `models/strategy.py`, `models/backtest.py` (Backtest, BacktestTrade),
`models/trader.py` (Trader, TraderFollow — these two files are deleted in T03; fix them anyway so
T02's tests can import the package in between). Rename every one to `extra_data: Mapped[dict]`
with `mapped_column("extra_data", JSONDict, default=dict)`. Do not rename the Python attribute to
anything else — `extra_data` is the name every later task uses.

Add to `models/base.py`:
```python
from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB
JSONDict = JSON().with_variant(JSONB(), "postgresql")
JSONList = JSON().with_variant(JSONB(), "postgresql")
```
and replace every direct `JSONB` column type in `models/*.py` with `JSONDict` (dict-shaped) or
`JSONList` (list-shaped). Keep `from sqlalchemy.dialects.postgresql import JSONB` out of the model
modules after this change (grep must find it only in `base.py`).

Fix the relationship that will fail at mapper configuration: `MarketPrice.market_id` in
`models/market.py` has no `ForeignKey` but `Market.prices` / `MarketPrice.market` declare a
relationship pair → add `ForeignKey("markets.id")`. Check every other `relationship(` in
`models/` the same way (`Backtest.strategy_id` already has an FK; `Trade.order_id` has one).

Fix `database.py::init_db` to `await conn.execute(text("SELECT 1"))`.

Create `backend/app/utils/time.py` with `utcnow() -> datetime` (aware UTC) and
`ensure_aware(dt: datetime) -> datetime` (raises `ValueError("naive datetime")` if `dt.tzinfo is None`).
Do not yet change callers of `datetime.utcnow()` outside `models/` — T08 does the engine.

Read `docs` for conventions: Google docstrings, type hints on all defs, black line length 88.

**Acceptance.**
1. `python3 -c "import app.models, app.main"` exits 0.
2. `grep -rn "metadata: Mapped" app/models/` returns nothing; `grep -rn "extra_data" app/models/ | wc -l` ≥ 9.
3. `grep -rln "postgresql import JSONB" app/models/` prints exactly `app/models/base.py`.
4. `python3 -c "from sqlalchemy.orm import configure_mappers; import app.models; configure_mappers()"` exits 0.
5. `python3 -c "from app.utils.time import utcnow, ensure_aware; assert utcnow().tzinfo is not None"` exits 0.
6. `ruff check app/models app/utils app/database.py` reports no findings introduced by this task (compare against `git stash`-free baseline: these files must be clean).

**Verify.**
```bash
cd backend && python3 -c "import app.models, app.main; from sqlalchemy.orm import configure_mappers; configure_mappers(); from app.utils.time import utcnow; assert utcnow().tzinfo" && test -z "$(grep -rn 'metadata: Mapped' app/models/)" && test "$(grep -rln 'postgresql import JSONB' app/models/)" = "app/models/base.py" && ruff check app/models app/utils app/database.py
```

---

### T02 — Runnable test infrastructure on SQLite, no network
- status: done
- model: sonnet
- depends: T01
- independent: no

**Brief.** `backend/tests/conftest.py` is unrunnable: `aiosqlite` is not installed;
`AsyncClient(app=app)` was removed in httpx 0.27 (use `httpx.ASGITransport(app=app)`); the custom
`event_loop` fixture is unsupported by pytest-asyncio 1.3 (delete it; `asyncio_mode = "auto"` is
already set in `pyproject.toml`).

1. Add `aiosqlite>=0.20.0` to `backend/requirements.txt` under `# Testing` and `pip install aiosqlite`.
2. Rewrite `conftest.py`: engine `sqlite+aiosqlite:///:memory:` with
   `poolclass=StaticPool, connect_args={"check_same_thread": False}` so one in-memory DB survives
   across connections; `Base.metadata.create_all` per test function; session fixture; `client`
   fixture using `ASGITransport` and overriding `app.database.get_async_session`. Keep
   `sample_market_data` / `sample_order_data` fixtures. Set env `TRADING_MODE=paper` in a
   session-scoped autouse fixture via `monkeypatch`-free `os.environ.setdefault` at import time
   (Settings is cached with `lru_cache` — tests must never construct a live adapter).
3. Create `backend/tests/helpers.py` with `make_snapshot(market_id="m1", ts=None, yes=0.5, no=0.5,
   spread=0.02, volume_24h=50_000.0, end_date=None, **kw) -> MarketSnapshot` (aware UTC `ts`
   default = `utcnow()`), and `make_book(bids, asks, venue="polymarket", market_id="m1",
   outcome="YES")` returning a plain dict until T05 introduces `OrderBook` (T05 updates it).
4. Create `backend/tests/test_smoke.py`: (a) `GET /health` == 200; (b) `GET /api/v1/backtests/strategies`
   returns 200 and ≥ 9 strategies; (c) `Base.metadata.create_all` on SQLite succeeds for every table
   (this is the JSON-portability check); (d) `InMemoryDataReplayer` + `Backtester` run
   `favorite_compounder` over `create_sample_snapshots(...)` with **aware** datetimes and return a
   `BacktestResult` (this will surface any naive/aware bug in the engine — if it fails for THAT
   reason, mark the test `xfail(strict=True, reason="T08")` rather than papering over it).
5. Add `pytest-cov` is already in requirements; do not add coverage gates.

**Acceptance.**
1. `python3 -m pytest -q` exits 0 with ≥ 4 tests collected and 0 errors.
2. `python3 -m pytest -q -p no:cacheprovider -W error::DeprecationWarning tests/test_smoke.py` may
   emit warnings from third parties but must not fail on the repo's own code (filterwarnings in
   pyproject handles DeprecationWarning; leave it).
3. `grep -n "aiosqlite" requirements.txt` finds one line.
4. No test opens a socket: `python3 -m pytest -q --disable-socket` is NOT available (no plugin);
   instead `grep -rn "http[s]*://" tests/` must return only `http://test` base URLs.

**Verify.**
```bash
cd backend && python3 -m pytest -q && grep -q aiosqlite requirements.txt && test "$(grep -rhoE 'https?://[^"'"'"' ]+' tests/ | grep -v '^http://test' | wc -l | tr -d ' ')" = "0"
```

---

### T03 — Delete trader mimicry end to end; migration 002
- status: done
- model: sonnet
- depends: T02
- independent: no

**Brief.** Remove copy-trading from backend, frontend, docs, and skills. PLAN.md D2 has the
rationale — reproduce it in the migration docstring and the CLAUDE.md edit.

DELETE these files: `backend/app/models/trader.py`, `backend/app/models/tracked_trader.py`,
`backend/app/strategies/whale_copy_trading.py`, `backend/app/api/routes/traders.py`,
`backend/app/tasks/trader_tracking.py`, `frontend/src/hooks/useTraders.ts`,
`.claude/skills/trader-analysis/SKILL.md` (and its directory).

EDIT:
- `backend/app/models/__init__.py`: drop `Trader`, `TraderFollow`, `TrackedTrader` imports/exports. Keep `TradeHistory`, `TradeSide`, `TradeOutcome`.
- `backend/app/strategies/__init__.py`: drop the whale import, registry entry, DEFAULT_CONFIGS entry, the `"copy_trading"` category, and the `__all__` entry. Registry must have exactly 9 keys after.
- `backend/app/models/strategy.py`: remove `StrategyType.COPY_TRADING`.
- `backend/app/tasks/__init__.py`: remove `"app.tasks.trader_tracking"` from `include` and the `track-traders-every-hour` beat entry.
- `backend/app/tasks/bot_execution.py`: delete `execute_copy_trade`.
- `backend/app/api/__init__.py`: remove the `traders` import and `include_router` line.
- `backend/app/services/data_collector.py`: in `PolymarketDataClient` delete `get_leaderboard`, `get_trader`, `get_trader_trades`, `get_trader_positions` (KEEP `get_market_trades` — the tape); in `DataCollector` delete `update_trader_leaderboard` and `get_whale_trades`; remove the `TrackedTrader` import; update the class docstring.
- `frontend/src/services/api.ts`: remove the `// Traders` block (6 methods) and the `Trader`, `TraderFollow` imports; the file-local `interface Trade` stays if `getBotTrades` still uses it.
- `frontend/src/types/index.ts`: remove `Trader`, `TraderFollow`, and `'COPY_TRADING'` from `StrategyType`.
- `frontend/src/App.tsx`: remove `'traders'` from the tab union and array and its content block.
- `frontend/src/components/backtesting/StrategySelector.tsx`: remove the two `copy_trading` map entries.
- `CLAUDE.md`: remove "trader shadowing", the `trader.py`/`traders.py`/`trader_analysis.py`/`copy_trading.py`/`trader_tracking.py`/`traders/` tree lines, the two `/api/v1/traders` endpoint lines, the `trader:activity` event, and the "Data API (unofficial) — Trader data, leaderboards" item. Add one sentence under Project Overview: "Copy-trading was removed deliberately: per-wallet samples on prediction markets are too small and too correlated to separate skill from variance."
- `README.md`: remove the `trader-analysis` skill bullets and tree line.
- `.claude/commands/add-strategy.md`: remove the "Copy Trading: Following other traders" classification line.
- `.claude/skills/trading-strategies/SKILL.md`: delete the copy-trading strategy section (the class whose docstring is "Mirror trades of successful traders." and its helper methods) — nothing else in that file.

MIGRATION: `backend/alembic/versions/20260904_000100_002_drop_trader_mimicry.py`, `revision="002"`,
`down_revision="001"`. `upgrade()`: drop the five `tracked_traders` indexes then the table (mirror
`001`'s `downgrade` block for that table), then `op.execute("DROP TABLE IF EXISTS trader_follows")`
and `op.execute("DROP TABLE IF EXISTS traders")`. `downgrade()`: recreate `tracked_traders`
exactly as `001` does (copy the block). Module docstring carries the D2 rationale.

**Acceptance.**
1. `grep -rniE "whale|TrackedTrader|copy_trad|leaderboard|TraderFollow|trader_tracking|trader-analysis" backend/app frontend/src .claude CLAUDE.md README.md` returns nothing. (`README.md`'s `mkdir polymarket-trader` lines are fine — the pattern above does not match them.)
2. `python3 -c "from app.strategies import STRATEGIES; assert len(STRATEGIES)==9 and 'whale_copy_trading' not in STRATEGIES"` exits 0.
3. `cd backend && alembic upgrade head --sql 2>/dev/null | grep -c "DROP TABLE"` ≥ 3.
4. `alembic heads` prints `002 (head)`.
5. `python3 -m pytest -q` passes.
6. The frontend still type-checks: `cd frontend && npm ci --no-audit --no-fund && npx tsc -p tsconfig.app.json --noEmit` exits 0.

**Verify.**
```bash
cd backend && test -z "$(grep -rniE 'whale|TrackedTrader|copy_trad|leaderboard|TraderFollow|trader_tracking|trader-analysis' app ../frontend/src ../.claude ../CLAUDE.md ../README.md)" && python3 -c "from app.strategies import STRATEGIES; assert len(STRATEGIES)==9" && alembic heads | grep -q '^002' && test "$(alembic upgrade head --sql 2>/dev/null | grep -c 'DROP TABLE')" -ge 3 && python3 -m pytest -q && (cd ../frontend && npm ci --no-audit --no-fund >/dev/null && npx tsc -p tsconfig.app.json --noEmit)
```

---

## Phase 1 — Costs, venue seam, backtest integrity

### T04 — Venue types and the `VenueAdapter` protocol
- status: done
- model: sonnet
- depends: T03
- independent: no

**Brief.** Create `backend/app/venues/__init__.py`, `types.py`, `base.py`, `registry.py` (registry
holds only the lookup function and an empty `_ADAPTERS` dict for now; T11/T12/T14 register).
Implement exactly the types in PLAN.md D3 as frozen dataclasses (or pydantic models — pick
dataclasses to match `strategies/base.py`), with validation: prices in `[0.0, 1.0]`, sizes ≥ 0,
timestamps aware (use `ensure_aware`). `OrderBook` gets helpers: `best_bid()`, `best_ask()`,
`mid()`, `depth_at(price, side) -> float`, `walk(side, size) -> list[tuple[price,size]]` (levels
consumed to fill `size`, best first; returns fewer if the book runs dry). `VenueAdapter` is a
`typing.Protocol` with the methods listed in D3; `stream_books` raises `NotImplementedError` in
a provided `BaseAdapter` mixin. Define `VenueError`, `VenuePayloadError(raw: Any)`,
`VenueAuthError`, `VenueRateLimited(retry_after_s: float|None)`.

Update `tests/helpers.py::make_book` to return an `OrderBook`. Write `tests/venues/test_types.py`
covering: validation rejects price 1.2 / naive ts; `walk` consumes across levels and returns
partial when dry; `best_ask` of empty asks is `None`.

**Acceptance.**
1. `python3 -c "from app.venues.types import OrderBook, BookLevel, VenueMarket, OrderRequest, Fill; from app.venues.base import VenueAdapter, BaseAdapter"` exits 0.
2. `walk("buy", 150)` on asks `[(0.40, 100), (0.41, 100)]` returns `[(0.40,100),(0.41,50)]`; on `[(0.40,100)]` returns `[(0.40,100)]` (partial).
3. `ruff check app/venues && mypy app/venues` clean.
4. Full suite passes.

**Verify.**
```bash
cd backend && ruff check app/venues && mypy app/venues && python3 -m pytest -q tests/venues/test_types.py && python3 -m pytest -q
```

---

### T05 — Fee models and cost settings
- status: done
- model: sonnet
- depends: T04
- independent: no

**Brief.** Create `backend/app/venues/fees.py` with `FeeSchedule(taker_rate: float, maker_rate:
float, source: str)` and `FeeModel` (ABC) → `PolymarketFeeModel`, `KalshiFeeModel`, each with
`fee(price: float, size_contracts: float, liquidity: Literal["maker","taker"],
schedule: FeeSchedule) -> float` returning **dollars, non-negative**.

- Polymarket: `size × rate × p × (1 − p)`; makers pay 0 regardless of schedule. Category default
  table lives in `POLYMARKET_CATEGORY_TAKER_RATES` (values from PLAN.md §3 Venue facts) with a
  `category_rate(category: str|None) -> float` normalizer (case-insensitive; unknown → 0.05;
  `"geopolitics"` → 0.0). Docstring cites `https://docs.polymarket.com/trading/fees` and the
  fetch date 2026-09-04.
- Kalshi: `size × rate × p × (1 − p)`, then `ceil` to $0.000001 per fill (docs: `trade_fee =
  ceil_6dp(model_fee)`), then an optional `round_net_to_cents: bool` (default True) that
  represents the non-direct-member $0.01 floor on the fill's net. Defaults `taker_rate=0.07`,
  `maker_rate=0.0` come from `Settings`, never literals in this module's public API (module
  constants are allowed only as documented fallbacks). Docstring MUST say: "Re-confirmed against
  <URL> on <date>" — if the implementer cannot reach the Kalshi fee page, write "NOT re-confirmed
  on 2026-09-04; 0.07 is Kalshi's historically published standard taker rate" verbatim.

Add to `backend/app/config.py::Settings` (with env aliases in UPPER_SNAKE): `kalshi_taker_fee_rate:
float = 0.07`, `kalshi_maker_fee_rate: float = 0.0`, `polymarket_taker_fee_overrides: dict[str,
float] = {}` (JSON env), `redemption_gas_usd: float = 0.05`, `transfer_latency_hours: float = 72`,
`transfer_cost_usd: float = 5.0`, `liquidity_fraction: float = 0.02`, `near_resolution_hours: float
= 72`, `settlement_delay_hours: float = 24`, `min_hours_for_annualization: float = 6`,
`min_viable_annualized: float = 0.05`. Add all to `.env.example` under a new `# COSTS` block, commented.

`tests/venues/test_fees.py` with worked examples (state expected numbers in the test, computed by
hand): Polymarket taker 100 contracts @0.60, rate 0.05 → `100×0.05×0.6×0.4 = 1.20`; maker → 0;
Kalshi taker 100 @0.50 rate 0.07 → `1.75`; Kalshi 1 contract @0.99 → `0.000693 → ceil6dp
0.000693`; fee is symmetric in p ↔ 1−p; fee at p=0.5 is the maximum over a grid.

**Acceptance.**
1. The four worked examples pass to 1e-9 (Kalshi ceil case exact).
2. `grep -rn "fee_rate\"\?: *0\.0\|kalshi_fee_rate\|polymarket_fee_rate" app/strategies/` — this grep is NOT yet required to be empty (T10 does that); record its count in NOTES.md.
3. `python3 -c "from app.config import settings; assert settings.kalshi_taker_fee_rate == 0.07"` exits 0.
4. `ruff check app/venues app/config.py && mypy app/venues app/config.py` clean; suite passes.

**Verify.**
```bash
cd backend && python3 -m pytest -q tests/venues/test_fees.py && python3 -c "from app.config import settings; assert settings.kalshi_taker_fee_rate==0.07 and settings.redemption_gas_usd>0" && ruff check app/venues app/config.py && mypy app/venues app/config.py && python3 -m pytest -q
```

---

### T06 — `Intent`/`Leg` in the strategy base; engine accepts both
- status: done
- model: sonnet
- depends: T05
- independent: no

**Brief.** In `backend/app/strategies/base.py` add `Leg` and `Intent` exactly per PLAN.md D7
(dataclasses; `Leg.venue: VenueId` default `"polymarket"`; exactly one of `size_contracts` /
`size_usd` may be set at construction, both may be None → sized later by
`calculate_position_size`). `Intent.__post_init__` validates: ≥ 1 leg; `kind == "complement"`
⇒ 2 legs, same venue+market, outcomes {YES, NO}; `kind == "cross_venue"` ⇒ 2 legs on different
venues; `kind == "bundle"` ⇒ ≥ 3 legs same market, distinct outcomes; `expected_resolution_ts`
aware or None; `confidence ∈ [0,1]`. Add `Signal.to_intent() -> Intent` (one leg, `kind="single"`).
`on_market_data` return annotation becomes `Signal | Intent | None`. `MarketSnapshot` gains
`venue: VenueId = "polymarket"` and `book: OrderBook | None = None` (import from
`app.venues.types`), and `__post_init__` calls `ensure_aware(self.timestamp)` — this is the
naive-datetime tripwire (R9). Fix `create_sample_snapshots` in `data_replay.py` to accept and
produce aware datetimes. `Backtester._process_snapshot`: normalize a `Signal` to `Intent` via
`to_intent()`; for now the engine still executes only `legs[0]` (T08 makes it multi-leg) — but it
MUST log a warning `"multi-leg intent executed as single leg (pre-T08)"` when `len(legs) > 1`,
so the intermediate state is loud.

Tests `tests/strategies/test_intent.py`: validation cases above; `to_intent` round-trip; a
`MarketSnapshot` with naive `timestamp` raises `ValueError`.

**Acceptance.**
1. All validation cases in the brief have a passing test.
2. `python3 -c "from app.strategies.base import Intent, Leg, Signal, MarketSnapshot"` exits 0.
3. The T02 smoke backtest still passes (if it was `xfail(strict)` for tz reasons, it now must pass — remove the xfail).
4. `ruff check app/strategies/base.py && mypy app/strategies/base.py` clean; suite passes.

**Verify.**
```bash
cd backend && python3 -m pytest -q tests/strategies/test_intent.py && test -z "$(grep -n 'xfail' tests/test_smoke.py)" && ruff check app/strategies/base.py && mypy app/strategies/base.py && python3 -m pytest -q
```

---

### T07 — `SimulatedFillEngine` (depth-walking, partial fills, fees)
- status: done
- model: opus
- depends: T06
- independent: no

**Brief.** Create `backend/app/execution/__init__.py` and `fill_engine.py`.
`SimulatedFillEngine(fee_models: dict[VenueId, FeeModel], schedules: Callable[[VenueId, str], FeeSchedule],
latency_ms: int = 0, rng_seed: int | None = None)`.
`fill(order: OrderRequest, book: OrderBook, now: datetime) -> FillResult` where
`FillResult(fills: list[Fill], filled_size: float, remaining_size: float, avg_price: float|None,
total_fee: float, status: filled|partial|unfilled, levels_consumed: int)`.
Rules: BUY walks asks from best upward while `level.price <= order.price` (limit); SELL walks bids
downward while `level.price >= order.price`; respects `tick_size` (limit must be on-tick, else
`ValueError`) and `min_size` (a residual below `min_size` is not filled). `tif == "FOK"` → all-or-nothing;
`"IOC"`/`"GTC"` → partial allowed (GTC residual is reported `remaining`, the engine does not rest
orders — the router does). Fee per fill via the venue `FeeModel` with `liquidity="taker"` (this
engine only models taking liquidity; maker fills are the router's business in T14 and are not
simulated here — say so in the docstring). `latency_ms > 0` is informational for now (recorded on
each Fill as `metadata["latency_ms"]`); T08 uses next-snapshot fills instead.

Also implement `synthesize_book(snapshot: MarketSnapshot, liquidity_fraction: float) -> OrderBook`
for top-of-book-only history: one bid level at `yes_bid` and one ask at `yes_ask` (or `no_*` for
outcome NO), each with `size = liquidity_fraction × volume_24h / price` contracts; tag
`OrderBook.metadata["depth_source"] = "synthetic"`. Real books carry `"recorded"`.

Tests `tests/execution/test_fill_engine.py`: buy 150 against asks `[(0.40,100),(0.41,100)]`
limit 0.41 → filled 150, avg 0.40333…, 2 levels; limit 0.40 → partial 100; FOK with limit 0.40 →
unfilled; sell mirrors; off-tick limit raises; residual below min_size not filled; fee equals
`FeeModel` value summed per level (compare against direct computation); synthetic book sizes.

**Acceptance.**
1. Every rule in the brief has a test; the avg-price case asserts to 1e-9.
2. `FillResult.status == "partial"` never comes with `remaining_size == 0`; `"filled"` never with `remaining_size > 0` (property test over 200 random books, seed fixed).
3. `ruff check app/execution && mypy app/execution` clean; suite passes.

**Verify.**
```bash
cd backend && python3 -m pytest -q tests/execution/test_fill_engine.py && ruff check app/execution && mypy app/execution && python3 -m pytest -q
```

---

### T08 — Engine: multi-leg intents, next-snapshot fills, fill engine, tz-aware
- status: done
- model: opus
- depends: T07
- independent: no

**Brief.** Modify `backend/app/services/backtesting/engine.py` in place (public names preserved —
see PLAN.md §2).
1. `BacktestConfig`: `fill_at: Literal["same","next"] = "next"`; `start_date`/`end_date` must be
   aware (`ensure_aware` in `__post_init__`); add `liquidity_fraction: float | None = None`
   (None → `settings.liquidity_fraction`); keep `fee_rate` for back-compat but mark it deprecated in
   the docstring — fees now come from `FeeModel` via the fill engine; when `fee_rate > 0` is passed
   explicitly, it is applied IN ADDITION (documented) so old callers don't silently lose their fee.
2. Replace `_apply_slippage` usage with `SimulatedFillEngine` (constructed in `__init__` from the
   venue fee models; schedule resolver uses `snapshot.category` → Polymarket category rate, or the
   Kalshi settings). Keep the `SlippageModel` enum importable (the API imports it) but document that
   only `NONE` and `FIXED` still have meaning: `FIXED` adds `slippage_value` to the limit price as
   a conservative pad; others map to `NONE` with a warning.
3. Multi-leg: `_execute_intent(intent, snapshot)`; `atomicity == "all_or_none"` ⇒ compute fills
   for every leg first (against each leg's book), and if any leg is `unfilled` OR any leg's
   `filled_size < leg size × (1 − partial_tolerance)` (config, default 0.0), execute NOTHING and
   record `intent_rejections += 1` with reason; otherwise commit all fills atomically (cash
   deducted for all legs). `best_effort` ⇒ commit whatever filled. For a `complement`/`bundle`
   intent the cost basis is per leg; the position id is `f"{venue}:{market_id}:{outcome}"`.
   Sizing: if the strategy returns `size_usd` for the intent, split equally across legs in
   contracts = `size_usd / sum(leg limit prices)` (so a complement with asks 0.45+0.48 buys the
   same contract count of each).
4. `fill_at == "next"`: an intent generated on snapshot N for market M is queued as
   `PendingIntent(intent, created_ts)`; it executes when the NEXT snapshot for that same market
   arrives (using that snapshot's book); if `pending_ttl` (config, default 1h) elapses first it
   expires (`intent_expirations += 1`). Cross-venue intents wait for the next snapshot of EACH
   leg's market. `fill_at == "same"` executes immediately and tags every resulting trade
   `metadata["diagnostic_same_snapshot"] = True`.
5. `BacktestResult` gains `intents_generated`, `intents_executed`, `intent_rejections`,
   `intent_expirations`, `depth_source: Literal["synthetic","recorded","mixed"]`, `fill_at`.
6. Replace every `datetime.utcnow()` in `engine.py`, `data_replay.py`, `tasks/backtesting.py`,
   `api/routes/backtesting.py` with `app.utils.time.utcnow()`; `tasks/backtesting.py` parses ISO
   strings with `ensure_aware(datetime.fromisoformat(...))` (a `Z`-suffixed string parses aware in
   3.11+; a bare date does not — make it aware in UTC).
7. `_check_position_exits` currently uses `<=` for both YES and NO stop-loss identically — keep
   behavior but make the price lookup use the position's own outcome price (it already does); no
   semantic change beyond ids.

Tests `tests/backtesting/test_engine_multileg.py`: (a) complement intent with asks 0.45/0.48 and
`fill_at="same"` → two positions of equal contract count, cash reduced by both legs + fees;
(b) same with the NO book too thin under `all_or_none` → zero positions, `intent_rejections == 1`;
(c) `fill_at="next"`: signal on N fills at N+1's ask, not N's — assert the fill price equals
snapshot N+1's ask; (d) an intent whose market never gets another snapshot within `pending_ttl`
expires; (e) look-ahead invariance: two runs whose snapshot streams are identical through N+1
but differ wildly at N+2 produce identical trades through N+1 (compare `trades[:k]` fields).

**Acceptance.**
1. Tests (a)–(e) pass.
2. `grep -n "datetime.utcnow" app/services/backtesting app/tasks/backtesting.py app/api/routes/backtesting.py` returns nothing.
3. The API route still imports (`python3 -c "import app.api.routes.backtesting"`).
4. `ruff check app/services/backtesting && mypy app/services/backtesting` clean (the pre-existing 2 `metrics.py` date-key errors may be fixed by annotating the dict as `dict[date, list[float]]`; do it — it is a 2-line change in a touched package); suite passes.

**Verify.**
```bash
cd backend && python3 -m pytest -q tests/backtesting/test_engine_multileg.py && test -z "$(grep -n 'datetime.utcnow' app/services/backtesting app/tasks/backtesting.py app/api/routes/backtesting.py)" && python3 -c "import app.api.routes.backtesting" && ruff check app/services/backtesting && mypy app/services/backtesting && python3 -m pytest -q
```

---

### T09 — Resolution settlement, survivorship coverage, look-ahead hygiene in replay
- status: done
- model: opus
- depends: T08
- independent: no

**Brief.** `data_replay.py` + `engine.py`.
1. Define `ResolutionEvent(market_id, venue, winning_outcome: str, resolved_at: datetime)` in
   `data_replay.py`. `DataReplayer.__aiter__` yields `MarketSnapshot | ResolutionEvent` in
   timestamp order: for each market in the window, if `Market.is_resolved and Market.resolved_at
   <= end_date`, emit the event at `resolved_at` (interleaved by time with snapshots — keep a small
   heap of pending events). `InMemoryDataReplayer` accepts an optional `resolutions:
   list[ResolutionEvent]` and interleaves the same way.
2. `_get_market_metadata` must NOT attach `resolution_outcome`, `is_resolved`, `resolved_at`, or
   current `outcome_prices` to snapshots (audit: it currently attaches `question`, `category`,
   `end_date`, `resolution_rules` — fine). Add a unit test that constructs a `Market` row with a
   resolution and asserts the emitted `MarketSnapshot` has no such attribute and `resolution_rules`
   is present.
3. Engine: on `ResolutionEvent`, every open position on that market settles at `1.00` if
   `position.outcome == winning_outcome` else `0.00`, minus `settings.redemption_gas_usd` per
   position (config-overridable on `BacktestConfig.redemption_gas_usd`), with the cash credited at
   `resolved_at + settlement_delay_hours`. Implement the delay by holding proceeds in
   `Portfolio.pending_settlements: list[(available_at, amount)]` released as later timestamps
   arrive (and all released at `end_date`). Trades recorded with `side="SETTLE"` and
   `metadata["settlement"]=True`. Strategy callback `on_position_closed` fires with `pnl`.
4. End-of-run: positions still open are marked to last price into `final_value` AND listed in
   `BacktestResult.unrealized_at_end: list[Position]` with their notional; `BacktestResult.coverage:
   CoverageReport(markets_seen, markets_resolved, markets_closed_unresolved, snapshots_per_market:
   dict[str,int], resolution_coverage: float, low_resolution_coverage: bool)` where
   `low_resolution_coverage = resolution_coverage < 0.8`.
5. `tasks/backtesting.py` and `api/routes/backtesting.py`: persist and expose `coverage`,
   `unrealized_at_end` count, `depth_source`, `fill_at`, and the intent counters. `BacktestRun`
   has no columns for these — store them in a new `BacktestRun.report: Mapped[dict]` (`JSONDict`)
   column added in this task with migration
   `backend/alembic/versions/20260904_000200_003_backtest_run_report.py` (`revision="003"`,
   `down_revision="002"`, one `add_column` with `server_default="{}"`), and surface them in
   `BacktestStatusResponse` as `report: dict[str, Any]`.

Tests `tests/backtesting/test_resolution.py`: a YES position settles to +1 per contract minus gas
on a YES resolution; a NO position on the same event settles to 0; a complement pair (YES+NO
bought at 0.45+0.48) nets `1.00 − 0.93 − fees − 2×gas` per contract; proceeds are not spendable
until `settlement_delay_hours` later (an intent between resolution and release that needs the
cash is rejected for insufficient cash); coverage report counts are right on a 3-market fixture
where one market never resolves.

**Acceptance.**
1. All five test scenarios pass; the complement-pair PnL asserts to 1e-9 against a hand computation written in the test.
2. `alembic heads` prints `003 (head)`; `alembic upgrade head --sql | grep -c "report"` ≥ 1.
3. `GET /api/v1/backtests/{id}` response model includes `report`.
4. `ruff check app/services/backtesting app/tasks/backtesting.py app/api/routes/backtesting.py && mypy app/services/backtesting` clean; suite passes.

**Verify.**
```bash
cd backend && python3 -m pytest -q tests/backtesting/test_resolution.py && alembic heads | grep -q '^003' && test "$(alembic upgrade head --sql 2>/dev/null | grep -c report)" -ge 1 && ruff check app/services/backtesting app/tasks/backtesting.py app/api/routes/backtesting.py && mypy app/services/backtesting && python3 -m pytest -q
```

---

### T10 — Strategies emit real multi-leg intents; fee literals removed
- status: done
- model: sonnet
- depends: T09
- independent: no

**Brief.** Refactor in place:
- `binary_complement_arbitrage.py`: return an `Intent(kind="complement", legs=[YES@yes_ask, NO@no_ask],
  hold_to_resolution=True, atomicity="all_or_none", expected_resolution_ts=snapshot.end_date)`.
  Edge = `1 − (yes_ask + no_ask) − fee(YES) − fee(NO) − 2×redemption_gas`, fees from
  `FeeModel` (taker) using `snapshot.category`; signal only if edge ≥ `min_profit_margin`.
  Use the book when `snapshot.book` is present: the "ask" for sizing purposes is the price at
  which `min_position_size` contracts can be bought (walk), not top-of-book. Delete `fee_rate`
  and `use_mid_prices` from `DEFAULT_CONFIG`.
- `multi_outcome_bundle_arbitrage.py`: `kind="bundle"`, one leg per outcome at its ask; edge uses
  per-leg fees; requires every outcome's ask (no "fallback to binary" — a market with < 3 outcomes
  returns None); delete `fee_rate`.
- `favorite_compounder.py`, `no_bias_exploit.py`, `settlement_edge.py`, `term_structure_spreads.py`,
  `catalyst_momentum.py`, `correlation_hedging.py`: return `Intent` via `Signal.to_intent()` (or
  keep returning `Signal` — both are accepted; prefer `Intent` for the ones you touch), remove any
  `*fee*` keys from `DEFAULT_CONFIG`, and set `expected_resolution_ts=snapshot.end_date` where a
  `Signal` becomes an `Intent`.
- `cross_platform_arbitrage.py`: DELETE the file and its registry entries (T18 creates
  `cross_venue_arbitrage.py`). Registry drops to 8 keys until T18 restores 9. Update
  `STRATEGY_CATEGORIES["arbitrage"]`.
- `Settlement/no-bias/favorite` still use `snapshot.yes_price` where the book is absent — fine.

Tests `tests/strategies/test_arbitrage_intents.py`: complement emits two legs with equal implied
contract counts and correct edge given a fee schedule (hand-computed); no intent when fees eat
the gross edge (e.g. asks 0.49/0.49, category crypto, rate 0.07 → gross 0.02, fees ≈ 2×0.07×0.49×0.51
≈ 0.035 → None); bundle emits N legs; a 2-outcome market returns None from bundle.

**Acceptance.**
1. `grep -rnE "fee_rate|fee_rates|_fee_rate|platform_fees" app/strategies/` returns nothing.
2. `python3 -c "from app.strategies import STRATEGIES; assert len(STRATEGIES)==8 and 'cross_platform_arbitrage' not in STRATEGIES"` exits 0.
3. A backtest of `binary_complement_arbitrage` on a fixture with a persistent YES+NO < 1 gap produces trades on BOTH outcomes of the market (`{t.outcome for t in result.trades} ⊇ {"YES","NO"}`).
4. `ruff check app/strategies && mypy app/strategies/base.py app/strategies/binary_complement_arbitrage.py app/strategies/multi_outcome_bundle_arbitrage.py` clean; suite passes.

**Verify.**
```bash
cd backend && test -z "$(grep -rnE 'fee_rate|fee_rates|_fee_rate|platform_fees' app/strategies/)" && python3 -c "from app.strategies import STRATEGIES; assert len(STRATEGIES)==8" && python3 -m pytest -q tests/strategies/test_arbitrage_intents.py && ruff check app/strategies && mypy app/strategies/base.py app/strategies/binary_complement_arbitrage.py app/strategies/multi_outcome_bundle_arbitrage.py && python3 -m pytest -q
```

---

### T11 — Polymarket `VenueAdapter` (read path async over httpx; live order path fenced)
- status: done
- model: sonnet
- depends: T10
- independent: no

**Brief.** Create `backend/app/venues/polymarket/{__init__,adapter,live}.py`.
`PolymarketAdapter(BaseAdapter)` uses `httpx.AsyncClient` (accept an injected `transport` for
tests) against `settings.clob_api_url` and `settings.gamma_api_url`:
- `list_markets`/`get_market`: Gamma `/markets` (+ `/events` for multi-outcome grouping →
  `VenueMarket.event_id`); map `question`, `outcomes`, `clobTokenIds`/`token_ids` → `outcome_ids`,
  `description`+`resolutionSource` → `rules_text`/`resolution_source`, `endDate` → `close_time`,
  `closed`/`resolved` flags → `status`, category → `FeeSchedule` via `category_rate`, overriding
  with the CLOB market's `taker_base_fee`/`maker_base_fee` if the payload carries them (record
  `FeeSchedule.source` as `"clob_market"` or `"category_table"`).
- `get_book`: CLOB `GET /book?token_id=` → `OrderBook` (decimal strings → float; `tick_size`,
  `min_order_size` → `OrderBook.tick_size/min_size`; `timestamp` ms → aware UTC).
- `get_balance`, `get_positions`, `get_open_orders`, `get_fills`: via the existing
  `ClobClientWrapper` wrapped in `asyncio.to_thread` (it is synchronous — PLAN §3). These require
  credentials; when `settings.polymarket_private_key` is empty they raise `VenueAuthError`, not
  `ValueError`.
- `place_order`/`cancel_order` live in `live.py::PolymarketLiveAdapter(PolymarketAdapter)` and are
  the ONLY place `ClobClientWrapper.create_order/post_order/cancel_order` are called. The
  constructor calls `app.execution.fences.assert_live_allowed()` — that module does not exist until
  T13; create a minimal `backend/app/execution/fences.py` NOW with `assert_live_allowed()` that
  raises `LiveTradingDisabled` unless `settings.trading_mode == "live"` and
  `settings.live_trading_confirmation == "I_UNDERSTAND_REAL_MONEY"` (T13 extends it with the kill
  switch and limits). Add the two settings fields now (`trading_mode: Literal["paper","live"] =
  "paper"`, `live_trading_confirmation: str = ""`).
- Fix the existing bug in `bots/executor.py` (`signal.side`) by making `OrderExecutor` delegate to
  the router in T16; for now change it to raise `NotImplementedError("use OrderRouter (T14)")`
  in `execute_signal` so no one relies on the broken path.

Fixtures: `backend/tests/fixtures/polymarket/{gamma_markets.json,clob_book.json,clob_market.json}`
— hand-written to match the documented shapes in PLAN.md §3 (do not fetch from the network).
Tests `tests/venues/test_polymarket_adapter.py` with `httpx.MockTransport`: markets parse; book
parse; fee schedule source precedence; `VenuePayloadError` on a book missing `asks`;
constructing `PolymarketLiveAdapter` under default settings raises `LiveTradingDisabled`.
Register `"polymarket"` in `venues/registry.py` (`mode="read"` → adapter; `mode="live"` → live adapter).

**Acceptance.**
1. All tests pass with `MockTransport`; `grep -rn "post_order\|create_order\|cancel_order" app/venues/polymarket/` matches only `live.py`.
2. `python3 -c "from app.venues.registry import get_adapter; get_adapter('polymarket','live')"` FAILS with `LiveTradingDisabled` under default env.
3. `ruff check app/venues app/execution app/bots && mypy app/venues app/execution` clean; suite passes.

**Verify.**
```bash
cd backend && python3 -m pytest -q tests/venues/test_polymarket_adapter.py && test "$(grep -rln 'post_order\|create_order\|cancel_order' app/venues/polymarket/)" = "app/venues/polymarket/live.py" && ! python3 -c "from app.venues.registry import get_adapter; get_adapter('polymarket','live')" 2>/dev/null && ruff check app/venues app/execution app/bots && mypy app/venues app/execution && python3 -m pytest -q
```

---

### T12 — Kalshi `VenueAdapter` (auth, markets, bids-only book, V2 orders; live fenced)
- status: done
- model: opus
- depends: T11
- independent: no

**Brief.** Create `backend/app/venues/kalshi/{__init__,auth,adapter,live}.py`. Facts in PLAN.md §3
"Kalshi" are pinned; put the doc URLs in docstrings.
- Settings: `kalshi_api_key_id: str = ""`, `kalshi_private_key_pem: SecretStr = ""`,
  `kalshi_base_url: str = "https://external-api.kalshi.com/trade-api/v2"`,
  `kalshi_env: Literal["prod","demo"] = "demo"` (demo ⇒ base
  `https://external-api.demo.kalshi.co/trade-api/v2`). Add to `.env.example`. Add `cryptography`
  to `requirements.txt` if not importable (check: `python3 -c "import cryptography"`).
- `auth.py::sign_request(private_key, timestamp_ms: int, method: str, path: str) -> str`:
  RSA-PSS, SHA256, MGF1(SHA256), salt length = digest length, over
  `f"{timestamp_ms}{method.upper()}{path_without_query}"`, base64. `path` is the full path
  including `/trade-api/v2`. Provide `auth_headers(key_id, private_key, method, url) -> dict`.
  Test with a generated 2048-bit key: verify the signature with the public key; assert the query
  string is stripped; assert timestamp is ms.
- `adapter.py::KalshiAdapter(BaseAdapter)`: `list_markets` (`GET /markets?status=open&limit=…`
  with cursor pagination), `get_market(ticker)`, `get_book(ticker, outcome)`; parse
  `orderbook_fp.yes_dollars`/`no_dollars` (strings) AND legacy `orderbook.yes/no` (int cents);
  build `OrderBook` for outcome YES as bids = yes levels, asks = `[(1 − p, q) for (p,q) in no
  levels]` sorted ascending; for NO the mirror. `VenueMarket` from the market payload fields
  listed in PLAN §3 (`rules_primary` + `rules_secondary` → `rules_text`; `close_time`,
  `expected_expiration_time`; `result` → `status/result`; `fee_waiver_expiration_time` → if in the
  future, `FeeSchedule(taker_rate=0, maker_rate=0, source="fee_waiver")` else settings rates,
  `source="settings"`; `price_level_structure` → tick size at the current price, default 0.01).
  `get_balance` (`/portfolio/balance`, dollars from cents or `*_dollars` — handle both),
  `get_positions`, `get_open_orders`, `get_fills` (`/portfolio/fills`).
- `live.py::KalshiLiveAdapter`: `place_order` → `POST /portfolio/events/orders` with
  `side ∈ {bid,ask}` mapping (BUY YES → `bid` on the YES market; BUY NO → the adapter buys NO by
  placing a YES `ask`? NO — Kalshi supports `side: yes|no` in legacy and `bid/ask` in V2 on the
  event-market shape; read the V2 doc at implementation time and encode the mapping in a table
  with a unit test per case; if V2 is ambiguous for NO, use the legacy `POST /portfolio/orders`
  with `side: no`, and say which in the docstring), `count`/`price` as fixed-point strings,
  `client_order_id` passthrough, `time_in_force` mapping GTC/IOC/FOK. Constructor calls
  `assert_live_allowed()`.
- `429` → `VenueRateLimited(retry_after)`; `401/403` → `VenueAuthError`; unknown shape →
  `VenuePayloadError(raw=body)`.
- Fixtures under `tests/fixtures/kalshi/`: `markets.json`, `market.json`, `orderbook_fp.json`,
  `orderbook_legacy_cents.json`, `balance.json`, `order_ack.json` — hand-written to the documented
  shapes. Tests `tests/venues/test_kalshi_adapter.py` (MockTransport): both book formats yield
  identical `OrderBook`s; derived asks are `1 − no_bid`; fee waiver precedence; pagination
  follows `cursor`; auth headers present on every request; live adapter refuses under default
  settings; order body serializes prices with ≤ 4 dp and count with ≤ 2 dp.
- Add `tests/venues/test_adapter_contract.py`: a parametrized contract suite run against BOTH
  adapters on fixtures — `list_markets` returns `VenueMarket`s with aware `close_time`;
  `get_book` returns sorted, validated books; fee schedule `source` is non-empty.
- Register `"kalshi"` in the registry.

**Acceptance.**
1. All Kalshi tests + the contract suite pass for both venues.
2. `grep -rln "portfolio/events/orders\|portfolio/orders" app/venues/kalshi/` prints only `app/venues/kalshi/live.py`.
3. `ruff check app/venues && mypy app/venues` clean; suite passes.

**Verify.**
```bash
cd backend && python3 -m pytest -q tests/venues/test_kalshi_adapter.py tests/venues/test_adapter_contract.py && test "$(grep -rln 'portfolio/events/orders\|portfolio/orders' app/venues/kalshi/)" = "app/venues/kalshi/live.py" && ruff check app/venues && mypy app/venues && python3 -m pytest -q
```

---

## Phase 2 — Execution (paper first)

### T13 — Live fences, kill switch, risk limits, AST fence test
- status: done
- model: sonnet
- depends: T12
- independent: no

**Brief.** Extend `backend/app/execution/fences.py` and `config.py` per PLAN.md D13:
`kill_switch_path: str = "TRADING_KILL_SWITCH"` (relative to CWD or absolute), `max_order_notional_usd:
float = 250`, `max_daily_loss_usd: float = 100`, `max_open_notional_usd: float = 1000`,
`max_near_resolution_notional_usd: float = 500`. `assert_live_allowed()` additionally raises
`KillSwitchEngaged` if the file exists. Add `check_order_limits(order_notional, open_notional,
daily_pnl) -> None` raising `RiskLimitExceeded` — used by paper AND live (limits are exercised in
paper too). Add `.env.example` entries with a loud comment block.

Create `backend/tests/test_fences.py`:
1. AST walk of every `.py` under `backend/app`: collect `Attribute`/`Name`/`Constant` nodes; fail if
   any file other than `app/venues/polymarket/live.py`, `app/venues/kalshi/live.py` contains an
   attribute or call named `post_order`, `create_order`, `cancel_order`, `create_or_derive_api_creds`,
   a string constant containing `/portfolio/events/orders` or `/portfolio/orders`, or a `Name`
   `ClobClient` — EXCEPT `app/services/polymarket/client.py` (the wrapper, allowed to define them)
   and `app/bots/executor.py` (allowed only until T16 deletes its usage; list it in an
   `ALLOWED_UNTIL_T16` set that T16 must empty).
2. Under default settings `assert_live_allowed()` raises `LiveTradingDisabled`; with
   `trading_mode="live"` but no confirmation → still raises; with both set and the kill-switch
   file present (tmp_path) → `KillSwitchEngaged`; with both set and no file → passes. Use a fresh
   `Settings(...)` instance passed explicitly (add an optional `settings` parameter) — do not
   mutate the cached global.
3. `check_order_limits` boundary cases.

**Acceptance.**
1. `tests/test_fences.py` passes; deliberately adding `client.post_order(x)` to `app/services/scoring.py` (temp copy in `tmp_path`, run the walker function on that path) makes the walker return a violation — the test must include this positive-control case.
2. `python3 -c "from app.config import settings; assert settings.trading_mode=='paper'"` exits 0.
3. `.env.example` contains `TRADING_MODE=paper` and `LIVE_TRADING_CONFIRMATION=`.
4. `ruff check app/execution app/config.py && mypy app/execution` clean; suite passes.

**Verify.**
```bash
cd backend && python3 -m pytest -q tests/test_fences.py && python3 -c "from app.config import settings; assert settings.trading_mode=='paper'" && grep -q '^TRADING_MODE=paper' ../.env.example && grep -q '^LIVE_TRADING_CONFIRMATION=' ../.env.example && ruff check app/execution app/config.py && mypy app/execution && python3 -m pytest -q
```

---

### T15 — Persisted orders/trades/positions with venue columns; migration 004
- status: done
- model: sonnet
- depends: T13
- independent: no

**Brief.** (Numbered T15 but runs before T14 — the router persists through these.)
`models/trade.py`: `Order` gains `venue: Mapped[str]` (String(16), index), `client_order_id:
Mapped[str]` (String(64), unique), `intent_id: Mapped[str|None]` (String(64), index), `outcome:
Mapped[str]` (String(50)), `mode: Mapped[str]` (`"paper"|"live"`, String(8)); `order_id` becomes
nullable (assigned by venue on ack). `Trade` gains `venue`, `outcome`, `liquidity: Mapped[str|None]`
(`maker|taker`), `mode`; `tx_hash` becomes nullable (Kalshi has none). `Position` gains `venue`,
`mode`, `intent_id`, `hold_to_resolution: Mapped[bool]`, `settled_at`, `settlement_outcome`.
`Market` gains `venue: Mapped[str]` (default `"polymarket"`), and `condition_id`'s unique index
becomes a composite unique on `(venue, condition_id)` — write the `UniqueConstraint` explicitly.
Add `intents` table via `models/intent.py::IntentRecord(id: str PK, kind, strategy, mode,
created_at, status: pending|executed|rejected|expired, legs JSONList, score JSONDict, extra_data)`.

Migration `backend/alembic/versions/20260904_000300_004_core_trading_tables.py`
(`revision="004"`, `down_revision="003"`): CREATE `markets`, `market_prices`, `orders`, `trades`,
`positions`, `strategies`, `backtests`, `backtest_trades`, `intents` — hand-written from the models
(these tables have never had a migration; do not reference `tracked_traders`). Enums: reuse
`sa.Enum(..., name="orderside")` etc. once; `downgrade` drops in reverse and drops the enum types.
Keep `op.execute` blocks offline-safe.

Tests `tests/models/test_core_tables.py`: SQLite `create_all` then insert one row per new/changed
table via the ORM with `venue="kalshi"`; composite unique on markets enforced (second insert with
same `(venue, condition_id)` raises `IntegrityError`; same `condition_id` on a different venue
does not).

**Acceptance.**
1. `alembic heads` prints `004 (head)`; `alembic upgrade head --sql` contains `CREATE TABLE orders`, `CREATE TABLE intents`, and the string `client_order_id`.
2. Tests pass.
3. `ruff check app/models && mypy app/models` clean; suite passes.

**Verify.**
```bash
cd backend && alembic heads | grep -q '^004' && alembic upgrade head --sql 2>/dev/null | grep -q 'CREATE TABLE orders' && alembic upgrade head --sql 2>/dev/null | grep -q 'CREATE TABLE intents' && python3 -m pytest -q tests/models/test_core_tables.py && ruff check app/models && mypy app/models && python3 -m pytest -q
```

---

### T14 — `OrderRouter`, `CapitalLedger`, `PaperVenueAdapter`, reconciliation
- status: done
- model: opus
- depends: T15
- independent: no

**Brief.** Create `backend/app/execution/{ledger,router,reconcile}.py` and
`backend/app/venues/paper.py`.
- `CapitalLedger`: per-venue `available`/`locked`; `reserve(venue, amount) -> ReservationId`,
  `release`, `settle`; NEVER sums across venues (R6). Seeded from `adapter.get_balance()` or, in
  paper mode, from `settings.paper_starting_balances: dict[VenueId, float]` (add; default
  `{"polymarket": 1000.0, "kalshi": 1000.0}`).
- `PaperVenueAdapter(inner: VenueAdapter, fill_engine: SimulatedFillEngine, ledger: CapitalLedger)`:
  proxies `list_markets/get_market/get_book` to `inner`; `place_order` fetches the current book
  from `inner`, runs `fill_engine.fill`, records fills in memory, returns an `OrderAck` with
  `status ∈ {filled, partial, open, rejected}`; GTC residuals rest in an internal open-order map
  and are re-checked on `poll()`; `cancel_order` cancels a rested order; `get_open_orders/get_fills/
  get_positions/get_balance` answer from internal state. Paper `client_order_id` duplicates return
  the ORIGINAL ack (idempotency is exercised, not faked).
- `OrderRouter(adapters: dict[VenueId, VenueAdapter], ledger, session_factory, fences)`:
  `submit(intent: Intent, strategy: str) -> RoutedIntent`:
  1. `check_order_limits` per leg and for the sum (raises → intent `rejected`, persisted).
  2. Reserve capital per venue per leg (fail → rejected, nothing placed).
  3. `client_order_id = f"{intent.id}:{i}:{attempt}"`; persist `Order` rows `PENDING` BEFORE calling
     the venue (crash-safety: a row with no ack is reconciled later).
  4. Place legs. `all_or_none`: place with `tif=IOC`; if any leg fills < tolerance, UNWIND the
     others by selling what filled at market (best-effort, records `unwind=True`) — document that
     unwinding is itself not free and the reason it happens is that prediction-market venues offer
     no cross-venue atomicity.
  5. Persist `Trade` rows per fill and upsert `Position` rows; release unused reservations.
- `reconcile(venue)`: compares local `PENDING/OPEN` orders against `adapter.get_open_orders()` and
  `get_fills(since)`; marks filled/cancelled/unknown; a local `PENDING` with no venue record after
  `reconcile_grace_s` is marked `FAILED` with reason `no_ack`. Runs as a Celery task
  `app.tasks.execution.reconcile_all` every 60s (add the module to `tasks/__init__.py` include +
  beat).
- Every mutation logs a structured line (`logger.info("order", extra={...})`) — no secrets, ever.

Tests `tests/execution/test_router.py` (paper adapters over `InMemory` inner adapters built from
fixtures — write a tiny `FixtureAdapter` in `tests/venues/fixture_adapter.py`): single-leg fill
persists Order+Trade+Position; duplicate `client_order_id` returns the original ack and creates
no second Trade; complement `all_or_none` with a thin NO book → YES leg unwound, ledger back to
starting balance minus fees; ledger never allows kalshi reservation to draw on polymarket
balance; reconcile marks a stale PENDING as FAILED; risk limit rejects an oversized leg before any
placement (adapter's `place_order` call count == 0).

**Acceptance.**
1. All six router tests pass.
2. `grep -rn "sum(.*available" app/execution/ledger.py` returns nothing (no cross-venue sum).
3. `python3 -c "import app.tasks; assert 'app.tasks.execution' in app.tasks.celery_app.conf.include"` exits 0.
4. `ruff check app/execution app/venues app/tasks && mypy app/execution app/venues` clean; suite passes.

**Verify.**
```bash
cd backend && python3 -m pytest -q tests/execution/test_router.py && test -z "$(grep -rn 'sum(.*available' app/execution/ledger.py)" && python3 -c "import app.tasks; assert 'app.tasks.execution' in app.tasks.celery_app.conf.include" && ruff check app/execution app/venues app/tasks && mypy app/execution app/venues && python3 -m pytest -q
```

---

### T16 — Wire trading API and bots to the router; delete the broken executor path
- status: done
- model: sonnet
- depends: T14
- independent: no

**Brief.** `api/routes/trading.py`: `POST /orders` builds a one-leg `Intent` from `OrderRequest`
(+ `venue` field, default `polymarket`) and calls `OrderRouter.submit`; `DELETE /orders/{id}` →
router cancel; `GET /orders`, `GET /positions`, `GET /positions/{venue}/{market_id}` read the
persisted rows; add `GET /trading/mode` returning `{"mode": settings.trading_mode,
"kill_switch": bool}`. Router construction lives in `api/deps.py::get_router()` (app-scoped
singleton built from the registry in `settings.trading_mode`). `bots/executor.py`: delete
`OrderExecutor`; `bots/base.py::BaseBot.execute_signal` becomes concrete and calls the router.
Empty `ALLOWED_UNTIL_T16` in `tests/test_fences.py`. Response models are pydantic; no raw dicts.

Tests `tests/api/test_trading.py` (client fixture, paper mode, FixtureAdapter injected through
`app.dependency_overrides[get_router]`): place → 200 with `status` and `client_order_id`; list
orders shows it; positions reflect the fill; `GET /trading/mode` says `paper`.

**Acceptance.**
1. Tests pass; `tests/test_fences.py` passes with `ALLOWED_UNTIL_T16 = set()`.
2. `test ! -e app/bots/executor.py || ! grep -q "class OrderExecutor" app/bots/executor.py`.
3. `ruff check app/api app/bots && mypy app/api/routes/trading.py app/bots` clean; suite passes.

**Verify.**
```bash
cd backend && python3 -m pytest -q tests/api/test_trading.py tests/test_fences.py && grep -q 'ALLOWED_UNTIL_T16 = set()' tests/test_fences.py && ! grep -rq 'class OrderExecutor' app/bots/ && ruff check app/api app/bots && mypy app/api/routes/trading.py app/bots && python3 -m pytest -q
```

---

## Phase 3 — Edge discovery

### T17 — Event-equivalence subsystem: `event_links`, matcher, review API; migration 005
- status: done
- model: opus
- depends: T16
- independent: no

**Brief.** PLAN.md D9 is the spec. Files: `models/event_link.py::EventLink` (columns per D9, plus
`created_at/updated_at`, unique on `(venue_a, market_a, venue_b, market_b)`),
`services/matching/{__init__,normalize,matcher}.py`, `api/routes/links.py` (prefix `/links`),
migration `20260904_000400_005_event_links.py` (`revision="005"`, `down_revision="004"`).

`normalize.py`: `normalize_title(s) -> list[str]` — lowercase, strip punctuation, remove
stopwords and the tokens `will/the/by/before/after/on/in`, replace month names + dates + years
with `<date>`, numbers with `<num>` but ALSO extract them into `thresholds: list[float]`
(handles `100k`, `100,000`, `$100K`, `1.5m`), Porter-stem (implement a minimal stemmer or use
`nltk` ONLY if already installed — check; else a 20-line suffix stripper is fine).
`matcher.py::score_pair(a: VenueMarket, b: VenueMarket) -> LinkEvidence(title_jaccard, close_delta_h,
threshold_match: bool|None, source_match: bool|None, outcome_map: dict, confidence)` where
`confidence = 0.55×title_jaccard + 0.20×close_score + 0.15×threshold_score + 0.10×source_score`
with `close_score = max(0, 1 − |Δh|/48)`, threshold/source scores ∈ {0, 0.5 (unknown), 1}.
`propose_links(markets_a, markets_b, min_confidence=0.5) -> list[EventLink]` uses a token-index
blocking step so it is not O(n²) over thousands of markets (index by any shared non-`<…>` token).
Outcome map: binary↔binary is `{"YES":"YES","NO":"NO"}`; anything else is `proposed` with
`outcome_map={}` and `evidence["needs_outcome_map"]=True`.

API: `GET /links?status=`, `POST /links/propose` (runs the matcher over both adapters'
`list_markets` — in tests, fixtures), `GET /links/{id}` returns both markets' `rules_text`,
`close_time`, `resolution_source` side by side, `POST /links/{id}/approve` (body:
`{reviewed_by, notes, outcome_map?}`), `POST /links/{id}/reject`. Approving a link with an
empty `outcome_map` is a 422.

Tests `tests/matching/test_matcher.py`: near-identical titles across venues with same close
date → confidence ≥ 0.85; same title, close dates 10 days apart → < 0.7; "above 100k" vs "≥
100,000" → `threshold_match=True`; "above 100k" vs "above 150k" → False and confidence < 0.5;
blocking never drops a pair that shares a content token; API approve/reject flow; approving
without outcome_map on a multi-outcome pair → 422.

**Acceptance.**
1. Tests pass; `alembic heads` prints `005 (head)`; offline SQL contains `CREATE TABLE event_links`.
2. `grep -rn "status.*approved" app/services/matching/` returns nothing — the matcher never sets `approved` (only humans via the API).
3. `ruff check app/services/matching app/api/routes/links.py app/models/event_link.py && mypy app/services/matching` clean; suite passes.

**Verify.**
```bash
cd backend && python3 -m pytest -q tests/matching && alembic heads | grep -q '^005' && alembic upgrade head --sql 2>/dev/null | grep -q 'CREATE TABLE event_links' && test -z "$(grep -rn 'status.*approved' app/services/matching/)" && ruff check app/services/matching app/api/routes/links.py app/models/event_link.py && mypy app/services/matching && python3 -m pytest -q
```

---

### T18 — Cross-venue complement arbitrage (`cross_venue_arbitrage.py`)
- status: done
- model: opus
- depends: T17
- independent: no

**Brief.** Create `backend/app/strategies/cross_venue_arbitrage.py` (registry key
`cross_venue_arbitrage`, category `arbitrage`; registry back to 9 keys). PLAN.md D8 is the math.
Input: the strategy receives snapshots from both venues; it holds a `LinkBook` (injected
`links: list[EventLink]` restricted to `status == "approved"` by the caller — the strategy asserts
this and raises if any link is not approved) and caches the latest book per `(venue, market,
outcome)`. On each snapshot, for every approved link touching that market, evaluate both
directions: `YES@A + NO@B` and `NO@A + YES@B`. For each: walk each leg's book for
`size_contracts` (config `probe_size`, default 50) to get the marginal ask; `cost = ask_A + ask_B
+ fee_A(taker) + fee_B(taker) + 2 × redemption_gas`; `gross_edge = 1 − cost`;
`net_edge = gross_edge × p_same_resolution − (1 − p_same_resolution) × worst_case_loss` where
`p_same_resolution = link.confidence` (a `proposed` link never reaches here) and
`worst_case_loss = max(ask_A, ask_B)` (one leg pays 0, the other pays 1 — you lose the losing
leg's cost); `hours_to_resolution = min(close_time_A, close_time_B) − now`; emit
`Intent(kind="cross_venue", legs=[…], hold_to_resolution=True, atomicity="all_or_none",
expected_resolution_ts=…, metadata={gross_edge, net_edge, p_same_resolution, worst_case_loss,
annualized, link_id, capital_lockup_usd})` iff `net_edge ≥ min_net_edge` (default 0.015) and
`annualized ≥ settings.min_viable_annualized`. Sizing in `calculate_position_size` uses the
per-venue ledger snapshot passed in `positions["__ledger__"]` (the engine/router passes
`{"polymarket": available, "kalshi": available}`): `contracts = min(available_A / ask_A,
available_B / ask_B, max_contracts)`. NEVER assume transfers (R6).

Tests `tests/strategies/test_cross_venue.py`: asks 0.46 (poly, politics 0.04) + 0.50 (kalshi 0.07)
→ hand-compute cost and net edge, assert to 1e-9; the same pair with confidence 0.8 → lower net
edge and `worst_case_loss` present; a `proposed` link raises; sizing caps at the poorer venue;
no intent when annualized < threshold because resolution is 400 days out; an 8%+ edge sets
`metadata["suspect_link"]=True` (R1 tripwire).

**Acceptance.**
1. Tests pass; `python3 -c "from app.strategies import STRATEGIES; assert len(STRATEGIES)==9 and 'cross_venue_arbitrage' in STRATEGIES"` exits 0.
2. `grep -n "transfer" app/strategies/cross_venue_arbitrage.py` matches only comments/docstrings explaining that transfers are NOT assumed (`grep -c` ≥ 1 is expected; a call like `ledger.transfer(` must not exist: `! grep -q "\.transfer(" …`).
3. `ruff check app/strategies && mypy app/strategies/cross_venue_arbitrage.py` clean; suite passes.

**Verify.**
```bash
cd backend && python3 -m pytest -q tests/strategies/test_cross_venue.py && python3 -c "from app.strategies import STRATEGIES; assert len(STRATEGIES)==9 and 'cross_venue_arbitrage' in STRATEGIES" && ! grep -q '\.transfer(' app/strategies/cross_venue_arbitrage.py && ruff check app/strategies && mypy app/strategies/cross_venue_arbitrage.py && python3 -m pytest -q
```

---

### T19 — Opportunity scoring, live scanner, `/arbitrage/opportunities`
- status: done
- model: sonnet
- depends: T18
- independent: no

**Brief.** PLAN.md D10. `services/scoring.py::OpportunityScore` (dataclass, all fields in D10 +
`link_status: str|None`, `depth_source`) and `score(intent, ctx: ScoreContext) -> OpportunityScore`
where `ScoreContext(now, books: dict[(venue,market,outcome), OrderBook], markets: dict[(venue,market),
VenueMarket], settings)`. `fill_confidence` = fraction of the intent's contracts fillable within
`max_slippage_bps` (config 50) of the leg limit across all legs, min over legs. `resolution_risk`
∈ [0,1] = `0.15 base + 0.25 if rules_text < 200 chars + 0.20 if resolution_source is None + 0.25 if
cross_venue and link.confidence < 0.95 + 0.15 if hours_to_resolution < 6 (dispute window)`,
capped at 1. `composite = annualized_return × fill_confidence × (1 − resolution_risk)`.

`services/scanner.py::scan(strategies: list[str], adapters, links, session) -> list[ScoredIntent]`:
pulls `list_markets(status="open")` from each adapter, builds `MarketSnapshot`s (with `book` from
`get_book`) for the top-N markets by 24h volume (config `scan_top_n`, default 200 per venue),
runs each strategy's `on_market_data`, scores every intent, persists `IntentRecord`s with
`status="pending"` and the score, returns them sorted by `composite` desc. Celery task
`app.tasks.scanner.scan_opportunities` every `scan_interval_s` (config, 120s) — it does NOT route
anything; routing is a separate explicit call. `api/routes/arbitrage.py`: `GET /opportunities`
returns the latest scan's `IntentRecord`s with scores (query `min_composite`, `venue`,
`near_resolution: bool`); `POST /scan` triggers a scan synchronously in tests / enqueues in prod;
`GET /history` lists executed intents.

Tests `tests/services/test_scoring.py` (hand-computed composite for one intent; monotonicity:
more hours-to-resolution ⇒ lower annualized; thinner book ⇒ lower fill_confidence) and
`tests/api/test_arbitrage.py` (scan over FixtureAdapters with one planted complement gap →
`/opportunities` returns ≥ 1 with all score fields present and non-null).

**Acceptance.**
1. Tests pass; `/opportunities` payload item has keys `net_edge, annualized_return, hours_to_resolution, fill_confidence, resolution_risk, capital_lockup_usd, composite`.
2. `grep -n "submit(" app/services/scanner.py app/tasks/scanner.py` returns nothing (scanner never routes).
3. `ruff check app/services/scoring.py app/services/scanner.py app/api/routes/arbitrage.py app/tasks && mypy app/services/scoring.py app/services/scanner.py` clean; suite passes.

**Verify.**
```bash
cd backend && python3 -m pytest -q tests/services/test_scoring.py tests/api/test_arbitrage.py && test -z "$(grep -n 'submit(' app/services/scanner.py app/tasks/scanner.py)" && ruff check app/services/scoring.py app/services/scanner.py app/api/routes/arbitrage.py app/tasks && mypy app/services/scoring.py app/services/scanner.py && python3 -m pytest -q
```

---

### T20 — Near-resolution scanner path; `settlement_edge` rewrite
- status: done
- model: sonnet
- depends: T19
- independent: no

**Brief.** PLAN.md D10 (a)–(d). `services/scanner.py` gains `near_resolution_pass(...)` selecting
markets with `hours_to_resolution ≤ settings.near_resolution_hours`, computing per market:
`spread_now / median_spread_24h` (from `PriceHistory` when available else 1.0 → `liquidity_collapse
= ratio > 3`), `in_dispute_window` (Polymarket: `close_time` passed and not resolved; Kalshi:
`expected_expiration_time` passed and `result == ""`), and `outcome_determined = price ≥ 0.97 or
≤ 0.03 with close_time passed`. Intents from this pass carry `metadata["bucket"]="near_resolution"`;
the router (T14) enforces `max_near_resolution_notional_usd` across that bucket (add the check to
`check_order_limits` with a `bucket` argument). `resolution_risk` adds +0.3 when
`in_dispute_window` and +0.2 when `liquidity_collapse`.

Rewrite `strategies/settlement_edge.py` around (c): when `outcome_determined`, emit a single-leg
`Intent` buying the ~certain side at its ask iff `(1 − ask − fee − gas) / ask` annualized over
`max(hours_to_resolution, settlement_delay_hours)` ≥ `min_viable_annualized`; skip if
`in_dispute_window` unless `allow_dispute_window` (config, default False). Delete the keyword
"ambiguity" heuristics and their config keys. Docstring explains: this is a capital-lockup trade,
not an information edge; its risk is settlement risk (R2).

Tests `tests/services/test_near_resolution.py` + `tests/strategies/test_settlement_edge.py`:
0.98 ask with 30h to go and Kalshi fee → annualized computed by hand; same at 0.995 → None
(fees eat it); dispute window → None by default; bucket cap rejects the second intent when the
first fills the cap; liquidity-collapse flag from a synthetic 24h spread series.

**Acceptance.**
1. Tests pass; `grep -n "ambiguity_keywords\|clarity_keywords" app/strategies/settlement_edge.py` returns nothing.
2. `check_order_limits` signature includes `bucket` (`python3 -c "import inspect; from app.execution.fences import check_order_limits; assert 'bucket' in inspect.signature(check_order_limits).parameters"`).
3. `ruff check app/services app/strategies/settlement_edge.py app/execution && mypy app/services/scanner.py app/strategies/settlement_edge.py` clean; suite passes.

**Verify.**
```bash
cd backend && python3 -m pytest -q tests/services/test_near_resolution.py tests/strategies/test_settlement_edge.py && test -z "$(grep -n 'ambiguity_keywords\|clarity_keywords' app/strategies/settlement_edge.py)" && python3 -c "import inspect; from app.execution.fences import check_order_limits; assert 'bucket' in inspect.signature(check_order_limits).parameters" && ruff check app/services app/strategies/settlement_edge.py app/execution && mypy app/services/scanner.py app/strategies/settlement_edge.py && python3 -m pytest -q
```

---

### T21 — Depth recording: `book_snapshots` table, collector, replayer uses recorded depth; migration 006
- status: done
- model: sonnet
- depends: T20
- independent: no

**Brief.** `models/book_snapshot.py::BookSnapshot(id, venue, market_id, outcome, ts, bids JSONList,
asks JSONList, tick_size, min_size, depth_source="recorded")`, unique on `(venue, market_id,
outcome, ts)`, migration `20260904_000500_006_book_snapshots.py` (`revision="006"`,
`down_revision="005"`; hypertable block guarded like `001`). `services/data_collector.py::DataCollector`
gains `collect_books(adapters, market_ids_per_venue, session)` (top-N by volume; both venues) and
`scripts/collect_prices.py` gets a `--books` flag that calls it on the same loop. `data_replay.py`:
when a `BookSnapshot` exists for `(venue, market, outcome)` at or within `book_match_window`
(config 120s) before the price row's `ts`, attach it as `MarketSnapshot.book` (recorded);
otherwise the engine synthesizes (T07). `BacktestResult.depth_source` becomes `"recorded"` /
`"synthetic"` / `"mixed"` from the actual mix.

Tests `tests/backtesting/test_recorded_depth.py`: a replay with one recorded book and one without
→ `depth_source == "mixed"`; the fill against the recorded book walks its real levels (assert avg
price from two levels); `collect_books` on FixtureAdapters writes rows and is idempotent on
re-run (unique constraint, `ON CONFLICT DO NOTHING` semantics via `session.merge` or dialect-safe
upsert — SQLite must pass too).

**Acceptance.**
1. Tests pass; `alembic heads` prints `006 (head)`; offline SQL has `CREATE TABLE book_snapshots`.
2. `ruff check app/models/book_snapshot.py app/services/data_collector.py app/services/backtesting app/scripts/collect_prices.py && mypy app/services/backtesting` clean; suite passes.

**Verify.**
```bash
cd backend && python3 -m pytest -q tests/backtesting/test_recorded_depth.py && alembic heads | grep -q '^006' && alembic upgrade head --sql 2>/dev/null | grep -q 'CREATE TABLE book_snapshots' && ruff check app/models/book_snapshot.py app/services/data_collector.py app/services/backtesting app/scripts/collect_prices.py && mypy app/services/backtesting && python3 -m pytest -q
```

---

## Phase 4 — Measurement and surface

### T22 — Capital sweep and `EdgeDecayReport`
- status: done
- model: sonnet
- depends: T21
- independent: no

**Brief.** PLAN.md D12. `services/backtesting/sweep.py::run_sweep(strategy_name, strategy_config,
base_config: BacktestConfig, data_source_factory: Callable[[], AsyncIterator], capital_levels:
list[float] | None) -> EdgeDecayReport` (dataclass with `rows: list[CapitalRow]`, `edge_dies_at`,
`depth_source`, `fill_at`, `sweep_ceiling_note: str`). Each row runs a fresh `Backtester` with
`initial_capital=level`; `pct_intents_downsized` = share of executed intents whose filled contracts
< requested; `capital_utilization` = mean over the equity curve of `(equity − cash)/equity`;
`avg_slippage_bps` = size-weighted `(avg_fill − limit)/limit × 1e4` over BUY fills.
`edge_dies_at` = first level with `annualized < settings.min_viable_annualized`; `None` if none,
and then `sweep_ceiling_note` says the top level tested. Export `run_sweep` and `EdgeDecayReport`
from `services/backtesting/__init__.py`. API: `POST /api/v1/backtests/sweep` (body =
`BacktestRequest` + `capital_levels`) runs via Celery `run_sweep_task` and stores each level as
its own `BacktestRun` plus a parent `BacktestRun` with `strategy_name=f"sweep:{name}"` whose
`report["edge_decay"]` holds the report; `GET /api/v1/backtests/{id}/edge-decay` returns it.
CLI `python3 -m app.scripts.sweep --strategy … --start … --end … --levels 500,2000,10000` prints
a table and writes JSON to `--out`. Also `python3 -m app.scripts.sweep --synthetic` runs on
`create_sample_snapshots` with a planted complement gap so the command is demonstrable without a
DB.

Tests `tests/backtesting/test_sweep.py`: on a synthetic fixture with a planted 4% complement gap
and a one-level book of fixed size, the report has one row per level, `net_return` is
non-increasing in capital, `pct_intents_downsized` is 0 at $500 and > 0 at $250k, and
`edge_dies_at` is not None; on a fixture with no gap every row's trades == 0 and `edge_dies_at`
== the first level (no edge at any size).

**Acceptance.**
1. Tests pass; `python3 -m app.scripts.sweep --synthetic --levels 500,5000,50000 --out /tmp/x.json` exits 0 and the JSON has `rows` of length 3 and an `edge_dies_at` key.
2. `ruff check app/services/backtesting app/scripts/sweep.py app/api/routes/backtesting.py app/tasks/backtesting.py && mypy app/services/backtesting` clean; suite passes.

**Verify.**
```bash
cd backend && python3 -m pytest -q tests/backtesting/test_sweep.py && python3 -m app.scripts.sweep --synthetic --levels 500,5000,50000 --out "$TMPDIR/market-edge-sweep.json" && python3 -c "import json,os; d=json.load(open(os.environ['TMPDIR']+'/market-edge-sweep.json')); assert len(d['rows'])==3 and 'edge_dies_at' in d" && ruff check app/services/backtesting app/scripts/sweep.py app/api/routes/backtesting.py app/tasks/backtesting.py && mypy app/services/backtesting && python3 -m pytest -q
```

---

### T23 — Frontend: Opportunities tab and edge-decay table
- status: done
- model: sonnet
- depends: T22
- independent: yes (touches only `frontend/`)

**Brief.** Minimal, typed, no redesign. `frontend/src/types/index.ts`: add `OpportunityScore`,
`Opportunity` (mirrors T19's payload), `EdgeDecayRow`, `EdgeDecayReport`, `TradingMode`.
`frontend/src/services/api.ts`: `getOpportunities(params)`, `triggerScan()`, `getEdgeDecay(backtestId)`,
`getTradingMode()`; replace the stale `ArbitrageOpportunity` usages. Hooks
`useOpportunities.ts`, `useEdgeDecay.ts`, `useTradingMode.ts`. Components:
`components/opportunities/OpportunitiesTable.tsx` (sortable by composite/annualized/hours; columns:
venue(s), market, kind, net edge %, annualized %, hours to resolution, fill conf, resolution risk,
lockup $, link status badge, `near_resolution` badge), `components/backtesting/EdgeDecayTable.tsx`
(rows per capital level, highlights `edge_dies_at`, shows `depth_source` and `fill_at` labels — a
`synthetic` depth badge must be visible, per PLAN D6). `App.tsx`: the `'arbitrage'` tab renders
`OpportunitiesTable` and a header pill showing `PAPER`/`LIVE` from `useTradingMode`
(`LIVE` in red). `Backtesting.tsx`: render `EdgeDecayTable` when the run's `strategy_name` starts
with `sweep:` or `report.edge_decay` exists. No `any`. ESLint must pass.

**Acceptance.**
1. `cd frontend && npx tsc -p tsconfig.app.json --noEmit` exits 0.
2. `npm run lint` exits 0.
3. `grep -rn "synthetic" frontend/src/components/backtesting/EdgeDecayTable.tsx` ≥ 1; `grep -rn "PAPER\|LIVE" frontend/src/App.tsx` ≥ 1.
4. `grep -rn ": any\b" frontend/src` returns nothing.

**Verify.**
```bash
cd frontend && npx tsc -p tsconfig.app.json --noEmit && npm run lint && grep -q synthetic src/components/backtesting/EdgeDecayTable.tsx && grep -q 'LIVE' src/App.tsx && test -z "$(grep -rn ': any\b' src)"
```

---

### T24 — Docs and project skills reflect the new shape
- status: done
- model: haiku
- depends: T22
- independent: yes (docs only; may run parallel with T23)

**Brief.** Edit `CLAUDE.md`: Project Overview (multi-venue, paper-first, mimicry removed with the
D2 sentence if T03's edit is not already present), Tech Stack (React 19 / Tailwind 4 — read
`frontend/package.json`), Project Structure (add `venues/`, `execution/`, `services/matching/`,
`services/scoring.py`, `services/scanner.py`, `services/backtesting/sweep.py`, `models/event_link.py`,
`models/book_snapshot.py`, `models/intent.py`; remove `services/market_data.py`, `trading.py`,
`arbitrage.py` placeholders that never existed), Commands (add `python3 -m app.scripts.sweep
--synthetic`, `alembic upgrade head --sql`), Environment Variables (add the `TRADING_MODE`,
`LIVE_TRADING_CONFIRMATION`, Kalshi, and cost variables — copy names from `.env.example`), and a
new `## Money Invariants` section with exactly these three bullets: "`TRADING_MODE` defaults to
`paper`; live requires `LIVE_TRADING_CONFIRMATION=I_UNDERSTAND_REAL_MONEY` and no
`TRADING_KILL_SWITCH` file", "Only `app/venues/*/live.py` may place or cancel venue orders —
`tests/test_fences.py` enforces this", "Never place an order from a test, a verifier, or CI".
`README.md`: replace the "setup kit" narrative (it still describes copying config files into a
new project) with a 40–80 line description of what the repo is, how to run tests, how to run the
synthetic sweep, and the paper/live fence. Add `.claude/skills/kalshi-api/SKILL.md` (≤ 120 lines)
summarizing PLAN.md §3 Kalshi facts with doc URLs — copy, don't invent. Update
`.claude/skills/polymarket-api/SKILL.md` fee section to the `C × rate × p × (1−p)` taker-only
formula with the category table. Do not touch code.

**Acceptance.**
1. `grep -c "Money Invariants" CLAUDE.md` == 1; the three bullets are present verbatim.
2. `test -f .claude/skills/kalshi-api/SKILL.md` and it contains `external-api.kalshi.com`.
3. `grep -n "setup kit\|polymarket-setup\|cp -r /path/to" README.md` returns nothing.
4. `grep -n "p × (1 − p)\|p \* (1 - p)\|p(1-p)\|p \\* (1 \\- p)" .claude/skills/polymarket-api/SKILL.md` ≥ 1.
5. `git diff --stat -- backend frontend` shows no changes from this task (docs only): the executor records the pre-task `git rev-parse HEAD` and diffs against it.

**Verify.**
```bash
cd /Users/michaelcave/Developer/reposV2/polymarket-trader && test "$(grep -c 'Money Invariants' CLAUDE.md)" = "1" && grep -q 'I_UNDERSTAND_REAL_MONEY' CLAUDE.md && grep -q 'test_fences.py' CLAUDE.md && test -f .claude/skills/kalshi-api/SKILL.md && grep -q 'external-api.kalshi.com' .claude/skills/kalshi-api/SKILL.md && test -z "$(grep -n 'polymarket-setup\|cp -r /path/to' README.md)" && grep -qE 'p ?[×*] ?\(1 ?[−-] ?p\)' .claude/skills/polymarket-api/SKILL.md
```

---

## Acceptance-set cross-check (architect's self-audit against `contradictory-acceptance`)

- T03 requires 9 strategies; T10 requires 8 (deletes `cross_platform_arbitrage`); T18 requires 9
  (adds `cross_venue_arbitrage`). These are sequential states, not contradictions — each task's
  count is asserted only at that task.
- T13 allows `app/bots/executor.py` in the fence walker via `ALLOWED_UNTIL_T16`; T16 empties it and
  deletes the class. T11 makes the executor raise `NotImplementedError` in between.
- T08 sets `fill_at="next"` default; T10's acceptance 3 and T22's synthetic sweep therefore need
  fixtures with ≥ 2 snapshots per market after the signal — `create_sample_snapshots` already
  produces many; test authors must not use single-snapshot fixtures for execution assertions
  unless they pass `fill_at="same"`.
- T05 adds Settings fields with defaults; T13 adds more; T12 adds Kalshi fields; T14 adds
  `paper_starting_balances`. `.env.example` is appended to in each — no task rewrites it wholesale.
- Migration chain is strictly linear: 001 → 002 (T03) → 003 (T09) → 004 (T15) → 005 (T17) → 006 (T21).
  `alembic heads` must print exactly one head at every task.

## Phase 3R — Remediation (added during execution, not by the architect)

These five tasks were dispatched by the execute loop in response to findings from the Phase 3
boundary review and a red-team pass on the T20/T21 surface. They are recorded here so the routing
ledger scores them; the architect did not plan them.

### T21b — Phase 3 review remediation: bundle `net_edge`, two stale/false comments
- status: done
- model: sonnet
- depends: T21
- independent: no

**Brief.** `multi_outcome_bundle_arbitrage` published only `"profit_margin"`, which `scoring._net_edge`
does not read, so every bundle opportunity scored `net_edge=0.0` and `composite=0.0` — a real 7.5% arb
ranked below every cross-venue intent. Publish `"net_edge"` at the strategy end (not by aliasing
`profit_margin` into the scorer, which would make two differently-defined quantities compete on one
axis). Also correct `config.py`'s near-resolution cap comment (described T14 behavior T20 replaced) and
`cross_venue_arbitrage.py:664-666`'s false "amortized over the fill it is actually charged on" claim.

**Acceptance.** Bundle intent scores non-zero `net_edge` and `composite`, asserted by hand; `_net_edge`
unchanged; suite green.

**Verify.** `cd backend && python3 -m pytest -q`

---

### T21c — Red-team remediation: scanner book selection, empty-book fallback
- status: done
- model: sonnet
- depends: T21b
- independent: no

**Brief.** `scanner.py` computed `primary_book`/`yes_book`/`no_book` then attached only `primary_book`
(always YES), so `settlement_edge` priced the NO side at top-of-book — 4.0x overstated return on the
ranking path. And `_priced_fills` applied its optimistic single-fill fallback to an observed EMPTY
recorded book, reading "no liquidity" as "maximum liquidity". Pass the real per-outcome books through;
distinguish "no book observed" from "book observed and empty".

**Acceptance.** Mismatch repro yields the honest annualized number; an observed-empty book produces no
intent; suite green.

**Verify.** `cd backend && python3 -m pytest -q tests/services/test_near_resolution.py && python3 -m pytest -q`

---

### T21d — Red-team remediation: outcome identity vs display, unmarkable-position observability
- status: done
- model: opus
- depends: T21c
- independent: no

**Brief.** `normalize_outcome` is the identity function for non-YES/NO labels, so outcome RESOLUTION
was case-insensitive while outcome IDENTITY was case-sensitive: a `"TRUMP"` position against a
`"Trump"` payload marked at entry price forever, and a trailing space additionally defeated
`_check_position_exits` so stop-loss and take-profit were never evaluated. Introduce a separate
identity canonicalization used at every keying site; keep the venue's display spelling. Make an
unmarkable position loud (log + a field on `BacktestResult`). Also fix `{"ask": null}` falling through
to entry price. Migration `007` to canonicalize existing `book_snapshots` rows.

**Acceptance.** Binary identities byte-identical to pre-task; `total_equity` 10062.00 not 10020.00;
`alembic heads` → `007`; suite green.

**Verify.** `cd backend && python3 -m pytest -q && alembic heads`

---

### T21e — Red-team remediation: near-resolution cap bypass, concurrent-submit lost update
- status: done
- model: opus
- depends: T21d
- independent: no

**Brief.** One untagged $0.50 order permanently blinded the near-resolution cap, because
`_upsert_position` never updates `intent_id` and the bucket aggregate attributed the whole position to
the opening intent ($1,200.50 of exposure against a $500 cap, fence reading $0.00). Two concurrent
`submit()` calls breached the cap AND lost a 900-lot to an unguarded select-then-write (venue filled
1802, ledger recorded 902). A misspelled bucket tag silently disabled the cap. Attribute bucket
exposure to the contributions that carry the tag; serialize the read-then-write sections; warn on an
unrecognized tag without raising.

**Acceptance.** Cap stops at $500 and stays stopped; trades == positions under concurrency; all six
misspellings warn; each test proven to fail pre-fix; suite green.

**Verify.** `cd backend && python3 -m pytest -q tests/execution/test_money_fences.py && python3 -m pytest -q`

---

### T21f — Red-team follow-up: `fill_engine` outcome comparison without strip
- status: done
- model: sonnet
- depends: T21d
- independent: no

**Brief.** `_check_book_matches` compared `casefold()` without `strip()`, so a whitespace-carrying book
outcome raised `ValueError` and aborted the ENTIRE backtest. `synthesize_book`'s internal dispatch had
the identical defect, and T21d's own fix had just widened the caller's gate to strip — so a stripped
label passed the gate and hit an unstripped consumer one line later. Route both through the shared
identity function.

**Acceptance.** `"Trump "` vs `"Trump"` fills; `"Trump"` vs `"Biden"` still raises; proven red-green;
suite green.

**Verify.** `cd backend && python3 -m pytest -q tests/execution/test_fill_engine.py && python3 -m pytest -q`

---

## Phase 4R — Post-review remediation (added during execution, not by the architect)

These tasks were dispatched by the execute loop in response to a Phase 4 review, two red-team passes,
and a verification pass. Recorded here so the routing ledger scores them.

### T26 — Docs and deployment: one-process rule, kill-switch name, venue API errors
- status: done
- model: sonnet
- independent: no

**Brief.** The production Dockerfile shipped `--workers 4` against a documented one-process rule; CLAUDE.md documented `TRADING_KILL_SWITCH_PATH` while the setting binds `KILL_SWITCH_PATH` (silently ignored); the Kalshi skill inverted the order-book encoding (100x price error) and the Polymarket skill omitted a basis-points division (10,000x fee error).

**Acceptance.** Dockerfile pins one worker with the reason at the CMD; CLAUDE.md matches `.env.example`; both skill files match their adapters.

**Verify.**
```bash
cd /Users/michaelcave/Developer/reposV2/polymarket-trader && grep -q 'KILL_SWITCH_PATH' CLAUDE.md && grep -q 'workers., .1' docker/backend/Dockerfile
```

---

### T27 — Frontend: label an ordinary backtest result, guard the edge-decay payload
- status: done
- model: sonnet
- independent: no

**Brief.** A non-sweep backtest rendered with `depth_source`/`fill_at` shown nowhere, violating GUARDRAILS 1.7. An unguarded `report.rows.map` white-screened the app on a malformed payload.

**Acceptance.** Badges visible without hover on the results view; a malformed payload degrades to a readable message; `tsc` and `npm run lint` exit 0.

**Verify.**
```bash
cd frontend && npx tsc -p tsconfig.app.json --noEmit && npm run lint
```

---

### T29 — Cross-venue fee basis per fill; persist `unmarked_positions`
- status: done
- model: opus
- independent: no

**Brief.** `cross_venue_arbitrage` priced its Kalshi leg as one aggregate fill, understating cost by up to 59% of its own `min_net_edge` gate. `build_report()` dropped `unmarked_positions`, so a partly fictional equity curve could never announce itself downstream.

**Acceptance.** Fee summed per walked level with a Polymarket control proving the change is Kalshi-specific; the field survives into the persisted report.

**Verify.**
```bash
cd backend && python3 -m pytest -q
```

---

### T30 — Schedule link proposal, still human-gated
- status: done
- model: opus
- independent: no

**Brief.** `propose_links` had one caller (a manual endpoint), so `event_links` stayed empty and cross-venue arbitrage was built over an empty list forever.

**Acceptance.** An hourly beat on its own interval; no path writes `approved`; a rejected link is never resurrected.

**Verify.**
```bash
cd backend && python3 -m pytest -q tests/matching/
```

---

### T31 — Price every risk haircut exactly once across strategies
- status: done
- model: opus
- independent: no

**Brief.** `composite` ranked four differently-defined quantities in one sorted list, and discounted cross-venue twice (inside its own `net_edge` and again via a +0.25 resolution-risk penalty).

**Acceptance.** Strategies publish a pre-risk edge; scoring applies each haircut once; two intents with equal economics score equal composite.

**Verify.**
```bash
cd backend && python3 -m pytest -q tests/services/test_scoring.py
```

---

### T32 — Reconcile the client with the API; sweep and link surfaces
- status: done
- model: sonnet
- independent: no

**Brief.** Twelve client/backend mismatches including a silent slippage no-op (`slippage_bps` discarded by `extra=ignore`). No UI producer for the sweep route and no link-review surface.

**Acceptance.** Results tab activates; sweep runnable from the form; link review shows both venues' rules text with no bulk approve; gates exit 0.

**Verify.**
```bash
cd frontend && npx tsc -p tsconfig.app.json --noEmit && npm run lint
```

---

### T33 — Reject unknown request fields; promote `edge_basis`; populate metrics
- status: done
- model: opus
- independent: no

**Brief.** All 37 request models inherited `extra=ignore`, so a caller could be wrong without being told — including a body naming `exchange` that routed an order to the wrong venue. `edge_basis` reached the API only inside a metadata blob. 11 of 15 metrics fields were never populated.

**Acceptance.** Six request models forbid extras with a structural test re-deriving the set from the route table; `edge_basis` is a typed field with no fallback label; metrics populate or stay null.

**Verify.**
```bash
cd backend && python3 -m pytest -q tests/api/
```

---

### T34 — Refuse to score a directional edge as arbitrage
- status: done
- model: sonnet
- independent: no

**Brief.** `favorite_compounder`/`no_bias_exploit` publish an `edge` that is a directional mispricing, and `POST /arbitrage/scan?strategies=` already reached `score()` with them.

**Acceptance.** An allowlist of scorable bases, checked before the edge is read; the four legitimate strategies score unchanged.

**Verify.**
```bash
cd backend && python3 -m pytest -q tests/services/test_scoring.py
```

---

### T35 — Bounded concurrent book fetch, interleaved across venues
- status: done
- model: sonnet
- independent: no

**Brief.** 800 sequential `get_book` calls per pass — a rate-limit risk and, worse, snapshot skew: a cross-venue signal claims two prices are inconsistent at the same moment.

**Acceptance.** One shared bounded semaphore, round-robined across venues; a failing venue does not abort the pass; the bound is a setting.

**Verify.**
```bash
cd backend && python3 -m pytest -q tests/services/test_scanner_concurrency.py
```

---

### T36 — Report an uncomputed metric as null and render it as such
- status: done
- model: sonnet
- independent: no

**Brief.** Eleven metrics fields can be legitimately absent, but the client typed all fifteen as `number` and the API coerced three nullable columns to 0.0.

**Acceptance.** Frontend handles null first, then the backend stops coercing; a genuine zero still renders as zero.

**Verify.**
```bash
cd frontend && npx tsc -p tsconfig.app.json --noEmit && cd ../backend && python3 -m pytest -q
```

---

### T37 — Hold the human-approval gate under concurrency
- status: done
- model: opus
- independent: no

**Brief.** The link-proposal beat read, checked decidedness, mutated and committed once per pass, so a human approving inside that window was silently clobbered — confidence and the hand-built outcome map both overwritten on an approved row.

**Acceptance.** Decidedness is enforced in the same statement as the write; a concurrent approval survives; an uncontested pass still rescores undecided rows.

**Verify.**
```bash
cd backend && python3 -m pytest -q tests/matching/test_link_write_races.py
```

---
