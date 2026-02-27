'use client';

import { useState, useEffect, useMemo, memo, useRef } from 'react';
import dynamic from 'next/dynamic';
import { Search, Plus, Settings, Bell, CandlestickChart, ListOrdered, Loader2, ExternalLink } from 'lucide-react';
import { Button, Input } from '@/components/ui';
import { formatPercentage, roundToDecimals } from '@/lib/utils';
import { useWebSocket } from '@/hooks/useWebSocket';
import { isMarketOpen } from '@/lib/market-hours';
import { getTradingViewUrl } from '@/lib/tradingview';

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
type OrderType = 'MARKET' | 'LIMIT' | 'SL' | 'SL-M';
type TerminalView = 'options' | 'positions' | 'chart';

interface OptionsLeg {
  symbol: string | null;
  ltp: number | null;
  oi: number | null;
  volume: number | null;
  change_pct: number | null;
}

interface OptionsBoardRow {
  strike: number;
  ce: OptionsLeg | null;
  pe: OptionsLeg | null;
}

interface OptionsBoardResponse {
  underlying: string;
  spot_price: number;
  expiry: string;
  atm_strike: number;
  strikes: OptionsBoardRow[];
  timestamp: string;
}

interface OptionsOrderflowResponse {
  pcr_oi: number | null;
  pcr_volume: number | null;
  ce_oi: number;
  pe_oi: number;
  ce_volume: number;
  pe_volume: number;
  timestamp: string;
}

interface PositionItem {
  id: string;
  symbol: string;
  net_qty: number;
  entry_price: number;
  current_price: number;
  unrealized_pnl: number;
  realized_pnl?: number;
  net_pnl?: number;
  product_type: string;
  mode?: 'PAPER' | 'LIVE';
}

interface PositionsBookResponse {
  live_positions: PositionItem[];
  paper_positions: PositionItem[];
  net_pnl_live: number;
  net_pnl_paper: number;
  net_pnl_total: number;
}

const TIMEFRAMES = ['1m', '5m', '15m', '30m', '1H', 'D', 'W', 'M'];
const OPTION_LOT_SIZES: Record<string, number> = {
  NIFTY: 75,
  BANKNIFTY: 30,
  FINNIFTY: 25,
};

function toSafeNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function toSafeString(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback;
}

// PriceChart component - reserved for future chart view implementation
const _PriceChart = dynamic(
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
  onClick,
  onOpenTradingView,
}: {
  stock: WatchlistItem;
  isSelected: boolean;
  onClick: () => void;
  onOpenTradingView: (symbol: string) => void;
}) {
  return (
    <tr
      onClick={onClick}
      className={`cursor-pointer transition-colors ${isSelected ? 'bg-surface' : 'hover:bg-surface/50'}`}
    >
      <td className="py-2 pl-3">
        <div className="flex items-center gap-1">
          <div className="font-medium">{stock.symbol}</div>
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
  const [watchlist, setWatchlist] = useState<WatchlistItem[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Chart state - reserved for future chart view implementation
  const [_chartData, setChartData] = useState<CandlePoint[]>([]);
  const [_chartSource, setChartSource] = useState<string>('');
  const [_chartLoading, setChartLoading] = useState(false);
  const [_chartError, setChartError] = useState<string | null>(null);
  const [tradingMode, setTradingMode] = useState<TradingMode>('PAPER');
  const [orderType, setOrderType] = useState<OrderType>('MARKET');
  const [orderQty, setOrderQty] = useState<number>(1);
  const [optionLots, setOptionLots] = useState<number>(1);
  const [orderPrice, setOrderPrice] = useState<number>(0);
  const [orderTrigger, setOrderTrigger] = useState<number>(0);
  const [orderBusy, setOrderBusy] = useState(false);
  const [orderMessage, setOrderMessage] = useState<string | null>(null);
  const [positions, setPositions] = useState<PositionItem[]>([]);
  const [positionsError, setPositionsError] = useState<string | null>(null);
  const [watchlistQuery, setWatchlistQuery] = useState('');
  const [searchResults, setSearchResults] = useState<SearchResultItem[]>([]);
  const [searchLoading, setSearchLoading] = useState(false);
  const [watchlistActionError, setWatchlistActionError] = useState<string | null>(null);
  const [terminalView, setTerminalView] = useState<TerminalView>('options');
  const [optionsUnderlying, setOptionsUnderlying] = useState<string>('NIFTY');
  const [optionsExpiry, setOptionsExpiry] = useState<string>('');
  const [optionsBoard, setOptionsBoard] = useState<OptionsBoardResponse | null>(null);
  const [optionsOrderflow, setOptionsOrderflow] = useState<OptionsOrderflowResponse | null>(null);
  const [optionsLoading, setOptionsLoading] = useState(false);
  const [optionsError, setOptionsError] = useState<string | null>(null);
  const [selectedOptionSymbol, setSelectedOptionSymbol] = useState<string>('');
  const [riskOverrideReason, setRiskOverrideReason] = useState<string>('');
  const subscribedSymbolsKeyRef = useRef('');

  useEffect(() => {
    const params = new URLSearchParams(globalThis.location.search);
    const urlSymbol = params.get('symbol');
    if (urlSymbol) {
      setSelectedSymbol(urlSymbol);
    }
  }, []);

  useEffect(() => {
    const storedMode = globalThis.localStorage.getItem('terminal_trading_mode');
    if (storedMode === 'LIVE' || storedMode === 'PAPER') {
      setTradingMode(storedMode);
    }
  }, []);

  useEffect(() => {
    globalThis.localStorage.setItem('terminal_trading_mode', tradingMode);
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
              symbol: toSafeString(row.symbol),
              name: toSafeString(row.name, toSafeString(row.symbol)),
              price: toSafeNumber(row.price),
              change: toSafeNumber(row.change),
              ltp: toSafeNumber(row.ltp) ?? toSafeNumber(row.price),
            })).filter((row) => row.symbol.length > 0)
              .map((row) => ({
                ...row,
                price: typeof row.price === 'number' ? roundToDecimals(row.price, 2) : row.price,
                ltp: typeof row.ltp === 'number' ? roundToDecimals(row.ltp, 2) : row.ltp,
                change: typeof row.change === 'number' ? roundToDecimals(row.change, 2) : row.change,
              }))
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
      if (terminalView !== 'chart') {
        return;
      }
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
  }, [terminalView, selectedSymbol, selectedTimeframe]);

  useEffect(() => {
    if (terminalView !== 'options') {
      return;
    }

    let cancelled = false;

    const fetchOptionsBoard = async () => {
      try {
        if (!optionsBoard) {
          setOptionsLoading(true);
        }
        setOptionsError(null);
        const boardParams = new URLSearchParams({
          underlying: optionsUnderlying,
          strike_count: '15',
        });
        if (optionsExpiry) {
          boardParams.set('expiry', optionsExpiry);
        }
        const boardRes = await fetch(`/api/terminal/options/board?${boardParams.toString()}`);

        if (!boardRes.ok) {
          const err = await boardRes.json().catch(() => null) as { detail?: string } | null;
          throw new Error(err?.detail || `Options board failed (${boardRes.status})`);
        }
        const board = (await boardRes.json()) as OptionsBoardResponse;

        if (cancelled) {
          return;
        }
        setOptionsBoard(board);
        const computed = Array.isArray(board.strikes)
          ? board.strikes.reduce(
              (acc, row) => {
                acc.ceOi += Number(row.ce?.oi || 0);
                acc.peOi += Number(row.pe?.oi || 0);
                acc.ceVol += Number(row.ce?.volume || 0);
                acc.peVol += Number(row.pe?.volume || 0);
                return acc;
              },
              { ceOi: 0, peOi: 0, ceVol: 0, peVol: 0 }
            )
          : { ceOi: 0, peOi: 0, ceVol: 0, peVol: 0 };
        setOptionsOrderflow({
          ce_oi: computed.ceOi,
          pe_oi: computed.peOi,
          ce_volume: computed.ceVol,
          pe_volume: computed.peVol,
          pcr_oi: computed.ceOi > 0 ? roundToDecimals(computed.peOi / computed.ceOi, 4) : null,
          pcr_volume: computed.ceVol > 0 ? roundToDecimals(computed.peVol / computed.ceVol, 4) : null,
          timestamp: board.timestamp,
        });
        if (!optionsExpiry && board.expiry) {
          setOptionsExpiry(board.expiry);
        }
      } catch (err) {
        if (!cancelled) {
          setOptionsError(err instanceof Error ? err.message : 'Failed to load options board');
        }
      } finally {
        if (!cancelled) {
          setOptionsLoading(false);
        }
      }
    };

    void fetchOptionsBoard();
    const interval = setInterval(() => {
      void fetchOptionsBoard();
    }, 3000);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [terminalView, optionsUnderlying, optionsExpiry, optionsBoard]);

  useEffect(() => {
    if (watchlistQuery.trim().length < 2) {
      setSearchResults([]);
      setSearchLoading(false);
      return;
    }

    const controller = new AbortController();
    const timer = setTimeout(async () => {
      try {
        setSearchLoading(true);
        const response = await fetch(
          `/api/market/search?query=${encodeURIComponent(watchlistQuery.trim())}&exclude_indices=true`,
          { signal: controller.signal }
        );
        if (!response.ok) {
          setSearchResults([]);
          return;
        }
        const data = (await response.json()) as Array<Record<string, unknown>>;
        const normalized = Array.isArray(data)
          ? data
              .map((row) => ({
                symbol: toSafeString(row.symbol).trim(),
                name: toSafeString(row.name, toSafeString(row.symbol)).trim(),
                type: typeof row.type === 'string' ? row.type : undefined,
                instrument_type: typeof row.instrument_type === 'string' ? row.instrument_type : undefined,
              }))
              .filter((row) => row.symbol.length > 0)
          : [];
        setSearchResults(normalized.slice(0, 8));
      } catch (error) {
        if (error instanceof DOMException && error.name === 'AbortError') {
          return;
        }
        setSearchResults([]);
      } finally {
        if (!controller.signal.aborted) {
          setSearchLoading(false);
        }
      }
    }, 250);

    return () => {
      clearTimeout(timer);
      controller.abort();
    };
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
      selectedOptionSymbol,
      ...positions.map((position) => position.symbol),
    ].filter(Boolean))).sort((a, b) => a.localeCompare(b));
    if (symbols.length === 0) return;

    const symbolsKey = symbols.join(',');
    if (symbolsKey === subscribedSymbolsKeyRef.current) return;

    if (subscribedSymbolsKeyRef.current) {
      sendMessage({ action: 'unsubscribe', symbols: subscribedSymbolsKeyRef.current.split(',') });
    }

    sendMessage({ action: 'subscribe', symbols });
    subscribedSymbolsKeyRef.current = symbolsKey;
  }, [isConnected, watchlist, selectedSymbol, selectedOptionSymbol, positions, sendMessage]);

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
                ltp: typeof tick.ltp === 'number' ? roundToDecimals(tick.ltp, 2) : item.ltp,
                price: typeof tick.ltp === 'number' ? roundToDecimals(tick.ltp, 2) : item.price,
                change: typeof tick.change_pct === 'number' ? roundToDecimals(tick.change_pct, 2) : item.change,
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
    const pollPositions = async () => {
      try {
        setPositionsError(null);
        const response = await fetch('/api/trading/positions/book');
        if (!response.ok) {
          throw new Error('Failed to load positions');
        }
        const data = (await response.json()) as PositionsBookResponse;
        const liveRows = Array.isArray(data.live_positions) ? data.live_positions : [];
        const paperRows = Array.isArray(data.paper_positions) ? data.paper_positions : [];
        setPositions(
          [...liveRows, ...paperRows].map((row) => ({
            ...row,
            current_price: Number(row.current_price || 0),
            entry_price: Number(row.entry_price || 0),
            unrealized_pnl: Number(row.unrealized_pnl || 0),
            realized_pnl: Number(row.realized_pnl || 0),
            net_pnl: Number(row.net_pnl || row.unrealized_pnl || 0),
          }))
        );
      } catch (error) {
        setPositionsError(error instanceof Error ? error.message : 'Failed to load positions');
      }
    };

    void pollPositions();
    const interval = setInterval(() => { void pollPositions(); }, 7000);
    return () => clearInterval(interval);
  }, []);

  useEffect(() => {
    if (lastMessage?.type !== 'ticker' || !lastMessage.data) return;
    const tick = lastMessage.data as { symbol?: string; ltp?: number };
    if (!tick.symbol || typeof tick.ltp !== 'number') return;

    setPositions((prev) => prev.map((pos) => {
      if (pos.symbol !== tick.symbol) return pos;
      const currentPrice = roundToDecimals(tick.ltp as number, 2);
      const qty = Number(pos.net_qty || 0);
      const unrealized = roundToDecimals((currentPrice - Number(pos.entry_price || 0)) * qty, 2);
      const realized = Number(pos.realized_pnl || 0);
      return {
        ...pos,
        current_price: currentPrice,
        unrealized_pnl: unrealized,
        net_pnl: roundToDecimals(realized + unrealized, 2),
      };
    }));
  }, [lastMessage]);

  const currentSymbol = useMemo(() =>
    watchlist.find(s => s.symbol === selectedSymbol) || watchlist[0] || null,
  [watchlist, selectedSymbol]);

  const positionsSummary = useMemo(() => {
    const net_pnl_live = positions
      .filter((p) => p.mode !== 'PAPER')
      .reduce((acc, p) => acc + Number(p.net_pnl || p.unrealized_pnl || 0), 0);
    const net_pnl_paper = positions
      .filter((p) => p.mode === 'PAPER')
      .reduce((acc, p) => acc + Number(p.net_pnl || p.unrealized_pnl || 0), 0);
    return {
      net_pnl_live: roundToDecimals(net_pnl_live, 2),
      net_pnl_paper: roundToDecimals(net_pnl_paper, 2),
      net_pnl_total: roundToDecimals(net_pnl_live + net_pnl_paper, 2),
    };
  }, [positions]);

  const activeTradeSymbol = useMemo(() => {
    if (terminalView === 'options' && selectedOptionSymbol) {
      return selectedOptionSymbol;
    }
    return currentSymbol?.symbol || '';
  }, [terminalView, selectedOptionSymbol, currentSymbol]);

  const isOptionSymbol = useMemo(
    () => activeTradeSymbol.endsWith('CE') || activeTradeSymbol.endsWith('PE'),
    [activeTradeSymbol]
  );

  const optionLotSize = useMemo(() => OPTION_LOT_SIZES[optionsUnderlying] ?? 1, [optionsUnderlying]);
  const effectiveOrderQuantity = useMemo(() => {
    if (terminalView === 'options' && isOptionSymbol) {
      return Math.max(1, optionLots) * optionLotSize;
    }
    return Math.max(1, orderQty);
  }, [terminalView, isOptionSymbol, optionLots, optionLotSize, orderQty]);

  const openTradingView = (symbol: string) => {
    globalThis.open(getTradingViewUrl(symbol), '_blank', 'noopener,noreferrer');
  };

  const placeOrder = async (side: 'BUY' | 'SELL') => {
    if (!activeTradeSymbol || effectiveOrderQuantity <= 0) {
      setOrderMessage('Select symbol and enter valid quantity');
      return;
    }

    try {
      setOrderBusy(true);
      setOrderMessage(null);
      let isLiveConfirmationAck = false;
      if (tradingMode === 'LIVE') {
        isLiveConfirmationAck = globalThis.confirm(`Confirm LIVE ${side} ${effectiveOrderQuantity} ${activeTradeSymbol}?`);
        if (!isLiveConfirmationAck) {
          setOrderMessage('Live order cancelled');
          return;
        }
      }

      await fetch('/api/trading/mode', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode: tradingMode }),
      });

      const response = await fetch('/api/trading/order?x_user_id=default_user', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          symbol: activeTradeSymbol,
          side,
          quantity: effectiveOrderQuantity,
          product: 'INTRADAY',
          type: orderType,
          price: orderType === 'MARKET' ? 0 : orderPrice,
          trigger_price: (orderType === 'SL' || orderType === 'SL-M') ? orderTrigger : 0,
          tag: terminalView === 'options' ? 'terminal-options' : 'terminal-live',
          instrument_type: activeTradeSymbol.endsWith('PE') ? 'PE' : activeTradeSymbol.endsWith('CE') ? 'CE' : 'EQ',
          is_live_confirmation_ack: isLiveConfirmationAck,
          risk_override_reason: riskOverrideReason.trim() || null,
        }),
      });

      const data = await response.json();
      if (!response.ok) {
        throw new Error(data?.detail || 'Failed to place live order');
      }
      setOrderMessage(`${tradingMode} ${side} ${data.status || 'submitted'} (${data.order_id || data.id || 'ok'})`);
    } catch (err) {
      setOrderMessage(err instanceof Error ? err.message : 'Order failed');
    } finally {
      setOrderBusy(false);
    }
  };

  const squarePosition = async (row: PositionItem) => {
    try {
      setOrderMessage(null);
      const response = await fetch('/api/trading/positions/square', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          symbol: row.symbol,
          mode: row.mode || 'PAPER',
        }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload?.detail || 'Failed to square position');
      }
      setOrderMessage(`${row.mode || 'PAPER'} square submitted for ${row.symbol}`);
      setPositions((prev) => prev.filter((p) => !(p.symbol === row.symbol && p.mode === row.mode)));
    } catch (error) {
      setOrderMessage(error instanceof Error ? error.message : 'Failed to square position');
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
                {currentSymbol?.symbol && (
                  <button
                    type="button"
                    onClick={() => openTradingView(currentSymbol.symbol)}
                    className="text-foreground-muted hover:text-foreground"
                    title="Open on TradingView"
                    aria-label={`Open ${currentSymbol.symbol} on TradingView`}
                  >
                    <ExternalLink className="h-4 w-4" />
                  </button>
                )}
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
                      onOpenTradingView={openTradingView}
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
              <Button
                variant={terminalView === 'options' ? 'secondary' : 'ghost'}
                size="sm"
                className="h-7 gap-1 text-xs"
                onClick={() => setTerminalView('options')}
              >
                <CandlestickChart className="w-4 h-4" /> Option Chain
              </Button>
              <Button
                variant={terminalView === 'positions' ? 'secondary' : 'ghost'}
                size="sm"
                className="h-7 gap-1 text-xs"
                onClick={() => setTerminalView('positions')}
              >
                <ListOrdered className="w-4 h-4" /> Positions
              </Button>
            </div>
            <div className="flex items-center gap-2">
              {terminalView === 'options' && optionsBoard && (
                <span className="text-xs text-foreground-muted">
                  {optionsBoard.underlying} Spot: {roundToDecimals(optionsBoard.spot_price, 2).toFixed(2)}
                </span>
              )}
              <Button variant="profit" size="sm" className="h-7 text-xs px-4">Buy</Button>
              <Button variant="loss" size="sm" className="h-7 text-xs px-4">Sell</Button>
            </div>
          </div>

          {/* Chart / Options Board */}
          <div className="flex-1 flex items-center justify-center">
            {terminalView === 'options' && optionsLoading && !optionsBoard ? (
              <div className="text-center">
                <Loader2 className="w-8 h-8 animate-spin mx-auto mb-2 text-foreground-muted" />
                <p className="text-sm text-foreground-muted">Loading options board...</p>
              </div>
            ) : terminalView === 'options' && optionsError && !optionsBoard ? (
              <div className="text-center">
                <p className="text-sm text-loss">{optionsError}</p>
              </div>
            ) : terminalView === 'options' && !optionsBoard ? (
              <div className="text-center">
                <p className="text-sm text-foreground-muted">No options data</p>
              </div>
            ) : terminalView === 'options' ? (
              <div className="h-full w-full overflow-auto p-2">
                <div className="mb-2 flex items-center gap-2">
                  <select
                    value={optionsUnderlying}
                    onChange={(event) => setOptionsUnderlying(event.target.value)}
                    className="h-8 rounded border border-border bg-background px-2 text-xs"
                  >
                    <option value="NIFTY">NIFTY</option>
                    <option value="BANKNIFTY">BANKNIFTY</option>
                    <option value="FINNIFTY">FINNIFTY</option>
                  </select>
                  <input
                    type="date"
                    value={optionsExpiry}
                    onChange={(event) => setOptionsExpiry(event.target.value)}
                    className="h-8 rounded border border-border bg-background px-2 text-xs"
                  />
                  {optionsOrderflow && (
                    <div className="text-xs text-foreground-muted">
                      PCR(OI): {optionsOrderflow.pcr_oi ?? '--'} | PCR(Vol): {optionsOrderflow.pcr_volume ?? '--'}
                    </div>
                  )}
                </div>
                <table className="w-full text-xs">
                  <thead className="border-b border-border text-foreground-muted">
                    <tr>
                      <th className="py-1 text-left font-normal">CE</th>
                      <th className="py-1 text-right font-normal">CE LTP</th>
                      <th className="py-1 text-right font-normal">Strike</th>
                      <th className="py-1 text-right font-normal">PE LTP</th>
                      <th className="py-1 text-left font-normal">PE</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(optionsBoard?.strikes ?? []).map((row) => (
                      <tr key={row.strike} className="border-b border-border/60">
                        <td className="py-1">
                          <button
                            type="button"
                            className="text-left hover:text-profit"
                            onClick={() => {
                              if (row.ce?.symbol) {
                                setSelectedOptionSymbol(row.ce.symbol);
                              }
                            }}
                          >
                            {row.ce?.symbol || '--'}
                          </button>
                        </td>
                        <td className="py-1 text-right tabular-nums">{typeof row.ce?.ltp === 'number' ? row.ce.ltp.toFixed(2) : '--'}</td>
                        <td className={`py-1 text-right tabular-nums ${row.strike === optionsBoard?.atm_strike ? 'text-foreground font-semibold' : 'text-foreground-muted'}`}>{row.strike.toFixed(2)}</td>
                        <td className="py-1 text-right tabular-nums">{typeof row.pe?.ltp === 'number' ? row.pe.ltp.toFixed(2) : '--'}</td>
                        <td className="py-1 text-left">
                          <button
                            type="button"
                            className="text-left hover:text-loss"
                            onClick={() => {
                              if (row.pe?.symbol) {
                                setSelectedOptionSymbol(row.pe.symbol);
                              }
                            }}
                          >
                            {row.pe?.symbol || '--'}
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : terminalView === 'positions' ? (
              <div className="h-full w-full overflow-auto p-2">
                <div className="mb-2 flex items-center gap-4 text-xs">
                  <span className="text-foreground-muted">
                    Live Net P&L:{' '}
                    <span className={positionsSummary.net_pnl_live >= 0 ? 'text-profit tabular-nums' : 'text-loss tabular-nums'}>
                      {positionsSummary.net_pnl_live.toFixed(2)}
                    </span>
                  </span>
                  <span className="text-foreground-muted">
                    Paper Net P&L:{' '}
                    <span className={positionsSummary.net_pnl_paper >= 0 ? 'text-profit tabular-nums' : 'text-loss tabular-nums'}>
                      {positionsSummary.net_pnl_paper.toFixed(2)}
                    </span>
                  </span>
                  <span className="text-foreground-muted">
                    Total:{' '}
                    <span className={positionsSummary.net_pnl_total >= 0 ? 'text-profit tabular-nums' : 'text-loss tabular-nums'}>
                      {positionsSummary.net_pnl_total.toFixed(2)}
                    </span>
                  </span>
                </div>
                {positionsError && <div className="mb-2 text-xs text-loss">{positionsError}</div>}
                <table className="w-full text-xs">
                  <thead className="sticky top-0 bg-surface border-b border-border text-foreground-muted">
                    <tr>
                      <th className="py-1 text-left font-normal">Symbol</th>
                      <th className="py-1 text-left font-normal">Mode</th>
                      <th className="py-1 text-left font-normal">Side</th>
                      <th className="py-1 text-right font-normal">Qty</th>
                      <th className="py-1 text-right font-normal">Entry</th>
                      <th className="py-1 text-right font-normal">LTP</th>
                      <th className="py-1 text-right font-normal">Unrealized</th>
                      <th className="py-1 text-right font-normal">Net P&L</th>
                      <th className="py-1 text-right font-normal">Action</th>
                    </tr>
                  </thead>
                  <tbody>
                    {positions.map((pos) => (
                      <tr key={`${pos.mode}-${pos.id}`} className="border-b border-border/60">
                        <td className="py-1 pr-2">{pos.symbol}</td>
                        <td className="py-1 pr-2">{pos.mode || 'LIVE'}</td>
                        <td className="py-1 pr-2">{pos.net_qty >= 0 ? 'LONG' : 'SHORT'}</td>
                        <td className="py-1 pr-2 text-right tabular-nums">{Math.abs(pos.net_qty)}</td>
                        <td className="py-1 pr-2 text-right tabular-nums">{Number(pos.entry_price || 0).toFixed(2)}</td>
                        <td className="py-1 pr-2 text-right tabular-nums">{Number(pos.current_price || 0).toFixed(2)}</td>
                        <td className={`py-1 pr-2 text-right tabular-nums ${Number(pos.unrealized_pnl || 0) >= 0 ? 'text-profit' : 'text-loss'}`}>
                          {Number(pos.unrealized_pnl || 0).toFixed(2)}
                        </td>
                        <td className={`py-1 pr-2 text-right tabular-nums ${Number(pos.net_pnl || 0) >= 0 ? 'text-profit' : 'text-loss'}`}>
                          {Number(pos.net_pnl || 0).toFixed(2)}
                        </td>
                        <td className="py-1 pr-2 text-right">
                          <Button
                            size="sm"
                            variant="secondary"
                            className="h-6 px-2 text-xs"
                            onClick={() => void squarePosition(pos)}
                          >
                            Square
                          </Button>
                        </td>
                      </tr>
                    ))}
                    {positions.length === 0 && (
                      <tr>
                        <td colSpan={10} className="py-8 text-center text-foreground-muted">No open positions</td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            ) : (
              <div className="text-center text-sm text-foreground-muted">Select a view</div>
            )}
          </div>
        </div>

        {/* Right Sidebar - Order Panel */}
        <div className="w-56 flex-shrink-0 p-3 border-l border-border bg-surface">
          <div className="mb-2 text-xs text-foreground-muted">
            Trade Symbol: <span className="tabular-nums text-foreground">{activeTradeSymbol || '--'}</span>
          </div>
          {terminalView === 'options' && isOptionSymbol && (
            <div className="mb-2 text-xs text-foreground-muted">
              Lot Size: <span className="tabular-nums text-foreground">{optionLotSize}</span> | Lots: <span className="tabular-nums text-foreground">{optionLots}</span> | Qty: <span className="tabular-nums text-foreground">{effectiveOrderQuantity}</span>
            </div>
          )}
          <div className="flex items-center gap-2 mb-3">
            <Button variant="profit" className="flex-1 h-8 text-sm" disabled={orderBusy} onClick={() => void placeOrder('BUY')}>Buy</Button>
            <Button variant="loss" className="flex-1 h-8 text-sm" disabled={orderBusy} onClick={() => void placeOrder('SELL')}>Sell</Button>
          </div>

          <div className="space-y-3">
            <div>
              <label htmlFor="order-type" className="text-xs text-foreground-muted mb-1 block">Order Type</label>
              <select
                id="order-type"
                value={orderType}
                onChange={(event) => setOrderType(event.target.value as OrderType)}
                className="w-full h-8 px-2 rounded text-sm border border-border bg-background outline-none"
              >
                <option value="MARKET">Market</option>
                <option value="LIMIT">Limit</option>
                <option value="SL">Stop Loss</option>
                <option value="SL-M">Stop Loss Market</option>
              </select>
            </div>
            {terminalView === 'options' && isOptionSymbol ? (
              <div>
                <label htmlFor="order-lots" className="text-xs text-foreground-muted mb-1 block">Lots</label>
                <Input
                  id="order-lots"
                  type="number"
                  min={1}
                  value={optionLots}
                  onChange={(event) => setOptionLots(Math.max(1, Number(event.target.value) || 1))}
                  placeholder="Lots"
                  className="h-8 text-sm"
                />
              </div>
            ) : (
              <div>
                <label htmlFor="order-qty" className="text-xs text-foreground-muted mb-1 block">Quantity</label>
                <Input
                  id="order-qty"
                  type="number"
                  min={1}
                  value={orderQty}
                  onChange={(event) => setOrderQty(Math.max(1, Number(event.target.value) || 1))}
                  placeholder="Qty"
                  className="h-8 text-sm"
                />
              </div>
            )}
            <div>
              <label htmlFor="order-price" className="text-xs text-foreground-muted mb-1 block">Price</label>
              <Input
                id="order-price"
                type="number"
                value={orderPrice}
                disabled={orderType === 'MARKET'}
                onChange={(event) => setOrderPrice(Number(event.target.value) || 0)}
                placeholder="Price"
                className="h-8 text-sm"
              />
            </div>
            {(orderType === 'SL' || orderType === 'SL-M') && (
              <div>
                <label htmlFor="order-trigger" className="text-xs text-foreground-muted mb-1 block">Trigger</label>
                <Input
                  id="order-trigger"
                  type="number"
                  value={orderTrigger}
                  onChange={(event) => setOrderTrigger(Number(event.target.value) || 0)}
                  placeholder="Trigger"
                  className="h-8 text-sm"
                />
              </div>
            )}
            {tradingMode === 'LIVE' && (
              <div>
                <label htmlFor="risk-override-reason" className="text-xs text-foreground-muted mb-1 block">Risk Override Reason (if warning)</label>
                <Input
                  id="risk-override-reason"
                  type="text"
                  value={riskOverrideReason}
                  onChange={(event) => setRiskOverrideReason(event.target.value)}
                  placeholder="Optional unless risk warning"
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
