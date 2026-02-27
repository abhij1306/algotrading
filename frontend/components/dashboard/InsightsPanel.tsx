'use client';

import { useState, useEffect, useCallback, useRef } from 'react';
import { MarketStatus } from '@/lib/market-hours';
import { apiClient } from '@/lib/api-client';
import { MarketHoursInsights } from './MarketHoursInsights';
import { PostMarketInsights } from './PostMarketInsights';

interface InsightsPanelProps {
  marketStatus: MarketStatus;
  lastMessage?: { type?: string; data?: unknown } | null;
  onSymbolsChange?: (symbols: string[]) => void;
}

function toSafeString(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback;
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

export function InsightsPanel({ marketStatus, lastMessage, onSymbolsChange }: Readonly<InsightsPanelProps>) {
  const [marketInsights, setMarketInsights] = useState<MarketInsights | null>(null);
  const [postMarketData, setPostMarketData] = useState<PostMarketData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const marketFetchInFlightRef = useRef(false);
  const postFetchInFlightRef = useRef(false);

  const fetchMarketInsights = useCallback(async () => {
    if (marketFetchInFlightRef.current) return;
    marketFetchInFlightRef.current = true;
    try {
      setLoading(true);
      setError(null);

      const [moversRes, sectorsRes] = await Promise.all([
        apiClient.get('/api/market/top-movers?index=NIFTY50&limit=5'),
        apiClient.get('/api/market/sector-performance')
      ]);
      const moversData = (moversRes.data ?? {}) as Record<string, unknown>;

      const gainers = Array.isArray(moversData.gainers)
        ? (moversData.gainers as Array<Record<string, unknown>>).map((row) => ({
            symbol: toSafeString(row.symbol),
            name: toSafeString(row.name, toSafeString(row.symbol)),
            price: typeof row.price === 'number' ? row.price : 0,
            changePercent: typeof row.changePercent === 'number' ? row.changePercent : 0,
          }))
        : [];
      const losers = Array.isArray(moversData.losers)
        ? (moversData.losers as Array<Record<string, unknown>>).map((row) => ({
            symbol: toSafeString(row.symbol),
            name: toSafeString(row.name, toSafeString(row.symbol)),
            price: typeof row.price === 'number' ? row.price : 0,
            changePercent: typeof row.changePercent === 'number' ? row.changePercent : 0,
          }))
        : [];
      const sectors = Array.isArray(sectorsRes.data)
        ? (sectorsRes.data as Array<Record<string, unknown>>).map((row) => ({
            name: toSafeString(row.name),
            symbol: toSafeString(row.symbol, toSafeString(row.name)),
            value: typeof row.value === 'number' ? row.value : 0,
            changePercent: typeof row.changePercent === 'number' ? row.changePercent : 0,
          }))
        : [];

      setMarketInsights({
        topGainers: gainers,
        topLosers: losers,
        sectorPerformance: sectors
      });
      if (onSymbolsChange) {
        const symbols = Array.from(new Set([
          ...gainers.map((row) => row.symbol),
          ...losers.map((row) => row.symbol),
          ...sectors.map((row) => row.symbol),
        ].filter(Boolean))).sort((a, b) => a.localeCompare(b));
        onSymbolsChange(symbols);
      }
    } catch {
      setError('Failed to load market insights');
      onSymbolsChange?.([]);
    } finally {
      marketFetchInFlightRef.current = false;
      setLoading(false);
    }
  }, [onSymbolsChange]);

  const fetchPostMarketData = useCallback(async () => {
    if (postFetchInFlightRef.current) return;
    postFetchInFlightRef.current = true;
    try {
      setLoading(true);
      setError(null);

      const [overviewRes, currencyRes] = await Promise.all([
        apiClient.get('/api/market/overview'),
        apiClient.get('/api/market/currency/USDINR')
      ]);

      const overviewData = (overviewRes.data ?? {}) as Record<string, unknown>;
      const indicesRaw = Array.isArray(overviewData.indices) ? overviewData.indices : [];
      const sentimentRaw = (overviewData.sentiment ?? {}) as Record<string, unknown>;
      const conditionRaw = (overviewData.condition ?? null) as MarketConditionData | null;

      const parsedIndices = indicesRaw.map((idx: Record<string, unknown>) => ({
        name: toSafeString(idx.name),
        symbol: toSafeString(idx.symbol),
        price: typeof idx.price === 'number' ? idx.price : null,
        changePct: typeof idx.change_pct === 'number' ? idx.change_pct : null,
      }));

      const usIndices: IndexData[] = parsedIndices
        .filter((idx) => ['S&P 500', 'Nasdaq', 'Dow Jones'].includes(idx.name))
        .map((idx) => ({ name: idx.name, value: idx.price, changePercent: idx.changePct }));

      const vix: IndexData[] = parsedIndices
        .filter((idx) => idx.name.includes('VIX'))
        .map((idx) => ({ name: idx.name, value: idx.price, changePercent: idx.changePct }));

      const commodities: CommodityData[] = parsedIndices
        .filter((idx) => ['Gold', 'Silver', 'Crude Oil'].includes(idx.name))
        .map((idx) => ({ name: idx.name, symbol: idx.symbol, price: idx.price, changePercent: idx.changePct }));

      const usFearGreedRaw = (sentimentRaw.us_fear_greed ?? {}) as Record<string, unknown>;
      const indiaMMIRaw = (sentimentRaw.india_sentiment ?? {}) as Record<string, unknown>;
      const currencyRaw = (currencyRes.data ?? {}) as Record<string, unknown>;

      const currency: CurrencyData = {
        pair: 'USDINR',
        rate: typeof currencyRaw.rate === 'number' ? currencyRaw.rate : null,
        changePercent: typeof currencyRaw.changePercent === 'number' ? currencyRaw.changePercent : null,
      };

      setPostMarketData({
        usIndices,
        vix,
        commodities,
        currency,
        sentiment: {
          usFearGreed: {
            score: typeof usFearGreedRaw.score === 'number' ? usFearGreedRaw.score : null,
            status: toSafeString(usFearGreedRaw.status, 'Unavailable'),
            source: typeof usFearGreedRaw.source === 'string' ? usFearGreedRaw.source : undefined,
          },
          indiaMMI: {
            score: typeof indiaMMIRaw.score === 'number' ? indiaMMIRaw.score : null,
            status: toSafeString(indiaMMIRaw.status, 'Unavailable'),
            source: typeof indiaMMIRaw.source === 'string' ? indiaMMIRaw.source : undefined,
          },
        },
        condition: conditionRaw,
        timestamp: typeof overviewData.timestamp === 'string' ? overviewData.timestamp : null,
      });
    } catch (err) {
      console.error('[InsightsPanel] Error fetching post-market data:', err);
      setError('Failed to load post-market data');
    } finally {
      postFetchInFlightRef.current = false;
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let intervalId: ReturnType<typeof setInterval> | null = null;
    if (marketStatus.isOpen) {
      setPostMarketData(null);
      void fetchMarketInsights();
      // Auto-refresh every 60 seconds during market hours
      intervalId = setInterval(() => {
        void fetchMarketInsights();
      }, 60000);
    } else {
      setMarketInsights(null);
      onSymbolsChange?.([]);
      void fetchPostMarketData();
    }

    return () => {
      if (intervalId) clearInterval(intervalId);
    };
  }, [marketStatus.isOpen, fetchMarketInsights, fetchPostMarketData, onSymbolsChange]);

  // Handle live WebSocket updates for top movers
  useEffect(() => {
    if (!marketStatus.isOpen) return;
    if (lastMessage?.type === 'ticker' && lastMessage.data) {
      const tick = lastMessage.data as { symbol?: string; ltp?: number; change_pct?: number };
      if (tick.symbol) {
        setMarketInsights(prev => {
          if (!prev) return prev;

          // Check if this symbol is in our lists before updating
          const hasGainer = prev.topGainers.some(g => g.symbol === tick.symbol);
          const hasLoser = prev.topLosers.some(l => l.symbol === tick.symbol);

          if (!hasGainer && !hasLoser) return prev;

          return {
            ...prev,
            topGainers: prev.topGainers.map(g =>
              g.symbol === tick.symbol
                ? { ...g, price: tick.ltp ?? g.price, changePercent: tick.change_pct ?? g.changePercent }
                : g
            ),
            topLosers: prev.topLosers.map(l =>
              l.symbol === tick.symbol
                ? { ...l, price: tick.ltp ?? l.price, changePercent: tick.change_pct ?? l.changePercent }
                : l
            ),
            sectorPerformance: prev.sectorPerformance.map(sector =>
              sector.symbol === tick.symbol
                ? { ...sector, value: tick.ltp ?? sector.value, changePercent: tick.change_pct ?? sector.changePercent }
                : sector
            ),
          };
        });
      }
    }
  }, [lastMessage, marketStatus.isOpen]);

  if (loading && !marketInsights && !postMarketData) {
    return (
      <div className="p-4">
        <div className="space-y-2">
          {[1, 2, 3].map(i => (
            <div key={i} className="h-16 bg-background-tertiary/30 rounded animate-pulse" />
          ))}
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-4 text-center text-sm text-loss">
        {error}
      </div>
    );
  }

  return (
    <div className="transition-opacity duration-300">
      {marketStatus.isOpen && marketInsights ? (
        <MarketHoursInsights data={marketInsights} />
      ) : postMarketData ? (
        <PostMarketInsights data={postMarketData} />
      ) : null}
    </div>
  );
}
