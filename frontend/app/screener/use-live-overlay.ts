import { useMemo } from "react";
import { useLiveQuotePoll, type ScreenerRow } from "@/lib/queries/screener";
import { useConnectionStatus, useQuotes } from "@/lib/realtime/hooks";
import type { QuoteMap } from "@/lib/realtime/quote-store";
import { isMarketOpen } from "@/lib/market-hours";
import { useNow } from "@/lib/use-now";
import {
  rowQuotes,
  symbolsToPoll,
  type QuotePair,
  type StreamState,
} from "@/components/screener/screener-view";

const NO_QUOTES: QuoteMap = {};
const CLOCK_TICK_MS = 1000;

/**
 * Live values for the rendered rows: the store subscribes exactly these symbols and drops them
 * when the rows change or the page unmounts. Symbols the socket is not streaming fall back to
 * REST polling. While the market is open a connected socket can still go silent, so streamed
 * quotes age out: stale ones are polled too, and whichever quote arrived last wins. The clock
 * re-renders the page so a silent socket is noticed.
 */
export function useLiveOverlay(rows: readonly ScreenerRow[]) {
  const connected = useConnectionStatus() === "connected";
  const visibleSymbols = useMemo(() => rows.map((row) => row.symbol), [rows]);
  const streamedQuotes = useQuotes(visibleSymbols);
  const now = useNow(CLOCK_TICK_MS);
  const stream: StreamState = {
    connected,
    aging: connected && isMarketOpen(new Date(now)).isOpen,
    now,
  };
  const polledQuotes =
    useLiveQuotePoll(symbolsToPoll(visibleSymbols, streamedQuotes, stream)).data ?? NO_QUOTES;

  /** A row's quotes in priority order. */
  const quotesFor = (symbol: string): QuotePair =>
    rowQuotes(streamedQuotes[symbol], polledQuotes[symbol], stream);

  return { connected, quotesFor };
}
