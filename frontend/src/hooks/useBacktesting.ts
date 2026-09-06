import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { backtestApi } from '../services/backtestApi';
import type { BacktestRequest, BacktestStatus, SweepRequest } from '../types';

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
  params?: { skip?: number; limit?: number }
) {
  return useQuery({
    queryKey: ['backtest', 'trades', backtestId, params],
    queryFn: () => backtestApi.getTrades(backtestId!, params),
    enabled: backtestId !== null,
  });
}

export function useBacktests(params?: {
  skip?: number;
  limit?: number;
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

/**
 * Start a capital sweep (PLAN.md D12, T22) — `BacktestForm`'s producer
 * for `POST /backtests/sweep`. Asynchronous exactly like
 * `useRunBacktest`: the mutation resolves with the PARENT run's
 * `{ id, status: "PENDING" }`, and the actual per-level report only
 * exists once that run completes (`useEdgeDecay`, read via
 * `GET /backtests/{id}/edge-decay`).
 */
export function useRunSweep() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (request: SweepRequest) => backtestApi.runSweep(request),
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
    finalValue: statusQuery.data?.final_value ?? undefined,
    totalReturn: statusQuery.data?.total_return ?? undefined,
    totalReturnPct: statusQuery.data?.total_return_pct ?? undefined,
    tradeMetrics: statusQuery.data?.trade_metrics ?? undefined,
    riskMetrics: statusQuery.data?.risk_metrics ?? undefined,
    errorMessage: statusQuery.data?.error_message ?? undefined,
    strategyName: statusQuery.data?.strategy_name,
    report: statusQuery.data?.report,
    equityCurve: equityCurveQuery.data?.points ?? [],
    initialCapital: statusQuery.data?.initial_capital ?? 0,
    trades: tradesQuery.data?.trades ?? [],
    isLoading: statusQuery.isLoading,
    isLoadingEquity: equityCurveQuery.isLoading,
    isLoadingTrades: tradesQuery.isLoading,
    error: statusQuery.error || equityCurveQuery.error || tradesQuery.error,
  };
}
