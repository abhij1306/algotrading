import { useEffect, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { queryKeys } from "@/lib/queries/keys";
import { quoteStore } from "@/lib/realtime/quote-store";
import { MarketStatus } from "@/lib/market-hours";
import { apiClient } from "@/lib/api-client";
import { MarketHoursInsights } from "./MarketHoursInsights";
import { PostMarketInsights } from "./PostMarketInsights";

interface InsightsPanelProps {
  marketStatus: MarketStatus;
}

function toSafeString(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
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

export interface MarketConditionData {
  status: string;
  adx?: number;
  trend_strength?: string;
  technical_summary?: string;
}

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

async function fetchMarketInsights(): Promise<MarketInsights> {
  const [moversRes, sectorsRes] = await Promise.all([
    apiClient.get("/api/market/top-movers?index=NIFTY50&limit=5"),
    apiClient.get("/api/market/sector-performance"),
  ]);
  const moversData = (moversRes.data ?? {}) as Record<string, unknown>;

  const gainers = Array.isArray(moversData.gainers)
    ? (moversData.gainers as Array<Record<string, unknown>>).map((row) => ({
        symbol: toSafeString(row.symbol),
        name: toSafeString(row.name, toSafeString(row.symbol)),
        price: typeof row.price === "number" ? row.price : 0,
        changePercent: typeof row.changePercent === "number" ? row.changePercent : 0,
      }))
    : [];
  const losers = Array.isArray(moversData.losers)
    ? (moversData.losers as Array<Record<string, unknown>>).map((row) => ({
        symbol: toSafeString(row.symbol),
        name: toSafeString(row.name, toSafeString(row.symbol)),
        price: typeof row.price === "number" ? row.price : 0,
        changePercent: typeof row.changePercent === "number" ? row.changePercent : 0,
      }))
    : [];
  const sectors = Array.isArray(sectorsRes.data)
    ? (sectorsRes.data as Array<Record<string, unknown>>).map((row) => ({
        name: toSafeString(row.name),
        symbol: toSafeString(row.symbol, toSafeString(row.name)),
        value: typeof row.value === "number" ? row.value : 0,
        changePercent: typeof row.changePercent === "number" ? row.changePercent : 0,
      }))
    : [];

  return { topGainers: gainers, topLosers: losers, sectorPerformance: sectors };
}

async function fetchPostMarketData(): Promise<PostMarketData> {
  const [overviewRes, currencyRes] = await Promise.all([
    apiClient.get("/api/market/overview"),
    apiClient.get("/api/market/currency/USDINR"),
  ]);

  const overviewData = (overviewRes.data ?? {}) as Record<string, unknown>;
  const indicesRaw = Array.isArray(overviewData.indices) ? overviewData.indices : [];
  const sentimentRaw = (overviewData.sentiment ?? {}) as Record<string, unknown>;
  const conditionRaw = (overviewData.condition ?? null) as MarketConditionData | null;

  const parsedIndices = indicesRaw.map((idx: Record<string, unknown>) => ({
    name: toSafeString(idx.name),
    symbol: toSafeString(idx.symbol),
    price: typeof idx.price === "number" ? idx.price : null,
    changePct: typeof idx.change_pct === "number" ? idx.change_pct : null,
  }));

  const usIndices: IndexData[] = parsedIndices
    .filter((idx) => ["S&P 500", "Nasdaq", "Dow Jones"].includes(idx.name))
    .map((idx) => ({ name: idx.name, value: idx.price, changePercent: idx.changePct }));

  const vix: IndexData[] = parsedIndices
    .filter((idx) => idx.name.includes("VIX"))
    .map((idx) => ({ name: idx.name, value: idx.price, changePercent: idx.changePct }));

  const commodities: CommodityData[] = parsedIndices
    .filter((idx) => ["Gold", "Silver", "Crude Oil"].includes(idx.name))
    .map((idx) => ({
      name: idx.name,
      symbol: idx.symbol,
      price: idx.price,
      changePercent: idx.changePct,
    }));

  const usFearGreedRaw = (sentimentRaw.us_fear_greed ?? {}) as Record<string, unknown>;
  const indiaMMIRaw = (sentimentRaw.india_sentiment ?? {}) as Record<string, unknown>;
  const currencyRaw = (currencyRes.data ?? {}) as Record<string, unknown>;

  const currency: CurrencyData = {
    pair: "USDINR",
    rate: typeof currencyRaw.rate === "number" ? currencyRaw.rate : null,
    changePercent: typeof currencyRaw.changePercent === "number" ? currencyRaw.changePercent : null,
  };

  return {
    usIndices,
    vix,
    commodities,
    currency,
    sentiment: {
      usFearGreed: {
        score: typeof usFearGreedRaw.score === "number" ? usFearGreedRaw.score : null,
        status: toSafeString(usFearGreedRaw.status, "Unavailable"),
        source: typeof usFearGreedRaw.source === "string" ? usFearGreedRaw.source : undefined,
      },
      indiaMMI: {
        score: typeof indiaMMIRaw.score === "number" ? indiaMMIRaw.score : null,
        status: toSafeString(indiaMMIRaw.status, "Unavailable"),
        source: typeof indiaMMIRaw.source === "string" ? indiaMMIRaw.source : undefined,
      },
    },
    condition: conditionRaw,
    timestamp: typeof overviewData.timestamp === "string" ? overviewData.timestamp : null,
  };
}

/** Live top movers and sectors; refreshes every 60 seconds while `enabled` (market hours). */
export function useMarketInsights(enabled: boolean) {
  return useQuery({
    queryKey: queryKeys.dashboard.marketInsights(),
    queryFn: fetchMarketInsights,
    enabled,
    refetchInterval: 60_000,
    refetchIntervalInBackground: true,
  });
}

/** Symbols shown by the market insights, for websocket subscription. */
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

type InsightTick = { symbol?: string; ltp?: number; change_pct?: number };

function applyInsightTick(prev: MarketInsights, tick: InsightTick): MarketInsights {
  // Check if this symbol is in our lists before updating
  const hasGainer = prev.topGainers.some((g) => g.symbol === tick.symbol);
  const hasLoser = prev.topLosers.some((l) => l.symbol === tick.symbol);

  if (!hasGainer && !hasLoser) return prev;

  return {
    ...prev,
    topGainers: prev.topGainers.map((g) =>
      g.symbol === tick.symbol
        ? { ...g, price: tick.ltp ?? g.price, changePercent: tick.change_pct ?? g.changePercent }
        : g,
    ),
    topLosers: prev.topLosers.map((l) =>
      l.symbol === tick.symbol
        ? { ...l, price: tick.ltp ?? l.price, changePercent: tick.change_pct ?? l.changePercent }
        : l,
    ),
    sectorPerformance: prev.sectorPerformance.map((sector) =>
      sector.symbol === tick.symbol
        ? {
            ...sector,
            value: tick.ltp ?? sector.value,
            changePercent: tick.change_pct ?? sector.changePercent,
          }
        : sector,
    ),
  };
}

export function InsightsPanel({ marketStatus }: Readonly<InsightsPanelProps>) {
  const queryClient = useQueryClient();
  const marketQuery = useMarketInsights(marketStatus.isOpen);
  const postMarketQuery = useQuery({
    queryKey: queryKeys.dashboard.postMarketInsights(),
    queryFn: fetchPostMarketData,
    enabled: !marketStatus.isOpen,
  });
  const marketInsights = marketStatus.isOpen ? (marketQuery.data ?? null) : null;
  const postMarketData = marketStatus.isOpen ? null : (postMarketQuery.data ?? null);
  const loading = marketStatus.isOpen ? marketQuery.isPending : postMarketQuery.isPending;
  let error: string | null = null;
  if (marketStatus.isOpen && marketQuery.isError) error = "Failed to load market insights";
  if (!marketStatus.isOpen && postMarketQuery.isError) error = "Failed to load post-market data";

  // Apply live WebSocket ticks to the top movers during market hours.
  useEffect(() => {
    if (!marketStatus.isOpen) return;
    return quoteStore.onMessage((message) => {
      if (message.type !== "ticker" || !message.data) return;
      const tick = message.data as InsightTick;
      if (!tick.symbol) return;
      queryClient.setQueryData<MarketInsights>(queryKeys.dashboard.marketInsights(), (prev) =>
        prev ? applyInsightTick(prev, tick) : prev,
      );
    });
  }, [marketStatus.isOpen, queryClient]);

  if (loading && !marketInsights && !postMarketData) {
    return (
      <div className="p-4">
        <div className="space-y-2">
          {[1, 2, 3].map((i) => (
            <div key={i} className="h-16 bg-background-tertiary/30 rounded animate-pulse" />
          ))}
        </div>
      </div>
    );
  }

  if (error) {
    return <div className="p-4 text-center text-sm text-loss">{error}</div>;
  }

  let insights: ReactNode = null;
  if (marketStatus.isOpen && marketInsights) {
    insights = <MarketHoursInsights data={marketInsights} />;
  } else if (postMarketData) {
    insights = <PostMarketInsights data={postMarketData} />;
  }

  return <div className="transition-opacity duration-300">{insights}</div>;
}
