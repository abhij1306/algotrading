import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { z } from "zod";
import { api, parseResponse, unwrap } from "@/lib/api/client";
import { roundToDecimals } from "@/lib/format";
import type { MarketStatus } from "@/lib/market-hours";
import { formatIstTime, type DateInput } from "@/lib/time";
import { queryKeys } from "./keys";

/** Dashboard panels refresh every minute; TanStack Query pauses the polls while the tab is hidden. */
const DASHBOARD_REFRESH_MS = 60_000;
const WATCHLIST_ROWS = 10;
const US_INDEX_NAMES = new Set(["S&P 500", "Nasdaq", "Dow Jones"]);
const COMMODITY_NAMES = new Set(["Gold", "Silver"]);

const optionalNumber = z.number().nullish().catch(null);
const optionalString = z.string().nullish().catch(null);

const marketStatusSchema = z.object({
  is_open: z.boolean().catch(false),
  message: optionalString,
  current_time_ist: optionalString,
});

const portfolioStatsSchema = z.object({
  totalValue: z.number().catch(0),
  dayChange: z.number().catch(0),
  dayChangePercent: z.number().catch(0),
  totalReturn: z.number().catch(0),
  totalReturnPercent: z.number().catch(0),
});

const watchlistRowSchema = z.object({
  symbol: z.string().catch(""),
  price: optionalNumber,
  // `change` is the absolute price difference; it must never stand in for the percent.
  change: optionalNumber,
  changePercent: optionalNumber,
});

const liveIndexSchema = z.object({
  name: z.string().catch(""),
  symbol: z.string().catch(""),
  value: z.number().catch(0),
  change: z.number().catch(0),
  changePercent: z.number().catch(0),
});

const overviewIndexSchema = z.object({
  name: z.string().catch(""),
  symbol: z.string().catch(""),
  price: optionalNumber,
  change: optionalNumber,
  change_pct: optionalNumber,
  changePercent: optionalNumber,
});

const sentimentSchema = z
  .object({
    score: optionalNumber,
    status: z.string().catch("Unavailable"),
    source: z.string().optional().catch(undefined),
  })
  .catch({ score: null, status: "Unavailable" });

const conditionSchema = z
  .object({
    status: z.string(),
    adx: z.number().optional().catch(undefined),
    trend_strength: z.string().optional().catch(undefined),
    technical_summary: z.string().optional().catch(undefined),
  })
  .nullable()
  .catch(null);

const overviewSchema = z.object({
  indices: z.array(overviewIndexSchema).catch([]),
  sentiment: z.object({ us_fear_greed: sentimentSchema, india_sentiment: sentimentSchema }).catch({
    us_fear_greed: { score: null, status: "Unavailable" },
    india_sentiment: { score: null, status: "Unavailable" },
  }),
  condition: conditionSchema,
  timestamp: optionalString,
});

const commoditySchema = z.object({
  name: z.string().catch(""),
  symbol: z.string().catch(""),
  price: optionalNumber,
  changePercent: optionalNumber,
});

const currencySchema = z.object({ rate: optionalNumber, changePercent: optionalNumber });

const moverSchema = z.object({
  symbol: z.string().catch(""),
  name: z.string().nullish().catch(null),
  price: z.number().catch(0),
  changePercent: z.number().catch(0),
});

const topMoversSchema = z.object({
  gainers: z.array(moverSchema).catch([]),
  losers: z.array(moverSchema).catch([]),
});

const sectorSchema = z.object({
  name: z.string().catch(""),
  symbol: z.string().nullish().catch(null),
  value: z.number().catch(0),
  changePercent: z.number().catch(0),
});

export type PortfolioStats = z.output<typeof portfolioStatsSchema>;

export interface MarketIndex {
  name: string;
  symbol: string;
  value: number;
  change: number;
  changePercent: number;
  type: "indian" | "global";
  source: "websocket" | "yahoo" | "api";
}

export interface DashboardWatchlistItem {
  symbol: string;
  price: number | null;
  change: number | null;
  changePercent: number | null;
}

export type DashboardMode = "market" | "post_market";

export interface MarketIndicesSnapshot {
  indicesData: MarketIndex[];
  nextMode: DashboardMode;
}

export interface MarketStatusSnapshot {
  status: MarketStatus;
  istTime: string;
}

export interface StockMover {
  symbol: string;
  name: string;
  price: number;
  changePercent: number;
}

export interface SectorData {
  name: string;
  symbol: string;
  value: number;
  changePercent: number;
}

export interface MarketInsights {
  topGainers: StockMover[];
  topLosers: StockMover[];
  sectorPerformance: SectorData[];
}

export interface IndexData {
  name: string;
  value: number | null;
  changePercent: number | null;
}

export interface CommodityData {
  name: string;
  symbol: string;
  price: number | null;
  changePercent: number | null;
}

export interface CurrencyData {
  pair: string;
  rate: number | null;
  changePercent: number | null;
}

export interface SentimentData {
  score: number | null;
  status: string;
  source?: string;
}

export type MarketConditionData = NonNullable<z.output<typeof conditionSchema>>;

export interface PostMarketData {
  usIndices: IndexData[];
  vix: IndexData[];
  commodities: CommodityData[];
  currency: CurrencyData;
  sentiment: {
    usFearGreed: SentimentData;
    indiaMMI: SentimentData;
  };
  condition: MarketConditionData | null;
  timestamp: string | null;
}

/** The IST wall clock as the backend's market status prints it: "14:05:09 IST". */
export function formatIstClock(now: DateInput = Date.now()): string {
  return `${formatIstTime(now)} IST`;
}

function roundOrNull(value: number | null | undefined): number | null {
  return typeof value === "number" ? roundToDecimals(value, 2) : null;
}

function toLiveIndex(row: z.output<typeof liveIndexSchema>): MarketIndex {
  return {
    name: row.name,
    symbol: row.symbol || row.name,
    value: roundToDecimals(row.value, 2),
    change: roundToDecimals(row.change, 2),
    changePercent: roundToDecimals(row.changePercent, 2),
    type: "indian",
    source: "api",
  };
}

/** Yahoo overview rows with a symbol and a price; `change_pct` wins over `changePercent`. */
function toGlobalIndices(rows: z.output<typeof overviewIndexSchema>[]): MarketIndex[] {
  const indices: MarketIndex[] = [];
  for (const row of rows) {
    const price = roundOrNull(row.price);
    if (!row.symbol || price === null) continue;
    indices.push({
      name: row.name || row.symbol,
      symbol: row.symbol,
      value: price,
      change: roundOrNull(row.change) ?? 0,
      changePercent: roundOrNull(row.change_pct ?? row.changePercent) ?? 0,
      type: "global",
      source: "yahoo",
    });
  }
  return indices;
}

function toSentiment(raw: z.output<typeof sentimentSchema>): SentimentData {
  return { score: raw.score ?? null, status: raw.status, source: raw.source };
}

function toIndexData(row: z.output<typeof overviewIndexSchema>): IndexData {
  return { name: row.name, value: row.price ?? null, changePercent: row.change_pct ?? null };
}

/** Never fails: a missing status reads as "closed, status unavailable". */
async function fetchMarketStatus(): Promise<MarketStatusSnapshot> {
  try {
    const body = parseResponse(marketStatusSchema, await unwrap(api.GET("/api/market/status")));
    return {
      status: { isOpen: body.is_open, message: body.message || "Market status unavailable" },
      istTime: body.current_time_ist || formatIstClock(),
    };
  } catch {
    return {
      status: { isOpen: false, message: "Market status unavailable" },
      istTime: formatIstClock(),
    };
  }
}

const POLL_OPTIONS = { refetchInterval: DASHBOARD_REFRESH_MS } as const;

export function useMarketStatus() {
  return useQuery({
    queryKey: queryKeys.dashboard.marketStatus(),
    queryFn: fetchMarketStatus,
    ...POLL_OPTIONS,
  });
}

/** Fails on a backend error so the last good stats stay on screen. */
export function usePortfolioStats() {
  return useQuery({
    queryKey: queryKeys.dashboard.portfolioStats(),
    queryFn: async () =>
      parseResponse(portfolioStatsSchema, await unwrap(api.GET("/api/portfolio/stats"))),
    ...POLL_OPTIONS,
  });
}

/** The first ten watchlist rows that carry a symbol. */
export function useDashboardWatchlist() {
  return useQuery({
    queryKey: queryKeys.dashboard.watchlist(),
    queryFn: async (): Promise<DashboardWatchlistItem[]> =>
      parseResponse(z.array(watchlistRowSchema), await unwrap(api.GET("/api/market/watchlist")))
        .slice(0, WATCHLIST_ROWS)
        .map((row) => ({
          symbol: row.symbol,
          price: roundOrNull(row.price),
          change: roundOrNull(row.change),
          changePercent: roundOrNull(row.changePercent),
        }))
        .filter((row) => row.symbol.length > 0),
    ...POLL_OPTIONS,
  });
}

/** NSE indices while the market is open, the Yahoo global overview otherwise. */
export function useMarketIndices(params: { marketOpen: boolean; enabled: boolean }) {
  const { marketOpen, enabled } = params;
  return useQuery({
    queryKey: queryKeys.dashboard.indices(marketOpen),
    queryFn: async (): Promise<MarketIndicesSnapshot> => {
      if (marketOpen) {
        const rows = parseResponse(
          z.array(liveIndexSchema),
          await unwrap(api.GET("/api/market/indices")),
        );
        return { indicesData: rows.map(toLiveIndex), nextMode: "market" };
      }
      const overview = parseResponse(overviewSchema, await unwrap(api.GET("/api/market/overview")));
      return { indicesData: toGlobalIndices(overview.indices), nextMode: "post_market" };
    },
    enabled,
    placeholderData: keepPreviousData,
    ...POLL_OPTIONS,
  });
}

function toMover(row: z.output<typeof moverSchema>): StockMover {
  return {
    symbol: row.symbol,
    name: row.name ?? row.symbol,
    price: row.price,
    changePercent: row.changePercent,
  };
}

/** Live top movers and sectors; refreshes every minute while `enabled` (market hours). */
export function useMarketInsights(enabled: boolean) {
  return useQuery({
    queryKey: queryKeys.dashboard.marketInsights(),
    queryFn: async (): Promise<MarketInsights> => {
      const [movers, sectors] = await Promise.all([
        unwrap(
          api.GET("/api/market/top-movers", { params: { query: { index: "NIFTY50", limit: 5 } } }),
        ).then((body) => parseResponse(topMoversSchema, body)),
        unwrap(api.GET("/api/market/sector-performance")).then((body) =>
          parseResponse(z.array(sectorSchema), body),
        ),
      ]);
      return {
        topGainers: movers.gainers.map(toMover),
        topLosers: movers.losers.map(toMover),
        sectorPerformance: sectors.map((row) => ({
          name: row.name,
          symbol: row.symbol ?? row.name,
          value: row.value,
          changePercent: row.changePercent,
        })),
      };
    },
    enabled,
    ...POLL_OPTIONS,
  });
}

/** US indices, VIX, commodities, USDINR and sentiment for after the close. */
export function usePostMarketInsights(enabled: boolean) {
  return useQuery({
    queryKey: queryKeys.dashboard.postMarketInsights(),
    queryFn: async (): Promise<PostMarketData> => {
      const [overview, currency] = await Promise.all([
        unwrap(api.GET("/api/market/overview")).then((body) => parseResponse(overviewSchema, body)),
        unwrap(
          api.GET("/api/market/currency/{pair}", { params: { path: { pair: "USDINR" } } }),
        ).then((body) => parseResponse(currencySchema, body)),
      ]);
      return {
        usIndices: overview.indices.filter((row) => US_INDEX_NAMES.has(row.name)).map(toIndexData),
        vix: overview.indices.filter((row) => row.name.includes("VIX")).map(toIndexData),
        commodities: overview.indices
          .filter((row) => COMMODITY_NAMES.has(row.name))
          .map((row) => ({
            name: row.name,
            symbol: row.symbol,
            price: row.price ?? null,
            changePercent: row.change_pct ?? null,
          })),
        currency: {
          pair: "USDINR",
          rate: currency.rate ?? null,
          changePercent: currency.changePercent ?? null,
        },
        sentiment: {
          usFearGreed: toSentiment(overview.sentiment.us_fear_greed),
          indiaMMI: toSentiment(overview.sentiment.india_sentiment),
        },
        condition: overview.condition,
        timestamp: overview.timestamp ?? null,
      };
    },
    enabled,
    ...POLL_OPTIONS,
  });
}

/**
 * Yahoo futures quotes (Gold, Crude Oil) from /api/market/commodities. The overview carries Gold
 * and Silver but not Crude Oil, so the post-market panel adds the rows the overview lacks.
 */
export function useCommodities(enabled: boolean) {
  return useQuery({
    queryKey: queryKeys.dashboard.commodities(),
    queryFn: async (): Promise<CommodityData[]> =>
      parseResponse(z.array(commoditySchema), await unwrap(api.GET("/api/market/commodities")))
        .filter((row) => row.name.length > 0)
        .map((row) => ({
          name: row.name,
          symbol: row.symbol,
          price: row.price ?? null,
          changePercent: row.changePercent ?? null,
        })),
    enabled,
    ...POLL_OPTIONS,
  });
}

/** The overview's commodity rows, then each extra row whose name the overview does not have. */
export function mergeCommodities(
  overviewRows: readonly CommodityData[],
  extraRows: readonly CommodityData[],
): CommodityData[] {
  const known = new Set(overviewRows.map((row) => row.name));
  return [...overviewRows, ...extraRows.filter((row) => !known.has(row.name))];
}

/** Symbols shown by the market insights, for live quote subscription. */
export function getInsightSymbols(data: MarketInsights): string[] {
  return Array.from(
    new Set(
      [
        ...data.topGainers.map((row) => row.symbol),
        ...data.topLosers.map((row) => row.symbol),
        ...data.sectorPerformance.map((row) => row.symbol),
      ].filter(Boolean),
    ),
  ).sort((a, b) => a.localeCompare(b));
}
