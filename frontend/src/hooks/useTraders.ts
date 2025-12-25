import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { api } from '../services/api';

export function useTraders(params?: {
  skip?: number;
  limit?: number;
  sort_by?: string;
}) {
  return useQuery({
    queryKey: ['traders', params],
    queryFn: () => api.getTraders(params),
  });
}

export function useTrader(address: string) {
  return useQuery({
    queryKey: ['trader', address],
    queryFn: () => api.getTrader(address),
    enabled: !!address,
  });
}

export function useTraderTrades(
  address: string,
  params?: { skip?: number; limit?: number }
) {
  return useQuery({
    queryKey: ['traderTrades', address, params],
    queryFn: () => api.getTraderTrades(address, params),
    enabled: !!address,
  });
}

export function useLeaderboard(params?: {
  timeframe?: string;
  limit?: number;
}) {
  return useQuery({
    queryKey: ['leaderboard', params],
    queryFn: () => api.getLeaderboard(params),
  });
}

export function useFollowTrader() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (address: string) => api.followTrader(address),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['traders'] });
    },
  });
}

export function useUnfollowTrader() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (address: string) => api.unfollowTrader(address),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['traders'] });
    },
  });
}
