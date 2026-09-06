// Market types
export interface Market {
  condition_id: string;
  question_id?: string;
  question: string;
  description?: string;
  category?: string;
  token_ids: Record<string, string>;
  outcomes: string[];
  outcome_prices: Record<string, number>;
  is_active: boolean;
  is_resolved: boolean;
  resolution_outcome?: string;
  end_date?: string;
  resolved_at?: string;
  volume_24h: number;
  total_volume: number;
  liquidity: number;
  created_at: string;
  updated_at: string;
}

export interface MarketPrice {
  market_id: number;
  timestamp: string;
  outcome: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

// Order types
export type OrderSide = 'BUY' | 'SELL';
export type OrderType = 'GTC' | 'GTD' | 'FOK';
export type OrderStatus = 'PENDING' | 'OPEN' | 'FILLED' | 'PARTIALLY_FILLED' | 'CANCELLED' | 'EXPIRED' | 'FAILED';

export interface Order {
  id: number;
  order_id: string;
  market_id: number;
  token_id: string;
  side: OrderSide;
  order_type: OrderType;
  status: OrderStatus;
  price: number;
  size: number;
  filled_size: number;
  remaining_size: number;
  expires_at?: string;
  filled_at?: string;
  tx_hash?: string;
  error_message?: string;
  created_at: string;
  updated_at: string;
}

export interface OrderRequest {
  condition_id: string;
  token_id: string;
  side: OrderSide;
  size: number;
  price: number;
  order_type?: OrderType;
}

// Trade types
export interface Trade {
  id: number;
  trade_id: string;
  order_id?: number;
  market_id: number;
  token_id: string;
  side: OrderSide;
  price: number;
  size: number;
  fee: number;
  maker_address?: string;
  taker_address?: string;
  tx_hash: string;
  block_number?: number;
  executed_at: string;
  created_at: string;
}

// Position types
export interface Position {
  id: number;
  market_id: number;
  token_id: string;
  outcome: string;
  size: number;
  avg_entry_price: number;
  total_cost: number;
  current_price: number;
  current_value: number;
  unrealized_pnl: number;
  unrealized_pnl_pct: number;
  realized_pnl: number;
  opened_at: string;
  closed_at?: string;
}

// Strategy types
export type StrategyType = 'ARBITRAGE' | 'MOMENTUM' | 'MEAN_REVERSION' | 'MARKET_MAKING' | 'CUSTOM';

export interface Strategy {
  id: number;
  name: string;
  description?: string;
  strategy_type: StrategyType;
  parameters: Record<string, unknown>;
  default_parameters: Record<string, unknown>;
  max_position_size: number;
  max_daily_loss: number;
  stop_loss_pct?: number;
  take_profit_pct?: number;
  is_active: boolean;
  is_backtested: boolean;
  total_trades: number;
  win_rate: number;
  avg_profit: number;
  sharpe_ratio?: number;
  max_drawdown?: number;
}

// Enhanced strategy info from new backend. Mirrors `StrategyInfo`
// (backend/app/api/routes/backtesting.py) field by field — there is no
// `display_name`; the registry only carries `name` (its key, e.g.
// `"catalyst_momentum"`), so a caller wanting a human-friendly label
// must derive one from `name` client-side (see `formatStrategyName` in
// `components/backtesting/StrategySelector.tsx`).
export interface StrategyInfo {
  name: string;
  description: string;
  version: string;
  category: string;
  default_config: Record<string, unknown>;
}

export interface StrategiesResponse {
  strategies: StrategyInfo[];
  categories: Record<string, string[]>;
}

// Bot types
export type BotStatus = 'CREATED' | 'STARTING' | 'RUNNING' | 'STOPPING' | 'STOPPED' | 'ERROR';

export interface Bot {
  id: number;
  name: string;
  strategy_id: number;
  status: BotStatus;
  parameters: Record<string, unknown>;
  max_position_size: number;
  max_daily_trades: number;
  max_daily_loss: number;
  enabled: boolean;
  trades_today: number;
  pnl_today: number;
  total_pnl: number;
  last_trade_at?: string;
  started_at?: string;
  stopped_at?: string;
  created_at: string;
  updated_at: string;
}

// Backtest types (PLAN.md D12, T22/T23) — mirror
// backend/app/api/routes/backtesting.py's pydantic models field by
// field. That file is the source of truth; do not add a field here
// that is not declared on the corresponding backend model, and do not
// rename one to something friendlier — a rename here is exactly the
// class of bug T32 exists to fix (the client silently drifting from
// `BacktestResponse.id`, `TradesResponse.total_count`, etc.).
export type BacktestStatus = 'PENDING' | 'RUNNING' | 'COMPLETED' | 'FAILED' | 'CANCELLED';
export type SlippageModel = 'none' | 'fixed' | 'volume_based' | 'spread_based';

// `BacktestRequest` (backtesting.py). `slippage_value` is a PROBABILITY-
// UNIT pad (default `0.001` = 0.1%, see `engine.py`'s
// `BacktestConfig.slippage_value` docstring) added to a BUY limit /
// subtracted from a SELL limit — it is NOT basis points, and a caller
// collecting a "basis points" UI value must divide by 10,000 before
// sending it here (see `BacktestForm`).
export interface BacktestRequest {
  strategy: string;
  start_date: string;
  end_date: string;
  initial_capital?: number;
  fee_rate?: number;
  slippage_model?: SlippageModel;
  slippage_value?: number;
  markets?: string[];
  strategy_config?: Record<string, unknown>;
}

// `SweepRequest` (backtesting.py) — `BacktestRequest` plus the capital
// levels to sweep (PLAN.md D12, T22). `POST /backtests/sweep` is
// asynchronous exactly like `POST /backtests`: it returns a `SweepResponse`
// immediately and the actual `EdgeDecayReport` arrives later at
// `GET /backtests/{id}/edge-decay` once the parent run completes.
export interface SweepRequest extends BacktestRequest {
  capital_levels?: number[];
}

// `TradeMetrics` (backtesting.py). As of this writing,
// `GET /backtests/{id}` only ever populates `total_trades` and
// `win_rate` from `BacktestRun` columns — every other field here is a
// real, typed part of the response, but currently always the pydantic
// default `0`. Do not render one of those as if it were a measured
// zero; `BacktestResults` only surfaces the two fields that are
// actually populated today.
export interface TradeMetrics {
  total_trades: number;
  winning_trades: number;
  losing_trades: number;
  win_rate: number;
  profit_factor: number;
  avg_win: number;
  avg_loss: number;
  largest_win: number;
  largest_loss: number;
}

// `RiskMetrics` (backtesting.py). Same caveat as `TradeMetrics`: only
// `sharpe_ratio` and `max_drawdown` are populated by
// `GET /backtests/{id}` today.
export interface RiskMetrics {
  sharpe_ratio: number;
  sortino_ratio: number;
  max_drawdown: number;
  max_drawdown_pct: number;
  volatility: number;
  var_95: number;
}

// `EquityCurvePoint` (backtesting.py) — one point of
// `GET /backtests/{id}/equity-curve`'s `points`. There is no
// `drawdown_pct`; `drawdown` is already `(peak - equity) / peak`.
export interface EquityPoint {
  timestamp: string;
  equity: number;
  drawdown: number;
}

// `TradeRecord` (backtesting.py) — one row of
// `GET /backtests/{id}/trades`'s `trades`. This is a single FILL, not a
// closed round-trip position: there is no `entry_time`/`exit_time`
// pairing, `token_id`, or `market_condition_id` on the wire, and `pnl`
// is `null` for a fill that has not (yet) realized a gain or loss.
export interface BacktestTrade {
  timestamp: string;
  market_id: string;
  outcome: string;
  side: OrderSide;
  price: number;
  size: number;
  fee: number;
  pnl: number | null;
  signal_confidence: number;
}

// Trading mode (backend/app/config.py `Settings.trading_mode`;
// `GET /api/v1/trading/mode`, backend/app/api/routes/trading.py)
export type TradingMode = 'paper' | 'live';

export interface TradingModeResponse {
  mode: TradingMode;
  kill_switch: boolean;
}

// Opportunity-discovery types (PLAN.md D10, T19/T23).
//
// Mirrors `app.services.scoring.OpportunityScore` (backend/app/services/
// scoring.py) and `app.api.routes.arbitrage.OpportunityOut` — read those
// before changing shape here, this is not a guess.

// One leg of a scored opportunity, as `arbitrage.py`'s `_leg_out` emits
// it field-by-field from `app.strategies.base.Leg`.
export interface OpportunityLeg {
  venue: string;
  market_id: string;
  outcome: string;
  side: OrderSide;
  limit_price: number;
  size_contracts: number | null;
  size_usd: number | null;
}

// The seven scoring components (`net_edge` .. `composite`) plus the two
// GUARDRAILS.md §1.7 labels (`link_status`, `depth_source`) every reader
// of a score must carry along wherever it displays a number derived
// from it.
export interface OpportunityScore {
  net_edge: number;
  annualized_return: number;
  hours_to_resolution: number;
  fill_confidence: number;
  resolution_risk: number;
  capital_lockup_usd: number;
  composite: number;
  link_status: string | null;
  depth_source: string;
}

// `OpportunityOut` (`GET /api/v1/arbitrage/opportunities`,
// `POST /api/v1/arbitrage/scan`) — an `OpportunityScore` plus enough
// identity/legs for a caller to act on the row.
export interface Opportunity {
  id: string;
  strategy: string;
  kind: string;
  status: string;
  mode: TradingMode;
  created_at: string | null;
  legs: OpportunityLeg[];
  net_edge: number;
  annualized_return: number;
  hours_to_resolution: number;
  fill_confidence: number;
  resolution_risk: number;
  capital_lockup_usd: number;
  composite: number;
  link_status: string | null;
  depth_source: string;
  metadata: Record<string, unknown>;
}

export interface OpportunitiesResponse {
  opportunities: Opportunity[];
  count: number;
}

export interface ScanResponse {
  status: string;
  mode: TradingMode;
  opportunities_found: number;
  opportunities: Opportunity[];
}

// Edge-decay / capital-sweep types (PLAN.md D12, T22/T23).
//
// Mirror `capital_row_to_dict`/`edge_decay_report_to_dict` in
// backend/app/services/backtesting/sweep.py — those functions define
// the JSON keys, not the `CapitalRow`/`EdgeDecayReport` dataclass field
// names (they happen to match here, but the dict builders are the
// contract).

// backend/app/services/backtesting/engine.py: `ResultDepthSource`/`FillAt`.
export type ResultDepthSource = 'synthetic' | 'recorded' | 'mixed';
export type FillAt = 'same' | 'next';

export interface EdgeDecayRow {
  capital: number;
  net_return: number;
  annualized: number;
  fill_rate: number;
  avg_slippage_bps: number;
  pct_intents_downsized: number;
  capital_utilization: number;
  trades: number;
  depth_source: ResultDepthSource;
  fill_at: FillAt;
  tick_unvalidated_fills: number;
  // Non-empty means part of this row's equity curve is a position marked
  // at its entry price for want of an observed market price (T21d) — the
  // row's `net_return`/`annualized` is then partly not a market number.
  unmarked_positions: string[];
  rejection_reasons: Record<string, number>;
  // `null` when `trades > 0`. `"no_signal"` is a genuine no-edge-at-this-
  // size reading; a `"structural: ..."` string means the strategy could
  // never be FILLED by this backtester at this level — not evidence
  // about edge at all (see `EdgeDecayReport.unmeasurable_note`).
  zero_trades_cause: string | null;
  downsize_trackable_intents: number;
  // T28: `pct_intents_downsized` above is the UNION of capital-cap-driven
  // and book-depth-driven shortfall and is NOT PLAN.md R4's depth signal
  // on its own (see `CapitalRow`'s docstring in sweep.py). These four
  // separate the two causes over `sized_intents` as the denominator.
  sized_intents: number;
  pct_intents_capital_capped: number;
  // PLAN.md R4's tripwire number: share of `sized_intents` whose walk
  // ran out of book. Expected to RISE with capital.
  pct_intents_depth_limited: number;
  depth_blocked_intents: number;
}

export interface EdgeDecayReport {
  rows: EdgeDecayRow[];
  edge_dies_at: number | null;
  depth_source: ResultDepthSource;
  fill_at: FillAt;
  // PLAN.md D12: ALWAYS populated, and must be PRINTED/DISPLAYED by any
  // caller showing `edge_dies_at`, not just persisted.
  sweep_ceiling_note: string;
  // Non-null means `edge_dies_at` is anchored to a zero-trade row whose
  // cause is structural, not a shrinking edge.
  unmeasurable_note: string | null;
}

// `BacktestRun.report` (backend/app/api/routes/backtesting.py
// `BacktestStatusResponse.report`) carries far more than modeled here
// (intent counters, settlement/coverage census); only the fields read by
// a caller are added as they're needed.
//
// `depth_source`/`fill_at` are written by `build_report()`
// (backend/app/tasks/backtesting.py) into EVERY completed run's report,
// not just a sweep's — GUARDRAILS.md §1.7 requires both to be labeled
// wherever the run's numbers are shown, so `BacktestResults` (an
// ordinary, non-sweep run) reads them too, not only `EdgeDecayTable`.
// An empty `{}` report (a run that predates report capture) means both
// are simply absent, not `"recorded"`/`"next"` — do not default them.
//
// `unmarked_positions` IS written unconditionally to a single run's
// top-level report by `build_report()` (backend/app/tasks/backtesting.py;
// T29), so a clean run persists `[]` there, not an absent key — an
// ABSENT `unmarked_positions` means this run predates the field, not
// that its equity curve is fully marked. It stays OPTIONAL here for
// exactly that reason: a pre-migration run's `report` genuinely has no
// such key, and `BacktestResults.tsx`'s badge already guards on
// `Array.isArray(...)` rather than assuming presence. A sweep also
// carries the same field per level at `edge_decay.rows[].unmarked_positions`
// (see `sweep.py`), independently of this top-level one.
export interface BacktestReport {
  depth_source?: ResultDepthSource | string;
  fill_at?: FillAt | string;
  unmarked_positions?: string[];
  edge_decay?: EdgeDecayReport;
}

// API response types
export interface PaginatedResponse<T> {
  data: T[];
  total: number;
  skip: number;
  limit: number;
}

export interface ApiError {
  detail: string;
  status_code: number;
}

// Cross-venue link review types (PLAN.md D9, T17) — mirror
// backend/app/api/routes/links.py's response models field by field.
// That module's docstring is the reason this exists at all: two
// markets a token-overlap score calls equivalent can settle
// differently, so a human reads both venues' `rules_text` and decides.
// `question`/`rules_text` are untrusted venue text (GUARDRAILS.md §6):
// they are rendered for a person to read, never executed or
// interpreted as instructions.
export type LinkStatus = 'proposed' | 'approved' | 'rejected';

// `LinkOut` — one persisted `event_links` row.
export interface EventLink {
  id: number;
  venue_a: string;
  market_a: string;
  venue_b: string;
  market_b: string;
  outcome_map: Record<string, string>;
  confidence: number;
  evidence: Record<string, unknown>;
  status: LinkStatus | string;
  reviewed_by: string | null;
  reviewed_at: string | null;
  notes: string | null;
  created_at: string | null;
  updated_at: string | null;
}

// `GET /links` response.
export interface LinkListResponse {
  links: EventLink[];
}

// `MarketSideOut` — one side of the `GET /links/{id}` comparison.
export interface LinkMarketSide {
  venue: string;
  market_id: string;
  question: string;
  outcomes: string[];
  close_time: string;
  close_time_iso: string;
  expected_settle_time: string | null;
  resolution_source: string | null;
  status: string;
  rules_text: string;
}

// `FieldComparison` — one field of the side-by-side comparison.
export interface LinkFieldComparison {
  field: string;
  a: string | null;
  b: string | null;
  same: boolean;
}

// `LinkReviewResponse` (`GET /links/{id}`) — named `...Payload` here,
// not `LinkReview`, so the type does not collide with the review-surface
// component it feeds (`components/links/LinkReview.tsx`).
export interface LinkReviewPayload {
  link: EventLink;
  market_a: LinkMarketSide | null;
  market_b: LinkMarketSide | null;
  comparison: LinkFieldComparison[];
  warnings: string[];
}

// `ApproveRequest` body for `POST /links/{id}/approve`. `outcome_map`
// omitted (or empty) falls back to the link's existing map — a
// multi-outcome pair with no map at all (matcher refuses to guess one)
// 422s until a reviewer supplies one explicitly.
export interface ApproveLinkRequest {
  reviewed_by: string;
  notes?: string;
  outcome_map?: Record<string, string> | null;
}

// `RejectRequest` body for `POST /links/{id}/reject`.
export interface RejectLinkRequest {
  reviewed_by: string;
  notes?: string;
}
