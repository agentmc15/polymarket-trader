import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { api } from '../services/api';

export function useArbitrageOpportunities(minProfit?: number) {
  return useQuery({
    queryKey: ['arbitrageOpportunities', minProfit],
    queryFn: () => api.getArbitrageOpportunities(minProfit),
    refetchInterval: 10000, // Refresh every 10 seconds
  });
}

export function useScanArbitrage() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: () => api.scanArbitrage(),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['arbitrageOpportunities'] });
    },
  });
}
