import { useState } from 'react';
import { cn } from '../../utils/cn';
import { formatCurrency, formatPercent, formatRelativeTime } from '../../utils/format';
import {
  useStrategies,
  useRunBacktest,
  useBacktest,
  useBacktests,
  useDeleteBacktest,
} from '../../hooks/useBacktesting';
import { BacktestForm } from './BacktestForm';
import { BacktestResults } from './BacktestResults';
import { TradeList } from './TradeList';
import type { BacktestRequest } from '../../types';
import type { BacktestListItem } from '../../services/backtestApi';

type TabView = 'new' | 'results' | 'history';

export function Backtesting() {
  const [activeTab, setActiveTab] = useState<TabView>('new');
  const [currentBacktestId, setCurrentBacktestId] = useState<number | null>(null);

  // Fetch strategies
  const { data: strategiesData, isLoading: isLoadingStrategies } = useStrategies();

  // Run backtest mutation
  const runBacktestMutation = useRunBacktest();

  // Current backtest data
  const backtest = useBacktest(currentBacktestId);

  // Backtest history
  const { data: historyData, isLoading: isLoadingHistory } = useBacktests({ page_size: 20 });

  // Delete mutation
  const deleteBacktestMutation = useDeleteBacktest();

  const handleRunBacktest = async (request: BacktestRequest) => {
    try {
      const result = await runBacktestMutation.mutateAsync(request);
      setCurrentBacktestId(result.backtest_id);
      setActiveTab('results');
    } catch (error) {
      console.error('Failed to start backtest:', error);
    }
  };

  const handleSelectBacktest = (backtestId: number) => {
    setCurrentBacktestId(backtestId);
    setActiveTab('results');
  };

  const handleDeleteBacktest = async (backtestId: number) => {
    if (confirm('Are you sure you want to delete this backtest?')) {
      await deleteBacktestMutation.mutateAsync(backtestId);
      if (currentBacktestId === backtestId) {
        setCurrentBacktestId(null);
        setActiveTab('history');
      }
    }
  };

  const handleNewBacktest = () => {
    setCurrentBacktestId(null);
    setActiveTab('new');
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-xl font-semibold">Backtesting</h2>
          <p className="text-sm text-muted-foreground">
            Test trading strategies against historical data
          </p>
        </div>
        {activeTab !== 'new' && (
          <button
            onClick={handleNewBacktest}
            className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90"
          >
            New Backtest
          </button>
        )}
      </div>

      {/* Tabs */}
      <div className="border-b border-border">
        <div className="flex gap-1">
          {[
            { id: 'new', label: 'New Backtest' },
            { id: 'results', label: 'Results', disabled: !currentBacktestId },
            { id: 'history', label: 'History' },
          ].map((tab) => (
            <button
              key={tab.id}
              onClick={() => !tab.disabled && setActiveTab(tab.id as TabView)}
              disabled={tab.disabled}
              className={cn(
                'px-4 py-2 text-sm font-medium transition-colors',
                activeTab === tab.id
                  ? 'border-b-2 border-primary text-primary'
                  : 'text-muted-foreground hover:text-foreground',
                tab.disabled && 'cursor-not-allowed opacity-50'
              )}
            >
              {tab.label}
            </button>
          ))}
        </div>
      </div>

      {/* Tab Content */}
      {activeTab === 'new' && (
        <BacktestForm
          strategies={strategiesData?.strategies ?? []}
          categories={strategiesData?.categories ?? {}}
          isLoadingStrategies={isLoadingStrategies}
          onSubmit={handleRunBacktest}
          isSubmitting={runBacktestMutation.isPending}
        />
      )}

      {activeTab === 'results' && currentBacktestId && (
        <div className="space-y-6">
          <BacktestResults
            status={backtest.status}
            progress={backtest.progress}
            metrics={backtest.metrics}
            equityCurve={backtest.equityCurve}
            initialCapital={backtest.initialCapital}
            finalCapital={backtest.finalCapital}
            errorMessage={backtest.errorMessage}
          />

          {backtest.status === 'COMPLETED' && (
            <div className="rounded-lg border border-border bg-card p-4">
              <div className="mb-4 flex items-center justify-between">
                <h3 className="font-medium">Trade History</h3>
                <span className="text-sm text-muted-foreground">
                  {backtest.trades.length} trades
                </span>
              </div>
              <TradeList trades={backtest.trades} isLoading={backtest.isLoadingTrades} />
            </div>
          )}
        </div>
      )}

      {activeTab === 'history' && (
        <BacktestHistory
          backtests={historyData?.backtests ?? []}
          isLoading={isLoadingHistory}
          onSelect={handleSelectBacktest}
          onDelete={handleDeleteBacktest}
          currentId={currentBacktestId}
        />
      )}
    </div>
  );
}

interface BacktestHistoryProps {
  backtests: BacktestListItem[];
  isLoading: boolean;
  onSelect: (id: number) => void;
  onDelete: (id: number) => void;
  currentId: number | null;
}

function BacktestHistory({
  backtests,
  isLoading,
  onSelect,
  onDelete,
  currentId,
}: BacktestHistoryProps) {
  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-12">
        <div className="h-8 w-8 animate-spin rounded-full border-4 border-primary border-t-transparent" />
      </div>
    );
  }

  if (backtests.length === 0) {
    return (
      <div className="rounded-lg border border-border bg-card p-8 text-center">
        <p className="text-muted-foreground">No backtests found</p>
        <p className="mt-2 text-sm text-muted-foreground">
          Run a backtest to see results here
        </p>
      </div>
    );
  }

  return (
    <div className="overflow-x-auto rounded-lg border border-border">
      <table className="w-full">
        <thead className="bg-muted/50">
          <tr>
            <th className="px-4 py-3 text-left text-sm font-medium">Strategy</th>
            <th className="px-4 py-3 text-left text-sm font-medium">Period</th>
            <th className="px-4 py-3 text-left text-sm font-medium">Status</th>
            <th className="px-4 py-3 text-right text-sm font-medium">Return</th>
            <th className="px-4 py-3 text-right text-sm font-medium">Sharpe</th>
            <th className="px-4 py-3 text-right text-sm font-medium">Trades</th>
            <th className="px-4 py-3 text-left text-sm font-medium">Created</th>
            <th className="px-4 py-3 text-right text-sm font-medium">Actions</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {backtests.map((bt) => (
            <tr
              key={bt.id}
              className={cn(
                'cursor-pointer hover:bg-muted/30',
                currentId === bt.id && 'bg-primary/5'
              )}
              onClick={() => onSelect(bt.id)}
            >
              <td className="px-4 py-3 text-sm font-medium">{bt.strategy_name}</td>
              <td className="px-4 py-3 text-sm text-muted-foreground">
                {new Date(bt.start_date).toLocaleDateString()} -{' '}
                {new Date(bt.end_date).toLocaleDateString()}
              </td>
              <td className="px-4 py-3 text-sm">
                <StatusBadge status={bt.status} />
              </td>
              <td
                className={cn(
                  'px-4 py-3 text-right text-sm font-mono',
                  (bt.total_return_pct ?? 0) > 0 && 'text-success',
                  (bt.total_return_pct ?? 0) < 0 && 'text-destructive'
                )}
              >
                {bt.total_return_pct !== undefined
                  ? `${bt.total_return_pct >= 0 ? '+' : ''}${formatPercent(bt.total_return_pct)}`
                  : '-'}
              </td>
              <td className="px-4 py-3 text-right text-sm font-mono">
                {bt.sharpe_ratio?.toFixed(2) ?? '-'}
              </td>
              <td className="px-4 py-3 text-right text-sm">{bt.total_trades}</td>
              <td className="px-4 py-3 text-sm text-muted-foreground">
                {formatRelativeTime(bt.created_at)}
              </td>
              <td className="px-4 py-3 text-right">
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    onDelete(bt.id);
                  }}
                  className="rounded p-1 text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
                >
                  <svg
                    className="h-4 w-4"
                    fill="none"
                    viewBox="0 0 24 24"
                    stroke="currentColor"
                  >
                    <path
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      strokeWidth={2}
                      d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"
                    />
                  </svg>
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  const styles: Record<string, string> = {
    PENDING: 'bg-yellow-500/20 text-yellow-400',
    RUNNING: 'bg-blue-500/20 text-blue-400',
    COMPLETED: 'bg-success/20 text-success',
    FAILED: 'bg-destructive/20 text-destructive',
    CANCELLED: 'bg-muted text-muted-foreground',
  };

  return (
    <span
      className={cn(
        'rounded px-2 py-0.5 text-xs font-medium capitalize',
        styles[status] ?? 'bg-muted text-muted-foreground'
      )}
    >
      {status.toLowerCase()}
    </span>
  );
}

export default Backtesting;
