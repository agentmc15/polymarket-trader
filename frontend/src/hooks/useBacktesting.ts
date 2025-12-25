import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { backtestApi } from '../services/backtestApi';
import type { BacktestRequest, BacktestStatus } from '../types';

export function useStrategies() {
  return useQuery({
    queryKey: ['strategies'],
    queryFn: () => backtestApi.getStrategies(),
    staleTime: 5 * 60 * 1000, // Cache for 5 minutes
  });
}

export function useBacktestStatus(backtestId: number | null) {
  return useQuery({
    queryKey: ['backtest', 'status', backtestId],
    queryFn: () => backtestApi.getBacktestStatus(backtestId!),
    enabled: backtestId !== null,
    refetchInterval: (query) => {
      const data = query.state.data;
      // Poll while running
      if (data?.status === 'PENDING' || data?.status === 'RUNNING') {
        return 2000;
      }
      return false;
    },
  });
}

export function useBacktestEquityCurve(backtestId: number | null, enabled = true) {
  return useQuery({
    queryKey: ['backtest', 'equity-curve', backtestId],
    queryFn: () => backtestApi.getEquityCurve(backtestId!),
    enabled: backtestId !== null && enabled,
  });
}

export function useBacktestTrades(
  backtestId: number | null,
  params?: { page?: number; page_size?: number }
) {
  return useQuery({
    queryKey: ['backtest', 'trades', backtestId, params],
    queryFn: () => backtestApi.getTrades(backtestId!, params),
    enabled: backtestId !== null,
  });
}

export function useBacktests(params?: {
  page?: number;
  page_size?: number;
  status?: BacktestStatus;
  strategy?: string;
}) {
  return useQuery({
    queryKey: ['backtests', params],
    queryFn: () => backtestApi.listBacktests(params),
  });
}

export function useRunBacktest() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (request: BacktestRequest) => backtestApi.runBacktest(request),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['backtests'] });
    },
  });
}

export function useDeleteBacktest() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (backtestId: number) => backtestApi.deleteBacktest(backtestId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['backtests'] });
    },
  });
}

// Combined hook for a complete backtest view
export function useBacktest(backtestId: number | null) {
  const statusQuery = useBacktestStatus(backtestId);
  const isCompleted = statusQuery.data?.status === 'COMPLETED';

  const equityCurveQuery = useBacktestEquityCurve(backtestId, isCompleted);
  const tradesQuery = useBacktestTrades(backtestId);

  return {
    status: statusQuery.data?.status ?? 'PENDING',
    progress: statusQuery.data?.progress ?? 0,
    metrics: statusQuery.data?.metrics,
    errorMessage: statusQuery.data?.error_message,
    equityCurve: equityCurveQuery.data?.equity_curve ?? [],
    initialCapital: equityCurveQuery.data?.initial_capital ?? 0,
    finalCapital: equityCurveQuery.data?.final_capital,
    trades: tradesQuery.data?.trades ?? [],
    isLoading: statusQuery.isLoading,
    isLoadingEquity: equityCurveQuery.isLoading,
    isLoadingTrades: tradesQuery.isLoading,
    error: statusQuery.error || equityCurveQuery.error || tradesQuery.error,
  };
}
