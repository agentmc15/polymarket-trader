import { create } from 'zustand';
import { persist } from 'zustand/middleware';

interface AppState {
  // Theme
  theme: 'light' | 'dark' | 'system';
  setTheme: (theme: 'light' | 'dark' | 'system') => void;

  // Connection
  isConnected: boolean;
  setConnected: (connected: boolean) => void;

  // Selected market
  selectedMarketId: string | null;
  setSelectedMarket: (id: string | null) => void;

  // Notifications
  notifications: Array<{
    id: string;
    type: 'info' | 'success' | 'warning' | 'error';
    message: string;
    timestamp: number;
  }>;
  addNotification: (
    type: 'info' | 'success' | 'warning' | 'error',
    message: string
  ) => void;
  removeNotification: (id: string) => void;
  clearNotifications: () => void;
}

export const useAppStore = create<AppState>()(
  persist(
    (set) => ({
      // Theme
      theme: 'dark',
      setTheme: (theme) => set({ theme }),

      // Connection
      isConnected: false,
      setConnected: (isConnected) => set({ isConnected }),

      // Selected market
      selectedMarketId: null,
      setSelectedMarket: (selectedMarketId) => set({ selectedMarketId }),

      // Notifications
      notifications: [],
      addNotification: (type, message) =>
        set((state) => ({
          notifications: [
            ...state.notifications,
            {
              id: Math.random().toString(36).substring(7),
              type,
              message,
              timestamp: Date.now(),
            },
          ],
        })),
      removeNotification: (id) =>
        set((state) => ({
          notifications: state.notifications.filter((n) => n.id !== id),
        })),
      clearNotifications: () => set({ notifications: [] }),
    }),
    {
      name: 'polymarket-trader-storage',
      partialize: (state) => ({
        theme: state.theme,
      }),
    }
  )
);
