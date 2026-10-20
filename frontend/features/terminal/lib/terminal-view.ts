import { formatSignedPercent, roundToDecimals } from "@/lib/format";
import type {
  OptionsBoard,
  PositionItem,
  SearchResultItem,
  WatchlistItem,
} from "@/lib/queries/terminal";
import type { Quote } from "@/lib/realtime/quote-store";
import { inferInstrumentType, optionLotSize } from "./order";

export type TerminalView = "options" | "positions";

export const TIMEFRAMES = ["1m", "5m", "15m", "30m", "1H", "D", "W", "M"];

export function getChangeTone(change: number | null | undefined): string {
  if (typeof change !== "number") {
    return "text-foreground-muted";
  }
  return change >= 0 ? "text-profit" : "text-loss";
}

export function getChangeText(change: number | null | undefined): string {
  return typeof change === "number" ? formatSignedPercent(change) : "Unavailable";
}

export function errorText(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

export function pnlTone(value: number): string {
  return value >= 0 ? "text-profit" : "text-loss";
}

export function watchlistInstrumentType(item: SearchResultItem): string {
  if (item.instrument_type) return item.instrument_type;
  return item.type === "PE" || item.type === "CE" ? item.type : "EQ";
}

/** A watchlist row with the streamed LTP and day change, when the quote has them. */
export function withQuote(item: WatchlistItem, quote: Quote | undefined): WatchlistItem {
  if (!quote) return item;
  const ltp = typeof quote.ltp === "number" ? roundToDecimals(quote.ltp, 2) : item.ltp;
  return {
    ...item,
    ltp,
    price: typeof quote.ltp === "number" ? ltp : item.price,
    changePct:
      typeof quote.changePct === "number" ? roundToDecimals(quote.changePct, 2) : item.changePct,
  };
}

/** A position repriced at the streamed LTP: unrealized and net P&L follow the quote. */
export function withQuotePnl(pos: PositionItem, quote: Quote | undefined): PositionItem {
  const ltp = quote?.ltp;
  if (typeof ltp !== "number") return pos;
  const currentPrice = roundToDecimals(ltp, 2);
  const qty = Number(pos.net_qty || 0);
  const unrealized = roundToDecimals((currentPrice - Number(pos.entry_price || 0)) * qty, 2);
  const realized = Number(pos.realized_pnl || 0);
  return {
    ...pos,
    current_price: currentPrice,
    unrealized_pnl: unrealized,
    net_pnl: roundToDecimals(realized + unrealized, 2),
  };
}

export type PositionsSummary = {
  net_pnl_live: number;
  net_pnl_paper: number;
  net_pnl_total: number;
};

export function summarizePositions(positions: PositionItem[]): PositionsSummary {
  const sumNet = (rows: PositionItem[]) => rows.reduce((acc, p) => acc + p.net_pnl, 0);
  const live = sumNet(positions.filter((p) => p.mode !== "PAPER"));
  const paper = sumNet(positions.filter((p) => p.mode === "PAPER"));
  return {
    net_pnl_live: roundToDecimals(live, 2),
    net_pnl_paper: roundToDecimals(paper, 2),
    net_pnl_total: roundToDecimals(live + paper, 2),
  };
}

/** The selected symbol is always listed, even when it is not in the saved watchlist. */
export function listWithSelected(saved: WatchlistItem[], selectedSymbol: string): WatchlistItem[] {
  if (!selectedSymbol || saved.some((item) => item.symbol === selectedSymbol)) return saved;
  return [
    ...saved,
    { symbol: selectedSymbol, name: selectedSymbol, price: null, changePct: null, ltp: null },
  ];
}

/** The `?symbol=` selection, or the first saved row when none is set. */
export function selectedSymbolFor(urlSymbol: string, saved: WatchlistItem[]): string {
  return urlSymbol === "" ? (saved[0]?.symbol ?? "") : urlSymbol;
}

/** The selected row, else the first row, else none. */
export function currentRow(rows: WatchlistItem[], selectedSymbol: string): WatchlistItem | null {
  return rows.find((s) => s.symbol === selectedSymbol) ?? rows.at(0) ?? null;
}

/** The ticket trades the picked option while the option chain is on screen, else the symbol. */
export function tradeSymbolFor(
  view: TerminalView,
  optionSymbol: string,
  current: WatchlistItem | null,
): string {
  const option = view === "options" ? optionSymbol : "";
  return option || (current?.symbol ?? "");
}

/** Every distinct, non-empty symbol on screen. */
export function screenSymbols(groups: readonly (readonly string[])[]): string[] {
  return Array.from(new Set(groups.flat().filter(Boolean)));
}

/** An option picked on the chain, tagged with the chain (underlying and expiry) it was on. */
export interface OptionPick {
  chain: string;
  symbol: string;
}

/** The chain the selectors name; an empty expiry is the backend's nearest one. */
export function optionChainKey(underlying: string, expiry: string): string {
  return `${underlying}|${expiry}`;
}

/**
 * The picked option while it is a contract on the chain shown for the current selectors, else
 * "". A pick from another underlying or expiry, or one no longer listed, is never traded.
 */
export function shownOptionSymbol(
  pick: OptionPick | null,
  chain: string,
  board: Pick<OptionsBoard, "strikes"> | null,
): string {
  if (pick?.chain !== chain || !board) return "";
  const listed = board.strikes.some(
    (row) => row.ce?.symbol === pick.symbol || row.pe?.symbol === pick.symbol,
  );
  return listed ? pick.symbol : "";
}

export interface SearchView {
  /** Enough text is typed to search. */
  active: boolean;
  /** Results for exactly the typed text; empty while that search is pending. */
  results: SearchResultItem[];
  loading: boolean;
}

const NO_RESULTS: SearchResultItem[] = [];

/**
 * What the search box shows. Results answer `searchTerm`, the debounced text, so until it catches
 * up with the typed text and its own answer (not a placeholder) is in, they are withheld and the
 * search reads as pending: a result for an earlier query is never offered.
 */
export function searchView(input: {
  text: string;
  searchTerm: string;
  data: SearchResultItem[] | undefined;
  isPlaceholderData: boolean;
}): SearchView {
  const query = input.text.trim();
  const active = query.length >= 2;
  if (!active) return { active, results: NO_RESULTS, loading: false };
  const current = input.searchTerm === query && !input.isPlaceholderData;
  // Until this query has an answer (fetching, or paused offline) it reads as pending; a refresh
  // of an answered query keeps its results usable.
  if (!current || input.data === undefined) return { active, results: NO_RESULTS, loading: true };
  return { active, results: input.data, loading: false };
}

/** At least 1, as the quantity and lots inputs always were. */
export function atLeastOne(raw: string): number {
  return Math.max(1, Number(raw) || 1);
}

export function zeroIfBlank(raw: string): number {
  return Number(raw) || 0;
}

export interface TicketSizing {
  /** The ticket sizes an option in lots of its underlying. */
  lotsMode: boolean;
  lotSize: number | null;
  lots: { count: number; size: number } | null;
  /** Null when an option's lot size is unknown: the order is refused, not sized by a guess. */
  quantity: number | null;
}

/**
 * The order quantity. Options on the chain view are sized in lots; the lot size comes from the
 * option's own underlying, not the board on screen.
 */
export function ticketSizing(input: {
  view: TerminalView;
  symbol: string;
  orderQty: number;
  optionLots: number;
}): TicketSizing {
  const lotsMode = input.view === "options" && inferInstrumentType(input.symbol) !== "EQ";
  if (!lotsMode) {
    return { lotsMode, lotSize: null, lots: null, quantity: Math.max(1, input.orderQty) };
  }
  const lotSize = optionLotSize(input.symbol);
  if (lotSize === null) return { lotsMode, lotSize, lots: null, quantity: null };
  const lots = { count: Math.max(1, input.optionLots), size: lotSize };
  return { lotsMode, lotSize, lots, quantity: lots.count * lots.size };
}

/** The ticket message for a square-off the backend accepted. */
export function squareMessage(
  row: Pick<PositionItem, "mode" | "symbol">,
  result: { unconfirmed: boolean; message: string },
): string {
  const mode = row.mode || "PAPER";
  return result.unconfirmed
    ? `${mode} square for ${row.symbol} unconfirmed: ${result.message}`
    : `${mode} square submitted for ${row.symbol}`;
}
