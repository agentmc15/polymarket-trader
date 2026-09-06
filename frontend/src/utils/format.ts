import { format, formatDistanceToNow, parseISO } from 'date-fns';

export function formatCurrency(value: number, decimals = 2): string {
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  }).format(value);
}

export function formatNumber(value: number, decimals = 2): string {
  return new Intl.NumberFormat('en-US', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  }).format(value);
}

export function formatPercent(value: number, decimals = 2): string {
  return new Intl.NumberFormat('en-US', {
    style: 'percent',
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  }).format(value / 100);
}

// T36: `TradeMetrics`/`RiskMetrics` (types/index.ts) use `null` to mean
// "not computed", never interchangeable with a measured `0`.
// `formatPercent`/`formatNumber`/`formatCurrency` above deliberately
// keep a strict `number` parameter rather than widening to
// `number | null` internally — a `null` reaching `value / 100` (in
// `formatPercent`) or an `Intl.NumberFormat` call is coerced to `0` by
// JS, not rejected, so a formatter that "handled" null internally
// would render it as `'0.00%'`/`'0.00'`: the exact bug this exists to
// prevent, one level down. Keeping them strict makes that a compile
// error instead, and callers route a possibly-null metric through
// `formatMetric` instead, which branches BEFORE any arithmetic runs.
export function formatMetric(
  value: number | null,
  formatter: (value: number) => string
): string {
  return value === null ? '-' : formatter(value);
}

export function formatCompact(value: number): string {
  return new Intl.NumberFormat('en-US', {
    notation: 'compact',
    compactDisplay: 'short',
  }).format(value);
}

export function formatDate(date: string | Date): string {
  const d = typeof date === 'string' ? parseISO(date) : date;
  return format(d, 'MMM d, yyyy');
}

export function formatDateTime(date: string | Date): string {
  const d = typeof date === 'string' ? parseISO(date) : date;
  return format(d, 'MMM d, yyyy HH:mm');
}

export function formatRelativeTime(date: string | Date): string {
  const d = typeof date === 'string' ? parseISO(date) : date;
  return formatDistanceToNow(d, { addSuffix: true });
}

// The strategy registry (backend/app/strategies/__init__.py's
// `list_strategies()`) only carries `name` (its snake_case key, e.g.
// `"catalyst_momentum"`) — there is no `display_name` field on
// `StrategyInfo` (see that type's doc comment in types/index.ts). This
// derives a human-friendly label client-side.
export function formatStrategyName(name: string): string {
  return name
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

export function shortenAddress(address: string, chars = 4): string {
  if (!address) return '';
  return `${address.slice(0, chars + 2)}...${address.slice(-chars)}`;
}

export function formatPnL(value: number): {
  formatted: string;
  isPositive: boolean;
  isNegative: boolean;
} {
  const isPositive = value > 0;
  const isNegative = value < 0;
  const prefix = isPositive ? '+' : '';
  const formatted = `${prefix}${formatCurrency(value)}`;

  return { formatted, isPositive, isNegative };
}
