import { useState } from 'react';
import axios from 'axios';
import { cn } from '../../utils/cn';
import { formatDateTime } from '../../utils/format';
import { useApproveLink, useLinkReview, useLinks, useRejectLink } from '../../hooks/useLinks';
import type { EventLink } from '../../types';

// Prefilled into "Reviewed By" for attribution — this app's own backend
// records who approved/rejected a link (backend/app/api/routes/links.py
// `reviewed_by`), so the signed-in user's identity is the correct
// default there. Fully editable; never sent anywhere but this app's API.
const DEFAULT_REVIEWER = 'texasrangers1515@gmail.com';

// Labels for `FieldComparison.field` (links.py's `_COMPARED_FIELDS`),
// excluding `rules_text` — that one gets its own side-by-side panels
// below rather than a cramped table cell, per this defect's brief.
const FIELD_LABELS: Record<string, string> = {
  venue: 'Venue',
  market_id: 'Market ID',
  question: 'Question',
  close_time: 'Close Time',
  expected_settle_time: 'Expected Settle Time',
  resolution_source: 'Resolution Source',
  outcomes: 'Outcomes',
  status: 'Status',
};

function extractErrorMessage(error: unknown): string {
  if (axios.isAxiosError(error)) {
    const data = error.response?.data as { detail?: unknown } | undefined;
    if (typeof data?.detail === 'string') return data.detail;
  }
  if (error instanceof Error) return error.message;
  return 'Unknown error';
}

export function LinkReview() {
  const { data, isLoading, isError } = useLinks('proposed');
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const links = data?.links ?? [];

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-xl font-semibold">Cross-Venue Link Review</h2>
        <p className="mt-1 max-w-3xl text-sm text-muted-foreground">
          Proposed matches between Polymarket and Kalshi events, awaiting a human decision
          (PLAN.md D9). A link only becomes tradable once it is approved here — Kalshi's and
          Polymarket's nearest-equivalent contracts routinely differ in resolution source,
          settlement wording, and timezone cutoff even when a token-overlap score calls them
          identical, and a false link is a trade that looks hedged but can lose on both legs.
          This list can grow on its own — a scheduled pass proposes new links continuously.
        </p>
      </div>

      {isLoading && (
        <div className="flex items-center justify-center py-12">
          <div className="h-8 w-8 animate-spin rounded-full border-4 border-primary border-t-transparent" />
        </div>
      )}

      {isError && (
        <div className="rounded-md bg-destructive/10 p-4 text-destructive">
          Failed to load proposed links.
        </div>
      )}

      {!isLoading && !isError && links.length === 0 && (
        <div className="rounded-lg border border-border bg-card p-8 text-center">
          <p className="text-muted-foreground">No proposed links awaiting review</p>
        </div>
      )}

      {!isLoading && !isError && links.length > 0 && (
        <div className="grid gap-6 lg:grid-cols-[minmax(280px,1fr)_minmax(0,2fr)]">
          <LinkQueue links={links} selectedId={selectedId} onSelect={setSelectedId} />
          {selectedId !== null ? (
            <LinkDetail
              key={selectedId}
              linkId={selectedId}
              onDone={() => setSelectedId(null)}
            />
          ) : (
            <div className="rounded-lg border border-border bg-card p-8 text-center text-muted-foreground">
              Select a proposed link to compare both venues&apos; rules text.
            </div>
          )}
        </div>
      )}
    </div>
  );
}

interface LinkQueueProps {
  links: EventLink[];
  selectedId: number | null;
  onSelect: (id: number) => void;
}

function LinkQueue({ links, selectedId, onSelect }: LinkQueueProps) {
  return (
    <div className="h-fit overflow-x-auto rounded-lg border border-border">
      <table className="w-full">
        <thead className="bg-muted/50">
          <tr>
            <th className="px-3 py-2 text-right text-xs font-medium">Conf.</th>
            <th className="px-3 py-2 text-left text-xs font-medium">Proposed pair</th>
            <th className="px-3 py-2" />
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {links.map((link) => (
            <tr key={link.id} className={cn('text-sm', selectedId === link.id && 'bg-primary/5')}>
              <td className="px-3 py-2 text-right font-mono">
                {(link.confidence * 100).toFixed(0)}%
              </td>
              <td className="max-w-[220px] px-3 py-2">
                <div className="truncate" title={`${link.venue_a}: ${link.market_a}`}>
                  {link.venue_a}: {link.market_a}
                </div>
                <div
                  className="truncate text-muted-foreground"
                  title={`${link.venue_b}: ${link.market_b}`}
                >
                  {link.venue_b}: {link.market_b}
                </div>
                <div className="text-xs text-muted-foreground">
                  {link.created_at ? formatDateTime(link.created_at) : '-'}
                </div>
              </td>
              <td className="px-3 py-2 text-right">
                <button
                  type="button"
                  onClick={() => onSelect(link.id)}
                  className={cn(
                    'rounded-md border px-3 py-1 text-xs font-medium transition-colors',
                    selectedId === link.id
                      ? 'border-primary text-primary'
                      : 'border-border hover:border-primary/50'
                  )}
                >
                  Review
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

interface OutcomeRow {
  key: string;
  value: string;
}

interface LinkDetailProps {
  linkId: number;
  onDone: () => void;
}

function LinkDetail({ linkId, onDone }: LinkDetailProps) {
  const { data, isLoading, isError } = useLinkReview(linkId);
  const approveLink = useApproveLink();
  const rejectLink = useRejectLink();

  const [reviewedBy, setReviewedBy] = useState(DEFAULT_REVIEWER);
  const [notes, setNotes] = useState('');
  // `null` until the fetched link seeds it once — this component is
  // remounted per link (the parent renders it with `key={selectedId}`),
  // so a one-time seed on first data arrival is enough; it does not need
  // to re-seed on every refetch of the same link.
  const [outcomeRows, setOutcomeRows] = useState<OutcomeRow[] | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const link = data?.link;
  if (outcomeRows === null && link) {
    const entries = Object.entries(link.outcome_map);
    setOutcomeRows(
      entries.length > 0 ? entries.map(([key, value]) => ({ key, value })) : [{ key: '', value: '' }]
    );
  }

  if (isLoading) {
    return (
      <div className="flex items-center justify-center rounded-lg border border-border bg-card p-8">
        <div className="h-8 w-8 animate-spin rounded-full border-4 border-primary border-t-transparent" />
      </div>
    );
  }

  if (isError || !data || !link) {
    return (
      <div className="rounded-lg border border-destructive/50 bg-destructive/10 p-8 text-center text-destructive">
        Failed to load this link.
      </div>
    );
  }

  const { market_a, market_b, comparison, warnings } = data;
  const rulesTextComparison = comparison.find((c) => c.field === 'rules_text');
  const scalarComparison = comparison.filter((c) => c.field !== 'rules_text');
  const alreadyReviewed = link.status !== 'proposed';

  const updateRow = (index: number, field: keyof OutcomeRow, value: string) => {
    setOutcomeRows((rows) =>
      (rows ?? []).map((row, i) => (i === index ? { ...row, [field]: value } : row))
    );
  };

  const removeRow = (index: number) => {
    setOutcomeRows((rows) => (rows ?? []).filter((_, i) => i !== index));
  };

  const buildOutcomeMap = (): Record<string, string> | undefined => {
    const map = Object.fromEntries(
      (outcomeRows ?? [])
        .filter((row) => row.key.trim() && row.value.trim())
        .map((row) => [row.key.trim(), row.value.trim()])
    );
    return Object.keys(map).length > 0 ? map : undefined;
  };

  const handleApprove = async () => {
    setActionError(null);
    try {
      await approveLink.mutateAsync({
        linkId,
        request: { reviewed_by: reviewedBy, notes, outcome_map: buildOutcomeMap() },
      });
      onDone();
    } catch (error) {
      setActionError(extractErrorMessage(error));
    }
  };

  const handleReject = async () => {
    setActionError(null);
    try {
      await rejectLink.mutateAsync({ linkId, request: { reviewed_by: reviewedBy, notes } });
      onDone();
    } catch (error) {
      setActionError(extractErrorMessage(error));
    }
  };

  return (
    <div className="space-y-4">
      {warnings.length > 0 && (
        <div className="space-y-2">
          {warnings.map((warning) => (
            <div
              key={warning}
              className="rounded-md border border-warning/40 bg-warning/10 p-3 text-sm text-warning"
            >
              {warning}
            </div>
          ))}
        </div>
      )}

      <div className="overflow-x-auto rounded-lg border border-border">
        <table className="w-full text-sm">
          <thead className="bg-muted/50">
            <tr>
              <th className="px-3 py-2 text-left font-medium">Field</th>
              <th className="px-3 py-2 text-left font-medium">
                {market_a?.venue ?? link.venue_a}
              </th>
              <th className="px-3 py-2 text-left font-medium">
                {market_b?.venue ?? link.venue_b}
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {scalarComparison.map((row) => (
              <tr key={row.field} className={cn(!row.same && 'bg-warning/10')}>
                <td className="px-3 py-2 font-medium text-muted-foreground">
                  {FIELD_LABELS[row.field] ?? row.field}
                </td>
                <td className="px-3 py-2">{row.a ?? '-'}</td>
                <td className="px-3 py-2">{row.b ?? '-'}</td>
              </tr>
            ))}
            {rulesTextComparison && (
              <tr className={cn(!rulesTextComparison.same && 'bg-warning/10')}>
                <td className="px-3 py-2 font-medium text-muted-foreground">Rules Text</td>
                <td colSpan={2} className="px-3 py-2 text-xs">
                  {rulesTextComparison.same
                    ? 'Identical — full text below'
                    : 'Differs — read both panels below before deciding'}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <RulesTextPanel
          label={market_a ? `${market_a.venue}: ${market_a.market_id}` : `${link.venue_a} (unavailable)`}
          text={market_a?.rules_text}
        />
        <RulesTextPanel
          label={market_b ? `${market_b.venue}: ${market_b.market_id}` : `${link.venue_b} (unavailable)`}
          text={market_b?.rules_text}
        />
      </div>

      {alreadyReviewed ? (
        <div className="rounded-md border border-border bg-muted/30 p-3 text-sm text-muted-foreground">
          Already reviewed as <span className="font-medium">{link.status}</span>
          {link.reviewed_by && ` by ${link.reviewed_by}`}
          {link.notes && ` — "${link.notes}"`}
        </div>
      ) : (
        <div className="space-y-4 rounded-lg border border-border bg-card p-4">
          <h3 className="font-medium">Decision</h3>

          <div className="grid gap-4 md:grid-cols-2">
            <div>
              <label htmlFor="reviewed-by" className="mb-1 block text-sm font-medium">
                Reviewed By
              </label>
              <input
                id="reviewed-by"
                type="text"
                value={reviewedBy}
                onChange={(e) => setReviewedBy(e.target.value)}
                className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
              />
            </div>
          </div>

          <div>
            <label htmlFor="reviewer-notes" className="mb-1 block text-sm font-medium">
              Notes
            </label>
            <textarea
              id="reviewer-notes"
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              rows={3}
              placeholder="e.g. Kalshi settles on the AP call, Polymarket on state certification"
              className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
            />
          </div>

          <div>
            <div className="mb-2 flex items-center justify-between">
              <label className="text-sm font-medium">
                Outcome Map
                {Object.keys(link.outcome_map).length === 0 && (
                  <span className="ml-2 text-xs font-normal text-warning">
                    required before this link can be approved
                  </span>
                )}
              </label>
              <button
                type="button"
                onClick={() => setOutcomeRows((rows) => [...(rows ?? []), { key: '', value: '' }])}
                className="text-xs text-primary hover:underline"
              >
                + Add mapping
              </button>
            </div>
            <div className="space-y-2">
              {(outcomeRows ?? []).map((row, index) => (
                <div key={index} className="flex items-center gap-2">
                  <input
                    type="text"
                    placeholder={`${market_a?.venue ?? link.venue_a} outcome`}
                    value={row.key}
                    onChange={(e) => updateRow(index, 'key', e.target.value)}
                    className="w-1/2 rounded-md border border-input bg-background px-2 py-1.5 text-sm"
                  />
                  <span className="text-muted-foreground">&#8594;</span>
                  <input
                    type="text"
                    placeholder={`${market_b?.venue ?? link.venue_b} outcome`}
                    value={row.value}
                    onChange={(e) => updateRow(index, 'value', e.target.value)}
                    className="w-1/2 rounded-md border border-input bg-background px-2 py-1.5 text-sm"
                  />
                  <button
                    type="button"
                    onClick={() => removeRow(index)}
                    aria-label="Remove mapping"
                    className="text-muted-foreground hover:text-destructive"
                  >
                    &times;
                  </button>
                </div>
              ))}
            </div>
          </div>

          {actionError && <p className="text-sm text-destructive">{actionError}</p>}

          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={handleApprove}
              disabled={!reviewedBy.trim() || approveLink.isPending || rejectLink.isPending}
              className="rounded-md bg-success px-4 py-2 text-sm font-medium text-success-foreground transition-colors hover:bg-success/90 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {approveLink.isPending ? 'Approving...' : 'Approve'}
            </button>
            <button
              type="button"
              onClick={handleReject}
              disabled={!reviewedBy.trim() || approveLink.isPending || rejectLink.isPending}
              className="rounded-md border border-destructive px-4 py-2 text-sm font-medium text-destructive transition-colors hover:bg-destructive/10 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {rejectLink.isPending ? 'Rejecting...' : 'Reject'}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function RulesTextPanel({ label, text }: { label: string; text?: string }) {
  return (
    <div className="rounded-lg border border-border bg-card p-3">
      <p className="mb-2 truncate text-xs font-medium uppercase tracking-wide text-muted-foreground" title={label}>
        {label}
      </p>
      {text ? (
        <div className="max-h-96 overflow-y-auto whitespace-pre-wrap rounded-md bg-muted/30 p-3 text-sm">
          {text}
        </div>
      ) : (
        <p className="text-sm text-muted-foreground">
          Not available — this market could not be read from its venue.
        </p>
      )}
    </div>
  );
}

export default LinkReview;
