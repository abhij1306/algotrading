'use client';

import { useState, useEffect, useMemo, useCallback, memo, useRef } from 'react';
import { useRouter } from 'next/navigation';
import {
  Search,
  TrendingUp,
  TrendingDown,
  Loader2,
  ArrowUpDown,
  ArrowUp,
  ArrowDown,
  ExternalLink,
} from 'lucide-react';
import { useWebSocket } from '@/hooks/useWebSocket';
import { screenerAPI, apiClient } from '@/lib/api-client';
import { debounce } from '@/lib/debounce';
import { Badge, Button } from '@/components/ui';
import { formatPercentage, roundToDecimals } from '@/lib/utils';
import { getTradingViewUrl } from '@/lib/tradingview';

interface ScreenerResult {
  symbol: string;
  name: string;
  sector: string;
  marketCap: number;
  change: number;
  price: number;
  volume: number;
  rsi: number;
  macd: number;
  adx: number;
}

interface IndexItem {
  id: string;
  name: string;
  count: number;
}

interface TickData {
  symbol?: string;
  ltp?: number;
  change_pct?: number;
  volume?: number;
  lp?: number;
  chp?: number;
  v?: number;
  vol_traded_today?: number;
}

type SortField = 'symbol' | 'price' | 'change' | 'volume' | 'marketCap' | 'rsi' | 'macd' | 'adx';
type SortDirection = 'asc' | 'desc';
type FlashDirection = 'up' | 'down';
type FlashField = 'price' | 'change' | 'volume';
type FlashState = { direction: FlashDirection; phase: 0 | 1 };

const RPP_OPTIONS = [25, 50, 100];
const TICK_FLUSH_MS = 200;
const FALLBACK_POLL_MS = 3000;
const WS_STALE_MS = 5000;
const FLASH_DURATION_MS = 600;
const MAX_FALLBACK_SYMBOLS = 100;

function toNumber(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0;
}

function toSafeString(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback;
}

function formatVolume(value: number): string {
  if (value >= 10000000) return `${(value / 10000000).toFixed(1)}Cr`;
  if (value >= 100000) return `${(value / 100000).toFixed(1)}L`;
  if (value >= 1000) return `${(value / 1000).toFixed(1)}K`;
  return Math.round(value).toLocaleString('en-IN');
}

function formatMarketCap(value: number): string {
  if (value >= 100000) return `${(value / 100000).toFixed(0)}L Cr`;
  if (value >= 1000) return `${(value / 1000).toFixed(0)}K Cr`;
  return `${Math.round(value)} Cr`;
}

function compareBySort(a: ScreenerResult, b: ScreenerResult, field: SortField, direction: SortDirection): number {
  let cmp = 0;
  if (field === 'symbol') {
    cmp = a.symbol.localeCompare(b.symbol);
  } else {
    cmp = toNumber(a[field]) - toNumber(b[field]);
  }

  if (cmp === 0) {
    cmp = a.symbol.localeCompare(b.symbol);
  }
  return direction === 'asc' ? cmp : -cmp;
}

function getFlashClassName(flash?: FlashState): string | undefined {
  if (!flash) return undefined;
  if (flash.direction === 'up') return flash.phase === 0 ? 'screener-flash-up-a' : 'screener-flash-up-b';
  return flash.phase === 0 ? 'screener-flash-down-a' : 'screener-flash-down-b';
}

const StockRow = memo(function StockRow({
  stock,
  onClick,
  onOpenTradingView,
  flashStateByField,
}: {
  stock: ScreenerResult;
  onClick: () => void;
  onOpenTradingView: (symbol: string) => void;
  flashStateByField: Partial<Record<FlashField, FlashState>>;
}) {
  const isUp = toNumber(stock.change) >= 0;
  const rsi = toNumber(stock.rsi);
  const macd = toNumber(stock.macd);

  return (
    <tr onClick={onClick} className="cursor-pointer hover:bg-surface transition-colors border-b border-border">
      <td className="py-2 pl-4">
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
        <div className="text-xs text-foreground-muted truncate max-w-[120px]">{stock.name}</div>
      </td>
      <td className="py-2">
        <span className="text-xs px-1.5 py-0.5 rounded bg-background-tertiary">{stock.sector}</span>
      </td>
      <td className="py-2 text-right tabular-nums">
        <span className={getFlashClassName(flashStateByField.price)}>{toNumber(stock.price).toFixed(2)}</span>
      </td>
      <td className={`py-2 text-right tabular-nums ${isUp ? 'text-profit' : 'text-loss'}`}>
        <div className="flex items-center justify-end gap-1">
          {isUp ? <TrendingUp className="w-3 h-3" /> : <TrendingDown className="w-3 h-3" />}
          <span className={getFlashClassName(flashStateByField.change)}>{formatPercentage(toNumber(stock.change))}</span>
        </div>
      </td>
      <td className="py-2 text-right tabular-nums text-foreground-muted">
        <span className={getFlashClassName(flashStateByField.volume)}>{formatVolume(toNumber(stock.volume))}</span>
      </td>
      <td className="py-2 text-right tabular-nums text-foreground-muted">{formatMarketCap(toNumber(stock.marketCap))}</td>
      <td className={`py-2 text-right tabular-nums ${rsi > 70 ? 'text-loss' : rsi < 30 ? 'text-profit' : ''}`}>
        {rsi > 0 ? rsi.toFixed(1) : '--'}
      </td>
      <td className={`py-2 pr-4 text-right tabular-nums ${macd > 0 ? 'text-profit' : macd < 0 ? 'text-loss' : 'text-foreground-muted'}`}>
        {macd !== 0 ? macd.toFixed(2) : '--'}
      </td>
    </tr>
  );
});

export default function ScreenerPage() {
  const router = useRouter();
  const { isConnected, sendMessage, registerCallback } = useWebSocket({ skipStateUpdates: true });

  const [indices, setIndices] = useState<IndexItem[]>([]);
  const [selectedUniverse, setSelectedUniverse] = useState('NIFTY50');
  const [indicesError, setIndicesError] = useState<string | null>(null);
  const [isIndicesLoading, setIsIndicesLoading] = useState(true);
  const [searchQuery, setSearchQuery] = useState('');
  const [debouncedQuery, setDebouncedQuery] = useState('');

  const [results, setResults] = useState<ScreenerResult[]>([]);
  const [totalResults, setTotalResults] = useState(0);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [currentPage, setCurrentPage] = useState(1);
  const [itemsPerPage, setItemsPerPage] = useState(25);
  const [sortField, setSortField] = useState<SortField>('symbol');
  const [sortDirection, setSortDirection] = useState<SortDirection>('asc');

  const requestIdRef = useRef(0);
  const subscribedSymbolsKeyRef = useRef('');
  const resultsRef = useRef<ScreenerResult[]>([]);
  const pendingTicksRef = useRef<Record<string, TickData>>({});
  const lastTickAtRef = useRef(0);
  const flashTimeoutsRef = useRef<Record<string, ReturnType<typeof setTimeout>>>({});
  const [flashByCell, setFlashByCell] = useState<Record<string, FlashState>>({});
  const openTradingView = (symbol: string) => {
    window.open(getTradingViewUrl(symbol), '_blank', 'noopener,noreferrer');
  };

  const debouncedSetQuery = useMemo(
    () =>
      debounce((value: unknown) => {
        const nextQuery = toSafeString(value);
        setDebouncedQuery(nextQuery);
        setCurrentPage(1);
      }, 300),
    []
  );

  useEffect(() => {
    let cancelled = false;

    async function loadIndices() {
      setIsIndicesLoading(true);
      setIndicesError(null);
      const response = await screenerAPI.getIndices();
      const indicesPayload = (response.data ?? {}) as { indices?: Array<{ id: string; name: string; count: number }> };
      if (cancelled) return;

      if (response.error || !indicesPayload.indices) {
        setIndices([]);
        setIndicesError(response.error?.message || 'Failed to load universes');
        setIsIndicesLoading(false);
        return;
      }

      const list = indicesPayload.indices.map((item) => ({
        id: item.id,
        name: item.name,
        count: item.count,
      }));

      list.sort((a, b) => {
        if (a.id === 'NIFTY50') return -1;
        if (b.id === 'NIFTY50') return 1;
        return a.name.localeCompare(b.name);
      });

      setIndices(list);
      if (list.length > 0) {
        setSelectedUniverse((current) => (list.some((index) => index.id === current) ? current : list[0].id));
      }
      setIsIndicesLoading(false);
    }

    void loadIndices();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const requestId = ++requestIdRef.current;
    let cancelled = false;

    async function loadResults() {
      if (!selectedUniverse) {
        setResults([]);
        setTotalResults(0);
        setIsLoading(false);
        return;
      }

      setIsLoading(true);
      setError(null);

      const params: Record<string, string> = {
        universe: selectedUniverse,
        full: 'true',
        sort_by: sortField,
        sort_order: sortDirection,
      };

      if (debouncedQuery) params.query = debouncedQuery;

      const response = await screenerAPI.getStocks(params);
      const screenerPayload = (response.data ?? {}) as { results?: ScreenerResult[]; total?: number };
      if (cancelled || requestId !== requestIdRef.current) return;

      if (response.error) {
        setResults([]);
        setTotalResults(0);
        setError(response.error?.message || 'Failed to load screener results');
        setIsLoading(false);
        return;
      }

      const normalizedResults = (screenerPayload.results || []).map((row) => ({
        ...row,
        price: roundToDecimals(row.price, 2),
        change: roundToDecimals(row.change, 2),
      }));
      setResults(normalizedResults);
      resultsRef.current = normalizedResults;
      setTotalResults(normalizedResults.length || screenerPayload.total || 0);
      setIsLoading(false);
    }

    void loadResults();
    return () => {
      cancelled = true;
    };
  }, [selectedUniverse, debouncedQuery, sortField, sortDirection]);

  const totalPages = Math.max(1, Math.ceil(results.length / itemsPerPage));

  const currentPageRows = useMemo(() => {
    const start = (currentPage - 1) * itemsPerPage;
    return results.slice(start, start + itemsPerPage);
  }, [results, currentPage, itemsPerPage]);

  useEffect(() => {
    if (currentPage > totalPages) {
      setCurrentPage(totalPages);
    }
  }, [currentPage, totalPages]);

  const subscribedSymbolsKey = useMemo(() => {
    const symbols = Array.from(new Set(results.map((row) => row.symbol))).sort((a, b) => a.localeCompare(b));
    return symbols.join(',');
  }, [results]);

  const fallbackSymbolsKey = useMemo(() => {
    const symbols = Array.from(new Set(currentPageRows.map((row) => row.symbol))).sort(
      (a, b) => a.localeCompare(b)
    );
    return symbols.join(',');
  }, [currentPageRows]);

  useEffect(() => {
    if (!isConnected) {
      subscribedSymbolsKeyRef.current = '';
    }
  }, [isConnected]);

  useEffect(() => {
    if (!isConnected || !subscribedSymbolsKey) return;
    if (subscribedSymbolsKey === subscribedSymbolsKeyRef.current) return;

    if (subscribedSymbolsKeyRef.current) {
      sendMessage({ action: 'unsubscribe', symbols: subscribedSymbolsKeyRef.current.split(',') });
    }

    sendMessage({ action: 'subscribe', symbols: subscribedSymbolsKey.split(',') });
    subscribedSymbolsKeyRef.current = subscribedSymbolsKey;
  }, [isConnected, subscribedSymbolsKey, sendMessage]);

  useEffect(() => {
    return () => {
      if (!subscribedSymbolsKeyRef.current) return;
      sendMessage({ action: 'unsubscribe', symbols: subscribedSymbolsKeyRef.current.split(',') });
      subscribedSymbolsKeyRef.current = '';
    };
  }, [sendMessage]);

  useEffect(() => {
    return () => {
      Object.values(flashTimeoutsRef.current).forEach((timer) => clearTimeout(timer));
      flashTimeoutsRef.current = {};
    };
  }, []);

  const applyFlashes = useCallback((flashes: Array<{ symbol: string; field: FlashField; direction: FlashDirection }>) => {
    if (flashes.length === 0) return;
    const entries: Record<string, FlashDirection> = {};
    for (const flash of flashes) {
      const key = `${flash.symbol}:${flash.field}`;
      entries[key] = flash.direction;
    }

    setFlashByCell((prev) => {
      // OPTIMIZATION: Check if any values actually changed before creating new object
      const hasChanges = Object.keys(entries).some(key =>
        !prev[key] || prev[key].direction !== entries[key]
      );

      if (!hasChanges) return prev; // Prevent unnecessary re-render

      const next = { ...prev };
      for (const [key, direction] of Object.entries(entries)) {
        const current = prev[key];
        next[key] = {
          direction,
          phase: current ? (current.phase === 0 ? 1 : 0) : 0,
        };
      }
      return next;
    });

    for (const [key] of Object.entries(entries)) {
      const existing = flashTimeoutsRef.current[key];
      if (existing) clearTimeout(existing);

      flashTimeoutsRef.current[key] = setTimeout(() => {
        setFlashByCell((current) => {
          if (!current[key]) return current;
          const updated = { ...current };
          delete updated[key];
          return updated;
        });
        delete flashTimeoutsRef.current[key];
      }, FLASH_DURATION_MS);
    }
  }, []);

  useEffect(() => {
    const unregister = registerCallback((message) => {
      if (message?.type !== 'ticker' || !message.data) return;
      const raw = message.data as TickData;
      if (!raw.symbol) return;

      const normalized: TickData = {
        symbol: raw.symbol,
        ltp: typeof raw.ltp === 'number' ? raw.ltp : (typeof raw.lp === 'number' ? raw.lp : undefined),
        change_pct: typeof raw.change_pct === 'number' ? raw.change_pct : (typeof raw.chp === 'number' ? raw.chp : undefined),
        volume: typeof raw.volume === 'number' ? raw.volume : (typeof raw.v === 'number' ? raw.v : (typeof raw.vol_traded_today === 'number' ? raw.vol_traded_today : undefined)),
      };

      pendingTicksRef.current[raw.symbol] = normalized;
      lastTickAtRef.current = Date.now();
    });

    return unregister;
  }, [registerCallback]);

  useEffect(() => {
    const poll = setInterval(async () => {
      if (!fallbackSymbolsKey) return;

      const now = Date.now();
      const wsStale = !lastTickAtRef.current || (now - lastTickAtRef.current > WS_STALE_MS);
      if (!wsStale) return;

      try {
        const fallbackSymbols = fallbackSymbolsKey.split(',').filter(Boolean);
        if (fallbackSymbols.length === 0 || fallbackSymbols.length > MAX_FALLBACK_SYMBOLS) return;
        const response = await apiClient.get(`/api/market/quotes/live?symbols=${encodeURIComponent(fallbackSymbols.join(','))}`);
        const quoteMap = (response.data ?? {}) as Record<string, Record<string, unknown>>;
        for (const [symbol, quote] of Object.entries(quoteMap)) {
          const ltp = typeof quote.ltp === 'number' ? quote.ltp : undefined;
          const changePct =
            typeof quote.change_pct === 'number'
              ? quote.change_pct
              : (typeof quote.change === 'number' ? quote.change : undefined);
          const volume = typeof quote.volume === 'number' ? quote.volume : undefined;

          if (ltp === undefined && changePct === undefined && volume === undefined) continue;
          pendingTicksRef.current[symbol] = {
            symbol,
            ltp,
            change_pct: changePct,
            volume,
          };
        }
      } catch {
        // Keep silent; websocket will continue trying and poll retries automatically.
      }
    }, FALLBACK_POLL_MS);

    return () => clearInterval(poll);
  }, [fallbackSymbolsKey]);

  useEffect(() => {
    const timer = setInterval(() => {
      const pending = pendingTicksRef.current;
      const symbols = Object.keys(pending);
      if (symbols.length === 0) return;

      pendingTicksRef.current = {};
      const currentResults = resultsRef.current;
      if (!currentResults || currentResults.length === 0) return;

      const flashes: Array<{ symbol: string; field: FlashField; direction: FlashDirection }> = [];
      let nextResults: ScreenerResult[] | null = null;

      for (let i = 0; i < currentResults.length; i++) {
        const row = currentResults[i];
        const tick = pending[row.symbol];
        if (!tick) continue;

        const price = roundToDecimals(tick.ltp ?? row.price, 2);
        const change = roundToDecimals(tick.change_pct ?? row.change, 2);
        const volume = tick.volume ?? row.volume;

        if (price === row.price && change === row.change && volume === row.volume) {
          continue;
        }

        if (!nextResults) nextResults = [...currentResults];

        if (price !== row.price) {
          flashes.push({
            symbol: row.symbol,
            field: 'price',
            direction: price > row.price ? 'up' : 'down',
          });
        }
        if (change !== row.change) {
          flashes.push({
            symbol: row.symbol,
            field: 'change',
            direction: change > row.change ? 'up' : 'down',
          });
        }
        if (volume !== row.volume) {
          flashes.push({
            symbol: row.symbol,
            field: 'volume',
            direction: volume > row.volume ? 'up' : 'down',
          });
        }

        nextResults[i] = { ...row, price, change, volume };
      }

      if (nextResults) {
        // OPTIMIZATION: Only re-sort if the sorted field actually changed
        const sortFieldChanged = flashes.some(f =>
          f.field === sortField && ['change', 'volume', 'price'].includes(sortField)
        );

        if (sortFieldChanged && (sortField === 'change' || sortField === 'volume' || sortField === 'price')) {
          nextResults.sort((a, b) => compareBySort(a, b, sortField, sortDirection));
        }

        resultsRef.current = nextResults;
        setResults(nextResults);
        if (flashes.length > 0) {
          applyFlashes(flashes);
        }
      }
    }, TICK_FLUSH_MS);

    return () => clearInterval(timer);
  }, [applyFlashes, sortField, sortDirection]);

  const handleSort = useCallback(
    (field: SortField) => {
      setCurrentPage(1);
      if (sortField === field) {
        setSortDirection((prev) => (prev === 'asc' ? 'desc' : 'asc'));
      } else {
        setSortField(field);
        setSortDirection(field === 'symbol' ? 'asc' : 'desc');
      }
    },
    [sortField]
  );

  const SortIcon = ({ field }: { field: SortField }) => {
    if (sortField !== field) return <ArrowUpDown className="w-3 h-3 opacity-30" />;
    return sortDirection === 'asc' ? <ArrowUp className="w-3 h-3" /> : <ArrowDown className="w-3 h-3" />;
  };

  return (
    <div className="flex flex-col h-screen">
      <header className="flex h-12 shrink-0 items-center justify-between border-b border-border bg-surface px-4">
        <div className="flex items-center gap-3">
          <h1 className="text-xl font-semibold text-foreground">Stock Screener</h1>
          {isConnected && (
            <Badge variant="profit" pulse>
              LIVE
            </Badge>
          )}
        </div>

        <div className="flex items-center gap-3">
          <div className="flex items-center gap-2 px-3 py-1.5 rounded border border-border bg-background">
            <Search className="w-4 h-4 text-foreground-muted" />
            <input
              type="text"
              value={searchQuery}
              onChange={(event) => {
                setSearchQuery(event.target.value);
                debouncedSetQuery(event.target.value);
              }}
              placeholder="Search symbol..."
              className="bg-transparent outline-none w-32 text-sm"
            />
          </div>

          <select
            value={selectedUniverse}
            onChange={(event) => {
              setSelectedUniverse(event.target.value);
              setCurrentPage(1);
            }}
            disabled={isIndicesLoading || indices.length === 0}
            className="px-3 py-1.5 rounded text-sm border border-border bg-background outline-none"
          >
            {isIndicesLoading && <option value="">Loading universes...</option>}
            {indices.map((index) => (
              <option key={index.id} value={index.id}>
                {index.name}
              </option>
            ))}
          </select>

          <select
            value={itemsPerPage}
            onChange={(event) => {
              setItemsPerPage(Number(event.target.value));
              setCurrentPage(1);
            }}
            className="px-2 py-1.5 rounded text-sm border border-border bg-background outline-none"
          >
            {RPP_OPTIONS.map((rpp) => (
              <option key={rpp} value={rpp}>
                {rpp}/page
              </option>
            ))}
          </select>

          <span className="text-sm text-foreground-muted">
            {isIndicesLoading
              ? 'Loading universes...'
              : isLoading
                ? <Loader2 className="w-4 h-4 animate-spin inline" />
                : `${totalResults} stocks`}
          </span>
        </div>
      </header>

      <div className="flex-1 overflow-auto">
        {indicesError && <div className="px-4 py-2 text-sm text-loss border-b border-border">{indicesError}</div>}
        {error && <div className="px-4 py-2 text-sm text-loss border-b border-border">{error}</div>}

        <table className="w-full">
          <thead className="sticky top-0 bg-surface border-b border-border text-xs">
            <tr>
              <th className="py-2 pl-4 text-left font-normal cursor-pointer hover:text-foreground" onClick={() => handleSort('symbol')}>
                <div className="flex items-center gap-1">
                  Symbol <SortIcon field="symbol" />
                </div>
              </th>
              <th className="py-2 text-left font-normal">Sector</th>
              <th className="py-2 text-right font-normal cursor-pointer hover:text-foreground" onClick={() => handleSort('price')}>
                <div className="flex items-center justify-end gap-1">
                  Price <SortIcon field="price" />
                </div>
              </th>
              <th className="py-2 text-right font-normal cursor-pointer hover:text-foreground" onClick={() => handleSort('change')}>
                <div className="flex items-center justify-end gap-1">
                  Change <SortIcon field="change" />
                </div>
              </th>
              <th className="py-2 text-right font-normal cursor-pointer hover:text-foreground" onClick={() => handleSort('volume')}>
                <div className="flex items-center justify-end gap-1">
                  Volume <SortIcon field="volume" />
                </div>
              </th>
              <th className="py-2 text-right font-normal cursor-pointer hover:text-foreground" onClick={() => handleSort('marketCap')}>
                <div className="flex items-center justify-end gap-1">
                  Mkt Cap <SortIcon field="marketCap" />
                </div>
              </th>
              <th className="py-2 text-right font-normal cursor-pointer hover:text-foreground" onClick={() => handleSort('rsi')}>
                <div className="flex items-center justify-end gap-1">
                  RSI <SortIcon field="rsi" />
                </div>
              </th>
              <th className="py-2 pr-4 text-right font-normal cursor-pointer hover:text-foreground" onClick={() => handleSort('macd')}>
                <div className="flex items-center justify-end gap-1">
                  MACD <SortIcon field="macd" />
                </div>
              </th>
            </tr>
          </thead>
          <tbody className="text-sm">
            {currentPageRows.map((stock) => {
              // OPTIMIZATION: Create stable flash state object to prevent unnecessary re-renders
              const priceKey = `${stock.symbol}:price`;
              const changeKey = `${stock.symbol}:change`;
              const volumeKey = `${stock.symbol}:volume`;

              // Only create object if at least one flash exists
              const hasFlash = flashByCell[priceKey] || flashByCell[changeKey] || flashByCell[volumeKey];
              const flashStateByField: Partial<Record<FlashField, FlashState>> = hasFlash ? {
                price: flashByCell[priceKey],
                change: flashByCell[changeKey],
                volume: flashByCell[volumeKey],
              } : {};

              return (
                <StockRow
                  key={stock.symbol}
                  stock={stock}
                  flashStateByField={flashStateByField}
                  onClick={() => router.push(`/terminal?symbol=${stock.symbol}`)}
                  onOpenTradingView={openTradingView}
                />
              );
            })}
          </tbody>
        </table>

        {currentPageRows.length === 0 && !isLoading && (
          <div className="p-8 text-center text-foreground-muted">
            {indices.length === 0
              ? 'No universes available'
              : error
                ? 'Failed to load screener data'
                : 'No results found'}
          </div>
        )}
      </div>

      {totalPages > 1 && (
        <div className="flex items-center justify-between px-4 py-2 border-t border-border bg-surface">
          <span className="text-sm text-foreground-muted">
            Showing {(currentPage - 1) * itemsPerPage + 1}-{Math.min(currentPage * itemsPerPage, totalResults)} of {totalResults}
          </span>
          <div className="flex items-center gap-2">
            <Button onClick={() => setCurrentPage((page) => Math.max(1, page - 1))} disabled={currentPage === 1} variant="secondary" size="sm">
              Previous
            </Button>
            <span className="text-sm px-2">
              Page {currentPage} of {totalPages}
            </span>
            <Button
              onClick={() => setCurrentPage((page) => Math.min(totalPages, page + 1))}
              disabled={currentPage === totalPages}
              variant="secondary"
              size="sm"
            >
              Next
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
