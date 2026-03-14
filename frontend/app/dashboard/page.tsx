'use client';

import type { ReactNode } from 'react';
import { useState, useEffect, useCallback, memo, useRef, useMemo } from 'react';
import { useRouter } from 'next/navigation';
import { TrendingUp, TrendingDown, Activity, Zap, Globe, ChevronRight, Target, BarChart3, Radio, Moon, ExternalLink } from 'lucide-react';
import { useWebSocket } from '@/hooks/useWebSocket';
import { apiClient } from '@/lib/api-client';
import { Button } from '@/components/ui';
import { getISTTime, MarketStatus } from '@/lib/market-hours';
import { formatCurrency, formatPercentage, roundToDecimals } from '@/lib/utils';
import { InsightsPanel } from '@/components/dashboard/InsightsPanel';
import { getTradingViewUrl } from '@/lib/tradingview';

interface PortfolioStats {
  totalValue: number;
  dayChange: number;
  dayChangePercent: number;
  totalReturn: number;
  totalReturnPercent: number;
}

interface MarketIndex {
  name: string;
  symbol: string;
  value: number;
  change: number;
  changePercent: number;
  type: 'indian' | 'global';
  source: 'websocket' | 'yahoo' | 'api';
}

interface WatchlistItem {
  symbol: string;
  price: number | null;
  change: number | null;
  changePercent: number | null;
}

interface BackendMarketStatusResponse {
  is_open?: boolean;
  message?: string;
  current_time_ist?: string;
}

type DashboardMode = 'market' | 'post_market';
type TickRecord = { symbol?: string; ltp?: number; change_pct?: number; change?: number };
type LoadingState = { portfolio: boolean; indices: boolean; watchlist: boolean };
type DashboardFetchResult = {
  watchlistData: WatchlistItem[];
  indicesData: MarketIndex[];
  nextMode: DashboardMode;
};
type StatusBadge = {
  icon: typeof Radio;
  label: string;
  className: string;
};
type MarketSectionMeta = {
  title: string;
  subtitle: string;
};

const EMPTY_LOADING_STATE: LoadingState = { portfolio: false, indices: false, watchlist: false };
const LIVE_INDEX_SYMBOLS = new Set(['NIFTY50', 'BANKNIFTY', 'NIFTYIT']);

const QUICK_ACTIONS = [
  { icon: Zap, label: 'Trade', path: '/terminal' },
  { icon: Target, label: 'Backtest', path: '/backtest' },
  { icon: BarChart3, label: 'Screener', path: '/screener' },
];

function formatDashboardNumber(value: number | null | undefined): string {
  if (typeof value !== 'number' || Number.isNaN(value)) return '—';
  return value.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function formatDashboardPercent(value: number | null | undefined): string {
  if (typeof value !== 'number' || Number.isNaN(value)) return '—';
  return `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`;
}

function toSafeString(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback;
}

function normalizeNumber(value: unknown): number | null {
  return typeof value === 'number' ? roundToDecimals(value, 2) : null;
}

function normalizeWatchlistData(data: unknown): WatchlistItem[] {
  if (!Array.isArray(data)) return [];
  return data
    .slice(0, 10)
    .map((item: Record<string, unknown>) => {
      const rawChange = item.change;
      return {
        symbol: toSafeString(item.symbol),
        price: normalizeNumber(item.price),
        change: normalizeNumber(rawChange),
        changePercent: normalizeNumber(item.changePercent ?? rawChange),
      } as WatchlistItem;
    })
    .filter((item: WatchlistItem) => item.symbol.length > 0);
}

function normalizeLiveIndices(data: unknown): MarketIndex[] {
  if (!Array.isArray(data)) return [];
  return data.map((idx: MarketIndex) => ({
    ...idx,
    symbol: idx.symbol || idx.name,
    value: roundToDecimals(idx.value, 2),
    change: roundToDecimals(idx.change, 2),
    changePercent: roundToDecimals(idx.changePercent, 2),
    type: 'indian',
    source: 'api',
  }));
}

function getIndexChangePercent(idx: Record<string, unknown>): number {
  if (typeof idx.change_pct === 'number') {
    return roundToDecimals(idx.change_pct, 2);
  }
  if (typeof idx.changePercent === 'number') {
    return roundToDecimals(idx.changePercent, 2);
  }
  return 0;
}

function normalizeOverviewIndices(data: unknown): MarketIndex[] {
  const indicesRaw = Array.isArray(data) ? data : [];
  const globalData: MarketIndex[] = [];

  for (const idx of indicesRaw as Array<Record<string, unknown>>) {
    const symbol = toSafeString(idx.symbol);
    const price = normalizeNumber(idx.price);
    if (!symbol || price === null) continue;

    globalData.push({
      name: toSafeString(idx.name, symbol),
      symbol,
      value: price,
      change: normalizeNumber(idx.change) ?? 0,
      changePercent: getIndexChangePercent(idx),
      type: 'global',
      source: 'yahoo',
    });
  }

  return globalData;
}

function getDashboardFetchResult(
  marketStatus: MarketStatus,
  watchlistData: WatchlistItem[],
  marketPayload: unknown
): DashboardFetchResult {
  if (marketStatus.isOpen) {
    return {
      watchlistData,
      indicesData: normalizeLiveIndices(marketPayload),
      nextMode: 'market',
    };
  }

  const overview = (marketPayload ?? {}) as Record<string, unknown>;
  return {
    watchlistData,
    indicesData: normalizeOverviewIndices(overview.indices),
    nextMode: 'post_market',
  };
}

function getStatusBadge(isLive: boolean, isDelayed: boolean): StatusBadge {
  if (isLive) {
    return { icon: Radio, label: 'LIVE', className: 'bg-profit-bg text-profit' };
  }
  if (isDelayed) {
    return { icon: Activity, label: 'DELAYED', className: 'bg-amber-500/10 text-amber-400' };
  }
  return {
    icon: Moon,
    label: 'CLOSED',
    className: 'bg-background-tertiary text-foreground-secondary',
  };
}

function getMarketSectionMeta(isOpen: boolean, isConnected: boolean): MarketSectionMeta {
  if (isOpen) {
    return {
      title: 'Indian Markets',
      subtitle: isConnected ? 'NSE Real-time' : 'NSE Delayed (Disconnected)',
    };
  }
  return { title: 'Global Markets', subtitle: 'Yahoo Delayed' };
}

function getCompactIndexLabel(symbol: string): string {
  if (symbol === 'NIFTY50') return 'NIFTY';
  if (symbol === 'BANKNIFTY') return 'BANK';
  return 'IT';
}

function getStatTrend(value: number | null | undefined): 'up' | 'down' {
  return typeof value === 'number' && value >= 0 ? 'up' : 'down';
}

function applyWatchlistTicks(
  rows: WatchlistItem[],
  pending: Record<string, TickRecord>
): WatchlistItem[] {
  return rows.map((item) => {
    const tick = pending[item.symbol];
    if (!tick) return item;
    return {
      ...item,
      price: typeof tick.ltp === 'number' ? roundToDecimals(tick.ltp, 2) : item.price,
      changePercent: typeof tick.change_pct === 'number' ? roundToDecimals(tick.change_pct, 2) : item.changePercent,
      change: typeof tick.change === 'number' ? roundToDecimals(tick.change, 2) : item.change,
    };
  });
}

function applyIndexTicks(
  rows: MarketIndex[],
  pending: Record<string, TickRecord>
): MarketIndex[] {
  return rows.map((idx) => {
    const tick = pending[idx.symbol];
    if (!tick) return idx;
    return {
      ...idx,
      value: typeof tick.ltp === 'number' ? roundToDecimals(tick.ltp, 2) : idx.value,
      changePercent: typeof tick.change_pct === 'number' ? roundToDecimals(tick.change_pct, 2) : idx.changePercent,
      change: typeof tick.change === 'number' ? roundToDecimals(tick.change, 2) : idx.change,
      source: 'websocket',
    };
  });
}

// Memoized components for performance
const StatItem = memo(function StatItem({ label, value, change, trend, loading }: {
  label: string; value: string; change?: number; trend?: 'up' | 'down'; loading?: boolean
}) {
  if (loading) return <div className="text-foreground-muted">—</div>;
  return (
    <div className="flex items-center gap-3">
      <span className="text-sm text-foreground-muted">{label}</span>
      <span className="text-base font-semibold tabular-nums">{value}</span>
      {change !== undefined && (
        <span className={`text-sm tabular-nums ${trend === 'up' ? 'text-profit' : 'text-loss'}`}>
          {trend === 'up' ? '+' : ''}{formatPercentage(change)}
        </span>
      )}
    </div>
  );
});

const IndexRow = memo(function IndexRow({
  idx,
  onClick,
  onOpenTradingView,
}: {
  idx: MarketIndex;
  onClick: () => void;
  onOpenTradingView: (symbol: string) => void;
}) {
  const isUp = (idx.change ?? 0) >= 0;
  return (
    <tr onClick={onClick} className="cursor-pointer hover:bg-surface transition-colors">
      <td className="py-2 pl-4">
        <div className="flex items-center gap-2">
          <span className="font-medium">{idx.name}</span>
          <button
            type="button"
            onClick={(event) => {
              event.stopPropagation();
              onOpenTradingView(idx.symbol);
            }}
            className="text-foreground-muted hover:text-foreground"
            title="Open on TradingView"
            aria-label={`Open ${idx.name} on TradingView`}
          >
            <ExternalLink className="h-3.5 w-3.5" />
          </button>
          {idx.source === 'websocket' && <span className="w-1.5 h-1.5 rounded-full bg-profit" />}
        </div>
      </td>
      <td className="py-2 text-right tabular-nums">{formatDashboardNumber(idx.value)}</td>
      <td className={`py-2 pr-4 text-right tabular-nums ${isUp ? 'text-profit' : 'text-loss'}`}>
        {formatDashboardPercent(idx.changePercent)}
      </td>
    </tr>
  );
});

const WatchlistRow = memo(function WatchlistRow({
  stock,
  onClick,
  onOpenTradingView,
}: {
  stock: WatchlistItem;
  onClick: () => void;
  onOpenTradingView: (symbol: string) => void;
}) {
  const hasPrice = typeof stock.price === 'number' && Number.isFinite(stock.price);
  const hasChangePercent = typeof stock.changePercent === 'number' && Number.isFinite(stock.changePercent);
  const isUp = hasChangePercent ? (stock.changePercent ?? 0) >= 0 : false;
  const priceLabel = hasPrice
    ? `₹${(stock.price ?? 0).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
    : '—';
  let changeClass = 'text-foreground-muted';
  if (hasChangePercent) {
    changeClass = isUp ? 'text-profit' : 'text-loss';
  }
  let changeContent: ReactNode = 'Unavailable';
  if (hasChangePercent) {
    changeContent = (
      <div className="flex items-center justify-end gap-1">
        {isUp ? <TrendingUp className="w-3 h-3" /> : <TrendingDown className="w-3 h-3" />}
        {formatDashboardPercent(stock.changePercent)}
      </div>
    );
  }
  return (
    <tr onClick={onClick} className="cursor-pointer hover:bg-surface transition-colors">
      <td className="py-2.5 pl-4">
        <div className="flex items-center gap-1">
          <span className="font-medium">{stock.symbol}</span>
          <button
            type="button"
            onClick={(event) => {
              event.stopPropagation();
              onOpenTradingView(stock.symbol);
            }}
            className="text-foreground-muted hover:text-foreground"
            title="Open on TradingView"
            aria-label={`Open ${stock.symbol} on TradingView`}
          >
            <ExternalLink className="h-3.5 w-3.5" />
          </button>
        </div>
      </td>
      <td className="py-2.5 text-right tabular-nums">{priceLabel}</td>
      <td className={`py-2.5 pr-4 text-right tabular-nums ${changeClass}`}>
        {changeContent}
      </td>
    </tr>
  );
});

export default function DashboardPage() {
  const router = useRouter();
  const [marketStatus, setMarketStatus] = useState<MarketStatus>({ isOpen: false, message: 'Loading market status' });
  const { isConnected, lastMessage, sendMessage } = useWebSocket({ enabled: marketStatus.isOpen });

  const [mounted, setMounted] = useState(false);
  const [dashboardMode, setDashboardMode] = useState<DashboardMode>('post_market');
  const [istTime, setIstTime] = useState<string>(getISTTime());
  const [portfolioStats, setPortfolioStats] = useState<PortfolioStats | null>(null);
  const [marketIndices, setMarketIndices] = useState<MarketIndex[]>([]);
  const [watchlist, setWatchlist] = useState<WatchlistItem[]>([]);
  const [insightSymbols, setInsightSymbols] = useState<string[]>([]);
  const [loading, setLoading] = useState<LoadingState>({
    portfolio: true,
    indices: true,
    watchlist: true,
  });
  const indicesLoadedRef = useRef(false);
  const subscriptionSymbolsRef = useRef<string>('');
  const pendingTicksRef = useRef<Record<string, TickRecord>>({});
  const fetchInFlightRef = useRef(false);
  const fetchRequestIdRef = useRef(0);
  const openTradingView = (symbol: string) => {
    window.open(getTradingViewUrl(symbol), '_blank', 'noopener,noreferrer');
  };

  // Mount setup
  useEffect(() => {
    setMounted(true);
    // Reset refs on mount (for page returns)
    indicesLoadedRef.current = false;
    subscriptionSymbolsRef.current = '';
  }, []);

  const fetchMarketStatus = useCallback(async (): Promise<MarketStatus> => {
    try {
      const statusRes = await apiClient.get('/api/market/status');
      const statusData = (statusRes.data ?? {}) as BackendMarketStatusResponse;
      const nextStatus: MarketStatus = {
        isOpen: statusData.is_open === true,
        message: statusData.message || 'Market status unavailable',
      };
      setMarketStatus(nextStatus);
      setIstTime(statusData.current_time_ist || getISTTime());
      return nextStatus;
    } catch {
      const fallbackStatus: MarketStatus = {
        isOpen: false,
        message: 'Market status unavailable',
      };
      setMarketStatus(fallbackStatus);
      setIstTime(getISTTime());
      return fallbackStatus;
    }
  }, []);

  // Fetch data (NO WebSocket subscription here)
  const fetchData = useCallback(async () => {
    if (fetchInFlightRef.current) return;
    fetchInFlightRef.current = true;
    const requestId = ++fetchRequestIdRef.current;

    try {
      // Load core cards quickly; do not block them on market-status/indices calls.
      const statusPromise = fetchMarketStatus();
      const [statsRes, watchlistRes] = await Promise.all([
        apiClient.get('/api/portfolio/stats'),
        apiClient.get('/api/market/watchlist')
      ]);

      if (requestId !== fetchRequestIdRef.current) {
        fetchInFlightRef.current = false;
        return;
      }

      if (statsRes.data) setPortfolioStats(statsRes.data as PortfolioStats);
      const watchlistData = normalizeWatchlistData(watchlistRes.data);
      setWatchlist(watchlistData);
      setLoading((prev) => ({ ...prev, portfolio: false, watchlist: false }));

      const status = await statusPromise;
      if (requestId !== fetchRequestIdRef.current) return;
      const marketResponse = status.isOpen
        ? await apiClient.get('/api/market/indices')
        : await apiClient.get('/api/market/overview');
      if (requestId !== fetchRequestIdRef.current) return;

      const nextData = getDashboardFetchResult(status, watchlistData, marketResponse.data);
      setDashboardMode(nextData.nextMode);
      setMarketIndices(nextData.indicesData);
      indicesLoadedRef.current = true;

      setLoading((prev) => ({ ...prev, indices: false }));
    } catch (error) {
      console.error('Dashboard fetch error:', error);
      setLoading(EMPTY_LOADING_STATE);
    } finally {
      fetchInFlightRef.current = false;
    }
  }, [fetchMarketStatus]);

  useEffect(() => {
    void fetchData();
    const interval = setInterval(() => { void fetchData(); }, 60000);
    return () => clearInterval(interval);
  }, [fetchData]);

  const symbolsKey = useMemo(() => {
    const watchlistSymbols = watchlist.map((item: WatchlistItem) => item.symbol);
    const indexSymbols = marketIndices.map((idx: MarketIndex) => idx.symbol).filter(Boolean);
    return Array.from(new Set([...watchlistSymbols, ...indexSymbols, ...insightSymbols]))
      .sort((a, b) => a.localeCompare(b))
      .join(',');
  }, [watchlist, marketIndices, insightSymbols]);

  // Subscribe when connected AND symbol set changes.
  useEffect(() => {
    if (!isConnected || !marketStatus.isOpen || !indicesLoadedRef.current) return;
    if (!symbolsKey) return;
    const symbols = symbolsKey.split(',');

    // Guard: only act if the symbol set actually changed
    if (subscriptionSymbolsRef.current === symbolsKey) return;

    // Unsubscribe old set first (only if there was a previous set)
    if (subscriptionSymbolsRef.current) {
      const oldSymbols = subscriptionSymbolsRef.current.split(',');
      sendMessage({ action: 'unsubscribe', symbols: oldSymbols });
    }

    // Subscribe new set
    sendMessage({ action: 'subscribe', symbols });
    subscriptionSymbolsRef.current = symbolsKey;

    // NO cleanup function here - the server handles cleanup when WebSocket closes
  }, [isConnected, marketStatus.isOpen, sendMessage, symbolsKey]);

  // 3. Unsubscribe ONLY on component unmount
  useEffect(() => {
    return () => {
      if (subscriptionSymbolsRef.current) {
        const symbols = subscriptionSymbolsRef.current.split(',');
        sendMessage({ action: 'unsubscribe', symbols });
        subscriptionSymbolsRef.current = '';
      }
    };
  }, [sendMessage]);

  // Handle live ticks
  useEffect(() => {
    if (lastMessage?.type === 'ticker' && lastMessage.data) {
      const tick = lastMessage.data as TickRecord;
      if (tick.symbol) pendingTicksRef.current[tick.symbol] = tick;
    }
  }, [lastMessage]);

  useEffect(() => {
    if (dashboardMode === 'market') return;
    pendingTicksRef.current = {};
    if (subscriptionSymbolsRef.current) {
      const oldSymbols = subscriptionSymbolsRef.current.split(',');
      sendMessage({ action: 'unsubscribe', symbols: oldSymbols });
      subscriptionSymbolsRef.current = '';
    }
  }, [dashboardMode, sendMessage]);

  useEffect(() => {
    const timer = setInterval(() => {
      const pending = pendingTicksRef.current;
      const symbols = Object.keys(pending);
      if (symbols.length === 0) return;
      pendingTicksRef.current = {};

      setWatchlist((prev) => applyWatchlistTicks(prev, pending));
      setMarketIndices((prev) => applyIndexTicks(prev, pending));
    }, 250);

    return () => clearInterval(timer);
  }, []);

  if (!mounted) return null;

  const isLive = marketStatus.isOpen && isConnected;
  const isDelayed = marketStatus.isOpen && !isConnected;
  const statusBadge = getStatusBadge(isLive, isDelayed);
  const StatusIcon = statusBadge.icon;
  const marketSectionMeta = getMarketSectionMeta(marketStatus.isOpen, isConnected);
  const renderIndicesBody = () => {
    if (loading.indices) {
      return (
        <div className="p-4 space-y-2">
          {[1, 2, 3, 4].map((i) => <div key={i} className="h-8 bg-background-tertiary/30 rounded" />)}
        </div>
      );
    }
    if (marketIndices.length === 0) {
      return <div className="p-8 text-center text-foreground-muted">No market data</div>;
    }
    return (
      <table className="w-full">
        <thead className="text-xs text-foreground-muted border-b border-border">
          <tr>
            <th className="py-2 pl-4 text-left font-normal">Index</th>
            <th className="py-2 text-right font-normal">Value</th>
            <th className="py-2 pr-4 text-right font-normal">Change</th>
          </tr>
        </thead>
        <tbody className="text-sm">
          {marketIndices.map((idx) => (
            <IndexRow
              key={idx.name}
              idx={idx}
              onClick={() => router.push(`/terminal?symbol=${idx.symbol}`)}
              onOpenTradingView={openTradingView}
            />
          ))}
        </tbody>
      </table>
    );
  };
  const renderWatchlistBody = () => {
    if (loading.watchlist) {
      return (
        <div className="p-4 space-y-2">
          {[1, 2, 3, 4, 5].map((i) => <div key={i} className="h-8 bg-background-tertiary/30 rounded" />)}
        </div>
      );
    }
    if (watchlist.length === 0) {
      return <div className="p-8 text-center text-foreground-muted">No stocks in watchlist</div>;
    }
    return (
      <table className="w-full">
        <thead className="text-xs text-foreground-muted border-b border-border">
          <tr>
            <th className="py-2 pl-4 text-left font-normal">Symbol</th>
            <th className="py-2 text-right font-normal">Price</th>
            <th className="py-2 pr-4 text-right font-normal">Change</th>
          </tr>
        </thead>
        <tbody className="text-sm">
          {watchlist.map((stock) => (
            <WatchlistRow
              key={stock.symbol}
              stock={stock}
              onClick={() => router.push(`/terminal?symbol=${stock.symbol}`)}
              onOpenTradingView={openTradingView}
            />
          ))}
        </tbody>
      </table>
    );
  };

  const hasData = portfolioStats || marketIndices.length > 0 || watchlist.length > 0;
  if (!hasData && !loading.portfolio) {
    return (
      <div className="flex items-center justify-center h-full">
        <div className="text-center">
          <Activity className="w-10 h-10 mx-auto text-foreground-muted mb-3" />
          <h2 className="text-base font-medium">No Data Available</h2>
          <p className="text-sm text-foreground-muted">Connect to Fyers to view market data</p>
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-screen">
      {/* Header */}
      <header className="flex items-center justify-between px-4 py-2 border-b border-border bg-surface h-11">
        <div className="flex items-center gap-3">
          <h1 className="text-base font-semibold">Dashboard</h1>
          <span className={`flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium ${statusBadge.className}`}>
            <StatusIcon className="w-3 h-3" /> {statusBadge.label}
          </span>
          {/* Live Indices - NIFTY & BANKNIFTY */}
          {marketIndices.length > 0 && (
            <div className="flex items-center gap-3 ml-2 pl-3 border-l border-border">
              {marketIndices.filter((idx) => LIVE_INDEX_SYMBOLS.has(idx.symbol)).map(idx => {
                const isUp = (idx.change ?? 0) >= 0;
                const isLive = idx.source === 'websocket';
                const label = getCompactIndexLabel(idx.symbol);
                return (
                  <div key={idx.name} className="flex items-center gap-1.5">
                    <span className="text-xs text-foreground-muted">{label}</span>
                    <span className="text-sm font-semibold tabular-nums">{formatDashboardNumber(idx.value)}</span>
                    <span className={`text-xs tabular-nums ${isUp ? 'text-profit' : 'text-loss'}`}>
                      {formatDashboardPercent(idx.changePercent)}
                    </span>
                    {isLive && <span className="w-1 h-1 rounded-full bg-profit animate-pulse" />}
                  </div>
                );
              })}
            </div>
          )}
        </div>
        <div className="flex items-center gap-4">
          <span className="text-xs text-foreground-muted">{istTime}</span>
          <div className="flex items-center gap-1">
            {QUICK_ACTIONS.map(action => (
              <Button key={action.label} onClick={() => router.push(action.path)} variant="ghost" size="sm" className="h-8">
                <action.icon className="w-4 h-4 mr-1" />
                {action.label}
              </Button>
            ))}
          </div>
        </div>
      </header>

      {/* Stats Bar */}
      <div className="flex items-center gap-8 px-4 py-2 border-b border-border h-11">
        <StatItem
          label="Portfolio"
          value={portfolioStats ? formatCurrency(portfolioStats.totalValue, 'INR', 'en-IN') : '—'}
          loading={loading.portfolio}
        />
        <StatItem
          label="Day P&L"
          value={portfolioStats ? formatCurrency(portfolioStats.dayChange, 'INR', 'en-IN') : '—'}
          change={portfolioStats?.dayChangePercent}
          trend={getStatTrend(portfolioStats?.dayChange)}
          loading={loading.portfolio}
        />
        <StatItem
          label="Total Return"
          value={portfolioStats ? formatCurrency(portfolioStats.totalReturn, 'INR', 'en-IN') : '—'}
          change={portfolioStats?.totalReturnPercent}
          trend={getStatTrend(portfolioStats?.totalReturn)}
          loading={loading.portfolio}
        />
      </div>

      {/* Insights Panel - Top Gainers/Losers/Sectors */}
      <InsightsPanel
        marketStatus={marketStatus}
        lastMessage={lastMessage}
        onSymbolsChange={setInsightSymbols}
      />

      {/* Main Content */}
      <div className="flex-1 overflow-auto">
        <div className="grid grid-cols-1 lg:grid-cols-5 h-full">
          {/* Market Indices - 3 cols */}
          <div className="lg:col-span-3 border-r border-border">
            <div className="flex items-center justify-between px-4 py-2 border-b border-border bg-surface h-9">
              <div className="flex items-center gap-2">
                {marketStatus.isOpen ? (
                  <Activity className="w-4 h-4 text-foreground-muted" />
                ) : (
                  <Globe className="w-4 h-4 text-foreground-muted" />
                )}
                <span className="font-medium text-sm">{marketSectionMeta.title}</span>
              </div>
              <span className="text-xs text-foreground-muted">{marketSectionMeta.subtitle}</span>
            </div>
            {renderIndicesBody()}
          </div>

          {/* Watchlist - 2 cols */}
          <div className="lg:col-span-2">
            <div className="flex items-center justify-between px-4 py-2 border-b border-border bg-surface h-9">
              <div className="flex items-center gap-2">
                <TrendingUp className="w-4 h-4 text-foreground-muted" />
                <span className="font-medium text-sm">Watchlist</span>
                {marketStatus.isOpen && isConnected && <span className="w-1.5 h-1.5 rounded-full bg-profit" />}
              </div>
              <Button onClick={() => router.push('/screener')} variant="ghost" size="sm" className="h-6 text-xs">
                View All <ChevronRight className="w-3 h-3 ml-0.5" />
              </Button>
            </div>
            {renderWatchlistBody()}
          </div>
        </div>
      </div>
    </div>
  );
}
