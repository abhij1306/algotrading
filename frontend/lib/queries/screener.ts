import { keepPreviousData, queryOptions, useQuery } from "@tanstack/react-query";
import { z } from "zod";
import { api, parseResponse, unwrap } from "@/lib/api/client";
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

/** One-shot live quote fetch (used as a polling fallback when the websocket is stale). */
export async function fetchLiveQuotes(symbols: string[]): Promise<LiveQuotes> {
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
