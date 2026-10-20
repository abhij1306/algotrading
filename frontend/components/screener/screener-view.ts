/**
 * Pure view logic for the screener page: the sort a header click asks for, how universes are
 * ordered and chosen, which symbols the REST poll covers, which quote wins a row, and how cells
 * are formatted and coloured. Sorting the rows themselves stays on the server.
 */

import type { Quote, QuoteMap } from "@/lib/realtime/quote-store";
import type { ScreenerIndex, ScreenerRow } from "@/lib/queries/screener";
import {
  formatCompactNumber,
  formatCrores,
  formatSignedPercent,
  roundToDecimals,
} from "@/lib/format";

export type SortField =
  | "symbol"
  | "price"
  | "change"
  | "volume"
  | "marketCap"
  | "rsi"
  | "macd"
  | "adx";
export type SortDirection = "asc" | "desc";
export type SortState = { field: SortField; direction: SortDirection };

export type FlashDirection = "up" | "down";
export type FlashState = { direction: FlashDirection; phase: 0 | 1 };

/** The two quotes for a row: the one that wins, then the one that fills the fields it lacks. */
export type QuotePair = [Quote | undefined, Quote | undefined];

/** A streamed quote older than this counts as missing while the market is open. */
export const STREAM_STALE_MS = 5000;

export function toNumber(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

export function toSafeString(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

/**
 * The sort after clicking `field`: the same column flips direction, a new column starts
 * ascending for the symbol and descending for every number.
 */
export function nextSort(current: SortState, field: SortField): SortState {
  if (current.field === field) {
    return { field, direction: current.direction === "asc" ? "desc" : "asc" };
  }
  return { field, direction: field === "symbol" ? "asc" : "desc" };
}

/** Universes for the picker: NIFTY50 first, the rest by name. */
export function sortIndicesForUi(indices: readonly ScreenerIndex[]): ScreenerIndex[] {
  return [...indices].sort((a, b) => {
    if (a.id === "NIFTY50") return -1;
    if (b.id === "NIFTY50") return 1;
    return a.name.localeCompare(b.name);
  });
}

/** The chosen universe if offered, else the first one; empty while none are offered. */
export function resolveSelectedUniverse(
  current: string,
  indices: readonly ScreenerIndex[],
): string {
  if (indices.length === 0) return "";
  return indices.some((index) => index.id === current) ? current : indices[0].id;
}

/** Whether a streamed quote is missing or older than `STREAM_STALE_MS` at `now`. */
export function isStale(quote: Quote | undefined, now: number): boolean {
  return quote?.receivedAt === undefined || now - quote.receivedAt > STREAM_STALE_MS;
}

/** The two quotes with the one that arrived last first; a tie keeps `a` first. */
export function newestFirst(a: Quote | undefined, b: Quote | undefined): QuotePair {
  return (b?.receivedAt ?? -Infinity) > (a?.receivedAt ?? -Infinity) ? [b, a] : [a, b];
}

/** How the socket's quotes are trusted right now. */
export type StreamState = {
  connected: boolean;
  /** Connected while the market is open, so a silent socket's quotes age out. */
  aging: boolean;
  now: number;
};

/**
 * The symbols the REST poll covers: all of them while the socket is down, the ones it has not
 * streamed while connected, and the missing or stale ones while connected in market hours.
 */
export function symbolsToPoll(
  symbols: string[],
  streamed: QuoteMap,
  { connected, aging, now }: StreamState,
): string[] {
  if (aging) return symbols.filter((symbol) => isStale(streamed[symbol], now));
  if (connected) return symbols.filter((symbol) => streamed[symbol] === undefined);
  return symbols;
}

/**
 * A row's quotes in priority order: the newer one in market hours, the stream while connected,
 * and the poll while the socket is down (the store stops updating then).
 */
export function rowQuotes(
  streamed: Quote | undefined,
  polled: Quote | undefined,
  { connected, aging }: StreamState,
): QuotePair {
  if (aging) return newestFirst(streamed, polled);
  return connected ? [streamed, polled] : [polled, streamed];
}

/** A server row with live values laid over it, field by field: `first`, then `second`. */
export function withLiveQuote(
  row: ScreenerRow,
  first: Quote | undefined,
  second: Quote | undefined,
): ScreenerRow {
  return {
    ...row,
    price: roundToDecimals(first?.ltp ?? second?.ltp ?? row.price, 2),
    change: roundToDecimals(first?.changePct ?? second?.changePct ?? row.change, 2),
    volume: first?.volume ?? second?.volume ?? row.volume,
  };
}

/** The next flash for a value that changed from `previous` to `value`. */
export function nextFlash(previous: number, value: number, flash?: FlashState): FlashState {
  return {
    direction: value > previous ? "up" : "down",
    phase: flash?.phase === 0 ? 1 : 0,
  };
}

export function getFlashClassName(flash?: FlashState): string | undefined {
  if (!flash) return undefined;
  if (flash.direction === "up")
    return flash.phase === 0 ? "screener-flash-up-a" : "screener-flash-up-b";
  return flash.phase === 0 ? "screener-flash-down-a" : "screener-flash-down-b";
}

/** Overbought RSI reads as a loss, oversold as a profit. */
export function getRsiClassName(rsi: number): string {
  if (rsi > 70) return "text-loss";
  if (rsi < 30) return "text-profit";
  return "";
}

export function getMacdClassName(macd: number): string {
  if (macd > 0) return "text-profit";
  if (macd < 0) return "text-loss";
  return "text-foreground-muted";
}

/** The text of each numeric cell in a row. */
export type RowCells = {
  price: string;
  change: string;
  /** A zero change counts as up. */
  isUp: boolean;
  volume: string;
  marketCap: string;
  rsi: string;
  macd: string;
};

/** A row's cell text; an RSI of zero or below and a MACD of exactly zero show as missing. */
export function formatRowCells(stock: ScreenerRow): RowCells {
  const change = toNumber(stock.change);
  const rsi = toNumber(stock.rsi);
  const macd = toNumber(stock.macd);
  return {
    price: toNumber(stock.price).toFixed(2),
    change: formatSignedPercent(change),
    isUp: change >= 0,
    volume: formatCompactNumber(toNumber(stock.volume)),
    marketCap: formatCrores(toNumber(stock.marketCap)),
    rsi: rsi > 0 ? rsi.toFixed(1) : "--",
    macd: macd === 0 ? "--" : macd.toFixed(2),
  };
}

/** What the table shows when it has no rows. */
export function getEmptyStateMessage(
  indices: readonly ScreenerIndex[],
  error: string | null,
): string {
  if (indices.length === 0) return "No universes available";
  if (error) return "Failed to load screener data";
  return "No results found";
}

export function totalPagesFor(totalResults: number, itemsPerPage: number): number {
  return Math.max(1, Math.ceil(totalResults / itemsPerPage));
}

/** The 1-based first and last row numbers shown on `page`. */
export function pageRange(
  page: number,
  itemsPerPage: number,
  totalResults: number,
): { from: number; to: number } {
  return {
    from: (page - 1) * itemsPerPage + 1,
    to: Math.min(page * itemsPerPage, totalResults),
  };
}
