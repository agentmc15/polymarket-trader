import { cn } from '../../utils/cn';
import { formatCurrency, formatNumber, formatPercent } from '../../utils/format';
import { useEdgeDecay } from '../../hooks/useEdgeDecay';
import { DepthSourceBadge, FillAtBadge } from './DepthBadges';
import type { EdgeDecayRow } from '../../types';

interface EdgeDecayTableProps {
  backtestId: number;
}

// Net return / annualized cell for one row. Split out (module scope, not
// inline in the row map) because a zero-trade row needs three visually
// distinct readings, not one bare "0.00%" standing in for all of them:
//   - real trades: the actual number.
//   - zero_trades_cause === "no_signal": a genuine no-edge-at-this-size
//     reading.
//   - zero_trades_cause starts with "structural: ...": this backtester
//     could never get a fill at this level — NOT evidence about edge.
function ReturnCell({ row, value }: { row: EdgeDecayRow; value: number }) {
  if (row.trades > 0 || row.zero_trades_cause === null) {
    return (
      <span className={cn(value > 0 && 'text-success', value < 0 && 'text-destructive')}>
        {formatPercent(value * 100)}
      </span>
    );
  }
  if (row.zero_trades_cause === 'no_signal') {
    return (
      <span className="text-muted-foreground" title="Strategy generated no intent at this level">
        0.00% (no signal)
      </span>
    );
  }
  return (
    <span
      className="rounded bg-warning/20 px-1.5 py-0.5 text-xs font-medium text-warning"
      title={row.zero_trades_cause}
    >
      not measurable
    </span>
  );
}

export function EdgeDecayTable({ backtestId }: EdgeDecayTableProps) {
  const { data: report, isLoading, isError } = useEdgeDecay(backtestId);

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-12">
        <div className="h-8 w-8 animate-spin rounded-full border-4 border-primary border-t-transparent" />
      </div>
    );
  }

  if (isError || !report) {
    return (
      <div className="rounded-lg border border-border bg-card p-8 text-center">
        <p className="text-muted-foreground">
          No edge-decay report available for this run (not a sweep, or the sweep has not
          completed yet).
        </p>
      </div>
    );
  }

  // The backend route (`GET /backtests/{id}/edge-decay`) returns the
  // JSONB `report["edge_decay"]` column verbatim, typed `dict[str, Any]`
  // — nothing validates its shape before it reaches this component. A
  // missing or non-array `rows` must degrade to a readable message
  // instead of crashing `report.rows.map(...)` below and white-screening
  // the page (there is no error boundary at this depth to catch it).
  if (!Array.isArray(report.rows)) {
    return (
      <div className="rounded-lg border border-warning/40 bg-warning/10 p-8 text-center">
        <p className="text-warning">
          This backtest&apos;s edge-decay report is malformed (missing or invalid{' '}
          <code>rows</code>) — trustworthiness data cannot be displayed for this sweep.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="font-medium">Edge Decay (Capital Sweep)</h3>
        <div className="flex items-center gap-2 text-sm">
          <span className="text-muted-foreground">Aggregate depth:</span>
          <DepthSourceBadge depthSource={report.depth_source} />
          <span className="text-muted-foreground">Fill at:</span>
          <FillAtBadge fillAt={report.fill_at} />
        </div>
      </div>

      <div className="rounded-md border border-border bg-muted/30 p-3 text-sm text-muted-foreground">
        {report.edge_dies_at !== null ? (
          <p>
            Edge dies at <span className="font-medium text-foreground">{formatCurrency(report.edge_dies_at)}</span>.
          </p>
        ) : (
          <p>No tested capital level killed the edge.</p>
        )}
        {/* PLAN.md D12: this caveat must be DISPLAYED, not just stored. */}
        <p className="mt-1">{report.sweep_ceiling_note}</p>
      </div>

      {report.unmeasurable_note && (
        <div className="rounded-md border border-warning/40 bg-warning/10 p-3 text-sm text-warning">
          {report.unmeasurable_note}
        </div>
      )}

      <div className="overflow-x-auto rounded-lg border border-border">
        <table className="w-full">
          <thead className="bg-muted/50">
            <tr>
              <th className="px-4 py-3 text-right text-sm font-medium">Capital</th>
              <th className="px-4 py-3 text-right text-sm font-medium">Net Return</th>
              <th className="px-4 py-3 text-right text-sm font-medium">Annualized</th>
              <th className="px-4 py-3 text-right text-sm font-medium">Fill Rate</th>
              <th className="px-4 py-3 text-right text-sm font-medium">Avg Slippage (bps)</th>
              <th className="px-4 py-3 text-right text-sm font-medium">% Downsized</th>
              <th className="px-4 py-3 text-right text-sm font-medium">Capital Util.</th>
              <th className="px-4 py-3 text-right text-sm font-medium">Trades</th>
              <th className="px-4 py-3 text-left text-sm font-medium">Depth</th>
              <th className="px-4 py-3 text-left text-sm font-medium">Fill At</th>
              <th className="px-4 py-3 text-right text-sm font-medium">Unvalidated Fills</th>
              <th className="px-4 py-3 text-left text-sm font-medium">Unmarked Positions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {report.rows.map((row) => {
              const isDeathRow = report.edge_dies_at !== null && row.capital === report.edge_dies_at;
              return (
                <tr
                  key={row.capital}
                  className={cn(isDeathRow && 'bg-destructive/10')}
                  title={isDeathRow ? 'Edge dies at this capital level' : undefined}
                >
                  <td className="px-4 py-3 text-right text-sm font-mono font-medium">
                    {formatCurrency(row.capital)}
                    {isDeathRow && (
                      <span className="ml-2 rounded bg-destructive/20 px-1.5 py-0.5 text-xs font-medium text-destructive">
                        dies here
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-right text-sm font-mono">
                    <ReturnCell row={row} value={row.net_return} />
                  </td>
                  <td className="px-4 py-3 text-right text-sm font-mono">
                    <ReturnCell row={row} value={row.annualized} />
                  </td>
                  <td className="px-4 py-3 text-right text-sm font-mono">
                    {formatPercent(row.fill_rate * 100)}
                  </td>
                  <td className="px-4 py-3 text-right text-sm font-mono">
                    {formatNumber(row.avg_slippage_bps, 1)}
                  </td>
                  <td className="px-4 py-3 text-right text-sm font-mono">
                    {formatPercent(row.pct_intents_downsized * 100)}
                  </td>
                  <td className="px-4 py-3 text-right text-sm font-mono">
                    {formatPercent(row.capital_utilization * 100)}
                  </td>
                  <td className="px-4 py-3 text-right text-sm font-mono">{row.trades}</td>
                  <td className="px-4 py-3 text-sm">
                    <DepthSourceBadge depthSource={row.depth_source} />
                  </td>
                  <td className="px-4 py-3 text-sm">
                    <FillAtBadge fillAt={row.fill_at} />
                  </td>
                  <td className="px-4 py-3 text-right text-sm font-mono">
                    {row.tick_unvalidated_fills}
                  </td>
                  <td className="px-4 py-3 text-sm">
                    {/* Defensive: `rows` being an array does not guarantee every
                        element's shape, since the whole payload is unvalidated. */}
                    {Array.isArray(row.unmarked_positions) && row.unmarked_positions.length > 0 ? (
                      <span
                        className="rounded bg-warning/20 px-1.5 py-0.5 text-xs font-medium text-warning"
                        title={row.unmarked_positions.join(', ')}
                      >
                        {row.unmarked_positions.length} unmarked — equity partly fictional
                      </span>
                    ) : (
                      <span className="text-xs text-muted-foreground">-</span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default EdgeDecayTable;
