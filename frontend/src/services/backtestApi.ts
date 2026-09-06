import axios from 'axios';
import type { AxiosInstance } from 'axios';
import type {
  BacktestReport,
  BacktestRequest,
  SweepRequest,
  BacktestTrade,
  TradeMetrics,
  RiskMetrics,
  EquityPoint,
  StrategiesResponse,
  BacktestStatus,
} from '../types';

const API_BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000/api/v1';

// Response envelope types below mirror
// backend/app/api/routes/backtesting.py's pydantic response models
// field by field — that file is the source of truth (T32). Field NAMES
// here must match the backend exactly; a friendlier rename (`id` ->
// `backtest_id`, `points` -> `equity_curve`, `total_count` ->
// `total_trades`) is exactly the class of drift that left the Results
// tab permanently inactive.

// `BacktestResponse` (`POST /backtests`).
export interface BacktestResponse {
  id: number;
  status: string;
  message: string;
}

// `SweepResponse` (`POST /backtests/sweep`, PLAN.md D12/T22). Also
// asynchronous: this is the PARENT run's id and initial `"PENDING"`
// status, not the eventual `EdgeDecayReport` — that arrives later at
// `GET /backtests/{id}/edge-decay` once the sweep completes.
export interface SweepResponse {
  id: number;
  status: string;
  message: string;
}

// `BacktestStatusResponse` (`GET /backtests/{id}`). `trade_metrics`/
// `risk_metrics` are `null` until the run reaches `COMPLETED`.
export interface BacktestStatusResponse {
  id: number;
  strategy_name: string;
  strategy_config: Record<string, unknown>;
  status: BacktestStatus;
  start_date: string;
  end_date: string;
  initial_capital: number;
  fee_rate: number;
  final_value: number | null;
  total_return: number | null;
  total_return_pct: number | null;
  trade_metrics: TradeMetrics | null;
  risk_metrics: RiskMetrics | null;
  //: Trustworthiness/coverage payload (GUARDRAILS.md §1.7). Carries
  //: `depth_source`/`fill_at` on every completed run (not just a
  //: sweep's `edge_decay`) — `BacktestResults` labels an ordinary run
  //: with these; `EdgeDecayTable` labels a sweep's rows.
  report: BacktestReport;
  progress: number;
  error_message: string | null;
  created_at: string;
  completed_at: string | null;
}

// `EquityCurveResponse` (`GET /backtests/{id}/equity-curve`).
export interface EquityCurveResponse {
  backtest_id: number;
  points: EquityPoint[];
  initial_capital: number;
  final_value: number;
}

// `TradesResponse` (`GET /backtests/{id}/trades`). No `page`/`page_size`
// on the wire — pagination is `skip`/`limit` request params only.
export interface TradesResponse {
  backtest_id: number;
  trades: BacktestTrade[];
  total_count: number;
}

// `BacktestListItem` (`GET /backtests`).
export interface BacktestListItem {
  id: number;
  strategy_name: string;
  status: BacktestStatus;
  start_date: string;
  end_date: string;
  initial_capital: number;
  final_value: number | null;
  total_return: number | null;
  sharpe_ratio: number | null;
  max_drawdown: number | null;
  total_trades: number;
  created_at: string;
}

// `BacktestListResponse` (`GET /backtests`).
export interface BacktestListResponse {
  backtests: BacktestListItem[];
  total: number;
  skip: number;
  limit: number;
}

class BacktestApiClient {
  private client: AxiosInstance;

  constructor() {
    this.client = axios.create({
      baseURL: `${API_BASE_URL}/backtests`,
      headers: {
        'Content-Type': 'application/json',
      },
    });

    this.client.interceptors.response.use(
      (response) => response,
      (error) => {
        console.error('Backtest API Error:', error.response?.data || error.message);
        return Promise.reject(error);
      }
    );
  }

  /**
   * Get list of available strategies
   */
  async getStrategies(): Promise<StrategiesResponse> {
    const { data } = await this.client.get<StrategiesResponse>('/strategies');
    return data;
  }

  /**
   * Start a new backtest
   */
  async runBacktest(request: BacktestRequest): Promise<BacktestResponse> {
    const { data } = await this.client.post<BacktestResponse>('', request);
    return data;
  }

  /**
   * Start a capital sweep (PLAN.md D12, T22). Asynchronous — the
   * returned `id` is the PARENT run; poll `GET /{id}` for status the
   * same way as an ordinary run, then read
   * `GET /{id}/edge-decay` once it completes.
   */
  async runSweep(request: SweepRequest): Promise<SweepResponse> {
    const { data } = await this.client.post<SweepResponse>('/sweep', request);
    return data;
  }

  /**
   * Get backtest status and results
   */
  async getBacktestStatus(backtestId: number): Promise<BacktestStatusResponse> {
    const { data } = await this.client.get<BacktestStatusResponse>(`/${backtestId}`);
    return data;
  }

  /**
   * Get equity curve data for a completed backtest
   */
  async getEquityCurve(backtestId: number): Promise<EquityCurveResponse> {
    const { data } = await this.client.get<EquityCurveResponse>(`/${backtestId}/equity-curve`);
    return data;
  }

  /**
   * Get trades list for a backtest
   */
  async getTrades(
    backtestId: number,
    params?: { skip?: number; limit?: number }
  ): Promise<TradesResponse> {
    const { data } = await this.client.get<TradesResponse>(`/${backtestId}/trades`, { params });
    return data;
  }

  /**
   * List all backtests with pagination
   */
  async listBacktests(params?: {
    skip?: number;
    limit?: number;
    status?: BacktestStatus;
    strategy?: string;
  }): Promise<BacktestListResponse> {
    const { data } = await this.client.get<BacktestListResponse>('', { params });
    return data;
  }

  /**
   * Delete a backtest
   */
  async deleteBacktest(backtestId: number): Promise<{ message: string }> {
    const { data } = await this.client.delete<{ message: string }>(`/${backtestId}`);
    return data;
  }
}

export const backtestApi = new BacktestApiClient();
export default backtestApi;
