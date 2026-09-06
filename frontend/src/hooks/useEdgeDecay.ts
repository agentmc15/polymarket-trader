import { useQuery } from '@tanstack/react-query';
import { api } from '../services/api';

/**
 * Fetch a sweep's `EdgeDecayReport`
 * (`GET /api/v1/backtests/{id}/edge-decay`, PLAN.md D12/T22).
 *
 * `backtestId` is the PARENT sweep's id (`strategy_name=f"sweep:{name}"`),
 * not one of the per-level child runs. 404s (not a sweep, or not
 * completed yet) surface as `query.isError` for the caller to render.
 */
export function useEdgeDecay(backtestId: number | null) {
  return useQuery({
    queryKey: ['backtest', 'edge-decay', backtestId],
    queryFn: () => api.getEdgeDecay(backtestId!),
    enabled: backtestId !== null,
    retry: false,
  });
}
