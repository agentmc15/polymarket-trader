import { useState } from 'react';
import { cn } from '../../utils/cn';
import { formatCurrency, formatStrategyName } from '../../utils/format';
import type { StrategyInfo, BacktestRequest, SlippageModel, SweepRequest } from '../../types';
import { StrategySelector } from './StrategySelector';

// Mirrors `DEFAULT_CAPITAL_LEVELS` (backend/app/services/backtesting/
// sweep.py). Not exposed as structured API data — `SweepRequest.
// capital_levels`'s only trace of it is a human-readable string baked
// into that field's OpenAPI description, not a value a client can read
// at runtime — so this is a documented constant, kept in sync by hand.
const DEFAULT_SWEEP_CAPITAL_LEVELS = [500, 2000, 10000, 50000, 250000];

interface BacktestFormProps {
  strategies: StrategyInfo[];
  categories: Record<string, string[]>;
  isLoadingStrategies: boolean;
  onSubmit: (request: BacktestRequest) => void;
  //: Producer for `POST /backtests/sweep` (PLAN.md D12, T22) — runs the
  //: selected strategy once per capital level and reports where the
  //: edge stops being viable. Asynchronous, like `onSubmit`.
  onSubmitSweep: (request: SweepRequest) => void;
  isSubmitting: boolean;
  isSubmittingSweep: boolean;
}

/** Parse a comma-separated capital-levels field into positive numbers. */
function parseCapitalLevels(text: string): number[] {
  return text
    .split(',')
    .map((s) => Number(s.trim()))
    .filter((n) => Number.isFinite(n) && n > 0);
}

const SLIPPAGE_MODELS: { value: SlippageModel; label: string; description: string }[] = [
  { value: 'none', label: 'None', description: 'No slippage simulation' },
  { value: 'fixed', label: 'Fixed', description: 'Fixed basis points per trade' },
  { value: 'volume_based', label: 'Volume Based', description: 'Slippage based on trade size' },
  { value: 'spread_based', label: 'Spread Based', description: 'Based on bid-ask spread' },
];

export function BacktestForm({
  strategies,
  categories,
  isLoadingStrategies,
  onSubmit,
  onSubmitSweep,
  isSubmitting,
  isSubmittingSweep,
}: BacktestFormProps) {
  const [selectedStrategy, setSelectedStrategy] = useState<string | null>(null);
  const [showStrategyPicker, setShowStrategyPicker] = useState(true);

  // Form state
  const [startDate, setStartDate] = useState(() => {
    const date = new Date();
    date.setMonth(date.getMonth() - 3);
    return date.toISOString().split('T')[0];
  });
  const [endDate, setEndDate] = useState(() => {
    return new Date().toISOString().split('T')[0];
  });
  const [initialCapital, setInitialCapital] = useState(10000);
  const [feeRate, setFeeRate] = useState(0.001);
  const [slippageModel, setSlippageModel] = useState<SlippageModel>('fixed');
  const [slippageBps, setSlippageBps] = useState(5);

  // Capital sweep (PLAN.md D12, T22): a separate producer from the
  // single-run submit below, so `initial_capital` above stays what it
  // has always been — the single run's capital — while this drives
  // `SweepRequest.capital_levels` instead.
  const [capitalLevelsText, setCapitalLevelsText] = useState(
    DEFAULT_SWEEP_CAPITAL_LEVELS.join(', ')
  );
  const [capitalLevelsError, setCapitalLevelsError] = useState<string | null>(null);

  // Dynamic strategy config
  const [strategyConfig, setStrategyConfig] = useState<Record<string, unknown>>({});
  // Tracks which strategy `strategyConfig` was last reset for, so the
  // reset below can detect a strategy change during render.
  const [configForStrategy, setConfigForStrategy] = useState<string | null>(null);

  // Get selected strategy info
  const selectedStrategyInfo = strategies.find((s) => s.name === selectedStrategy);

  // Reset `strategyConfig` to the new strategy's defaults when
  // `selectedStrategy` changes. Done synchronously during render (React's
  // "adjusting state when a prop changes" pattern:
  // https://react.dev/learn/you-might-not-need-an-effect) instead of in a
  // `useEffect`, since a `setState` unconditionally called from an effect
  // body triggers an extra, avoidable render pass. The `if` guard makes
  // this a one-time adjustment per strategy change, not an infinite loop.
  if (configForStrategy !== selectedStrategy) {
    setConfigForStrategy(selectedStrategy);
    setStrategyConfig(
      selectedStrategyInfo?.default_config ? { ...selectedStrategyInfo.default_config } : {}
    );
  }

  const handleConfigChange = (key: string, value: unknown) => {
    setStrategyConfig((prev) => ({ ...prev, [key]: value }));
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedStrategy) return;

    onSubmit({
      strategy: selectedStrategy,
      start_date: startDate,
      end_date: endDate,
      initial_capital: initialCapital,
      fee_rate: feeRate,
      slippage_model: slippageModel,
      // `BacktestRequest.slippage_value` is a probability-unit pad
      // (0.001 = 0.1%), not basis points — see that field's doc comment
      // in types/index.ts.
      slippage_value: slippageBps / 10_000,
      strategy_config: strategyConfig,
    });
  };

  const handleRunSweep = () => {
    if (!selectedStrategy) return;
    const levels = parseCapitalLevels(capitalLevelsText);
    if (levels.length === 0) {
      setCapitalLevelsError('Enter at least one positive capital level, comma-separated.');
      return;
    }
    setCapitalLevelsError(null);

    onSubmitSweep({
      strategy: selectedStrategy,
      start_date: startDate,
      end_date: endDate,
      fee_rate: feeRate,
      slippage_model: slippageModel,
      slippage_value: slippageBps / 10_000,
      strategy_config: strategyConfig,
      capital_levels: levels,
    });
  };

  return (
    <form onSubmit={handleSubmit} className="space-y-6">
      {/* Strategy Selection */}
      <div className="rounded-lg border border-border bg-card p-4">
        <div className="mb-4 flex items-center justify-between">
          <h3 className="font-medium">Strategy</h3>
          {selectedStrategy && (
            <button
              type="button"
              onClick={() => setShowStrategyPicker(!showStrategyPicker)}
              className="text-sm text-primary hover:underline"
            >
              {showStrategyPicker ? 'Hide' : 'Change Strategy'}
            </button>
          )}
        </div>

        {showStrategyPicker ? (
          <StrategySelector
            strategies={strategies}
            categories={categories}
            selectedStrategy={selectedStrategy}
            onSelect={(name) => {
              setSelectedStrategy(name);
              setShowStrategyPicker(false);
            }}
            isLoading={isLoadingStrategies}
          />
        ) : (
          selectedStrategyInfo && (
            <div className="rounded-md border border-primary/30 bg-primary/5 p-3">
              <div className="flex items-center justify-between">
                <div>
                  <p className="font-medium">{formatStrategyName(selectedStrategyInfo.name)}</p>
                  <p className="text-sm text-muted-foreground">
                    {selectedStrategyInfo.description}
                  </p>
                </div>
              </div>
            </div>
          )
        )}
      </div>

      {/* Date Range & Capital */}
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        <div>
          <label className="mb-2 block text-sm font-medium">Start Date</label>
          <input
            type="date"
            value={startDate}
            onChange={(e) => setStartDate(e.target.value)}
            className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
            required
          />
        </div>

        <div>
          <label className="mb-2 block text-sm font-medium">End Date</label>
          <input
            type="date"
            value={endDate}
            onChange={(e) => setEndDate(e.target.value)}
            min={startDate}
            className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
            required
          />
        </div>

        <div>
          <label className="mb-2 block text-sm font-medium">Initial Capital</label>
          <div className="relative">
            <span className="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground">
              $
            </span>
            <input
              type="number"
              value={initialCapital}
              onChange={(e) => setInitialCapital(Number(e.target.value))}
              min={100}
              max={10000000}
              step={100}
              className="w-full rounded-md border border-input bg-background py-2 pl-7 pr-3 text-sm"
              required
            />
          </div>
        </div>

        <div>
          <label className="mb-2 block text-sm font-medium">Fee Rate</label>
          <div className="relative">
            <input
              type="number"
              value={feeRate * 100}
              onChange={(e) => setFeeRate(Number(e.target.value) / 100)}
              min={0}
              max={5}
              step={0.01}
              className="w-full rounded-md border border-input bg-background py-2 pl-3 pr-7 text-sm"
              required
            />
            <span className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground">
              %
            </span>
          </div>
        </div>
      </div>

      {/* Slippage Settings */}
      <div className="rounded-lg border border-border bg-card p-4">
        <h3 className="mb-4 font-medium">Slippage Model</h3>
        <div className="grid gap-3 md:grid-cols-2 lg:grid-cols-4">
          {SLIPPAGE_MODELS.map((model) => (
            <button
              key={model.value}
              type="button"
              onClick={() => setSlippageModel(model.value)}
              className={cn(
                'rounded-md border p-3 text-left transition-all',
                slippageModel === model.value
                  ? 'border-primary bg-primary/5'
                  : 'border-border hover:border-primary/50'
              )}
            >
              <p className="font-medium">{model.label}</p>
              <p className="text-xs text-muted-foreground">{model.description}</p>
            </button>
          ))}
        </div>

        {slippageModel !== 'none' && (
          <div className="mt-4">
            <label className="mb-2 block text-sm font-medium">
              Slippage (basis points)
            </label>
            <input
              type="number"
              value={slippageBps}
              onChange={(e) => setSlippageBps(Number(e.target.value))}
              min={0}
              max={100}
              step={1}
              className="w-32 rounded-md border border-input bg-background px-3 py-2 text-sm"
            />
            <p className="mt-1 text-xs text-muted-foreground">
              1 bps = 0.01% = $0.10 per $1000 traded
            </p>
          </div>
        )}
      </div>

      {/* Dynamic Strategy Config */}
      {selectedStrategyInfo && Object.keys(strategyConfig).length > 0 && (
        <div className="rounded-lg border border-border bg-card p-4">
          <h3 className="mb-4 font-medium">Strategy Parameters</h3>
          <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
            {Object.entries(strategyConfig).map(([key, value]) => (
              <ConfigField
                key={key}
                name={key}
                value={value}
                onChange={(newValue) => handleConfigChange(key, newValue)}
              />
            ))}
          </div>
        </div>
      )}

      {/* Capital Sweep (PLAN.md D12, T22) — a second, asynchronous
          producer alongside the single run below. `POST /backtests/sweep`
          runs `strategy` once per level in `capital_levels` and reports
          where the edge stops being viable; the parent run's
          `strategy_name` begins `sweep:`. It does not use the Initial
          Capital field above at all. */}
      <div className="rounded-lg border border-border bg-card p-4">
        <h3 className="mb-1 font-medium">Capital Sweep (optional)</h3>
        <p className="mb-4 text-sm text-muted-foreground">
          Instead of one run at a fixed capital, run this strategy once per capital level
          below and find where the edge dies. Runs asynchronously — the report appears on
          the Results tab once every level has completed, not immediately.
        </p>
        <label htmlFor="capital-levels" className="mb-2 block text-sm font-medium">
          Capital Levels (USD, comma-separated)
        </label>
        <input
          id="capital-levels"
          type="text"
          value={capitalLevelsText}
          onChange={(e) => {
            setCapitalLevelsText(e.target.value);
            setCapitalLevelsError(null);
          }}
          placeholder={DEFAULT_SWEEP_CAPITAL_LEVELS.join(', ')}
          className="w-full max-w-md rounded-md border border-input bg-background px-3 py-2 text-sm"
        />
        {capitalLevelsError && (
          <p className="mt-1 text-xs text-destructive">{capitalLevelsError}</p>
        )}
        <div className="mt-3">
          <button
            type="button"
            onClick={handleRunSweep}
            disabled={!selectedStrategy || isSubmittingSweep}
            className={cn(
              'rounded-md border border-primary px-4 py-2 text-sm font-medium text-primary transition-colors',
              'hover:bg-primary/10',
              'disabled:cursor-not-allowed disabled:opacity-50'
            )}
          >
            {isSubmittingSweep ? 'Queuing sweep...' : 'Run Capital Sweep'}
          </button>
        </div>
      </div>

      {/* Submit */}
      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground">
          Backtest will simulate {formatCurrency(initialCapital)} from {startDate} to{' '}
          {endDate}
        </p>
        <button
          type="submit"
          disabled={!selectedStrategy || isSubmitting}
          className={cn(
            'rounded-md px-6 py-2 font-medium transition-colors',
            'bg-primary text-primary-foreground hover:bg-primary/90',
            'disabled:cursor-not-allowed disabled:opacity-50'
          )}
        >
          {isSubmitting ? (
            <span className="flex items-center gap-2">
              <svg className="h-4 w-4 animate-spin" viewBox="0 0 24 24">
                <circle
                  className="opacity-25"
                  cx="12"
                  cy="12"
                  r="10"
                  stroke="currentColor"
                  strokeWidth="4"
                  fill="none"
                />
                <path
                  className="opacity-75"
                  fill="currentColor"
                  d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
                />
              </svg>
              Running...
            </span>
          ) : (
            'Run Backtest'
          )}
        </button>
      </div>
    </form>
  );
}

interface ConfigFieldProps {
  name: string;
  value: unknown;
  onChange: (value: unknown) => void;
}

function ConfigField({ name, value, onChange }: ConfigFieldProps) {
  const label = name
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase());

  // Determine field type based on value
  if (typeof value === 'boolean') {
    return (
      <div className="flex items-center gap-3">
        <input
          type="checkbox"
          id={name}
          checked={value}
          onChange={(e) => onChange(e.target.checked)}
          className="h-4 w-4 rounded border-input bg-background"
        />
        <label htmlFor={name} className="text-sm font-medium">
          {label}
        </label>
      </div>
    );
  }

  if (typeof value === 'number') {
    // Detect if it's likely a percentage (0-1 range)
    const isPercent = name.includes('pct') || name.includes('rate') || name.includes('threshold');
    const displayValue = isPercent && value <= 1 ? value * 100 : value;

    return (
      <div>
        <label htmlFor={name} className="mb-2 block text-sm font-medium">
          {label}
        </label>
        <div className="relative">
          <input
            type="number"
            id={name}
            value={displayValue}
            onChange={(e) => {
              const newValue = Number(e.target.value);
              onChange(isPercent && value <= 1 ? newValue / 100 : newValue);
            }}
            step={isPercent ? 0.1 : value < 1 ? 0.01 : 1}
            className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
          />
          {isPercent && (
            <span className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground">
              %
            </span>
          )}
        </div>
      </div>
    );
  }

  if (Array.isArray(value)) {
    return (
      <div>
        <label htmlFor={name} className="mb-2 block text-sm font-medium">
          {label}
        </label>
        <input
          type="text"
          id={name}
          value={value.join(', ')}
          onChange={(e) => onChange(e.target.value.split(',').map((s) => s.trim()))}
          placeholder="Comma-separated values"
          className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
        />
      </div>
    );
  }

  // String or other
  return (
    <div>
      <label htmlFor={name} className="mb-2 block text-sm font-medium">
        {label}
      </label>
      <input
        type="text"
        id={name}
        value={String(value)}
        onChange={(e) => onChange(e.target.value)}
        className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
      />
    </div>
  );
}

export default BacktestForm;
