import { cn } from '../../utils/cn';
import type { StrategyInfo } from '../../types';

interface StrategySelectorProps {
  strategies: StrategyInfo[];
  categories: Record<string, string[]>;
  selectedStrategy: string | null;
  onSelect: (strategyName: string) => void;
  isLoading?: boolean;
}

const categoryColors: Record<string, string> = {
  arbitrage: 'bg-blue-500/10 text-blue-400 border-blue-500/30',
  momentum: 'bg-green-500/10 text-green-400 border-green-500/30',
  mean_reversion: 'bg-purple-500/10 text-purple-400 border-purple-500/30',
  copy_trading: 'bg-orange-500/10 text-orange-400 border-orange-500/30',
  statistical: 'bg-pink-500/10 text-pink-400 border-pink-500/30',
};

const categoryIcons: Record<string, string> = {
  arbitrage: 'arrows-exchange',
  momentum: 'trending-up',
  mean_reversion: 'activity',
  copy_trading: 'users',
  statistical: 'chart-bar',
};

export function StrategySelector({
  strategies,
  categories,
  selectedStrategy,
  onSelect,
  isLoading,
}: StrategySelectorProps) {
  if (isLoading) {
    return (
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
        {[1, 2, 3, 4, 5, 6].map((i) => (
          <div
            key={i}
            className="h-40 animate-pulse rounded-lg border border-border bg-card"
          />
        ))}
      </div>
    );
  }

  if (strategies.length === 0) {
    return (
      <div className="rounded-lg border border-border bg-card p-8 text-center">
        <p className="text-muted-foreground">No strategies available</p>
      </div>
    );
  }

  // Group strategies by category
  const groupedStrategies = Object.entries(categories).map(([category, strategyNames]) => ({
    category,
    strategies: strategies.filter((s) => strategyNames.includes(s.name)),
  }));

  return (
    <div className="space-y-6">
      {groupedStrategies.map(({ category, strategies: categoryStrategies }) => (
        <div key={category}>
          <h3 className="mb-3 text-sm font-medium capitalize text-muted-foreground">
            {category.replace(/_/g, ' ')}
          </h3>
          <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
            {categoryStrategies.map((strategy) => (
              <StrategyCard
                key={strategy.name}
                strategy={strategy}
                isSelected={selectedStrategy === strategy.name}
                onClick={() => onSelect(strategy.name)}
                categoryColor={categoryColors[category] || 'bg-gray-500/10 text-gray-400 border-gray-500/30'}
              />
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

interface StrategyCardProps {
  strategy: StrategyInfo;
  isSelected: boolean;
  onClick: () => void;
  categoryColor: string;
}

function StrategyCard({ strategy, isSelected, onClick, categoryColor }: StrategyCardProps) {
  const configKeys = Object.keys(strategy.default_config || {});

  return (
    <button
      onClick={onClick}
      className={cn(
        'relative flex flex-col items-start rounded-lg border p-4 text-left transition-all',
        'hover:border-primary/50 hover:bg-accent/50',
        isSelected
          ? 'border-primary bg-primary/5 ring-1 ring-primary'
          : 'border-border bg-card'
      )}
    >
      {isSelected && (
        <div className="absolute right-3 top-3">
          <svg
            className="h-5 w-5 text-primary"
            fill="currentColor"
            viewBox="0 0 20 20"
          >
            <path
              fillRule="evenodd"
              d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z"
              clipRule="evenodd"
            />
          </svg>
        </div>
      )}

      <div className="mb-2 flex items-center gap-2">
        <span
          className={cn(
            'rounded-md border px-2 py-0.5 text-xs font-medium capitalize',
            categoryColor
          )}
        >
          {strategy.category.replace(/_/g, ' ')}
        </span>
      </div>

      <h4 className="mb-1 font-medium text-foreground">{strategy.display_name}</h4>
      <p className="mb-3 text-sm text-muted-foreground line-clamp-2">
        {strategy.description}
      </p>

      {configKeys.length > 0 && (
        <div className="mt-auto flex flex-wrap gap-1">
          {configKeys.slice(0, 3).map((key) => (
            <span
              key={key}
              className="rounded bg-muted px-1.5 py-0.5 text-xs text-muted-foreground"
            >
              {key.replace(/_/g, ' ')}
            </span>
          ))}
          {configKeys.length > 3 && (
            <span className="rounded bg-muted px-1.5 py-0.5 text-xs text-muted-foreground">
              +{configKeys.length - 3} more
            </span>
          )}
        </div>
      )}
    </button>
  );
}

export default StrategySelector;
