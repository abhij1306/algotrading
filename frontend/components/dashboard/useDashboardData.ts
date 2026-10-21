import { useMemo, useState } from "react";
import type { MarketStatus } from "@/lib/market-hours";
import {
  formatIstClock,
  useDashboardWatchlist,
  useMarketIndices,
  useMarketStatus,
  type DashboardWatchlistItem,
  type MarketIndex,
} from "@/lib/queries/dashboard";
import { useConnectionStatus, useQuotes } from "@/lib/realtime/hooks";
import { applyIndexQuotes, applyWatchlistQuotes } from "./model";

const INITIAL_MARKET_STATUS: MarketStatus = { isOpen: false, message: "Loading market status" };
const NO_INDICES: MarketIndex[] = [];
const NO_WATCHLIST: DashboardWatchlistItem[] = [];
const NO_SYMBOLS: string[] = [];

/** One panel's rows with its loading and failure state. */
export interface PanelData<Row> {
  rows: Row[];
  loading: boolean;
  error: Error | null;
}

export interface DashboardData {
  marketStatus: MarketStatus;
  /** False until the first market status arrives; `marketStatus` is a closed placeholder until then. */
  marketStatusLoaded: boolean;
  /** True only while the market is open and the quote socket is connected. */
  isConnected: boolean;
  istTime: string;
  indices: PanelData<MarketIndex>;
  watchlist: PanelData<DashboardWatchlistItem>;
  /** The first failure among the panels, for the page-wide empty state. */
  loadError: Error | null;
}

/**
 * Everything the dashboard shows: the polled panels from Query, with live quotes from the store
 * laid over the indices and watchlist while the market is open.
 */
export function useDashboardData(): DashboardData {
  const statusQuery = useMarketStatus();
  const watchlistQuery = useDashboardWatchlist();
  const marketStatus = statusQuery.data?.status ?? INITIAL_MARKET_STATUS;
  const indicesQuery = useMarketIndices({
    marketOpen: marketStatus.isOpen,
    enabled: statusQuery.data !== undefined,
  });
  // Connection state only; the socket is held open during market hours.
  const connection = useConnectionStatus({ enabled: marketStatus.isOpen });

  const [initialIstTime] = useState(() => formatIstClock());
  const fetchedIndices = indicesQuery.data?.indicesData ?? NO_INDICES;
  const fetchedWatchlist = watchlistQuery.data ?? NO_WATCHLIST;
  const streaming = marketStatus.isOpen && indicesQuery.data?.nextMode === "market";
  // Live quotes for the shown rows while the market is open; the store owns the subscriptions.
  const liveSymbols = useMemo(
    () =>
      streaming
        ? [
            ...fetchedWatchlist.map((item) => item.symbol),
            ...fetchedIndices.map((idx) => idx.symbol),
          ]
        : NO_SYMBOLS,
    [streaming, fetchedWatchlist, fetchedIndices],
  );
  const quotes = useQuotes(liveSymbols);

  return {
    marketStatus,
    marketStatusLoaded: statusQuery.data !== undefined,
    isConnected: marketStatus.isOpen && connection === "connected",
    istTime: statusQuery.data?.istTime ?? initialIstTime,
    indices: {
      rows: applyIndexQuotes(fetchedIndices, quotes),
      loading: indicesQuery.isPending,
      error: indicesQuery.error,
    },
    watchlist: {
      rows: applyWatchlistQuotes(fetchedWatchlist, quotes),
      loading: watchlistQuery.isPending,
      error: watchlistQuery.error,
    },
    loadError: watchlistQuery.error ?? indicesQuery.error,
  };
}
