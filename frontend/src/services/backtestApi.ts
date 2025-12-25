import axios from 'axios';
import type { AxiosInstance } from 'axios';
import type {
  Backtest,
  BacktestRequest,
  BacktestTrade,
  BacktestMetrics,
  EquityPoint,
  StrategiesResponse,
  StrategyInfo,
  BacktestStatus,
} from '../types';

const API_BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000/api/v1';

export interface BacktestResponse {
  backtest_id: number;
  status: BacktestStatus;
  message: string;
}

export interface BacktestStatusResponse {
  backtest_id: number;
  status: BacktestStatus;
  progress: number;
  message: string;
  started_at?: string;
  completed_at?: string;
  error_message?: string;
  metrics?: BacktestMetrics;
}

export interface EquityCurveResponse {
  backtest_id: number;
  equity_curve: EquityPoint[];
  initial_capital: number;
  final_capital: number;
}

export interface TradesResponse {
  backtest_id: number;
  trades: BacktestTrade[];
  total_trades: number;
  page: number;
  page_size: number;
}

export interface BacktestListItem {
  id: number;
  strategy_name: string;
  status: BacktestStatus;
  start_date: string;
  end_date: string;
  initial_capital: number;
  final_capital?: number;
  total_return_pct?: number;
  sharpe_ratio?: number;
  total_trades: number;
  created_at: string;
  completed_at?: string;
}

export interface BacktestListResponse {
  backtests: BacktestListItem[];
  total: number;
  page: number;
  page_size: number;
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
    params?: { page?: number; page_size?: number }
  ): Promise<TradesResponse> {
    const { data } = await this.client.get<TradesResponse>(`/${backtestId}/trades`, { params });
    return data;
  }

  /**
   * List all backtests with pagination
   */
  async listBacktests(params?: {
    page?: number;
    page_size?: number;
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
