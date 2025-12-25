import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { api } from '../services/api';
import type { OrderRequest } from '../types';

export function useOrders(status?: string) {
  return useQuery({
    queryKey: ['orders', status],
    queryFn: () => api.getOrders(status),
    refetchInterval: 10000, // Refresh every 10 seconds
  });
}

export function usePositions() {
  return useQuery({
    queryKey: ['positions'],
    queryFn: () => api.getPositions(),
    refetchInterval: 30000, // Refresh every 30 seconds
  });
}

export function usePosition(conditionId: string) {
  return useQuery({
    queryKey: ['position', conditionId],
    queryFn: () => api.getPosition(conditionId),
    enabled: !!conditionId,
  });
}

export function usePlaceOrder() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (order: OrderRequest) => api.placeOrder(order),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['orders'] });
      queryClient.invalidateQueries({ queryKey: ['positions'] });
    },
  });
}

export function useCancelOrder() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (orderId: string) => api.cancelOrder(orderId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['orders'] });
    },
  });
}
