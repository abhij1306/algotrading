import { keepPreviousData, queryOptions, useQuery } from "@tanstack/react-query";
import { z } from "zod";
import { api, parseResponse, unwrap } from "@/lib/api/client";
import { isMarketOpen } from "@/lib/market-hours";
import type { Quote, QuoteMap } from "@/lib/realtime/quote-store";
import { queryKeys } from "./keys";

const indicesSchema = z.object({
  indices: z.array(z.object({ id: z.string(), name: z.string(), count: z.number() })),
});

const screenerRowSchema = z.object({
  symbol: z.string(),
  name: z.string().catch(""),
  sector: z.string().catch(""),
  marketCap: z.number().catch(0),
  change: z.number().catch(0),
  price: z.number().catch(0),
  volume: z.number().catch(0),
  rsi: z.number().catch(0),
  macd: z.number().catch(0),
  adx: z.number().catch(0),
});

const screenerResultsSchema = z.object({
  results: z.array(screenerRowSchema).default([]),
  total: z.number().optional(),
});

const liveQuotesSchema = z.record(z.string(), z.record(z.string(), z.unknown()));

export type ScreenerIndex = z.output<typeof indicesSchema>["indices"][number];
export type ScreenerRow = z.output<typeof screenerRowSchema>;
export type ScreenerResultsPage = { rows: ScreenerRow[]; total: number };
export type LiveQuotes = z.output<typeof liveQuotesSchema>;

export type ScreenerParams = {
  universe: string;
  page: number;
  limit: number;
  sortBy: string;
  sortOrder: "asc" | "desc";
  query: string;
};

export const screenerIndicesQuery = () =>
  queryOptions({
    queryKey: queryKeys.screener.indices(),
    queryFn: async () =>
      parseResponse(indicesSchema, await unwrap(api.GET("/api/screener/indices"))).indices,
    staleTime: 5 * 60_000,
  });

export const screenerResultsQuery = (params: ScreenerParams) =>
  queryOptions({
    queryKey: queryKeys.screener.results(params),
    queryFn: async (): Promise<ScreenerResultsPage> => {
      const body = parseResponse(
        screenerResultsSchema,
        await unwrap(
          api.GET("/api/screener/results", {
            params: {
              query: {
                universe: params.universe,
                page: params.page,
                limit: params.limit,
                sort_by: params.sortBy,
                sort_order: params.sortOrder,
                query: params.query || undefined,
              },
            },
          }),
        ),
      );
      return { rows: body.results, total: body.total ?? body.results.length };
    },
  });

export function useScreenerIndices() {
  return useQuery(screenerIndicesQuery());
}

/** Disabled until a universe is resolved; keeps the previous page visible while paging/sorting. */
export function useScreenerResults(params: ScreenerParams) {
  return useQuery({
    ...screenerResultsQuery(params),
    enabled: params.universe !== "",
    placeholderData: keepPreviousData,
  });
}

/** One live quote fetch from the REST endpoint. */
export async function fetchLiveQuotes(symbols: readonly string[]): Promise<LiveQuotes> {
  return parseResponse(
    liveQuotesSchema,
    await unwrap(
      api.GET("/api/market/quotes/live", { params: { query: { symbols: symbols.join(",") } } }),
    ),
  );
}

/**
 * Day change in percent from one `/api/market/quotes/live` entry. The entry's `change` is the
 * absolute price difference, so it never stands in for the percent: no `change_pct`, no percent.
 * Database fallback entries (`source: "database"`) carry a placeholder `change_pct: 0`, not a
 * computed change, so they give no percent either and the row keeps its own.
 */
export function liveQuoteChangePercent(quote: LiveQuotes[string]): number | undefined {
  if (quote.source === "database") return undefined;
  return typeof quote.change_pct === "number" ? quote.change_pct : undefined;
}

const LIVE_POLL_OPEN_MS = 3000;
const LIVE_POLL_CLOSED_MS = 60_000;
/** The REST fallback is skipped above this many symbols. */
export const MAX_LIVE_POLL_SYMBOLS = 100;
const NO_SYMBOLS: readonly string[] = [];

function numberOrUndefined(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

/**
 * REST quotes in the quote store's shape, stamped with when they arrived; entries with no price,
 * percent or volume are dropped.
 */
export function toQuoteMap(quotes: LiveQuotes, receivedAt: number): QuoteMap {
  const map: Record<string, Quote> = {};
  for (const [symbol, entry] of Object.entries(quotes)) {
    const quote: Quote = {
      ltp: numberOrUndefined(entry.ltp),
      changePct: liveQuoteChangePercent(entry),
      volume: numberOrUndefined(entry.volume),
      receivedAt,
    };
    if (quote.ltp === undefined && quote.changePct === undefined && quote.volume === undefined) {
      continue;
    }
    map[symbol] = quote;
  }
  return map;
}

/**
 * REST polling fallback for symbols the websocket is not streaming (or streams stale quotes for):
 * every 3s while the market is
 * open, every minute otherwise, paused while the tab is hidden. Polls nothing for an empty list
 * or more than `MAX_LIVE_POLL_SYMBOLS` symbols.
 */
export function useLiveQuotePoll(symbols: readonly string[]) {
  const enabled = symbols.length > 0 && symbols.length <= MAX_LIVE_POLL_SYMBOLS;
  const sorted = enabled ? [...symbols].sort((a, b) => a.localeCompare(b)) : NO_SYMBOLS;
  return useQuery({
    queryKey: queryKeys.screener.liveQuotes(sorted),
    queryFn: async () => toQuoteMap(await fetchLiveQuotes(sorted), Date.now()),
    enabled,
    staleTime: 0,
    refetchInterval: () => (isMarketOpen().isOpen ? LIVE_POLL_OPEN_MS : LIVE_POLL_CLOSED_MS),
    placeholderData: keepPreviousData,
  });
}
