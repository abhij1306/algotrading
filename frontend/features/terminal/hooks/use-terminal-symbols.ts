import { useMemo } from "react";
import { useSearchParams } from "react-router";
import { useTerminalWatchlist, type WatchlistItem } from "@/lib/queries/terminal";
import { listWithSelected, selectedSymbolFor } from "../lib/terminal-view";

const NO_WATCHLIST: WatchlistItem[] = [];

/**
 * Saved watchlist plus the selected symbol. The selection lives in `?symbol=`, so a link (or the
 * command palette) can open any symbol and a pick replaces it; with none set it falls back to the
 * first saved row, derived on render rather than synced by an effect.
 */
export function useTerminalSymbols() {
  const watchlistQuery = useTerminalWatchlist();
  const [searchParams, setSearchParams] = useSearchParams();
  const saved = watchlistQuery.data ?? NO_WATCHLIST;
  const selectedSymbol = selectedSymbolFor(searchParams.get("symbol") ?? "", saved);
  const items = useMemo(() => listWithSelected(saved, selectedSymbol), [saved, selectedSymbol]);
  const setSelectedSymbol = (symbol: string) =>
    setSearchParams(
      (current) => {
        const next = new URLSearchParams(current);
        next.set("symbol", symbol);
        return next;
      },
      { replace: true },
    );
  return {
    items,
    selectedSymbol,
    setSelectedSymbol,
    isLoading: watchlistQuery.isPending,
    error: watchlistQuery.isError ? "Failed to fetch watchlist" : null,
  };
}

export type TerminalSymbols = ReturnType<typeof useTerminalSymbols>;
