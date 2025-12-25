import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { api } from '../services/api';

export function useBacktests(params?: {
  skip?: number;
  limit?: number;
}) {
  return useQuery({
    queryKey: ['backtests', params],
    queryFn: () => api.getBacktests(params),
  });
}

export function useBacktest(backtestId: string) {
  return useQuery({
    queryKey: ['backtest', backtestId],
    queryFn: () => api.getBacktest(backtestId),
    enabled: !!backtestId,
    refetchInterval: (data) =>
      data?.status === 'RUNNING' ? 2000 : false, // Poll while running
  });
}

export function useBacktestTrades(backtestId: string) {
  return useQuery({
    queryKey: ['backtestTrades', backtestId],
    queryFn: () => api.getBacktestTrades(backtestId),
    enabled: !!backtestId,
  });
}

export function useStrategies() {
  return useQuery({
    queryKey: ['strategies'],
    queryFn: () => api.getStrategies(),
  });
}

export function useRunBacktest() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (request: {
      strategy_id: string;
      start_date: string;
      end_date: string;
      initial_capital?: number;
      parameters?: Record<string, unknown>;
    }) => api.runBacktest(request),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['backtests'] });
    },
  });
}
