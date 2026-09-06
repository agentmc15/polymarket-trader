import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../services/api';

/**
 * Fetch the latest scan's scored, pending opportunities
 * (`GET /api/v1/arbitrage/opportunities`).
 *
 * Polls every 10s so the table reflects the periodic scanner beat
 * without a manual refresh, mirroring the old `useArbitrageOpportunities`
 * hook's refetch cadence.
 */
export function useOpportunities(params?: {
  minComposite?: number;
  venue?: string;
  nearResolution?: boolean;
}) {
  return useQuery({
    queryKey: ['opportunities', params],
    queryFn: () => api.getOpportunities(params),
    refetchInterval: 10000,
  });
}

/**
 * Trigger an on-demand discovery scan (`POST /api/v1/arbitrage/scan`) and
 * invalidate the opportunities list so the table picks up what it found.
 *
 * DISCOVERY ONLY — see `app.api.routes.arbitrage`'s module docstring;
 * this never places or routes an order.
 */
export function useTriggerScan() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (strategies?: string[]) => api.triggerScan(strategies),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['opportunities'] });
    },
  });
}
