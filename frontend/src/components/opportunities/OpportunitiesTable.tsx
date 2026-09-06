import { useMemo, useState } from 'react';
import { cn } from '../../utils/cn';
import { formatCurrency, formatNumber, formatPercent } from '../../utils/format';
import { useOpportunities, useTriggerScan } from '../../hooks/useOpportunities';
import type { Opportunity } from '../../types';

// Mirrors `Settings.near_resolution_hours`'s default (backend/app/config.py).
// The API only exposes this as a `near_resolution` boolean SERVER-SIDE
// filter on `GET /opportunities` (compared against whatever the running
// instance's setting actually is), not as a value this table can read —
// so a row's `near_resolution` badge below is this constant's best
// approximation, not a guarantee it matches the backend's configured
// threshold.
const NEAR_RESOLUTION_HOURS = 72;

type SortField = 'composite' | 'annualized_return' | 'hours_to_resolution';
type SortDirection = 'asc' | 'desc';

function uniqueVenues(opportunity: Opportunity): string {
  const venues = [...new Set(opportunity.legs.map((leg) => leg.venue))];
  return venues.join(', ') || '-';
}

function uniqueMarkets(opportunity: Opportunity): string {
  const markets = [...new Set(opportunity.legs.map((leg) => leg.market_id))];
  return markets.map((m) => (m.length > 12 ? `${m.slice(0, 12)}…` : m)).join(', ') || '-';
}

interface SortHeaderProps {
  label: string;
  field: SortField;
  sortField: SortField;
  sortDirection: SortDirection;
  onSort: (field: SortField) => void;
}

// Hoisted to module scope: a component defined inside the table's render
// body would be a new type every render, forcing React to remount it
// instead of reconciling (see TradeList.tsx for the bug this avoids).
function SortHeader({ label, field, sortField, sortDirection, onSort }: SortHeaderProps) {
  const active = sortField === field;
  return (
    <th
      className="cursor-pointer px-4 py-3 text-right text-sm font-medium"
      onClick={() => onSort(field)}
    >
      {label}
      {active && (
        <span className="ml-1 inline-block">{sortDirection === 'asc' ? '↑' : '↓'}</span>
      )}
    </th>
  );
}

function LinkStatusBadge({ status }: { status: string | null }) {
  if (!status) {
    return <span className="text-xs text-muted-foreground">-</span>;
  }
  // PLAN.md D9: a "proposed" (unexecutable) link may be SCORED but never
  // executed — this must read as distinctly non-actionable, not as a
  // routine status value.
  if (status === 'proposed') {
    return (
      <span
        className="rounded bg-warning/20 px-1.5 py-0.5 text-xs font-medium text-warning"
        title="proposed link: scored for visibility only, not executable"
      >
        proposed
      </span>
    );
  }
  return (
    <span className="rounded bg-success/20 px-1.5 py-0.5 text-xs font-medium text-success">
      {status}
    </span>
  );
}

function DepthSourceBadge({ depthSource }: { depthSource: string }) {
  // GUARDRAILS.md §1.7: a result scored against synthesized depth must
  // say so wherever it is shown — this is one of those places, so the
  // badge is a plain visible label, never a hover-only tooltip.
  if (depthSource === 'synthetic') {
    return (
      <span
        className="rounded bg-warning/20 px-1.5 py-0.5 text-xs font-medium text-warning"
        title="Scored against synthesized (invented) order-book depth, not a recorded book"
      >
        synthetic
      </span>
    );
  }
  return <span className="text-xs text-muted-foreground">{depthSource}</span>;
}

export function OpportunitiesTable() {
  const [sortField, setSortField] = useState<SortField>('composite');
  const [sortDirection, setSortDirection] = useState<SortDirection>('desc');

  const { data, isLoading, isError, error } = useOpportunities();
  const triggerScan = useTriggerScan();

  const opportunities = useMemo(() => data?.opportunities ?? [], [data]);

  const sorted = useMemo(() => {
    const rows = [...opportunities];
    rows.sort((a, b) => {
      const diff = a[sortField] - b[sortField];
      return sortDirection === 'asc' ? diff : -diff;
    });
    return rows;
  }, [opportunities, sortField, sortDirection]);

  const handleSort = (field: SortField) => {
    if (sortField === field) {
      setSortDirection((prev) => (prev === 'asc' ? 'desc' : 'asc'));
    } else {
      setSortField(field);
      setSortDirection('desc');
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-xl font-semibold">Opportunities</h2>
          <p className="text-sm text-muted-foreground">
            Latest scan's scored, pending opportunities — discovery only, nothing here places
            or routes an order.
          </p>
        </div>
        <button
          onClick={() => triggerScan.mutate(undefined)}
          disabled={triggerScan.isPending}
          className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {triggerScan.isPending ? 'Scanning...' : 'Scan Now'}
        </button>
      </div>

      {isLoading && (
        <div className="flex items-center justify-center py-12">
          <div className="h-8 w-8 animate-spin rounded-full border-4 border-primary border-t-transparent" />
        </div>
      )}

      {isError && (
        <div className="rounded-md bg-destructive/10 p-4 text-destructive">
          Failed to load opportunities{error instanceof Error ? `: ${error.message}` : ''}.
        </div>
      )}

      {!isLoading && !isError && sorted.length === 0 && (
        <div className="rounded-lg border border-border bg-card p-8 text-center">
          <p className="text-muted-foreground">No pending opportunities from the latest scan</p>
        </div>
      )}

      {!isLoading && !isError && sorted.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="w-full">
            <thead className="bg-muted/50">
              <tr>
                <th className="px-4 py-3 text-left text-sm font-medium">Venue(s)</th>
                <th className="px-4 py-3 text-left text-sm font-medium">Market</th>
                <th className="px-4 py-3 text-left text-sm font-medium">Kind</th>
                <th className="px-4 py-3 text-right text-sm font-medium">Net Edge</th>
                <SortHeader
                  label="Annualized"
                  field="annualized_return"
                  sortField={sortField}
                  sortDirection={sortDirection}
                  onSort={handleSort}
                />
                <SortHeader
                  label="Hours to Resolution"
                  field="hours_to_resolution"
                  sortField={sortField}
                  sortDirection={sortDirection}
                  onSort={handleSort}
                />
                <th className="px-4 py-3 text-right text-sm font-medium">Fill Conf.</th>
                <th className="px-4 py-3 text-right text-sm font-medium">Resolution Risk</th>
                <th className="px-4 py-3 text-right text-sm font-medium">Lockup</th>
                <SortHeader
                  label="Composite"
                  field="composite"
                  sortField={sortField}
                  sortDirection={sortDirection}
                  onSort={handleSort}
                />
                <th className="px-4 py-3 text-left text-sm font-medium">Depth</th>
                <th className="px-4 py-3 text-left text-sm font-medium">Link</th>
                <th className="px-4 py-3 text-left text-sm font-medium">Near Res.</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {sorted.map((opportunity) => {
                const nearResolution = opportunity.hours_to_resolution <= NEAR_RESOLUTION_HOURS;
                return (
                  <tr key={opportunity.id} className="hover:bg-muted/30">
                    <td className="px-4 py-3 text-sm">{uniqueVenues(opportunity)}</td>
                    <td className="px-4 py-3 text-sm text-muted-foreground" title={uniqueMarkets(opportunity)}>
                      {uniqueMarkets(opportunity)}
                    </td>
                    <td className="px-4 py-3 text-sm">{opportunity.kind}</td>
                    <td className="px-4 py-3 text-right text-sm font-mono">
                      {formatCurrency(opportunity.net_edge, 4)}
                    </td>
                    <td
                      className={cn(
                        'px-4 py-3 text-right text-sm font-mono',
                        opportunity.annualized_return > 0 && 'text-success',
                        opportunity.annualized_return < 0 && 'text-destructive'
                      )}
                    >
                      {formatPercent(opportunity.annualized_return * 100)}
                    </td>
                    <td className="px-4 py-3 text-right text-sm font-mono">
                      {formatNumber(opportunity.hours_to_resolution, 1)}h
                    </td>
                    <td className="px-4 py-3 text-right text-sm font-mono">
                      {formatPercent(opportunity.fill_confidence * 100)}
                    </td>
                    <td
                      className={cn(
                        'px-4 py-3 text-right text-sm font-mono',
                        opportunity.resolution_risk >= 0.5 && 'text-destructive'
                      )}
                    >
                      {formatPercent(opportunity.resolution_risk * 100)}
                    </td>
                    <td className="px-4 py-3 text-right text-sm font-mono">
                      {formatCurrency(opportunity.capital_lockup_usd)}
                    </td>
                    <td className="px-4 py-3 text-right text-sm font-mono font-medium">
                      {formatNumber(opportunity.composite, 4)}
                    </td>
                    <td className="px-4 py-3 text-sm">
                      <DepthSourceBadge depthSource={opportunity.depth_source} />
                    </td>
                    <td className="px-4 py-3 text-sm">
                      <LinkStatusBadge status={opportunity.link_status} />
                    </td>
                    <td className="px-4 py-3 text-sm">
                      {nearResolution ? (
                        <span className="rounded bg-primary/20 px-1.5 py-0.5 text-xs font-medium text-primary">
                          near
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
      )}
    </div>
  );
}

export default OpportunitiesTable;
