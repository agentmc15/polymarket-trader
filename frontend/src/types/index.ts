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

// Trader types
export interface Trader {
  id: number;
  address: string;
  username?: string;
  display_name?: string;
  bio?: string;
  avatar_url?: string;
  total_trades: number;
  winning_trades: number;
  losing_trades: number;
  win_rate: number;
  total_profit: number;
  total_volume: number;
  avg_position_size: number;
  first_trade_at?: string;
  last_trade_at?: string;
  is_tracked: boolean;
  track_priority: number;
}

export interface TraderFollow {
  id: number;
  trader_id: number;
  trader_address: string;
  is_active: boolean;
  copy_percentage: number;
  max_position_size: number;
  min_position_size: number;
  copy_buys: boolean;
  copy_sells: boolean;
  excluded_markets: string[];
  included_categories: string[];
  delay_seconds: number;
  max_slippage: number;
  total_copied_trades: number;
  total_profit: number;
}

// Strategy types
export type StrategyType = 'ARBITRAGE' | 'MOMENTUM' | 'MEAN_REVERSION' | 'MARKET_MAKING' | 'COPY_TRADING' | 'CUSTOM';

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

// Backtest types
export type BacktestStatus = 'PENDING' | 'RUNNING' | 'COMPLETED' | 'FAILED' | 'CANCELLED';

export interface Backtest {
  id: number;
  strategy_id: number;
  name?: string;
  start_date: string;
  end_date: string;
  initial_capital: number;
  parameters: Record<string, unknown>;
  status: BacktestStatus;
  progress: number;
  error_message?: string;
  final_capital?: number;
  total_return?: number;
  total_return_pct?: number;
  annualized_return?: number;
  sharpe_ratio?: number;
  sortino_ratio?: number;
  max_drawdown?: number;
  max_drawdown_pct?: number;
  volatility?: number;
  total_trades: number;
  winning_trades: number;
  losing_trades: number;
  win_rate?: number;
  avg_win?: number;
  avg_loss?: number;
  profit_factor?: number;
  started_at?: string;
  completed_at?: string;
  duration_seconds?: number;
  equity_curve: Array<{ date: string; value: number }>;
  created_at: string;
}

export interface BacktestTrade {
  id: number;
  backtest_id: number;
  market_condition_id: string;
  token_id: string;
  side: OrderSide;
  entry_price: number;
  exit_price?: number;
  size: number;
  fee: number;
  entry_time: string;
  exit_time?: string;
  pnl?: number;
  pnl_pct?: number;
  signal_name?: string;
  signal_strength?: number;
}

// Arbitrage types
export interface ArbitrageOpportunity {
  id: string;
  type: 'CROSS_MARKET' | 'INTERNAL' | 'EVENT';
  markets: string[];
  expected_profit: number;
  expected_profit_pct: number;
  required_capital: number;
  confidence: number;
  expires_at?: string;
  created_at: string;
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
