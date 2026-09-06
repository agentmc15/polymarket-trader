import axios from 'axios';
import type { AxiosInstance, AxiosError } from 'axios';
import type {
  Market,
  Order,
  OrderRequest,
  Position,
  Strategy,
  Bot,
  Backtest,
  BacktestTrade,
  OpportunitiesResponse,
  ScanResponse,
  EdgeDecayReport,
  TradingModeResponse,
  PaginatedResponse,
} from '../types';

const API_BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000/api/v1';

class ApiClient {
  private client: AxiosInstance;

  constructor() {
    this.client = axios.create({
      baseURL: API_BASE_URL,
      headers: {
        'Content-Type': 'application/json',
      },
    });

    // Response interceptor for error handling
    this.client.interceptors.response.use(
      (response) => response,
      (error: AxiosError) => {
        console.error('API Error:', error.response?.data || error.message);
        return Promise.reject(error);
      }
    );
  }

  // Markets
  async getMarkets(params?: {
    skip?: number;
    limit?: number;
    active?: boolean;
  }): Promise<PaginatedResponse<Market>> {
    const { data } = await this.client.get('/markets', { params });
    return data;
  }

  async getMarket(conditionId: string): Promise<Market> {
    const { data } = await this.client.get(`/markets/${conditionId}`);
    return data;
  }

  async getOrderbook(conditionId: string): Promise<{
    bids: Array<{ price: number; size: number }>;
    asks: Array<{ price: number; size: number }>;
  }> {
    const { data } = await this.client.get(`/markets/${conditionId}/orderbook`);
    return data;
  }

  async getPriceHistory(
    conditionId: string,
    interval?: string
  ): Promise<{
    candles: Array<{
      timestamp: string;
      open: number;
      high: number;
      low: number;
      close: number;
      volume: number;
    }>;
  }> {
    const { data } = await this.client.get(`/markets/${conditionId}/history`, {
      params: { interval },
    });
    return data;
  }

  // Trading
  async placeOrder(order: OrderRequest): Promise<{ order_id: string; status: string }> {
    const { data } = await this.client.post('/trading/orders', order);
    return data;
  }

  async cancelOrder(orderId: string): Promise<{ status: string }> {
    const { data } = await this.client.delete(`/trading/orders/${orderId}`);
    return data;
  }

  async getOrders(status?: string): Promise<{ orders: Order[] }> {
    const { data } = await this.client.get('/trading/orders', { params: { status } });
    return data;
  }

  async getPositions(): Promise<{ positions: Position[] }> {
    const { data } = await this.client.get('/trading/positions');
    return data;
  }

  async getPosition(conditionId: string): Promise<Position | null> {
    const { data } = await this.client.get(`/trading/positions/${conditionId}`);
    return data.position;
  }

  // Opportunities (PLAN.md D10, T19/T23) — GET /opportunities and
  // POST /scan are discovery-only; see backend/app/api/routes/arbitrage.py's
  // module docstring for why nothing here ever places or routes an order.
  async getOpportunities(params?: {
    minComposite?: number;
    venue?: string;
    nearResolution?: boolean;
  }): Promise<OpportunitiesResponse> {
    const { data } = await this.client.get<OpportunitiesResponse>('/arbitrage/opportunities', {
      params: {
        min_composite: params?.minComposite,
        venue: params?.venue,
        near_resolution: params?.nearResolution,
      },
    });
    return data;
  }

  async triggerScan(strategies?: string[]): Promise<ScanResponse> {
    const { data } = await this.client.post<ScanResponse>(
      '/arbitrage/scan',
      null,
      strategies ? { params: { strategies } } : undefined
    );
    return data;
  }

  // Backtesting — edge decay (PLAN.md D12, T22/T23)
  async getEdgeDecay(backtestId: number): Promise<EdgeDecayReport> {
    const { data } = await this.client.get<EdgeDecayReport>(
      `/backtests/${backtestId}/edge-decay`
    );
    return data;
  }

  // Trading mode (GUARDRAILS.md §1.2 — paper/live, read-only here)
  async getTradingMode(): Promise<TradingModeResponse> {
    const { data } = await this.client.get<TradingModeResponse>('/trading/mode');
    return data;
  }

  // Backtesting
  async getBacktests(params?: {
    skip?: number;
    limit?: number;
  }): Promise<PaginatedResponse<Backtest>> {
    const { data } = await this.client.get('/backtests', { params });
    return data;
  }

  async runBacktest(request: {
    strategy_id: string;
    start_date: string;
    end_date: string;
    initial_capital?: number;
    parameters?: Record<string, unknown>;
  }): Promise<Backtest> {
    const { data } = await this.client.post('/backtests', request);
    return data;
  }

  async getBacktest(backtestId: string): Promise<Backtest> {
    const { data } = await this.client.get(`/backtests/${backtestId}`);
    return data;
  }

  async getBacktestTrades(backtestId: string): Promise<{ trades: BacktestTrade[] }> {
    const { data } = await this.client.get(`/backtests/${backtestId}/trades`);
    return data;
  }

  async getStrategies(): Promise<{ strategies: Strategy[] }> {
    const { data } = await this.client.get('/backtests/strategies');
    return data;
  }

  // Bots
  async getBots(): Promise<{ bots: Bot[] }> {
    const { data } = await this.client.get('/bots');
    return data;
  }

  async createBot(config: {
    name: string;
    strategy_id: string;
    parameters?: Record<string, unknown>;
    max_position_size?: number;
    max_daily_trades?: number;
  }): Promise<Bot> {
    const { data } = await this.client.post('/bots', config);
    return data;
  }

  async getBot(botId: string): Promise<Bot> {
    const { data } = await this.client.get(`/bots/${botId}`);
    return data;
  }

  async updateBot(
    botId: string,
    config: Partial<{
      name: string;
      strategy_id: string;
      parameters: Record<string, unknown>;
      max_position_size: number;
      max_daily_trades: number;
      enabled: boolean;
    }>
  ): Promise<Bot> {
    const { data } = await this.client.put(`/bots/${botId}`, config);
    return data;
  }

  async deleteBot(botId: string): Promise<{ status: string }> {
    const { data } = await this.client.delete(`/bots/${botId}`);
    return data;
  }

  async startBot(botId: string): Promise<Bot> {
    const { data } = await this.client.post(`/bots/${botId}/start`);
    return data;
  }

  async stopBot(botId: string): Promise<Bot> {
    const { data } = await this.client.post(`/bots/${botId}/stop`);
    return data;
  }

  async getBotTrades(
    botId: string,
    params?: { skip?: number; limit?: number }
  ): Promise<{ trades: Trade[]; total: number }> {
    const { data } = await this.client.get(`/bots/${botId}/trades`, { params });
    return data;
  }
}

// Trade type for bot trades
interface Trade {
  id: number;
  trade_id: string;
  market_id: number;
  side: 'BUY' | 'SELL';
  price: number;
  size: number;
  executed_at: string;
}

export const api = new ApiClient();
export default api;
