import { useMemo } from "react";
import { isMarketOpen } from "@/lib/market-hours";
import type { PositionItem, WatchlistItem } from "@/lib/queries/terminal";
import { useQuotes } from "@/lib/realtime/hooks";
import { useNow } from "@/lib/use-now";
import { screenSymbols, withQuote, withQuotePnl } from "../lib/terminal-view";

const CLOCK_TICK_MS = 1000;
const NO_SYMBOLS: string[] = [];

/**
 * Live quotes for everything on screen; outside market hours nothing is subscribed. The clock
 * re-reads market hours each tick, so subscriptions start at the open and stop at the close
 * even when the symbols on screen do not change.
 */
function useScreenQuotes(groups: readonly (readonly string[])[]) {
  // The tick only re-renders; market hours are read at the real instant, not the floored tick,
  // so the first tick after 15:30:00.000 already reads closed.
  useNow(CLOCK_TICK_MS);
  const open = isMarketOpen().isOpen;
  const symbols = useMemo(() => (open ? screenSymbols(groups) : NO_SYMBOLS), [groups, open]);
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
