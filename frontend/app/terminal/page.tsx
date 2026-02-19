'use client';

import { useState, useEffect, useMemo, memo, useRef } from 'react';
import dynamic from 'next/dynamic';
import { Search, Plus, Settings, Bell, CandlestickChart, TrendingUp, Loader2 } from 'lucide-react';
import { Button, Input } from '@/components/ui';
import { formatPercentage } from '@/lib/utils';
import { useWebSocket } from '@/hooks/useWebSocket';
import { isMarketOpen } from '@/lib/market-hours';

interface WatchlistItem {
  symbol: string;
  name: string;
  price: number | null;
  change: number | null;
  ltp: number | null;
}

interface CandlePoint {
  ts: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  ema20: number;
  ema50: number;
}

interface ChartResponse {
  symbol: string;
  timeframe: string;
  source: string;
  candles: CandlePoint[];
}
interface SearchResultItem {
  symbol: string;
  name: string;
  type?: string;
  instrument_type?: string;
}

type TradingMode = 'PAPER' | 'LIVE';
type OrderType = 'MARKET' | 'LIMIT' | 'SL';

interface PositionItem {
  id: string;
  symbol: string;
  net_qty: number;
  entry_price: number;
  current_price: number;
  unrealized_pnl: number;
  product_type: string;
}

interface OrderItem {
  id: string;
  symbol: string;
  side: string;
  quantity: number;
  price: number;
  status: string;
  created_at: string;
  is_paper?: number;
}

const TIMEFRAMES = ['1m', '5m', '15m', '30m', '1H', 'D', 'W', 'M'];

function toSafeNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

const PriceChart = dynamic(
  () => import('@/components/terminal/PriceChart').then((mod) => mod.PriceChart),
  {
    ssr: false,
    loading: () => (
      <div className="text-center">
        <Loader2 className="w-8 h-8 animate-spin mx-auto mb-2 text-foreground-muted" />
        <p className="text-sm text-foreground-muted">Loading chart...</p>
      </div>
    ),
  }
);

// Memoized components for performance
const WatchlistRow = memo(function WatchlistRow({
  stock,
  isSelected,
  onClick
}: {
  stock: WatchlistItem;
  isSelected: boolean;
  onClick: () => void;
}) {
  return (
    <tr
      onClick={onClick}
      className={`cursor-pointer transition-colors ${isSelected ? 'bg-surface' : 'hover:bg-surface/50'}`}
    >
      <td className="py-2 pl-3">
        <div className="font-medium">{stock.symbol}</div>
        <div className="text-xs text-foreground-muted truncate max-w-[100px]">{stock.name}</div>
      </td>
      <td className="py-2 pr-3 text-right">
        <div className="tabular-nums font-medium">
          {typeof stock.ltp === 'number' ? stock.ltp.toFixed(2) : '--'}
        </div>
        <div className={`tabular-nums text-xs ${
          typeof stock.change === 'number'
            ? (stock.change >= 0 ? 'text-profit' : 'text-loss')
            : 'text-foreground-muted'
        }`}>
          {typeof stock.change === 'number' ? formatPercentage(stock.change) : 'Unavailable'}
        </div>
      </td>
    </tr>
  );
});

export default function TerminalPage() {
  const { isConnected, lastMessage, sendMessage } = useWebSocket();
  const [selectedSymbol, setSelectedSymbol] = useState<string>('');
  const [selectedTimeframe, setSelectedTimeframe] = useState('D');
  const [activeTab, setActiveTab] = useState('positions');
  const [watchlist, setWatchlist] = useState<WatchlistItem[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [chartData, setChartData] = useState<CandlePoint[]>([]);
  const [chartSource, setChartSource] = useState<string>('');
  const [chartLoading, setChartLoading] = useState(false);
  const [chartError, setChartError] = useState<string | null>(null);
  const [tradingMode, setTradingMode] = useState<TradingMode>('PAPER');
  const [orderType, setOrderType] = useState<OrderType>('MARKET');
  const [orderQty, setOrderQty] = useState<number>(1);
  const [orderPrice, setOrderPrice] = useState<number>(0);
  const [orderTrigger, setOrderTrigger] = useState<number>(0);
  const [orderBusy, setOrderBusy] = useState(false);
  const [orderMessage, setOrderMessage] = useState<string | null>(null);
  const [livePositions, setLivePositions] = useState<PositionItem[]>([]);
  const [liveOrders, setLiveOrders] = useState<OrderItem[]>([]);
  const [paperOrders, setPaperOrders] = useState<OrderItem[]>([]);
  const [panelError, setPanelError] = useState<string | null>(null);
  const [watchlistQuery, setWatchlistQuery] = useState('');
  const [searchResults, setSearchResults] = useState<SearchResultItem[]>([]);
  const [searchLoading, setSearchLoading] = useState(false);
  const [watchlistActionError, setWatchlistActionError] = useState<string | null>(null);
  const subscribedSymbolsKeyRef = useRef('');

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const urlSymbol = params.get('symbol');
    if (urlSymbol) {
      setSelectedSymbol(urlSymbol);
    }
  }, []);

  useEffect(() => {
    const storedMode = window.localStorage.getItem('terminal_trading_mode');
    if (storedMode === 'LIVE' || storedMode === 'PAPER') {
      setTradingMode(storedMode);
    }
  }, []);

  useEffect(() => {
    window.localStorage.setItem('terminal_trading_mode', tradingMode);
  }, [tradingMode]);

  useEffect(() => {
    const fetchWatchlist = async () => {
      try {
        setIsLoading(true);
        setError(null);
        const response = await fetch('/api/market/watchlist');
        if (!response.ok) throw new Error('Failed to fetch watchlist');
        const data = (await response.json()) as Array<Record<string, unknown>>;
        const normalized = Array.isArray(data)
          ? data.map((row) => ({
              symbol: String(row.symbol ?? ''),
              name: String(row.name ?? row.symbol ?? ''),
              price: toSafeNumber(row.price),
              change: toSafeNumber(row.change),
              ltp: toSafeNumber(row.ltp) ?? toSafeNumber(row.price),
            })).filter((row) => row.symbol.length > 0)
          : [];

        setWatchlist(normalized);
        setSelectedSymbol((current) => current || normalized[0]?.symbol || '');
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Failed to load watchlist');
      } finally {
        setIsLoading(false);
      }
    };
    fetchWatchlist();
  }, []);

  useEffect(() => {
    if (!selectedSymbol) return;
    setWatchlist((prev) => {
      if (prev.some((item) => item.symbol === selectedSymbol)) {
        return prev;
      }
      return [
        ...prev,
        {
          symbol: selectedSymbol,
          name: selectedSymbol,
          price: null,
          change: null,
          ltp: null,
        },
      ];
    });
  }, [selectedSymbol]);

  useEffect(() => {
    const fetchChartData = async () => {
      if (!selectedSymbol) {
        setChartData([]);
        setChartError(null);
        return;
      }

      try {
        setChartLoading(true);
        setChartError(null);

        const params = new URLSearchParams({
          symbol: selectedSymbol,
          timeframe: selectedTimeframe,
          limit: '240',
        });
        const response = await fetch(`/api/terminal/chart?${params.toString()}`);
        if (!response.ok) {
          throw new Error(`Chart API failed (${response.status})`);
        }

        const data = (await response.json()) as ChartResponse;
        setChartData(Array.isArray(data.candles) ? data.candles : []);
        setChartSource(data.source || '');
      } catch (err) {
        setChartData([]);
        setChartError(err instanceof Error ? err.message : 'Failed to load chart');
      } finally {
        setChartLoading(false);
      }
    };

    void fetchChartData();
  }, [selectedSymbol, selectedTimeframe]);

  useEffect(() => {
    if (watchlistQuery.trim().length < 2) {
      setSearchResults([]);
      setSearchLoading(false);
      return;
    }

    const timer = setTimeout(async () => {
      try {
        setSearchLoading(true);
        const response = await fetch(`/api/market/search?query=${encodeURIComponent(watchlistQuery.trim())}&exclude_indices=true`);
        if (!response.ok) {
          setSearchResults([]);
          return;
        }
        const data = (await response.json()) as Array<Record<string, unknown>>;
        const normalized = Array.isArray(data)
          ? data
              .map((row) => ({
                symbol: String(row.symbol ?? '').trim(),
                name: String(row.name ?? row.symbol ?? '').trim(),
                type: typeof row.type === 'string' ? row.type : undefined,
                instrument_type: typeof row.instrument_type === 'string' ? row.instrument_type : undefined,
              }))
              .filter((row) => row.symbol.length > 0)
          : [];
        setSearchResults(normalized.slice(0, 8));
      } catch {
        setSearchResults([]);
      } finally {
        setSearchLoading(false);
      }
    }, 250);

    return () => clearTimeout(timer);
  }, [watchlistQuery]);

  useEffect(() => {
    if (!isConnected) {
      subscribedSymbolsKeyRef.current = '';
    }
  }, [isConnected]);

  useEffect(() => {
    if (!isConnected || !isMarketOpen().isOpen) return;

    const symbols = Array.from(new Set([
      ...watchlist.map((item: WatchlistItem) => item.symbol),
      selectedSymbol,
      ...livePositions.map((position) => position.symbol),
    ].filter(Boolean))).sort();
    if (symbols.length === 0) return;

    const symbolsKey = symbols.join(',');
    if (symbolsKey === subscribedSymbolsKeyRef.current) return;

    if (subscribedSymbolsKeyRef.current) {
      sendMessage({ action: 'unsubscribe', symbols: subscribedSymbolsKeyRef.current.split(',') });
    }

    sendMessage({ action: 'subscribe', symbols });
    subscribedSymbolsKeyRef.current = symbolsKey;
  }, [isConnected, watchlist, selectedSymbol, livePositions, sendMessage]);

  useEffect(() => {
    return () => {
      if (!subscribedSymbolsKeyRef.current) return;
      sendMessage({ action: 'unsubscribe', symbols: subscribedSymbolsKeyRef.current.split(',') });
      subscribedSymbolsKeyRef.current = '';
    };
  }, [sendMessage]);

  // Handle live ticks
  useEffect(() => {
    if (lastMessage?.type === 'ticker' && lastMessage.data) {
      const tick = lastMessage.data as { symbol?: string; ltp?: number; change_pct?: number; change?: number; volume?: number };
      if (tick.symbol) {
        setWatchlist(prev => prev.map(item =>
          item.symbol === tick.symbol
            ? {
                ...item,
                ltp: typeof tick.ltp === 'number' ? tick.ltp : item.ltp,
                price: typeof tick.ltp === 'number' ? tick.ltp : item.price,
                change: typeof tick.change_pct === 'number' ? tick.change_pct : item.change,
              }
            : item
        ));

        if (tick.symbol === selectedSymbol && typeof tick.ltp === 'number') {
          setChartData((prev) => {
            if (prev.length === 0) return prev;
            const next = [...prev];
            const last = { ...next[next.length - 1] };
            const nextPrice = tick.ltp as number;

            last.close = Number(nextPrice.toFixed(2));
            last.high = Number(Math.max(last.high, nextPrice).toFixed(2));
            last.low = Number(Math.min(last.low, nextPrice).toFixed(2));
            if (typeof tick.volume === 'number') {
              last.volume = Math.max(last.volume, tick.volume);
            }
            next[next.length - 1] = last;
            return next;
          });
        }
      }
    }
  }, [lastMessage, selectedSymbol]);

  const addSymbolToWatchlist = async (item: SearchResultItem) => {
    const symbol = item.symbol.trim().toUpperCase();
    if (!symbol) return;

    try {
      setWatchlistActionError(null);
      const response = await fetch('/api/market/watchlist', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          symbol,
          instrument_type: item.instrument_type || (item.type === 'PE' || item.type === 'CE' ? item.type : 'EQ'),
        }),
      });

      if (!response.ok) {
        const data = await response.json().catch(() => null) as { detail?: string } | null;
        throw new Error(data?.detail || 'Failed to add symbol');
      }

      setWatchlist((prev) => {
        if (prev.some((w) => w.symbol === symbol)) return prev;
        return [
          {
            symbol,
            name: item.name || symbol,
            price: null,
            change: null,
            ltp: null,
          },
          ...prev,
        ];
      });
      setSelectedSymbol(symbol);
      setWatchlistQuery('');
      setSearchResults([]);
    } catch (err) {
      setWatchlistActionError(err instanceof Error ? err.message : 'Failed to add symbol');
    }
  };

  useEffect(() => {
    const pollPanels = async () => {
      try {
        setPanelError(null);
        const [positionsRes, ordersRes] = await Promise.all([
          fetch('/api/trading/positions'),
          fetch('/api/trading/orders?limit=100'),
        ]);

        const positionsData = positionsRes.ok ? (await positionsRes.json()) as PositionItem[] : [];
        const ordersData = ordersRes.ok ? (await ordersRes.json()) as OrderItem[] : [];

        setLivePositions(Array.isArray(positionsData) ? positionsData : []);
        if (Array.isArray(ordersData)) {
          setPaperOrders(ordersData.filter((order) => Number(order.is_paper) === 1));
          setLiveOrders(ordersData.filter((order) => Number(order.is_paper) !== 1));
        } else {
          setPaperOrders([]);
          setLiveOrders([]);
        }
      } catch {
        setPanelError('Failed to load positions/orderbook');
      }
    };

    void pollPanels();
    const interval = setInterval(() => { void pollPanels(); }, 10000);
    return () => clearInterval(interval);
  }, []);

  useEffect(() => {
    if (lastMessage?.type !== 'ticker' || !lastMessage.data) return;
    const tick = lastMessage.data as { symbol?: string; ltp?: number };
    if (!tick.symbol || typeof tick.ltp !== 'number') return;

    setLivePositions((prev) => prev.map((pos) => {
      if (pos.symbol !== tick.symbol) return pos;
      const currentPrice = tick.ltp as number;
      const qty = Number(pos.net_qty || 0);
      const pnl = (currentPrice - Number(pos.entry_price || 0)) * qty;
      return {
        ...pos,
        current_price: currentPrice,
        unrealized_pnl: pnl,
      };
    }));
  }, [lastMessage]);

  const currentSymbol = useMemo(() =>
    watchlist.find(s => s.symbol === selectedSymbol) || watchlist[0] || null,
  [watchlist, selectedSymbol]);

  const placeOrder = async (side: 'BUY' | 'SELL') => {
    if (!currentSymbol || orderQty <= 0) {
      setOrderMessage('Select symbol and enter valid quantity');
      return;
    }

    try {
      setOrderBusy(true);
      setOrderMessage(null);

      if (tradingMode === 'PAPER') {
        const response = await fetch('/api/terminal/paper/order', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            symbol: currentSymbol.symbol,
            side,
            quantity: orderQty,
            order_type: orderType,
            price: orderType === 'MARKET' ? 0 : orderPrice,
            trigger_price: orderType === 'SL' ? orderTrigger : 0,
          }),
        });

        const data = await response.json();
        if (!response.ok) {
          throw new Error(data?.detail || 'Failed to place paper order');
        }
        setOrderMessage(`PAPER ${side} submitted (${data.order_id})`);
        return;
      }

      await fetch('/api/trading/mode', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode: 'LIVE' }),
      });

      const response = await fetch('/api/trading/order', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          symbol: currentSymbol.symbol,
          side,
          quantity: orderQty,
          product: 'INTRADAY',
          type: orderType === 'SL' ? 'SL' : orderType,
          price: orderType === 'MARKET' ? 0 : orderPrice,
          trigger_price: orderType === 'SL' ? orderTrigger : 0,
          tag: 'terminal-live',
          instrument_type: 'EQ',
        }),
      });

      const data = await response.json();
      if (!response.ok) {
        throw new Error(data?.detail || 'Failed to place live order');
      }
      setOrderMessage(`LIVE ${side} submitted (${data.order_id || data.id || 'ok'})`);
    } catch (err) {
      setOrderMessage(err instanceof Error ? err.message : 'Order failed');
    } finally {
      setOrderBusy(false);
    }
  };

  return (
    <div className="flex flex-col h-[calc(100vh-48px)]">
      <div className={`px-4 py-1.5 text-xs border-b border-border ${tradingMode === 'PAPER' ? 'bg-amber-500/10 text-amber-400' : 'bg-loss-bg text-loss'}`}>
        Mode: {tradingMode}
      </div>
      {/* Top Bar */}
      <header className="flex items-center justify-between px-4 py-2 border-b border-border bg-surface">
        <div className="flex items-center gap-6">
          {/* Symbol Info */}
          <div className="flex items-center gap-4">
            <div>
              <div className="flex items-center gap-2">
                <span className="font-semibold text-lg">{currentSymbol?.symbol || '--'}</span>
                <span className="px-1.5 py-0.5 rounded text-xs bg-background-tertiary">NSE</span>
                {isConnected && <span className="w-1.5 h-1.5 rounded-full bg-profit" />}
              </div>
              <div className="text-xs text-foreground-muted truncate max-w-[150px]">
                {currentSymbol?.name || 'Select a symbol'}
              </div>
            </div>
            <div className="text-right">
              <div className="font-semibold tabular-nums text-xl">
                {typeof currentSymbol?.ltp === 'number' ? currentSymbol.ltp.toFixed(2) : '--'}
              </div>
              {currentSymbol && (
                <div className={`tabular-nums text-xs ${
                  typeof currentSymbol.change === 'number'
                    ? (currentSymbol.change >= 0 ? 'text-profit' : 'text-loss')
                    : 'text-foreground-muted'
                }`}>
                  {typeof currentSymbol.change === 'number' ? formatPercentage(currentSymbol.change) : 'Unavailable'}
                </div>
              )}
            </div>
          </div>

          {/* Timeframes */}
          <div className="flex items-center gap-0.5">
            {TIMEFRAMES.map((tf) => (
              <Button
                key={tf}
                onClick={() => setSelectedTimeframe(tf)}
                variant={selectedTimeframe === tf ? "secondary" : "ghost"}
                size="sm"
                className="h-7 px-2 text-xs"
              >
                {tf}
              </Button>
            ))}
          </div>
        </div>

        <div className="flex items-center gap-1">
          <Button
            variant={tradingMode === 'PAPER' ? 'secondary' : 'ghost'}
            size="sm"
            className="h-8 text-xs"
            onClick={() => setTradingMode('PAPER')}
          >
            PAPER
          </Button>
          <Button
            variant={tradingMode === 'LIVE' ? 'loss' : 'ghost'}
            size="sm"
            className="h-8 text-xs"
            onClick={() => setTradingMode('LIVE')}
          >
            LIVE
          </Button>
          <Button variant="ghost" size="icon" className="h-8 w-8"><Bell className="w-4 h-4" /></Button>
          <Button variant="ghost" size="icon" className="h-8 w-8"><Settings className="w-4 h-4" /></Button>
        </div>
      </header>

      {/* Main Content */}
      <div className="flex-1 flex overflow-hidden">
        {/* Left Sidebar - Watchlist */}
        <div className="w-64 flex-shrink-0 flex flex-col border-r border-border bg-surface">
          {/* Search */}
          <div className="p-2 border-b border-border">
            <div className="flex items-center gap-2 px-3 py-1.5 rounded bg-background border border-border">
              <Search className="w-4 h-4 text-foreground-muted" />
              <input
                type="text"
                value={watchlistQuery}
                onChange={(event) => setWatchlistQuery(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' && searchResults[0]) {
                    event.preventDefault();
                    void addSymbolToWatchlist(searchResults[0]);
                  }
                }}
                placeholder="Search..."
                className="flex-1 bg-transparent outline-none text-sm"
              />
              <button
                type="button"
                className="text-foreground-muted hover:text-foreground disabled:opacity-50"
                disabled={!searchResults[0]}
                onClick={() => {
                  if (searchResults[0]) {
                    void addSymbolToWatchlist(searchResults[0]);
                  }
                }}
                aria-label="Add symbol to watchlist"
              >
                <Plus className="w-4 h-4" />
              </button>
            </div>
            {watchlistActionError && (
              <div className="mt-1 text-xs text-loss">{watchlistActionError}</div>
            )}
            {watchlistQuery.trim().length >= 2 && (
              <div className="mt-1 rounded border border-border bg-surface max-h-44 overflow-y-auto">
                {searchLoading ? (
                  <div className="px-2 py-2 text-xs text-foreground-muted">Searching...</div>
                ) : searchResults.length === 0 ? (
                  <div className="px-2 py-2 text-xs text-foreground-muted">No matches</div>
                ) : (
                  searchResults.map((item) => (
                    <button
                      key={`${item.symbol}-${item.type || 'EQ'}`}
                      type="button"
                      className="w-full text-left px-2 py-1.5 hover:bg-background-secondary border-b border-border last:border-b-0"
                      onClick={() => { void addSymbolToWatchlist(item); }}
                    >
                      <div className="text-sm font-medium">{item.symbol}</div>
                      <div className="text-xs text-foreground-muted truncate">{item.name}</div>
                    </button>
                  ))
                )}
              </div>
            )}
          </div>

          {/* Watchlist */}
          <div className="flex-1 overflow-y-auto">
            {isLoading ? (
              <div className="flex items-center justify-center h-32">
                <Loader2 className="w-5 h-5 animate-spin text-foreground-muted" />
              </div>
            ) : error ? (
              <div className="p-4 text-center text-sm text-loss">{error}</div>
            ) : watchlist.length === 0 ? (
              <div className="p-4 text-center text-sm text-foreground-muted">No symbols</div>
            ) : (
              <table className="w-full">
                <thead className="text-xs text-foreground-muted border-b border-border">
                  <tr>
                    <th className="py-1.5 pl-3 text-left font-normal">Symbol</th>
                    <th className="py-1.5 pr-3 text-right font-normal">LTP</th>
                  </tr>
                </thead>
                <tbody className="text-sm">
                  {watchlist.map((stock) => (
                    <WatchlistRow
                      key={stock.symbol}
                      stock={stock}
                      isSelected={selectedSymbol === stock.symbol}
                      onClick={() => setSelectedSymbol(stock.symbol)}
                    />
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>

        {/* Center - Chart Area */}
        <div className="flex-1 flex flex-col">
          {/* Chart Toolbar */}
          <div className="h-9 flex items-center justify-between px-3 border-b border-border">
            <div className="flex items-center gap-2">
              <Button variant="ghost" size="sm" className="h-7 gap-1 text-xs">
                <CandlestickChart className="w-4 h-4" /> Candles
              </Button>
              <Button variant="ghost" size="sm" className="h-7 gap-1 text-xs">
                <TrendingUp className="w-4 h-4" /> Indicators
              </Button>
            </div>
            <div className="flex items-center gap-2">
              {chartSource && <span className="text-xs text-foreground-muted">Source: {chartSource}</span>}
              <Button variant="profit" size="sm" className="h-7 text-xs px-4">Buy</Button>
              <Button variant="loss" size="sm" className="h-7 text-xs px-4">Sell</Button>
            </div>
          </div>

          {/* Chart */}
          <div className="flex-1 flex items-center justify-center">
            {chartLoading ? (
              <div className="text-center">
                <Loader2 className="w-8 h-8 animate-spin mx-auto mb-2 text-foreground-muted" />
                <p className="text-sm text-foreground-muted">Loading chart...</p>
              </div>
            ) : chartError ? (
              <div className="text-center">
                <p className="text-sm text-loss">{chartError}</p>
              </div>
            ) : chartData.length === 0 ? (
              <div className="text-center">
                <CandlestickChart className="w-12 h-12 mx-auto mb-2 text-foreground-muted" />
                <p className="text-sm text-foreground-muted">No chart data</p>
              </div>
            ) : (
              <PriceChart data={chartData} />
            )}
          </div>

          {/* Bottom Panel */}
          <div className="h-40 border-t border-border bg-surface">
            <div className="flex items-center gap-1 px-3 h-8 border-b border-border">
              {['Positions', 'Orders', 'Trades'].map((tab) => (
                <Button
                  key={tab}
                  onClick={() => setActiveTab(tab.toLowerCase())}
                  variant={activeTab === tab.toLowerCase() ? "secondary" : "ghost"}
                  size="sm"
                  className="h-6 text-xs"
                >
                  {tab}
                </Button>
              ))}
            </div>
            <div className="p-3 h-[calc(100%-2rem)] overflow-auto">
              {panelError && <div className="text-xs text-loss mb-2">{panelError}</div>}
              {activeTab === 'positions' && (
                livePositions.length === 0 ? (
                  <div className="text-center text-sm text-foreground-muted">No live positions</div>
                ) : (
                  <table className="w-full text-xs">
                    <thead className="text-foreground-muted">
                      <tr>
                        <th className="text-left font-normal">Symbol</th>
                        <th className="text-right font-normal">Qty</th>
                        <th className="text-right font-normal">LTP</th>
                        <th className="text-right font-normal">P&L</th>
                      </tr>
                    </thead>
                    <tbody>
                      {livePositions.map((pos) => (
                        <tr key={pos.id} className="border-t border-border">
                          <td className="py-1.5">{pos.symbol} <span className="text-xs text-foreground-muted">LIVE</span></td>
                          <td className="py-1.5 text-right tabular-nums">{pos.net_qty}</td>
                          <td className="py-1.5 text-right tabular-nums">{Number(pos.current_price || 0).toFixed(2)}</td>
                          <td className={`py-1.5 text-right tabular-nums ${Number(pos.unrealized_pnl || 0) >= 0 ? 'text-profit' : 'text-loss'}`}>
                            {Number(pos.unrealized_pnl || 0).toFixed(2)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )
              )}
              {activeTab === 'orders' && (
                [...liveOrders, ...paperOrders].length === 0 ? (
                  <div className="text-center text-sm text-foreground-muted">No orders</div>
                ) : (
                  <table className="w-full text-xs">
                    <thead className="text-foreground-muted">
                      <tr>
                        <th className="text-left font-normal">Symbol</th>
                        <th className="text-left font-normal">Mode</th>
                        <th className="text-right font-normal">Qty</th>
                        <th className="text-right font-normal">Status</th>
                      </tr>
                    </thead>
                    <tbody>
                      {[...liveOrders, ...paperOrders].map((order) => (
                        <tr key={order.id} className="border-t border-border">
                          <td className="py-1.5">{order.symbol}</td>
                          <td className="py-1.5">{Number(order.is_paper) === 1 ? 'PAPER' : 'LIVE'}</td>
                          <td className="py-1.5 text-right tabular-nums">{order.quantity}</td>
                          <td className="py-1.5 text-right">{order.status}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )
              )}
              {activeTab === 'trades' && (
                <div className="text-center text-sm text-foreground-muted">Tradebook integration in T3 continuation</div>
              )}
            </div>
          </div>
        </div>

        {/* Right Sidebar - Order Panel */}
        <div className="w-56 flex-shrink-0 p-3 border-l border-border bg-surface">
          <div className="flex items-center gap-2 mb-3">
            <Button variant="profit" className="flex-1 h-8 text-sm" disabled={orderBusy} onClick={() => void placeOrder('BUY')}>Buy</Button>
            <Button variant="loss" className="flex-1 h-8 text-sm" disabled={orderBusy} onClick={() => void placeOrder('SELL')}>Sell</Button>
          </div>

          <div className="space-y-3">
            <div>
              <label className="text-xs text-foreground-muted mb-1 block">Order Type</label>
              <select
                value={orderType}
                onChange={(event) => setOrderType(event.target.value as OrderType)}
                className="w-full h-8 px-2 rounded text-sm border border-border bg-background outline-none"
              >
                <option value="MARKET">Market</option>
                <option value="LIMIT">Limit</option>
                <option value="SL">Stop Loss</option>
              </select>
            </div>
            <div>
              <label className="text-xs text-foreground-muted mb-1 block">Quantity</label>
              <Input
                type="number"
                min={1}
                value={orderQty}
                onChange={(event) => setOrderQty(Math.max(1, Number(event.target.value) || 1))}
                placeholder="Qty"
                className="h-8 text-sm"
              />
            </div>
            <div>
              <label className="text-xs text-foreground-muted mb-1 block">Price</label>
              <Input
                type="number"
                value={orderPrice}
                disabled={orderType === 'MARKET'}
                onChange={(event) => setOrderPrice(Number(event.target.value) || 0)}
                placeholder="Price"
                className="h-8 text-sm"
              />
            </div>
            {orderType === 'SL' && (
              <div>
                <label className="text-xs text-foreground-muted mb-1 block">Trigger</label>
                <Input
                  type="number"
                  value={orderTrigger}
                  onChange={(event) => setOrderTrigger(Number(event.target.value) || 0)}
                  placeholder="Trigger"
                  className="h-8 text-sm"
                />
              </div>
            )}
            {orderMessage && (
              <div className="text-xs text-foreground-muted border border-border rounded px-2 py-1.5">
                {orderMessage}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
