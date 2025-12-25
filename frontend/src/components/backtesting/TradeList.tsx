import { useState, useMemo } from 'react';
import { cn } from '../../utils/cn';
import { formatCurrency, formatPercent, formatDateTime } from '../../utils/format';
import type { BacktestTrade } from '../../types';

interface TradeListProps {
  trades: BacktestTrade[];
  isLoading?: boolean;
}

type SortField = 'entry_time' | 'exit_time' | 'side' | 'entry_price' | 'size' | 'pnl' | 'pnl_pct';
type SortDirection = 'asc' | 'desc';
type SideFilter = 'all' | 'BUY' | 'SELL';
type PnLFilter = 'all' | 'winners' | 'losers';

export function TradeList({ trades, isLoading }: TradeListProps) {
  const [sortField, setSortField] = useState<SortField>('entry_time');
  const [sortDirection, setSortDirection] = useState<SortDirection>('desc');
  const [sideFilter, setSideFilter] = useState<SideFilter>('all');
  const [pnlFilter, setPnlFilter] = useState<PnLFilter>('all');
  const [searchQuery, setSearchQuery] = useState('');

  // Filter and sort trades
  const filteredTrades = useMemo(() => {
    let result = [...trades];

    // Apply side filter
    if (sideFilter !== 'all') {
      result = result.filter((t) => t.side === sideFilter);
    }

    // Apply P&L filter
    if (pnlFilter === 'winners') {
      result = result.filter((t) => (t.pnl ?? 0) > 0);
    } else if (pnlFilter === 'losers') {
      result = result.filter((t) => (t.pnl ?? 0) < 0);
    }

    // Apply search filter
    if (searchQuery) {
      const query = searchQuery.toLowerCase();
      result = result.filter(
        (t) =>
          t.market_id.toLowerCase().includes(query) ||
          t.outcome.toLowerCase().includes(query)
      );
    }

    // Sort
    result.sort((a, b) => {
      let comparison = 0;
      switch (sortField) {
        case 'entry_time':
          comparison = new Date(a.entry_time).getTime() - new Date(b.entry_time).getTime();
          break;
        case 'exit_time':
          comparison =
            new Date(a.exit_time ?? 0).getTime() - new Date(b.exit_time ?? 0).getTime();
          break;
        case 'side':
          comparison = a.side.localeCompare(b.side);
          break;
        case 'entry_price':
          comparison = a.entry_price - b.entry_price;
          break;
        case 'size':
          comparison = a.size - b.size;
          break;
        case 'pnl':
          comparison = (a.pnl ?? 0) - (b.pnl ?? 0);
          break;
        case 'pnl_pct':
          comparison = (a.pnl_pct ?? 0) - (b.pnl_pct ?? 0);
          break;
      }
      return sortDirection === 'asc' ? comparison : -comparison;
    });

    return result;
  }, [trades, sortField, sortDirection, sideFilter, pnlFilter, searchQuery]);

  const handleSort = (field: SortField) => {
    if (sortField === field) {
      setSortDirection((prev) => (prev === 'asc' ? 'desc' : 'asc'));
    } else {
      setSortField(field);
      setSortDirection('desc');
    }
  };

  const SortIcon = ({ field }: { field: SortField }) => {
    if (sortField !== field) return null;
    return (
      <span className="ml-1 inline-block">
        {sortDirection === 'asc' ? '\u2191' : '\u2193'}
      </span>
    );
  };

  if (isLoading) {
    return (
      <div className="rounded-lg border border-border bg-card p-8">
        <div className="flex items-center justify-center">
          <div className="h-8 w-8 animate-spin rounded-full border-4 border-primary border-t-transparent" />
        </div>
      </div>
    );
  }

  if (trades.length === 0) {
    return (
      <div className="rounded-lg border border-border bg-card p-8 text-center">
        <p className="text-muted-foreground">No trades recorded during this backtest</p>
      </div>
    );
  }

  // Calculate summary stats
  const totalPnL = filteredTrades.reduce((sum, t) => sum + (t.pnl ?? 0), 0);
  const winningTrades = filteredTrades.filter((t) => (t.pnl ?? 0) > 0).length;
  const losingTrades = filteredTrades.filter((t) => (t.pnl ?? 0) < 0).length;

  return (
    <div className="space-y-4">
      {/* Filters */}
      <div className="flex flex-wrap items-center gap-4">
        <input
          type="text"
          placeholder="Search markets..."
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          className="rounded-md border border-input bg-background px-3 py-2 text-sm"
        />

        <div className="flex items-center gap-2">
          <span className="text-sm text-muted-foreground">Side:</span>
          <select
            value={sideFilter}
            onChange={(e) => setSideFilter(e.target.value as SideFilter)}
            className="rounded-md border border-input bg-background px-3 py-1.5 text-sm"
          >
            <option value="all">All</option>
            <option value="BUY">Buy</option>
            <option value="SELL">Sell</option>
          </select>
        </div>

        <div className="flex items-center gap-2">
          <span className="text-sm text-muted-foreground">Result:</span>
          <select
            value={pnlFilter}
            onChange={(e) => setPnlFilter(e.target.value as PnLFilter)}
            className="rounded-md border border-input bg-background px-3 py-1.5 text-sm"
          >
            <option value="all">All</option>
            <option value="winners">Winners</option>
            <option value="losers">Losers</option>
          </select>
        </div>

        <div className="ml-auto flex items-center gap-4 text-sm">
          <span className="text-muted-foreground">
            {filteredTrades.length} trades
          </span>
          <span className={cn('font-medium', totalPnL >= 0 ? 'text-success' : 'text-destructive')}>
            {totalPnL >= 0 ? '+' : ''}{formatCurrency(totalPnL)}
          </span>
          <span className="text-success">{winningTrades}W</span>
          <span className="text-destructive">{losingTrades}L</span>
        </div>
      </div>

      {/* Table */}
      <div className="overflow-x-auto rounded-lg border border-border">
        <table className="w-full">
          <thead className="bg-muted/50">
            <tr>
              <th
                className="cursor-pointer px-4 py-3 text-left text-sm font-medium"
                onClick={() => handleSort('entry_time')}
              >
                Entry Time
                <SortIcon field="entry_time" />
              </th>
              <th className="px-4 py-3 text-left text-sm font-medium">Market</th>
              <th
                className="cursor-pointer px-4 py-3 text-left text-sm font-medium"
                onClick={() => handleSort('side')}
              >
                Side
                <SortIcon field="side" />
              </th>
              <th
                className="cursor-pointer px-4 py-3 text-right text-sm font-medium"
                onClick={() => handleSort('entry_price')}
              >
                Price
                <SortIcon field="entry_price" />
              </th>
              <th
                className="cursor-pointer px-4 py-3 text-right text-sm font-medium"
                onClick={() => handleSort('size')}
              >
                Size
                <SortIcon field="size" />
              </th>
              <th
                className="cursor-pointer px-4 py-3 text-right text-sm font-medium"
                onClick={() => handleSort('pnl')}
              >
                P&L
                <SortIcon field="pnl" />
              </th>
              <th
                className="cursor-pointer px-4 py-3 text-right text-sm font-medium"
                onClick={() => handleSort('pnl_pct')}
              >
                P&L %
                <SortIcon field="pnl_pct" />
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {filteredTrades.map((trade) => (
              <TradeRow key={trade.id} trade={trade} />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

interface TradeRowProps {
  trade: BacktestTrade;
}

function TradeRow({ trade }: TradeRowProps) {
  const [isExpanded, setIsExpanded] = useState(false);
  const pnl = trade.pnl ?? 0;
  const pnlPct = trade.pnl_pct ?? 0;

  return (
    <>
      <tr
        className="cursor-pointer hover:bg-muted/30"
        onClick={() => setIsExpanded(!isExpanded)}
      >
        <td className="px-4 py-3 text-sm">
          {formatDateTime(trade.entry_time)}
        </td>
        <td className="max-w-[200px] truncate px-4 py-3 text-sm">
          <span className="font-medium">{trade.outcome}</span>
          <span className="ml-2 text-xs text-muted-foreground">
            {trade.market_id.slice(0, 8)}...
          </span>
        </td>
        <td className="px-4 py-3 text-sm">
          <span
            className={cn(
              'rounded px-2 py-0.5 text-xs font-medium',
              trade.side === 'BUY'
                ? 'bg-success/20 text-success'
                : 'bg-destructive/20 text-destructive'
            )}
          >
            {trade.side}
          </span>
        </td>
        <td className="px-4 py-3 text-right text-sm font-mono">
          {formatCurrency(trade.entry_price, 4)}
          {trade.exit_price && (
            <span className="text-muted-foreground">
              {' \u2192 '}{formatCurrency(trade.exit_price, 4)}
            </span>
          )}
        </td>
        <td className="px-4 py-3 text-right text-sm font-mono">
          {formatCurrency(trade.size)}
        </td>
        <td
          className={cn(
            'px-4 py-3 text-right text-sm font-mono font-medium',
            pnl > 0 && 'text-success',
            pnl < 0 && 'text-destructive'
          )}
        >
          {pnl >= 0 ? '+' : ''}{formatCurrency(pnl)}
        </td>
        <td
          className={cn(
            'px-4 py-3 text-right text-sm font-mono',
            pnlPct > 0 && 'text-success',
            pnlPct < 0 && 'text-destructive'
          )}
        >
          {pnlPct >= 0 ? '+' : ''}{formatPercent(pnlPct)}
        </td>
      </tr>
      {isExpanded && (
        <tr className="bg-muted/20">
          <td colSpan={7} className="px-4 py-3">
            <div className="grid grid-cols-2 gap-4 text-sm md:grid-cols-4">
              <div>
                <span className="text-muted-foreground">Exit Time:</span>
                <span className="ml-2">
                  {trade.exit_time ? formatDateTime(trade.exit_time) : 'Open'}
                </span>
              </div>
              <div>
                <span className="text-muted-foreground">Token ID:</span>
                <span className="ml-2 font-mono text-xs">{trade.token_id}</span>
              </div>
              <div>
                <span className="text-muted-foreground">Fee:</span>
                <span className="ml-2">{formatCurrency(trade.fee)}</span>
              </div>
              {trade.slippage !== undefined && (
                <div>
                  <span className="text-muted-foreground">Slippage:</span>
                  <span className="ml-2">{formatCurrency(trade.slippage)}</span>
                </div>
              )}
              {trade.signal_type && (
                <div>
                  <span className="text-muted-foreground">Signal:</span>
                  <span className="ml-2">{trade.signal_type}</span>
                </div>
              )}
              {trade.confidence !== undefined && (
                <div>
                  <span className="text-muted-foreground">Confidence:</span>
                  <span className="ml-2">{formatPercent(trade.confidence * 100)}</span>
                </div>
              )}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

export default TradeList;
