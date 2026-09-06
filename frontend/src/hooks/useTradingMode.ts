import { useQuery } from '@tanstack/react-query';
import { api } from '../services/api';

/**
 * Report the active trading mode and kill-switch state
 * (`GET /api/v1/trading/mode`). Polled, not one-shot, so a header pill
 * reading this never goes stale for the length of a session.
 */
export function useTradingMode() {
  return useQuery({
    queryKey: ['trading-mode'],
    queryFn: () => api.getTradingMode(),
    refetchInterval: 30000,
  });
}
