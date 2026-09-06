import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  ReferenceLine,
} from 'recharts';
import { cn } from '../../utils/cn';
import { formatCurrency, formatPercent, formatNumber, formatDateTime } from '../../utils/format';
import { DepthSourceBadge, FillAtBadge } from './DepthBadges';
import type {
  BacktestReport,
  EquityPoint,
  BacktestStatus,
  TradeMetrics,
  RiskMetrics,
} from '../../types';

interface BacktestResultsProps {
  status: BacktestStatus;
  progress: number;
  //: `null`/`undefined` until the run reaches `COMPLETED`
  //: (`BacktestStatusResponse`, backend/app/api/routes/backtesting.py).
  totalReturn?: number | null;
  totalReturnPct?: number | null;
  tradeMetrics?: TradeMetrics | null;
  riskMetrics?: RiskMetrics | null;
  equityCurve: EquityPoint[];
  initialCapital: number;
  finalValue?: number | null;
  errorMessage?: string;
  strategyName?: string;
  //: GUARDRAILS.md §1.7: `depth_source`/`fill_at` are written into
  //: EVERY completed run's report (`build_report()`,
  //: backend/app/tasks/backtesting.py), not just a sweep's — this is
  //: what an ordinary run's result must be labeled with.
  report?: BacktestReport;
}

export function BacktestResults({
  status,
  progress,
  totalReturn,
  totalReturnPct,
  tradeMetrics,
  riskMetrics,
  equityCurve,
  initialCapital,
  finalValue,
  errorMessage,
  strategyName,
  report,
}: BacktestResultsProps) {
  const isSweep = strategyName?.startsWith('sweep:') ?? false;

  // Loading/Pending state
  if (status === 'PENDING' || status === 'RUNNING') {
    return (
      <div className="rounded-lg border border-border bg-card p-8">
        <div className="flex flex-col items-center justify-center space-y-4">
          <div className="relative h-16 w-16">
            <svg className="h-16 w-16 animate-spin" viewBox="0 0 24 24">
              <circle
                className="opacity-25"
                cx="12"
                cy="12"
                r="10"
                stroke="currentColor"
                strokeWidth="3"
                fill="none"
              />
              <path
                className="opacity-75"
                fill="currentColor"
                d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
              />
            </svg>
          </div>
          <div className="text-center">
            <p className="text-lg font-medium">
              {isSweep
                ? status === 'PENDING'
                  ? 'Sweep queued...'
                  : 'Running capital sweep...'
                : status === 'PENDING'
                  ? 'Initializing backtest...'
                  : 'Running backtest...'}
            </p>
            {strategyName && (
              <p className="text-sm text-muted-foreground">Strategy: {strategyName}</p>
            )}
            {isSweep && (
              <p className="mt-1 text-sm text-muted-foreground">
                This runs one full backtest per capital level. The per-level breakdown
                appears below once every level has completed.
              </p>
            )}
          </div>
          <div className="w-64">
            <div className="mb-1 flex justify-between text-sm">
              <span>Progress</span>
              <span>{Math.round(progress * 100)}%</span>
            </div>
            <div className="h-2 overflow-hidden rounded-full bg-muted">
              <div
                className="h-full bg-primary transition-all duration-300"
                style={{ width: `${progress * 100}%` }}
              />
            </div>
          </div>
        </div>
      </div>
    );
  }

  // Failed state
  if (status === 'FAILED') {
    return (
      <div className="rounded-lg border border-destructive/50 bg-destructive/10 p-8">
        <div className="flex flex-col items-center justify-center space-y-4 text-center">
          <svg className="h-12 w-12 text-destructive" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
          </svg>
          <div>
            <p className="text-lg font-medium text-destructive">Backtest Failed</p>
            {errorMessage && (
              <p className="mt-2 text-sm text-muted-foreground">{errorMessage}</p>
            )}
          </div>
        </div>
      </div>
    );
  }

  // Cancelled state
  if (status === 'CANCELLED') {
    return (
      <div className="rounded-lg border border-border bg-card p-8 text-center">
        <p className="text-lg font-medium text-muted-foreground">Backtest was cancelled</p>
      </div>
    );
  }

  // Completed state - show results. `trade_metrics`/`risk_metrics` are
  // populated together by `get_backtest_status` once `status ===
  // "COMPLETED"` — see `BacktestStatusResponse` in
  // backend/app/api/routes/backtesting.py.
  if (!tradeMetrics || !riskMetrics) {
    return (
      <div className="rounded-lg border border-border bg-card p-8 text-center">
        <p className="text-muted-foreground">No results available</p>
      </div>
    );
  }

  const netGainLoss = finalValue != null ? finalValue - initialCapital : null;

  return (
    <div className="space-y-6">
      {/* GUARDRAILS.md §1.7: every completed run's `depth_source`/
          `fill_at` are labeled here, visibly, unconditionally — not
          only for a sweep (that's `EdgeDecayTable`'s per-row labeling).
          A missing `report` (a run recorded before report capture
          existed) renders NOTHING here rather than a false "recorded"/
          "next" default — see `BacktestReport`'s doc comment. */}
      {report && (report.depth_source !== undefined || report.fill_at !== undefined) && (
        <div className="flex flex-wrap items-center gap-2 rounded-lg border border-border bg-card p-3 text-sm">
          {report.depth_source !== undefined && (
            <>
              <span className="text-muted-foreground">Depth source:</span>
              <DepthSourceBadge depthSource={report.depth_source} />
            </>
          )}
          {report.fill_at !== undefined && (
            <>
              <span className="text-muted-foreground">Fill at:</span>
              <FillAtBadge fillAt={report.fill_at} />
            </>
          )}
          {Array.isArray(report.unmarked_positions) && report.unmarked_positions.length > 0 && (
            <span
              className="rounded bg-warning/20 px-1.5 py-0.5 text-xs font-medium text-warning"
              title={report.unmarked_positions.join(', ')}
            >
              {report.unmarked_positions.length} position(s) unmarked — equity curve partly
              fictional
            </span>
          )}
        </div>
      )}

      {/* Key Metrics Cards. Only fields `GET /backtests/{id}` actually
          populates today (`total_return`/`total_return_pct` top-level,
          `risk_metrics.sharpe_ratio`/`max_drawdown`,
          `trade_metrics.win_rate`/`total_trades`) — see `TradeMetrics`/
          `RiskMetrics`'s doc comments in types/index.ts for why the
          rest of those pydantic models isn't rendered as if it were a
          measured number. */}
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        <MetricCard
          label="Total Return"
          value={totalReturnPct != null ? formatPercent(totalReturnPct) : '-'}
          subValue={netGainLoss != null ? formatCurrency(netGainLoss) : undefined}
          isPositive={(totalReturn ?? 0) > 0}
          isNegative={(totalReturn ?? 0) < 0}
        />
        <MetricCard
          label="Sharpe Ratio"
          value={formatNumber(riskMetrics.sharpe_ratio, 2)}
          isPositive={riskMetrics.sharpe_ratio > 1}
          isNegative={riskMetrics.sharpe_ratio < 0}
        />
        <MetricCard
          label="Max Drawdown"
          value={formatPercent(riskMetrics.max_drawdown * 100)}
          isNegative={riskMetrics.max_drawdown > 0}
        />
        <MetricCard
          label="Win Rate"
          value={formatPercent(tradeMetrics.win_rate * 100)}
          subValue={`${tradeMetrics.total_trades} trades`}
          isPositive={tradeMetrics.win_rate > 0.5}
        />
      </div>

      {/* Equity Curve Chart */}
      <div className="rounded-lg border border-border bg-card p-4">
        <h3 className="mb-4 font-medium">Equity Curve</h3>
        <div className="h-80">
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart
              data={equityCurve.map((point) => ({
                ...point,
                date: new Date(point.timestamp).getTime(),
              }))}
              margin={{ top: 10, right: 30, left: 0, bottom: 0 }}
            >
              <defs>
                <linearGradient id="equityGradient" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor="hsl(var(--primary))" stopOpacity={0.3} />
                  <stop offset="95%" stopColor="hsl(var(--primary))" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
              <XAxis
                dataKey="date"
                type="number"
                domain={['dataMin', 'dataMax']}
                tickFormatter={(value) => {
                  const date = new Date(value);
                  return `${date.getMonth() + 1}/${date.getDate()}`;
                }}
                stroke="hsl(var(--muted-foreground))"
                fontSize={12}
              />
              <YAxis
                tickFormatter={(value) => `$${(value / 1000).toFixed(0)}k`}
                stroke="hsl(var(--muted-foreground))"
                fontSize={12}
              />
              <Tooltip
                contentStyle={{
                  backgroundColor: 'hsl(var(--card))',
                  border: '1px solid hsl(var(--border))',
                  borderRadius: '8px',
                }}
                labelFormatter={(value) => formatDateTime(new Date(value))}
                formatter={(value: number | undefined) => [
                  formatCurrency(value ?? 0),
                  'Equity',
                ]}
              />
              <ReferenceLine
                y={initialCapital}
                stroke="hsl(var(--muted-foreground))"
                strokeDasharray="3 3"
                label={{
                  value: 'Initial',
                  position: 'left',
                  fill: 'hsl(var(--muted-foreground))',
                  fontSize: 12,
                }}
              />
              <Area
                type="monotone"
                dataKey="equity"
                stroke="hsl(var(--primary))"
                strokeWidth={2}
                fill="url(#equityGradient)"
              />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      </div>

      {/* Additional Metrics Grid — every value below is a field
          `GET /backtests/{id}` actually returns (`TradeMetrics`/
          `RiskMetrics`), not a value this component invents. */}
      <div className="grid gap-4 md:grid-cols-2">
        <MetricsGroup
          title="Returns"
          metrics={[
            { label: 'Initial Capital', value: formatCurrency(initialCapital) },
            { label: 'Final Value', value: finalValue != null ? formatCurrency(finalValue) : '-' },
            {
              label: 'Net Gain/Loss',
              value: netGainLoss != null ? formatCurrency(netGainLoss) : '-',
            },
          ]}
        />
        <MetricsGroup
          title="Trading"
          metrics={[
            { label: 'Total Trades', value: tradeMetrics.total_trades.toString() },
            { label: 'Win Rate', value: formatPercent(tradeMetrics.win_rate * 100) },
            { label: 'Sharpe Ratio', value: formatNumber(riskMetrics.sharpe_ratio, 2) },
            { label: 'Max Drawdown', value: formatPercent(riskMetrics.max_drawdown * 100) },
          ]}
        />
      </div>
    </div>
  );
}

interface MetricCardProps {
  label: string;
  value: string;
  subValue?: string;
  isPositive?: boolean;
  isNegative?: boolean;
}

function MetricCard({ label, value, subValue, isPositive, isNegative }: MetricCardProps) {
  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <p className="text-sm text-muted-foreground">{label}</p>
      <p
        className={cn(
          'mt-1 text-2xl font-bold',
          isPositive && 'text-success',
          isNegative && 'text-destructive',
          !isPositive && !isNegative && 'text-foreground'
        )}
      >
        {value}
      </p>
      {subValue && <p className="mt-1 text-sm text-muted-foreground">{subValue}</p>}
    </div>
  );
}

interface MetricsGroupProps {
  title: string;
  metrics: { label: string; value: string }[];
}

function MetricsGroup({ title, metrics }: MetricsGroupProps) {
  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <h4 className="mb-3 font-medium">{title}</h4>
      <div className="space-y-2">
        {metrics.map((metric) => (
          <div key={metric.label} className="flex items-center justify-between text-sm">
            <span className="text-muted-foreground">{metric.label}</span>
            <span className="font-medium">{metric.value}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

export default BacktestResults;
