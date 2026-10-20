import { useState, useMemo, useCallback, memo, type ReactNode } from "react";
import { useNavigate } from "react-router";
import {
  Search,
  TrendingUp,
  TrendingDown,
  Loader2,
  ArrowUpDown,
  ArrowUp,
  ArrowDown,
  ExternalLink,
} from "lucide-react";
import {
  useLiveQuotePoll,
  useScreenerIndices,
  useScreenerResults,
  type ScreenerIndex,
  type ScreenerRow,
} from "@/lib/queries/screener";
import { useConnectionStatus, useQuotes } from "@/lib/realtime/hooks";
import type { Quote, QuoteMap } from "@/lib/realtime/quote-store";
import { debounce } from "@/lib/debounce";
import { Badge, Button } from "@/components/ui";
import {
  formatCompactNumber,
  formatCrores,
  formatSignedPercent,
  roundToDecimals,
} from "@/lib/format";
import { getTradingViewUrl } from "@/lib/tradingview";

type ScreenerResult = ScreenerRow;
type IndexItem = ScreenerIndex;

type SortField = "symbol" | "price" | "change" | "volume" | "marketCap" | "rsi" | "macd" | "adx";
type SortDirection = "asc" | "desc";
type FlashDirection = "up" | "down";
type FlashState = { direction: FlashDirection; phase: 0 | 1 };

const RPP_OPTIONS = [25, 50, 100];
const NO_ROWS: ScreenerResult[] = [];
const NO_QUOTES: QuoteMap = {};

function toNumber(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function toSafeString(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function getFlashClassName(flash?: FlashState): string | undefined {
  if (!flash) return undefined;
  if (flash.direction === "up")
    return flash.phase === 0 ? "screener-flash-up-a" : "screener-flash-up-b";
  return flash.phase === 0 ? "screener-flash-down-a" : "screener-flash-down-b";
}

/**
 * Flash class for a displayed value: when the value changes between renders the cell glows up
 * or down. The phase alternates so back-to-back changes restart the CSS animation; the class can
 * stay on afterwards because the animation does not repeat.
 */
function useFlash(value: number): string | undefined {
  const [seen, setSeen] = useState<{ value: number; flash?: FlashState }>({ value });
  if (seen.value === value) return getFlashClassName(seen.flash);
  const flash: FlashState = {
    direction: value > seen.value ? "up" : "down",
    phase: seen.flash?.phase === 0 ? 1 : 0,
  };
  // Storing information from the previous render (adjusting state during render, not in an effect).
  setSeen({ value, flash });
  return getFlashClassName(flash);
}

function getRsiClassName(rsi: number): string {
  if (rsi > 70) {
    return "text-loss";
  }
  if (rsi < 30) {
    return "text-profit";
  }
  return "";
}

function getMacdClassName(macd: number): string {
  if (macd > 0) {
    return "text-profit";
  }
  if (macd < 0) {
    return "text-loss";
  }
  return "text-foreground-muted";
}

function getResultCountContent(
  isIndicesLoading: boolean,
  isLoading: boolean,
  totalResults: number,
): ReactNode {
  if (isIndicesLoading) {
    return "Loading universes...";
  }
  if (isLoading) {
    return <Loader2 className="w-4 h-4 animate-spin inline" />;
  }
  return `${totalResults} stocks`;
}

function getEmptyStateMessage(indices: IndexItem[], error: string | null): string {
  if (indices.length === 0) {
    return "No universes available";
  }
  if (error) {
    return "Failed to load screener data";
  }
  return "No results found";
}

/** A server row with live values laid over it, field by field: `first`, then `second`. */
function withLiveQuote(
  row: ScreenerResult,
  first: Quote | undefined,
  second: Quote | undefined,
): ScreenerResult {
  return {
    ...row,
    price: roundToDecimals(first?.ltp ?? second?.ltp ?? row.price, 2),
    change: roundToDecimals(first?.changePct ?? second?.changePct ?? row.change, 2),
    volume: first?.volume ?? second?.volume ?? row.volume,
  };
}

/** The symbols the socket has not streamed a quote for. */
function unstreamedSymbols(symbols: string[], streamed: QuoteMap): string[] {
  return symbols.filter((symbol) => streamed[symbol] === undefined);
}

function openTradingView(symbol: string) {
  window.open(getTradingViewUrl(symbol), "_blank", "noopener,noreferrer");
}

const SortIcon = memo(function SortIcon({
  field,
  sortField,
  sortDirection,
}: {
  field: SortField;
  sortField: SortField;
  sortDirection: SortDirection;
}) {
  if (sortField !== field) {
    return <ArrowUpDown className="w-3 h-3 opacity-30" />;
  }
  return sortDirection === "asc" ? (
    <ArrowUp className="w-3 h-3" />
  ) : (
    <ArrowDown className="w-3 h-3" />
  );
});

function sortIndicesForUi(indices: IndexItem[]): IndexItem[] {
  return [...indices].sort((a, b) => {
    if (a.id === "NIFTY50") return -1;
    if (b.id === "NIFTY50") return 1;
    return a.name.localeCompare(b.name);
  });
}

function resolveSelectedUniverse(current: string, indices: IndexItem[]): string {
  if (indices.length === 0) return "";
  return indices.some((index) => index.id === current) ? current : indices[0].id;
}

const StockRow = memo(function StockRow({
  row,
  live,
  fallback,
  onOpen,
}: {
  row: ScreenerResult;
  /** The quote that wins; quotes are stable references until they change. */
  live: Quote | undefined;
  /** Fills the fields `live` lacks. */
  fallback: Quote | undefined;
  onOpen: (symbol: string) => void;
}) {
  const stock = withLiveQuote(row, live, fallback);
  const priceFlash = useFlash(toNumber(stock.price));
  const changeFlash = useFlash(toNumber(stock.change));
  const volumeFlash = useFlash(toNumber(stock.volume));
  const isUp = toNumber(stock.change) >= 0;
  const rsi = toNumber(stock.rsi);
  const macd = toNumber(stock.macd);
  const rsiClassName = getRsiClassName(rsi);
  const macdClassName = getMacdClassName(macd);

  return (
    <tr
      onClick={() => onOpen(stock.symbol)}
      className="cursor-pointer hover:bg-surface transition-colors border-b border-border"
    >
      <td className="py-2 pl-4">
        <div className="flex items-center gap-1">
          <div className="font-medium">{stock.symbol}</div>
          <button
            type="button"
            onClick={(event) => {
              event.stopPropagation();
              openTradingView(stock.symbol);
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
        <span className={priceFlash}>{toNumber(stock.price).toFixed(2)}</span>
      </td>
      <td className={`py-2 text-right tabular-nums ${isUp ? "text-profit" : "text-loss"}`}>
        <div className="flex items-center justify-end gap-1">
          {isUp ? <TrendingUp className="w-3 h-3" /> : <TrendingDown className="w-3 h-3" />}
          <span className={changeFlash}>{formatSignedPercent(toNumber(stock.change))}</span>
        </div>
      </td>
      <td className="py-2 text-right tabular-nums text-foreground-muted">
        <span className={volumeFlash}>{formatCompactNumber(toNumber(stock.volume))}</span>
      </td>
      <td className="py-2 text-right tabular-nums text-foreground-muted">
        {formatCrores(toNumber(stock.marketCap))}
      </td>
      <td className={`py-2 text-right tabular-nums ${rsiClassName}`}>
        {rsi > 0 ? rsi.toFixed(1) : "--"}
      </td>
      <td className={`py-2 pr-4 text-right tabular-nums ${macdClassName}`}>
        {macd !== 0 ? macd.toFixed(2) : "--"}
      </td>
    </tr>
  );
});

export default function ScreenerPage() {
  const navigate = useNavigate();
  const connected = useConnectionStatus() === "connected";

  const [universeChoice, setUniverseChoice] = useState("NIFTY50");
  const [searchQuery, setSearchQuery] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");

  const [currentPage, setCurrentPage] = useState(1);
  const [itemsPerPage, setItemsPerPage] = useState(25);
  const [sortField, setSortField] = useState<SortField>("symbol");
  const [sortDirection, setSortDirection] = useState<SortDirection>("asc");

  const debouncedSetQuery = useMemo(
    () =>
      debounce((value: unknown) => {
        const nextQuery = toSafeString(value);
        setDebouncedQuery(nextQuery);
        setCurrentPage(1);
      }, 300),
    [],
  );

  const indicesQuery = useScreenerIndices();
  const indices = useMemo(() => sortIndicesForUi(indicesQuery.data ?? []), [indicesQuery.data]);
  const isIndicesLoading = indicesQuery.isPending;
  const indicesError = indicesQuery.error?.message ?? null;
  // Until the universe list arrives, query the user's initial choice directly.
  const selectedUniverse = indicesQuery.data
    ? resolveSelectedUniverse(universeChoice, indices)
    : universeChoice;

  const resultsQuery = useScreenerResults({
    universe: selectedUniverse,
    page: currentPage,
    limit: itemsPerPage,
    sortBy: sortField,
    sortOrder: sortDirection,
    query: debouncedQuery,
  });
  const isLoading = resultsQuery.isFetching;
  const error = resultsQuery.error?.message ?? null;
  const totalResults = resultsQuery.data?.total ?? 0;
  const results = resultsQuery.data?.rows ?? NO_ROWS;

  // Live values for the rendered rows: the store subscribes exactly these symbols and drops
  // them when the rows change or the page unmounts. Symbols the socket is not streaming fall
  // back to REST polling.
  const visibleSymbols = useMemo(() => results.map((row) => row.symbol), [results]);
  const streamedQuotes = useQuotes(visibleSymbols);
  const polledQuotes =
    useLiveQuotePoll(connected ? unstreamedSymbols(visibleSymbols, streamedQuotes) : visibleSymbols)
      .data ?? NO_QUOTES;
  // While connected the stream wins; while down the store stops updating, so the poll does.
  const [liveQuotes, fallbackQuotes] = connected
    ? [streamedQuotes, polledQuotes]
    : [polledQuotes, streamedQuotes];

  const totalPages = Math.max(1, Math.ceil(totalResults / itemsPerPage));

  // Clamp the page when the result set shrinks (adjusting state during render, not in an effect).
  if (currentPage > totalPages) {
    setCurrentPage(totalPages);
  }

  const openTerminal = useCallback(
    (symbol: string) => void navigate(`/terminal?symbol=${encodeURIComponent(symbol)}`),
    [navigate],
  );

  const handleSort = useCallback(
    (field: SortField) => {
      setCurrentPage(1);
      if (sortField === field) {
        setSortDirection((prev) => (prev === "asc" ? "desc" : "asc"));
      } else {
        setSortField(field);
        setSortDirection(field === "symbol" ? "asc" : "desc");
      }
    },
    [sortField],
  );

  const resultCountContent = getResultCountContent(isIndicesLoading, isLoading, totalResults);
  const emptyStateMessage = getEmptyStateMessage(indices, error);

  return (
    <div className="flex flex-col h-screen">
      <header className="flex h-12 shrink-0 items-center justify-between border-b border-border bg-surface px-4">
        <div className="flex items-center gap-3">
          <h1 className="text-xl font-semibold text-foreground">Stock Screener</h1>
          {connected && (
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
              setUniverseChoice(event.target.value);
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

          <span className="text-sm text-foreground-muted">{resultCountContent}</span>
        </div>
      </header>

      <div className="flex-1 overflow-auto">
        {indicesError && (
          <div className="px-4 py-2 text-sm text-loss border-b border-border">{indicesError}</div>
        )}
        {error && <div className="px-4 py-2 text-sm text-loss border-b border-border">{error}</div>}

        <table className="w-full">
          <thead className="sticky top-0 bg-surface border-b border-border text-xs">
            <tr>
              <th
                className="py-2 pl-4 text-left font-normal cursor-pointer hover:text-foreground"
                onClick={() => handleSort("symbol")}
              >
                <div className="flex items-center gap-1">
                  Symbol{" "}
                  <SortIcon field="symbol" sortField={sortField} sortDirection={sortDirection} />
                </div>
              </th>
              <th className="py-2 text-left font-normal">Sector</th>
              <th
                className="py-2 text-right font-normal cursor-pointer hover:text-foreground"
                onClick={() => handleSort("price")}
              >
                <div className="flex items-center justify-end gap-1">
                  Price{" "}
                  <SortIcon field="price" sortField={sortField} sortDirection={sortDirection} />
                </div>
              </th>
              <th
                className="py-2 text-right font-normal cursor-pointer hover:text-foreground"
                onClick={() => handleSort("change")}
              >
                <div className="flex items-center justify-end gap-1">
                  Change{" "}
                  <SortIcon field="change" sortField={sortField} sortDirection={sortDirection} />
                </div>
              </th>
              <th
                className="py-2 text-right font-normal cursor-pointer hover:text-foreground"
                onClick={() => handleSort("volume")}
              >
                <div className="flex items-center justify-end gap-1">
                  Volume{" "}
                  <SortIcon field="volume" sortField={sortField} sortDirection={sortDirection} />
                </div>
              </th>
              <th
                className="py-2 text-right font-normal cursor-pointer hover:text-foreground"
                onClick={() => handleSort("marketCap")}
              >
                <div className="flex items-center justify-end gap-1">
                  Mkt Cap{" "}
                  <SortIcon field="marketCap" sortField={sortField} sortDirection={sortDirection} />
                </div>
              </th>
              <th
                className="py-2 text-right font-normal cursor-pointer hover:text-foreground"
                onClick={() => handleSort("rsi")}
              >
                <div className="flex items-center justify-end gap-1">
                  RSI <SortIcon field="rsi" sortField={sortField} sortDirection={sortDirection} />
                </div>
              </th>
              <th
                className="py-2 pr-4 text-right font-normal cursor-pointer hover:text-foreground"
                onClick={() => handleSort("macd")}
              >
                <div className="flex items-center justify-end gap-1">
                  MACD <SortIcon field="macd" sortField={sortField} sortDirection={sortDirection} />
                </div>
              </th>
            </tr>
          </thead>
          <tbody className="text-sm">
            {results.map((row) => (
              <StockRow
                key={row.symbol}
                row={row}
                live={liveQuotes[row.symbol]}
                fallback={fallbackQuotes[row.symbol]}
                onOpen={openTerminal}
              />
            ))}
          </tbody>
        </table>

        {results.length === 0 && !isLoading && (
          <div className="p-8 text-center text-foreground-muted">{emptyStateMessage}</div>
        )}
      </div>

      {totalPages > 1 && (
        <div className="flex items-center justify-between px-4 py-2 border-t border-border bg-surface">
          <span className="text-sm text-foreground-muted">
            Showing {(currentPage - 1) * itemsPerPage + 1}-
            {Math.min(currentPage * itemsPerPage, totalResults)} of {totalResults}
          </span>
          <div className="flex items-center gap-2">
            <Button
              onClick={() => setCurrentPage((page) => Math.max(1, page - 1))}
              disabled={currentPage === 1}
              variant="secondary"
              size="sm"
            >
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
