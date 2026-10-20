import { useMemo } from "react";
import { isMarketOpen } from "@/lib/market-hours";
import type { PositionItem, WatchlistItem } from "@/lib/queries/terminal";
import { useQuotes } from "@/lib/realtime/hooks";
import { screenSymbols, withQuote, withQuotePnl } from "../lib/terminal-view";

/** Live quotes for everything on screen; outside market hours nothing is subscribed. */
function useScreenQuotes(groups: readonly (readonly string[])[]) {
  const symbols = useMemo(() => {
    if (!isMarketOpen().isOpen) return [];
    return screenSymbols(groups);
  }, [groups]);
  return useQuotes(symbols);
}

/**
 * Watchlist and position rows with the shared quote store's live values. Rows are derived on
 * render, not synced via effects.
 */
export function useLiveRows(params: {
  watchlist: WatchlistItem[];
  selectedSymbol: string;
  optionSymbol: string;
  positions: PositionItem[];
}) {
  const { watchlist: items, selectedSymbol, optionSymbol, positions: basePositions } = params;
  const quoteGroups = useMemo(
    () => [
      items.map((item) => item.symbol),
      [selectedSymbol, optionSymbol],
      basePositions.map((position) => position.symbol),
    ],
    [items, selectedSymbol, optionSymbol, basePositions],
  );
  const quotes = useScreenQuotes(quoteGroups);
  const watchlist = useMemo(
    () => items.map((item) => withQuote(item, quotes[item.symbol])),
    [items, quotes],
  );
  const positions = useMemo(
    () => basePositions.map((pos) => withQuotePnl(pos, quotes[pos.symbol])),
    [basePositions, quotes],
  );
  return { watchlist, positions };
}
