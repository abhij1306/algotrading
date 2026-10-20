import { Activity, Moon, Radio } from "lucide-react";
import { roundToDecimals } from "@/lib/format";
import type { DashboardWatchlistItem, MarketIndex } from "@/lib/queries/dashboard";
import type { QuoteMap } from "@/lib/realtime/quote-store";

/** Pure dashboard view logic: badge and section labels, and live quotes laid over fetched rows. */

export type StatusBadge = {
  icon: typeof Radio;
  label: string;
  className: string;
};

export type MarketSectionMeta = {
  title: string;
  subtitle: string;
};

/** Indices shown in the header ticker. */
export const HEADER_INDEX_SYMBOLS: ReadonlySet<string> = new Set([
  "NIFTY50",
  "BANKNIFTY",
  "NIFTYIT",
]);

/** LIVE when the market is open and streaming, DELAYED when open without a socket, else CLOSED. */
export function getStatusBadge(params: { marketOpen: boolean; isConnected: boolean }): StatusBadge {
  const { marketOpen, isConnected } = params;
  if (marketOpen && isConnected) {
    return { icon: Radio, label: "LIVE", className: "bg-profit-bg text-profit" };
  }
  if (marketOpen) {
    return { icon: Activity, label: "DELAYED", className: "bg-amber-500/10 text-amber-400" };
  }
  return {
    icon: Moon,
    label: "CLOSED",
    className: "bg-background-tertiary text-foreground-secondary",
  };
}

export function getMarketSectionMeta(params: {
  marketOpen: boolean;
  isConnected: boolean;
}): MarketSectionMeta {
  const { marketOpen, isConnected } = params;
  if (marketOpen) {
    return {
      title: "Indian Markets",
      subtitle: isConnected ? "NSE Real-time" : "NSE Delayed (Disconnected)",
    };
  }
  return { title: "Global Markets", subtitle: "Yahoo Delayed" };
}

export function getCompactIndexLabel(symbol: string): string {
  if (symbol === "NIFTY50") return "NIFTY";
  if (symbol === "BANKNIFTY") return "BANK";
  return "IT";
}

/** The terminal route for a symbol, encoded so "M&M" stays one query value. */
export function terminalPath(symbol: string): string {
  return `/terminal?${new URLSearchParams({ symbol }).toString()}`;
}

function roundedOr(value: number | undefined, fallback: number | null): number | null {
  return typeof value === "number" ? roundToDecimals(value, 2) : fallback;
}

/** Watchlist rows with the live last price and day percent laid over the fetched ones. */
export function applyWatchlistQuotes(
  rows: readonly DashboardWatchlistItem[],
  quotes: QuoteMap,
): DashboardWatchlistItem[] {
  return rows.map((item) => {
    const quote = quotes[item.symbol];
    if (!quote) return item;
    return {
      ...item,
      price: roundedOr(quote.ltp, item.price),
      changePercent: roundedOr(quote.changePct, item.changePercent),
    };
  });
}

/** Index rows with live quotes laid over them; a quoted row is marked as streaming. */
export function applyIndexQuotes(rows: readonly MarketIndex[], quotes: QuoteMap): MarketIndex[] {
  return rows.map((idx) => {
    const quote = quotes[idx.symbol];
    if (!quote) return idx;
    return {
      ...idx,
      value: roundedOr(quote.ltp, idx.value) ?? idx.value,
      changePercent: roundedOr(quote.changePct, idx.changePercent) ?? idx.changePercent,
      source: "websocket",
    };
  });
}
