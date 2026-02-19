/**
 * Market Store - Global state management for market data
 * Using Zustand for lightweight, performant state management
 */
import { create } from 'zustand';
import { devtools } from 'zustand/middleware';
import { LiveQuote } from '@/lib/types/api';

interface MarketState {
  // Selected symbol
  selectedSymbol: string | null;
  setSelectedSymbol: (symbol: string | null) => void;

  // Watchlist
  watchlist: string[];
  addToWatchlist: (symbol: string) => void;
  removeFromWatchlist: (symbol: string) => void;
  setWatchlist: (symbols: string[]) => void;

  // Live quotes cache
  liveQuotes: Record<string, LiveQuote>;
  updateLiveQuote: (symbol: string, quote: LiveQuote) => void;
  updateLiveQuotes: (quotes: Record<string, LiveQuote>) => void;

  // Connection status
  isConnected: boolean;
  setIsConnected: (connected: boolean) => void;

  // Screener filters
  screenerFilters: {
    universe: string;
    query: string;
    preset: string | null;
  };
  setScreenerFilters: (filters: Partial<MarketState['screenerFilters']>) => void;
}

export const useMarketStore = create<MarketState>()(
  devtools(
    (set) => ({
      // Selected symbol
      selectedSymbol: null,
      setSelectedSymbol: (symbol) => set({ selectedSymbol: symbol }),

      // Watchlist
      watchlist: [],
      addToWatchlist: (symbol) =>
        set((state) => ({
          watchlist: state.watchlist.includes(symbol)
            ? state.watchlist
            : [...state.watchlist, symbol],
        })),
      removeFromWatchlist: (symbol) =>
        set((state) => ({
          watchlist: state.watchlist.filter((s) => s !== symbol),
        })),
      setWatchlist: (symbols) => set({ watchlist: symbols }),

      // Live quotes
      liveQuotes: {},
      updateLiveQuote: (symbol, quote) =>
        set((state) => ({
          liveQuotes: { ...state.liveQuotes, [symbol]: quote },
        })),
      updateLiveQuotes: (quotes) =>
        set((state) => ({
          liveQuotes: { ...state.liveQuotes, ...quotes },
        })),

      // Connection status
      isConnected: false,
      setIsConnected: (connected) => set({ isConnected: connected }),

      // Screener filters
      screenerFilters: {
        universe: 'nifty50',
        query: '',
        preset: null,
      },
      setScreenerFilters: (filters) =>
        set((state) => ({
          screenerFilters: { ...state.screenerFilters, ...filters },
        })),
    }),
    { name: 'MarketStore' }
  )
);
