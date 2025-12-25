import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { api } from '../services/api';

export function useBots() {
  return useQuery({
    queryKey: ['bots'],
    queryFn: () => api.getBots(),
    refetchInterval: 10000, // Refresh every 10 seconds
  });
}

export function useBot(botId: string) {
  return useQuery({
    queryKey: ['bot', botId],
    queryFn: () => api.getBot(botId),
    enabled: !!botId,
    refetchInterval: 5000,
  });
}

export function useBotTrades(
  botId: string,
  params?: { skip?: number; limit?: number }
) {
  return useQuery({
    queryKey: ['botTrades', botId, params],
    queryFn: () => api.getBotTrades(botId, params),
    enabled: !!botId,
  });
}

export function useCreateBot() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (config: {
      name: string;
      strategy_id: string;
      parameters?: Record<string, unknown>;
      max_position_size?: number;
      max_daily_trades?: number;
    }) => api.createBot(config),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['bots'] });
    },
  });
}

export function useUpdateBot() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      botId,
      config,
    }: {
      botId: string;
      config: Partial<{
        name: string;
        strategy_id: string;
        parameters: Record<string, unknown>;
        max_position_size: number;
        max_daily_trades: number;
        enabled: boolean;
      }>;
    }) => api.updateBot(botId, config),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['bots'] });
    },
  });
}

export function useDeleteBot() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (botId: string) => api.deleteBot(botId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['bots'] });
    },
  });
}

export function useStartBot() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (botId: string) => api.startBot(botId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['bots'] });
    },
  });
}

export function useStopBot() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (botId: string) => api.stopBot(botId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['bots'] });
    },
  });
}
