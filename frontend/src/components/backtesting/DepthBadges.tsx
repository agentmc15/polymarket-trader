import type { FillAt, ResultDepthSource } from '../../types';

// GUARDRAILS.md §1.7: a result computed on synthesized depth, or a
// same-snapshot ("look-ahead") fill, must be labeled wherever it is
// shown — as plain VISIBLE text, never behind a hover or an expand
// action. `title` attributes below are supplementary detail, not the
// label itself.
//
// Shared by `EdgeDecayTable` (one badge per sweep-level row, plus the
// sweep's aggregate) and `BacktestResults` (a single ordinary,
// non-sweep run) so both surfaces render the same label for the same
// underlying value.

export function DepthSourceBadge({ depthSource }: { depthSource: ResultDepthSource | string }) {
  if (depthSource === 'synthetic') {
    return (
      <span
        className="rounded bg-warning/20 px-1.5 py-0.5 text-xs font-medium text-warning"
        title="This result was computed against synthesized (invented) order-book depth, not a recorded book"
      >
        synthetic
      </span>
    );
  }
  if (depthSource === 'mixed') {
    return (
      <span
        className="rounded bg-warning/20 px-1.5 py-0.5 text-xs font-medium text-warning"
        title="Some of this result was computed against synthesized depth, some against a recorded book"
      >
        mixed
      </span>
    );
  }
  return (
    <span className="rounded bg-muted px-1.5 py-0.5 text-xs font-medium text-muted-foreground">
      {depthSource}
    </span>
  );
}

export function FillAtBadge({ fillAt }: { fillAt: FillAt | string }) {
  if (fillAt === 'same') {
    return (
      <span
        className="rounded bg-warning/20 px-1.5 py-0.5 text-xs font-medium text-warning"
        title="Filled against the SAME snapshot the signal fired on, not the next tick — a diagnostic mode, not a realistic fill"
      >
        same
      </span>
    );
  }
  return (
    <span className="rounded bg-muted px-1.5 py-0.5 text-xs font-medium text-muted-foreground">
      {fillAt}
    </span>
  );
}
