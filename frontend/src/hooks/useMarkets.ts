import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { api } from '../services/api';
import type { Market } from '../types';

export function useMarkets(params?: {
  skip?: number;
  limit?: number;
  active?: boolean;
}) {
  return useQuery({
    queryKey: ['markets', params],
    queryFn: async () => {
      const response = await api.getMarkets(params);
      return response.data || [];
    },
    staleTime: 1000 * 60, // 1 minute
  });
}

export function useMarket(conditionId: string) {
  return useQuery({
    queryKey: ['market', conditionId],
    queryFn: () => api.getMarket(conditionId),
    enabled: !!conditionId,
  });
}

export function useOrderbook(conditionId: string) {
  return useQuery({
    queryKey: ['orderbook', conditionId],
    queryFn: () => api.getOrderbook(conditionId),
    enabled: !!conditionId,
    refetchInterval: 5000, // Refresh every 5 seconds
  });
}

export function usePriceHistory(conditionId: string, interval?: string) {
  return useQuery({
    queryKey: ['priceHistory', conditionId, interval],
    queryFn: () => api.getPriceHistory(conditionId, interval),
    enabled: !!conditionId,
  });
}
