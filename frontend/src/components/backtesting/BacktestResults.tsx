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
import type { BacktestMetrics, EquityPoint, BacktestStatus } from '../../types';

interface BacktestResultsProps {
  status: BacktestStatus;
  progress: number;
  metrics?: BacktestMetrics;
  equityCurve: EquityPoint[];
  initialCapital: number;
  finalCapital?: number;
  errorMessage?: string;
  strategyName?: string;
}

export function BacktestResults({
  status,
  progress,
  metrics,
  equityCurve,
  initialCapital,
  finalCapital,
  errorMessage,
  strategyName,
}: BacktestResultsProps) {
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
              {status === 'PENDING' ? 'Initializing backtest...' : 'Running backtest...'}
            </p>
            {strategyName && (
              <p className="text-sm text-muted-foreground">Strategy: {strategyName}</p>
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

  // Completed state - show results
  if (!metrics) {
    return (
      <div className="rounded-lg border border-border bg-card p-8 text-center">
        <p className="text-muted-foreground">No results available</p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Key Metrics Cards */}
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        <MetricCard
          label="Total Return"
          value={formatPercent(metrics.total_return_pct)}
          subValue={formatCurrency(metrics.total_return)}
          isPositive={metrics.total_return > 0}
          isNegative={metrics.total_return < 0}
        />
        <MetricCard
          label="Sharpe Ratio"
          value={formatNumber(metrics.sharpe_ratio, 2)}
          subValue={`Sortino: ${formatNumber(metrics.sortino_ratio, 2)}`}
          isPositive={metrics.sharpe_ratio > 1}
          isNegative={metrics.sharpe_ratio < 0}
        />
        <MetricCard
          label="Max Drawdown"
          value={formatPercent(metrics.max_drawdown_pct)}
          subValue={formatCurrency(metrics.max_drawdown)}
          isNegative={true}
        />
        <MetricCard
          label="Win Rate"
          value={formatPercent(metrics.win_rate_pct)}
          subValue={`${metrics.winning_trades}/${metrics.total_trades} trades`}
          isPositive={metrics.win_rate_pct > 50}
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
                formatter={(value: number) => [formatCurrency(value), 'Equity']}
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

      {/* Additional Metrics Grid */}
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
        <MetricsGroup
          title="Returns"
          metrics={[
            { label: 'Final Capital', value: formatCurrency(finalCapital ?? 0) },
            { label: 'Annualized Return', value: formatPercent(metrics.annualized_return_pct) },
            { label: 'Best Trade', value: formatCurrency(metrics.best_trade) },
            { label: 'Worst Trade', value: formatCurrency(metrics.worst_trade) },
            { label: 'Avg Trade', value: formatCurrency(metrics.avg_trade) },
          ]}
        />
        <MetricsGroup
          title="Risk"
          metrics={[
            { label: 'Volatility', value: formatPercent(metrics.volatility_pct) },
            { label: 'VaR (95%)', value: formatCurrency(metrics.var_95) },
            { label: 'CVaR (95%)', value: formatCurrency(metrics.cvar_95) },
            { label: 'Calmar Ratio', value: formatNumber(metrics.calmar_ratio, 2) },
            { label: 'Ulcer Index', value: formatNumber(metrics.ulcer_index, 2) },
          ]}
        />
        <MetricsGroup
          title="Trading"
          metrics={[
            { label: 'Total Trades', value: metrics.total_trades.toString() },
            { label: 'Profit Factor', value: formatNumber(metrics.profit_factor, 2) },
            { label: 'Avg Win', value: formatCurrency(metrics.avg_win) },
            { label: 'Avg Loss', value: formatCurrency(metrics.avg_loss) },
            { label: 'Expectancy', value: formatCurrency(metrics.expectancy) },
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
