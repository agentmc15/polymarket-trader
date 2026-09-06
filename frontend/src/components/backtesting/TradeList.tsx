import { useMemo, useState } from 'react';
import { cn } from '../../utils/cn';
import { formatCurrency, formatPercent, formatDateTime } from '../../utils/format';
import type { BacktestTrade } from '../../types';

interface TradeListProps {
  trades: BacktestTrade[];
  isLoading?: boolean;
}

// `BacktestTrade` (`TradeRecord` on the wire) is one FILL, not a closed
// round-trip position — there is no entry/exit pairing on the wire (see
// that type's doc comment in types/index.ts), so sorting/filtering here
// is over a flat fill list rather than a position list.
type SortField = 'timestamp' | 'side' | 'price' | 'size' | 'pnl';
type SortDirection = 'asc' | 'desc';
type SideFilter = 'all' | 'BUY' | 'SELL';
type PnLFilter = 'all' | 'winners' | 'losers';

interface SortIconProps {
  field: SortField;
  sortField: SortField;
  sortDirection: SortDirection;
}

// Hoisted to module scope (not defined inside TradeList's render body):
// a component re-created on every render is a new type each time, so
// React unmounts/remounts it instead of reconciling, discarding any
// state it holds and defeating the point of the sort-direction props.
function SortIcon({ field, sortField, sortDirection }: SortIconProps) {
  if (sortField !== field) return null;
  return (
    <span className="ml-1 inline-block">
      {sortDirection === 'asc' ? '↑' : '↓'}
    </span>
  );
}

export function TradeList({ trades, isLoading }: TradeListProps) {
  const [sortField, setSortField] = useState<SortField>('timestamp');
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
        case 'timestamp':
          comparison = new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime();
          break;
        case 'side':
          comparison = a.side.localeCompare(b.side);
          break;
        case 'price':
          comparison = a.price - b.price;
          break;
        case 'size':
          comparison = a.size - b.size;
          break;
        case 'pnl':
          comparison = (a.pnl ?? 0) - (b.pnl ?? 0);
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
                onClick={() => handleSort('timestamp')}
              >
                Time
                <SortIcon field="timestamp" sortField={sortField} sortDirection={sortDirection} />
              </th>
              <th className="px-4 py-3 text-left text-sm font-medium">Market</th>
              <th
                className="cursor-pointer px-4 py-3 text-left text-sm font-medium"
                onClick={() => handleSort('side')}
              >
                Side
                <SortIcon field="side" sortField={sortField} sortDirection={sortDirection} />
              </th>
              <th
                className="cursor-pointer px-4 py-3 text-right text-sm font-medium"
                onClick={() => handleSort('price')}
              >
                Price
                <SortIcon field="price" sortField={sortField} sortDirection={sortDirection} />
              </th>
              <th
                className="cursor-pointer px-4 py-3 text-right text-sm font-medium"
                onClick={() => handleSort('size')}
              >
                Size
                <SortIcon field="size" sortField={sortField} sortDirection={sortDirection} />
              </th>
              <th className="px-4 py-3 text-right text-sm font-medium">Fee</th>
              <th
                className="cursor-pointer px-4 py-3 text-right text-sm font-medium"
                onClick={() => handleSort('pnl')}
              >
                P&L
                <SortIcon field="pnl" sortField={sortField} sortDirection={sortDirection} />
              </th>
              <th className="px-4 py-3 text-right text-sm font-medium">Confidence</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {filteredTrades.map((trade, index) => (
              <TradeRow key={`${trade.timestamp}-${trade.market_id}-${trade.outcome}-${index}`} trade={trade} />
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
  const pnl = trade.pnl ?? 0;

  return (
    <tr className="hover:bg-muted/30">
      <td className="px-4 py-3 text-sm">
        {formatDateTime(trade.timestamp)}
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
        {formatCurrency(trade.price, 4)}
      </td>
      <td className="px-4 py-3 text-right text-sm font-mono">
        {formatCurrency(trade.size)}
      </td>
      <td className="px-4 py-3 text-right text-sm font-mono text-muted-foreground">
        {formatCurrency(trade.fee)}
      </td>
      <td
        className={cn(
          'px-4 py-3 text-right text-sm font-mono font-medium',
          pnl > 0 && 'text-success',
          pnl < 0 && 'text-destructive'
        )}
      >
        {trade.pnl !== null ? `${pnl >= 0 ? '+' : ''}${formatCurrency(pnl)}` : '-'}
      </td>
      <td className="px-4 py-3 text-right text-sm font-mono">
        {formatPercent(trade.signal_confidence * 100)}
      </td>
    </tr>
  );
}

export default TradeList;
