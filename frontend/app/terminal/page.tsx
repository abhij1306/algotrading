import { useState, useMemo, useCallback, memo } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Search,
  Plus,
  Settings,
  Bell,
  CandlestickChart,
  ListOrdered,
  Loader2,
  ExternalLink,
} from "lucide-react";
import { Button, Input } from "@/components/ui";
import {
  DEFAULT_ORDER_TYPE,
  DEFAULT_TRADING_MODE,
  type OrderType,
  type TradingMode,
} from "./order-defaults";
import { formatPercentage, roundToDecimals } from "@/lib/utils";
import { useConnectionStatus, useQuotes } from "@/lib/realtime/hooks";
import { isMarketOpen } from "@/lib/market-hours";
import { debounce } from "@/lib/debounce";
import { queryKeys } from "@/lib/queries/keys";
import { shouldRetry } from "@/lib/queries/query-client";
import { ApiError, apiErrorFromResponse, apiErrorFromThrown } from "@/lib/api/errors";
import {
  SUBMIT_UNCONFIRMED,
  useAddToWatchlist,
  useOptionsBoard,
  usePositionsBook,
  useSquarePosition,
  useSymbolSearch,
  useTerminalWatchlist,
  type OptionsBoard,
  type OptionsOrderflow,
  type PositionItem,
  type SearchResultItem,
  type WatchlistItem,
} from "@/lib/queries/terminal";
import { getTradingViewUrl } from "@/lib/tradingview";

type TerminalView = "options" | "positions";
type OrderSide = "BUY" | "SELL";

const TIMEFRAMES = ["1m", "5m", "15m", "30m", "1H", "D", "W", "M"];
const OPTION_LOT_SIZES: Record<string, number> = {
  NIFTY: 75,
  BANKNIFTY: 30,
  FINNIFTY: 25,
};

function getChangeTone(change: number | null | undefined): string {
  if (typeof change !== "number") {
    return "text-foreground-muted";
  }
  return change >= 0 ? "text-profit" : "text-loss";
}

function getChangeText(change: number | null | undefined): string {
  return typeof change === "number" ? formatPercentage(change) : "Unavailable";
}

function inferInstrumentType(symbol: string): "PE" | "CE" | "EQ" {
  if (symbol.endsWith("PE")) {
    return "PE";
  }
  if (symbol.endsWith("CE")) {
    return "CE";
  }
  return "EQ";
}

/** The order tag names the flow: the instrument decides, not the terminal view. */
function orderTag(symbol: string): "terminal-equity" | "terminal-options" {
  return inferInstrumentType(symbol) === "EQ" ? "terminal-equity" : "terminal-options";
}

function formatLtp(value: number | null | undefined): string {
  return typeof value === "number" ? value.toFixed(2) : "--";
}

function errorText(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

type Quote = NonNullable<ReturnType<typeof useQuotes>[string]>;

type PositionsSummary = { net_pnl_live: number; net_pnl_paper: number; net_pnl_total: number };

const isPositive = (value: number): boolean => value > 0;

function pnlTone(value: number): string {
  return value >= 0 ? "text-profit" : "text-loss";
}

function watchlistInstrumentType(item: SearchResultItem): string {
  if (item.instrument_type) return item.instrument_type;
  return item.type === "PE" || item.type === "CE" ? item.type : "EQ";
}

function withQuote(item: WatchlistItem, quote: Quote | undefined): WatchlistItem {
  if (!quote) return item;
  const ltp = typeof quote.ltp === "number" ? roundToDecimals(quote.ltp, 2) : item.ltp;
  return {
    ...item,
    ltp,
    price: typeof quote.ltp === "number" ? ltp : item.price,
    changePct:
      typeof quote.changePct === "number" ? roundToDecimals(quote.changePct, 2) : item.changePct,
  };
}

function withQuotePnl(pos: PositionItem, quote: Quote | undefined): PositionItem {
  const ltp = quote?.ltp;
  if (typeof ltp !== "number") return pos;
  const currentPrice = roundToDecimals(ltp, 2);
  const qty = Number(pos.net_qty || 0);
  const unrealized = roundToDecimals((currentPrice - Number(pos.entry_price || 0)) * qty, 2);
  const realized = Number(pos.realized_pnl || 0);
  return {
    ...pos,
    current_price: currentPrice,
    unrealized_pnl: unrealized,
    net_pnl: roundToDecimals(realized + unrealized, 2),
  };
}

function summarizePositions(positions: PositionItem[]): PositionsSummary {
  const sumNet = (rows: PositionItem[]) => rows.reduce((acc, p) => acc + p.net_pnl, 0);
  const live = sumNet(positions.filter((p) => p.mode !== "PAPER"));
  const paper = sumNet(positions.filter((p) => p.mode === "PAPER"));
  return {
    net_pnl_live: roundToDecimals(live, 2),
    net_pnl_paper: roundToDecimals(paper, 2),
    net_pnl_total: roundToDecimals(live + paper, 2),
  };
}

/** A user-facing message when the order inputs are unusable, else null. */
function orderInputError(input: {
  symbol: string;
  quantity: number;
  orderType: OrderType;
  price: number;
  trigger: number;
}): string | null {
  const { symbol, quantity, orderType, price, trigger } = input;
  if (!symbol || quantity <= 0) return "Select symbol and enter valid quantity";
  if (orderType !== "MARKET" && orderType !== "SL-M" && !isPositive(price)) {
    return "Enter a price greater than 0";
  }
  if ((orderType === "SL" || orderType === "SL-M") && !isPositive(trigger)) {
    return "Enter a trigger price greater than 0";
  }
  return null;
}

type OrderInput = {
  mode: TradingMode;
  symbol: string;
  side: OrderSide;
  quantity: number;
  orderType: OrderType;
  price: number;
  trigger: number;
  liveConfirmed: boolean;
  riskOverrideReason: string;
  /** Idempotency key: one per submit intent, reused by every retry of that intent. */
  clientOrderId: string;
};

// Retries reuse the intent's client_order_id, so the backend places the order at most once.
const ORDER_RETRY_DELAY_MS = 250;

async function postOrder(body: string): Promise<Response> {
  try {
    return await fetch("/api/trading/order?x_user_id=default_user", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body,
    });
  } catch (error) {
    throw apiErrorFromThrown(error);
  }
}

/** Submit an order; resolves to the confirmation message, throws on rejection. */
async function submitOrder(input: OrderInput): Promise<string> {
  const { mode, side, orderType } = input;
  const response = await postOrder(
    JSON.stringify({
      mode,
      symbol: input.symbol,
      side,
      quantity: input.quantity,
      product: "INTRADAY",
      type: orderType,
      price: orderType === "MARKET" ? 0 : input.price,
      trigger_price: orderType === "SL" || orderType === "SL-M" ? input.trigger : 0,
      tag: orderTag(input.symbol),
      instrument_type: inferInstrumentType(input.symbol),
      is_live_confirmation_ack: input.liveConfirmed,
      risk_override_reason: input.riskOverrideReason.trim() || null,
      client_order_id: input.clientOrderId,
    }),
  );
  const data = await response.json().catch(() => null);
  if (response.ok && data === null) {
    // The order may have been placed: retry (same client_order_id) to read its state.
    throw new ApiError({
      code: "NETWORK_ERROR",
      message: "Order response was cut off",
      status: response.status,
    });
  }
  if (!response.ok) {
    throw apiErrorFromResponse({
      status: response.status,
      statusText: response.statusText,
      body: data,
    });
  }
  if (data?.status === SUBMIT_UNCONFIRMED) {
    // Not rejected: the order may be live, so the user must not place it again.
    return `${mode} ${side} unconfirmed: ${data.message || "check the broker orderbook"}`;
  }
  const summary = `${mode} ${side} ${data?.status || "submitted"} (${data?.order_id || data?.id || "ok"})`;
  return data?.duplicate ? `${summary} - already placed` : summary;
}

/** Order submission; a filled order can change the positions book, so it is refreshed. */
function useSubmitOrder() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: submitOrder,
    retry: shouldRetry,
    retryDelay: ORDER_RETRY_DELAY_MS,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.terminal.positions() }),
  });
}

function OptionsBoardPanel({
  board: optionsBoard,
  orderflow: optionsOrderflow,
  underlying: optionsUnderlying,
  expiry: optionsExpiry,
  onUnderlyingChange: setOptionsUnderlying,
  onExpiryChange: setOptionsExpiry,
  onSelectOption: setSelectedOptionSymbol,
}: Readonly<{
  board: OptionsBoard;
  orderflow: OptionsOrderflow | null;
  underlying: string;
  expiry: string;
  onUnderlyingChange: (value: string) => void;
  onExpiryChange: (value: string) => void;
  onSelectOption: (symbol: string) => void;
}>) {
  return (
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
            PCR(OI): {optionsOrderflow.pcr_oi ?? "--"} | PCR(Vol):{" "}
            {optionsOrderflow.pcr_volume ?? "--"}
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
          {optionsBoard.strikes.map((row) => (
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
                  {row.ce?.symbol || "--"}
                </button>
              </td>
              <td className="py-1 text-right tabular-nums">{formatLtp(row.ce?.ltp)}</td>
              <td
                className={`py-1 text-right tabular-nums ${
                  row.strike === optionsBoard.atm_strike
                    ? "text-foreground font-semibold"
                    : "text-foreground-muted"
                }`}
              >
                {row.strike.toFixed(2)}
              </td>
              <td className="py-1 text-right tabular-nums">{formatLtp(row.pe?.ltp)}</td>
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
                  {row.pe?.symbol || "--"}
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function PnlValue({ value }: Readonly<{ value: number }>) {
  return <span className={`${pnlTone(value)} tabular-nums`}>{value.toFixed(2)}</span>;
}

function PositionsPanel({
  summary: positionsSummary,
  error: positionsError,
  positions,
  liveStale,
  squaring,
  onSquare: squarePosition,
}: Readonly<{
  summary: PositionsSummary;
  error: string | null;
  positions: PositionItem[];
  /** The broker sync failed, so LIVE positions are unknown or the last synced values. */
  liveStale: boolean;
  /** A square-off is in flight; a second click could send a duplicate exit. */
  squaring: boolean;
  onSquare: (row: PositionItem) => void;
}>) {
  return (
    <div className="h-full w-full overflow-auto p-2">
      <div className="mb-2 flex items-center gap-4 text-xs">
        <span className="text-foreground-muted">
          Live Net P&L: <PnlValue value={positionsSummary.net_pnl_live} />
        </span>
        <span className="text-foreground-muted">
          Paper Net P&L: <PnlValue value={positionsSummary.net_pnl_paper} />
        </span>
        <span className="text-foreground-muted">
          Total: <PnlValue value={positionsSummary.net_pnl_total} />
        </span>
      </div>
      {positionsError && <div className="mb-2 text-xs text-loss">{positionsError}</div>}
      {liveStale && (
        <div className="mb-2 text-xs text-amber-400">
          Broker unavailable: live positions could not be refreshed. Any shown are the last synced
          values and cannot be squared until it reconnects.
        </div>
      )}
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
            <tr
              key={`${pos.mode}-${pos.id}`}
              className={`border-b border-border/60 ${pos.stale || positionsError ? "opacity-50" : ""}`}
            >
              <td className="py-1 pr-2">{pos.symbol}</td>
              <td className="py-1 pr-2">{pos.mode || "LIVE"}</td>
              <td className="py-1 pr-2">{pos.net_qty >= 0 ? "LONG" : "SHORT"}</td>
              <td className="py-1 pr-2 text-right tabular-nums">{Math.abs(pos.net_qty)}</td>
              <td className="py-1 pr-2 text-right tabular-nums">
                {Number(pos.entry_price || 0).toFixed(2)}
              </td>
              <td className="py-1 pr-2 text-right tabular-nums">
                {Number(pos.current_price || 0).toFixed(2)}
              </td>
              <td
                className={`py-1 pr-2 text-right tabular-nums ${Number(pos.unrealized_pnl || 0) >= 0 ? "text-profit" : "text-loss"}`}
              >
                {Number(pos.unrealized_pnl || 0).toFixed(2)}
              </td>
              <td className={`py-1 pr-2 text-right tabular-nums ${pnlTone(pos.net_pnl)}`}>
                {pos.net_pnl.toFixed(2)}
              </td>
              <td className="py-1 pr-2 text-right">
                <Button
                  size="sm"
                  variant="secondary"
                  className="h-6 px-2 text-xs"
                  disabled={squaring || pos.stale}
                  onClick={() => squarePosition(pos)}
                >
                  Square
                </Button>
              </td>
            </tr>
          ))}
          {positions.length === 0 && (
            <tr>
              <td colSpan={9} className="py-8 text-center text-foreground-muted">
                No open positions
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

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
      className={`cursor-pointer transition-colors ${isSelected ? "bg-surface" : "hover:bg-surface/50"}`}
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
          {typeof stock.ltp === "number" ? stock.ltp.toFixed(2) : "--"}
        </div>
        <div className={`tabular-nums text-xs ${getChangeTone(stock.changePct)}`}>
          {getChangeText(stock.changePct)}
        </div>
      </td>
    </tr>
  );
});

const NO_WATCHLIST: WatchlistItem[] = [];
const NO_POSITIONS: PositionItem[] = [];
const NO_SEARCH_RESULTS: SearchResultItem[] = [];

function initialSymbolFromUrl(): string {
  return new URLSearchParams(globalThis.location.search).get("symbol") ?? "";
}

function openTradingView(symbol: string) {
  globalThis.open(getTradingViewUrl(symbol), "_blank", "noopener,noreferrer");
}

/** The selected symbol is always listed, even when it is not in the saved watchlist. */
function listWithSelected(saved: WatchlistItem[], selectedSymbol: string): WatchlistItem[] {
  if (!selectedSymbol || saved.some((item) => item.symbol === selectedSymbol)) return saved;
  return [
    ...saved,
    { symbol: selectedSymbol, name: selectedSymbol, price: null, changePct: null, ltp: null },
  ];
}

/**
 * Saved watchlist plus the selected symbol. The selection is `?symbol=` or the user's pick,
 * and falls back to the first saved row, derived on render rather than synced by an effect.
 */
function useTerminalSymbols() {
  const watchlistQuery = useTerminalWatchlist();
  const [chosenSymbol, setChosenSymbol] = useState(initialSymbolFromUrl);
  const saved = watchlistQuery.data ?? NO_WATCHLIST;
  const selectedSymbol = chosenSymbol || (saved[0]?.symbol ?? "");
  const items = useMemo(() => listWithSelected(saved, selectedSymbol), [saved, selectedSymbol]);
  return {
    items,
    selectedSymbol,
    setSelectedSymbol: setChosenSymbol,
    isLoading: watchlistQuery.isPending,
    error: watchlistQuery.isError ? "Failed to fetch watchlist" : null,
  };
}

/**
 * Options chain state. Until the user picks an expiry the backend chooses the nearest one,
 * and the board's own expiry is shown as the effective expiry.
 */
function useOptionsChain(enabled: boolean) {
  const [underlying, setUnderlying] = useState("NIFTY");
  const [chosenExpiry, setChosenExpiry] = useState("");
  const [selectedOptionSymbol, setSelectedOptionSymbol] = useState("");
  const boardQuery = useOptionsBoard({ underlying, expiry: chosenExpiry, enabled });
  const snapshot = boardQuery.data ?? null;
  return {
    underlying,
    setUnderlying,
    expiry: chosenExpiry || (snapshot?.board.expiry ?? ""),
    setExpiry: setChosenExpiry,
    snapshot,
    isLoading: boardQuery.isFetching,
    error: boardQuery.error ? errorText(boardQuery.error, "Failed to load options board") : null,
    selectedOptionSymbol,
    setSelectedOptionSymbol,
  };
}

type OptionsChain = ReturnType<typeof useOptionsChain>;

/** Live quotes for everything on screen; outside market hours nothing is subscribed. */
function useScreenQuotes(groups: readonly (readonly string[])[]) {
  const symbols = useMemo(() => {
    if (!isMarketOpen().isOpen) return [];
    return Array.from(new Set(groups.flat().filter(Boolean)));
  }, [groups]);
  return useQuotes(symbols);
}

/** Squares a position and reports the outcome through the order ticket message. */
function useSquareAction(onMessage: (message: string | null) => void) {
  const square = useSquarePosition();
  return {
    squaring: square.isPending,
    squarePosition: (row: PositionItem) => {
      onMessage(null);
      // A new click is a new intent; retries of this mutation reuse this id.
      square.mutate(
        { row, clientOrderId: crypto.randomUUID() },
        {
          onSuccess: (result) =>
            onMessage(
              result.unconfirmed
                ? `${row.mode || "PAPER"} square for ${row.symbol} unconfirmed: ${result.message}`
                : `${row.mode || "PAPER"} square submitted for ${row.symbol}`,
            ),
          onError: (error) => onMessage(errorText(error, "Failed to square position")),
        },
      );
    },
  };
}

function TerminalHeader({
  currentSymbol,
  isConnected,
  tradingMode,
  onModeChange: setTradingMode,
}: Readonly<{
  currentSymbol: WatchlistItem | null;
  isConnected: boolean;
  tradingMode: TradingMode;
  onModeChange: (mode: TradingMode) => void;
}>) {
  const [selectedTimeframe, setSelectedTimeframe] = useState("D");
  return (
    <header className="flex items-center justify-between px-4 py-2 border-b border-border bg-surface">
      <div className="flex items-center gap-6">
        {/* Symbol Info */}
        <div className="flex items-center gap-4">
          <div>
            <div className="flex items-center gap-2">
              <span className="font-semibold text-lg">{currentSymbol?.symbol || "--"}</span>
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
              {currentSymbol?.name || "Select a symbol"}
            </div>
          </div>
          <div className="text-right">
            <div className="font-semibold tabular-nums text-xl">
              {formatLtp(currentSymbol?.ltp)}
            </div>
            {currentSymbol && (
              <div className={`tabular-nums text-xs ${getChangeTone(currentSymbol.changePct)}`}>
                {getChangeText(currentSymbol.changePct)}
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
          variant={tradingMode === "PAPER" ? "secondary" : "ghost"}
          size="sm"
          className="h-8 text-xs"
          onClick={() => setTradingMode("PAPER")}
        >
          PAPER
        </Button>
        <Button
          variant={tradingMode === "LIVE" ? "loss" : "ghost"}
          size="sm"
          className="h-8 text-xs"
          onClick={() => setTradingMode("LIVE")}
        >
          LIVE
        </Button>
        <Button variant="ghost" size="icon" className="h-8 w-8">
          <Bell className="w-4 h-4" />
        </Button>
        <Button variant="ghost" size="icon" className="h-8 w-8">
          <Settings className="w-4 h-4" />
        </Button>
      </div>
    </header>
  );
}

function WatchlistSearchResults({
  results,
  loading,
  onPick,
}: Readonly<{
  results: SearchResultItem[];
  loading: boolean;
  onPick: (item: SearchResultItem) => void;
}>) {
  if (loading) {
    return <div className="px-2 py-2 text-xs text-foreground-muted">Searching...</div>;
  }
  if (results.length === 0) {
    return <div className="px-2 py-2 text-xs text-foreground-muted">No matches</div>;
  }
  return results.map((item) => (
    <button
      key={`${item.symbol}-${item.type || "EQ"}`}
      type="button"
      className="w-full text-left px-2 py-1.5 hover:bg-background-secondary border-b border-border last:border-b-0"
      onClick={() => onPick(item)}
    >
      <div className="text-sm font-medium">{item.symbol}</div>
      <div className="text-xs text-foreground-muted truncate">{item.name}</div>
    </button>
  ));
}

/** Debounced symbol search; results show once at least two characters are typed. */
function useWatchlistSearch() {
  const [text, setText] = useState("");
  const [searchTerm, setSearchTerm] = useState("");
  const debouncedSetSearchTerm = useMemo(
    () => debounce((value: unknown) => setSearchTerm(typeof value === "string" ? value : ""), 250),
    [],
  );
  // Stable ref callback on the search input: its cleanup drops a pending search on unmount.
  const cancelOnUnmount = useCallback(
    () => () => debouncedSetSearchTerm.cancel(),
    [debouncedSetSearchTerm],
  );
  const active = text.trim().length >= 2;
  const symbolSearchQuery = useSymbolSearch(searchTerm);
  const results = active ? (symbolSearchQuery.data ?? NO_SEARCH_RESULTS) : NO_SEARCH_RESULTS;
  return {
    text,
    active,
    results,
    loading: symbolSearchQuery.isFetching,
    cancelOnUnmount,
    onTextChange: (value: string) => {
      setText(value);
      debouncedSetSearchTerm(value.trim());
    },
    /** Drop any pending debounced search so a stale query does not fire. */
    cancelPending: () => debouncedSetSearchTerm.cancel(),
    reset: () => {
      setText("");
      setSearchTerm("");
    },
  };
}

/** Search box that adds a symbol to the watchlist and selects it. */
function WatchlistSearch({ onAdded }: Readonly<{ onAdded: (symbol: string) => void }>) {
  const { cancelOnUnmount, ...search } = useWatchlistSearch();
  const addToWatchlist = useAddToWatchlist();
  const firstResult = search.results.at(0);

  const addSymbol = (item: SearchResultItem) => {
    const symbol = item.symbol.trim().toUpperCase();
    if (!symbol) return;
    search.cancelPending();
    addToWatchlist.mutate(
      { symbol, name: item.name, instrumentType: watchlistInstrumentType(item) },
      {
        onSuccess: () => {
          onAdded(symbol);
          search.reset();
        },
      },
    );
  };

  return (
    <div className="p-2 border-b border-border">
      <div className="flex items-center gap-2 px-3 py-1.5 rounded bg-background border border-border">
        <Search className="w-4 h-4 text-foreground-muted" />
        <input
          ref={cancelOnUnmount}
          type="text"
          value={search.text}
          onChange={(event) => search.onTextChange(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && firstResult) {
              event.preventDefault();
              addSymbol(firstResult);
            }
          }}
          placeholder="Search..."
          className="flex-1 bg-transparent outline-none text-sm"
        />
        <button
          type="button"
          className="text-foreground-muted hover:text-foreground disabled:opacity-50"
          disabled={!firstResult}
          onClick={() => {
            if (firstResult) addSymbol(firstResult);
          }}
          aria-label="Add symbol to watchlist"
        >
          <Plus className="w-4 h-4" />
        </button>
      </div>
      {addToWatchlist.error && (
        <div className="mt-1 text-xs text-loss">
          {errorText(addToWatchlist.error, "Failed to add symbol")}
        </div>
      )}
      {search.active && (
        <div className="mt-1 rounded border border-border bg-surface max-h-44 overflow-y-auto">
          <WatchlistSearchResults
            results={search.results}
            loading={search.loading}
            onPick={addSymbol}
          />
        </div>
      )}
    </div>
  );
}

function WatchlistTable({
  rows,
  isLoading,
  error,
  selectedSymbol,
  onSelect,
}: Readonly<{
  rows: WatchlistItem[];
  isLoading: boolean;
  error: string | null;
  selectedSymbol: string;
  onSelect: (symbol: string) => void;
}>) {
  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-32">
        <Loader2 className="w-5 h-5 animate-spin text-foreground-muted" />
      </div>
    );
  }
  if (error) {
    return <div className="p-4 text-center text-sm text-loss">{error}</div>;
  }
  if (rows.length === 0) {
    return <div className="p-4 text-center text-sm text-foreground-muted">No symbols</div>;
  }
  return (
    <table className="w-full">
      <thead className="text-xs text-foreground-muted border-b border-border">
        <tr>
          <th className="py-1.5 pl-3 text-left font-normal">Symbol</th>
          <th className="py-1.5 pr-3 text-right font-normal">LTP</th>
        </tr>
      </thead>
      <tbody className="text-sm">
        {rows.map((stock) => (
          <WatchlistRow
            key={stock.symbol}
            stock={stock}
            isSelected={selectedSymbol === stock.symbol}
            onClick={() => onSelect(stock.symbol)}
            onOpenTradingView={openTradingView}
          />
        ))}
      </tbody>
    </table>
  );
}

function OptionsView({ chain }: Readonly<{ chain: OptionsChain }>) {
  const { snapshot } = chain;
  if (snapshot) {
    return (
      <OptionsBoardPanel
        board={snapshot.board}
        orderflow={snapshot.orderflow}
        underlying={chain.underlying}
        expiry={chain.expiry}
        onUnderlyingChange={chain.setUnderlying}
        onExpiryChange={chain.setExpiry}
        onSelectOption={chain.setSelectedOptionSymbol}
      />
    );
  }
  if (chain.isLoading) {
    return (
      <div className="text-center">
        <Loader2 className="w-8 h-8 animate-spin mx-auto mb-2 text-foreground-muted" />
        <p className="text-sm text-foreground-muted">Loading options board...</p>
      </div>
    );
  }
  return (
    <div className="text-center">
      {chain.error ? (
        <p className="text-sm text-loss">{chain.error}</p>
      ) : (
        <p className="text-sm text-foreground-muted">No options data</p>
      )}
    </div>
  );
}

function ViewToolbar({
  view,
  onViewChange: setTerminalView,
  board,
}: Readonly<{
  view: TerminalView;
  onViewChange: (view: TerminalView) => void;
  board: OptionsBoard | null;
}>) {
  return (
    <div className="h-9 flex items-center justify-between px-3 border-b border-border">
      <div className="flex items-center gap-2">
        <Button
          variant={view === "options" ? "secondary" : "ghost"}
          size="sm"
          className="h-7 gap-1 text-xs"
          onClick={() => setTerminalView("options")}
        >
          <CandlestickChart className="w-4 h-4" /> Option Chain
        </Button>
        <Button
          variant={view === "positions" ? "secondary" : "ghost"}
          size="sm"
          className="h-7 gap-1 text-xs"
          onClick={() => setTerminalView("positions")}
        >
          <ListOrdered className="w-4 h-4" /> Positions
        </Button>
      </div>
      <div className="flex items-center gap-2">
        {view === "options" && board && (
          <span className="text-xs text-foreground-muted">
            {board.underlying} Spot: {roundToDecimals(board.spot_price, 2).toFixed(2)}
          </span>
        )}
        <Button variant="profit" size="sm" className="h-7 text-xs px-4">
          Buy
        </Button>
        <Button variant="loss" size="sm" className="h-7 text-xs px-4">
          Sell
        </Button>
      </div>
    </div>
  );
}

/**
 * PAPER needs no confirmation. LIVE asks the user and returns null when they cancel,
 * so a live order is never sent without an explicit acknowledgement.
 */
function confirmLiveOrder(input: {
  mode: TradingMode;
  side: OrderSide;
  quantity: number;
  symbol: string;
}): boolean | null {
  if (input.mode !== "LIVE") return false;
  const confirmed = globalThis.confirm(
    `Confirm LIVE ${input.side} ${input.quantity} ${input.symbol}?`,
  );
  return confirmed ? true : null;
}

function isStopOrder(orderType: OrderType): boolean {
  return orderType === "SL" || orderType === "SL-M";
}

/** Order ticket inputs and submission. Options are sized in lots of the underlying. */
function useOrderTicket(params: {
  tradingMode: TradingMode;
  terminalView: TerminalView;
  activeTradeSymbol: string;
  optionsUnderlying: string;
  onMessage: (message: string | null) => void;
}) {
  const { tradingMode, terminalView, activeTradeSymbol, onMessage } = params;
  const [orderType, setOrderType] = useState<OrderType>(DEFAULT_ORDER_TYPE);
  const [orderQty, setOrderQty] = useState<number>(1);
  const [optionLots, setOptionLots] = useState<number>(1);
  const [orderPrice, setOrderPrice] = useState<number>(0);
  const [orderTrigger, setOrderTrigger] = useState<number>(0);
  const [riskOverrideReason, setRiskOverrideReason] = useState<string>("");
  const submit = useSubmitOrder();

  const lotsMode = terminalView === "options" && inferInstrumentType(activeTradeSymbol) !== "EQ";
  const optionLotSize = OPTION_LOT_SIZES[params.optionsUnderlying] ?? 1;
  const quantity = lotsMode ? Math.max(1, optionLots) * optionLotSize : Math.max(1, orderQty);

  const placeOrder = (side: OrderSide) => {
    const invalid = orderInputError({
      symbol: activeTradeSymbol,
      quantity,
      orderType,
      price: orderPrice,
      trigger: orderTrigger,
    });
    if (invalid) {
      onMessage(invalid);
      return;
    }
    onMessage(null);
    const liveConfirmed = confirmLiveOrder({
      mode: tradingMode,
      side,
      quantity,
      symbol: activeTradeSymbol,
    });
    if (liveConfirmed === null) {
      onMessage("Live order cancelled");
      return;
    }
    submit.mutate(
      {
        mode: tradingMode,
        symbol: activeTradeSymbol,
        side,
        quantity,
        orderType,
        price: orderPrice,
        trigger: orderTrigger,
        liveConfirmed,
        riskOverrideReason,
        // A new click is a new intent; retries of this mutation reuse this id.
        clientOrderId: crypto.randomUUID(),
      },
      {
        onSuccess: onMessage,
        onError: (error) => onMessage(errorText(error, "Order failed")),
      },
    );
  };

  return {
    orderType,
    setOrderType,
    orderQty,
    setOrderQty,
    optionLots,
    setOptionLots,
    orderPrice,
    setOrderPrice,
    orderTrigger,
    setOrderTrigger,
    riskOverrideReason,
    setRiskOverrideReason,
    lotsMode,
    optionLotSize,
    quantity,
    isSubmitting: submit.isPending,
    placeOrder,
  };
}

type OrderTicketState = ReturnType<typeof useOrderTicket>;

function NumberField({
  id,
  label,
  placeholder,
  value,
  min,
  disabled,
  onChange,
}: Readonly<{
  id: string;
  label: string;
  placeholder: string;
  value: number;
  min?: number;
  disabled?: boolean;
  onChange: (raw: string) => void;
}>) {
  return (
    <div>
      <label htmlFor={id} className="text-xs text-foreground-muted mb-1 block">
        {label}
      </label>
      <Input
        id={id}
        type="number"
        min={min}
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        className="h-8 text-sm"
      />
    </div>
  );
}

/** At least 1, as the quantity and lots inputs always were. */
function atLeastOne(raw: string): number {
  return Math.max(1, Number(raw) || 1);
}

function zeroIfBlank(raw: string): number {
  return Number(raw) || 0;
}

function OrderFields({
  ticket,
  tradingMode,
}: Readonly<{ ticket: OrderTicketState; tradingMode: TradingMode }>) {
  return (
    <>
      <div>
        <label htmlFor="order-type" className="text-xs text-foreground-muted mb-1 block">
          Order Type
        </label>
        <select
          id="order-type"
          value={ticket.orderType}
          onChange={(event) => ticket.setOrderType(event.target.value as OrderType)}
          className="w-full h-8 px-2 rounded text-sm border border-border bg-background outline-none"
        >
          <option value="MARKET">Market</option>
          <option value="LIMIT">Limit</option>
          <option value="SL">Stop Loss</option>
          <option value="SL-M">Stop Loss Market</option>
        </select>
      </div>
      {ticket.lotsMode ? (
        <NumberField
          id="order-lots"
          label="Lots"
          placeholder="Lots"
          min={1}
          value={ticket.optionLots}
          onChange={(raw) => ticket.setOptionLots(atLeastOne(raw))}
        />
      ) : (
        <NumberField
          id="order-qty"
          label="Quantity"
          placeholder="Qty"
          min={1}
          value={ticket.orderQty}
          onChange={(raw) => ticket.setOrderQty(atLeastOne(raw))}
        />
      )}
      <NumberField
        id="order-price"
        label="Price"
        placeholder="Price"
        value={ticket.orderPrice}
        disabled={ticket.orderType === "MARKET"}
        onChange={(raw) => ticket.setOrderPrice(zeroIfBlank(raw))}
      />
      {isStopOrder(ticket.orderType) && (
        <NumberField
          id="order-trigger"
          label="Trigger"
          placeholder="Trigger"
          value={ticket.orderTrigger}
          onChange={(raw) => ticket.setOrderTrigger(zeroIfBlank(raw))}
        />
      )}
      {tradingMode === "LIVE" && (
        <div>
          <label
            htmlFor="risk-override-reason"
            className="text-xs text-foreground-muted mb-1 block"
          >
            Risk Override Reason (if warning)
          </label>
          <Input
            id="risk-override-reason"
            type="text"
            value={ticket.riskOverrideReason}
            onChange={(event) => ticket.setRiskOverrideReason(event.target.value)}
            placeholder="Optional unless risk warning"
            className="h-8 text-sm"
          />
        </div>
      )}
    </>
  );
}

function OrderTicket({
  tradingMode,
  terminalView,
  activeTradeSymbol,
  optionsUnderlying,
  message: orderMessage,
  onMessage,
}: Readonly<{
  tradingMode: TradingMode;
  terminalView: TerminalView;
  activeTradeSymbol: string;
  optionsUnderlying: string;
  message: string | null;
  onMessage: (message: string | null) => void;
}>) {
  const ticket = useOrderTicket({
    tradingMode,
    terminalView,
    activeTradeSymbol,
    optionsUnderlying,
    onMessage,
  });

  return (
    <div className="w-56 flex-shrink-0 p-3 border-l border-border bg-surface">
      <div className="mb-2 text-xs text-foreground-muted">
        Trade Symbol:{" "}
        <span className="tabular-nums text-foreground">{activeTradeSymbol || "--"}</span>
      </div>
      {ticket.lotsMode && (
        <div className="mb-2 text-xs text-foreground-muted">
          Lot Size: <span className="tabular-nums text-foreground">{ticket.optionLotSize}</span> |
          Lots: <span className="tabular-nums text-foreground">{ticket.optionLots}</span> | Qty:{" "}
          <span className="tabular-nums text-foreground">{ticket.quantity}</span>
        </div>
      )}
      <div className="flex items-center gap-2 mb-3">
        <Button
          variant="profit"
          className="flex-1 h-8 text-sm"
          disabled={ticket.isSubmitting}
          onClick={() => ticket.placeOrder("BUY")}
        >
          Buy
        </Button>
        <Button
          variant="loss"
          className="flex-1 h-8 text-sm"
          disabled={ticket.isSubmitting}
          onClick={() => ticket.placeOrder("SELL")}
        >
          Sell
        </Button>
      </div>

      <div className="space-y-3">
        <OrderFields ticket={ticket} tradingMode={tradingMode} />
        {orderMessage && (
          <div className="text-xs text-foreground-muted border border-border rounded px-2 py-1.5">
            {orderMessage}
          </div>
        )}
      </div>
    </div>
  );
}

export default function TerminalPage() {
  const isConnected = useConnectionStatus() === "connected";
  const [tradingMode, setTradingMode] = useState<TradingMode>(DEFAULT_TRADING_MODE);
  const [terminalView, setTerminalView] = useState<TerminalView>("options");
  const [orderMessage, setOrderMessage] = useState<string | null>(null);
  const symbols = useTerminalSymbols();
  const chain = useOptionsChain(terminalView === "options");
  const positionsQuery = usePositionsBook(terminalView === "positions");
  const { squaring, squarePosition } = useSquareAction(setOrderMessage);
  const basePositions = positionsQuery.data?.rows ?? NO_POSITIONS;

  // Live quotes come from the shared quote store; rows below are derived, not synced via effects.
  const quoteGroups = useMemo(
    () => [
      symbols.items.map((item) => item.symbol),
      [symbols.selectedSymbol, chain.selectedOptionSymbol],
      basePositions.map((position) => position.symbol),
    ],
    [symbols.items, symbols.selectedSymbol, chain.selectedOptionSymbol, basePositions],
  );
  const quotes = useScreenQuotes(quoteGroups);
  const watchlist = useMemo(
    () => symbols.items.map((item) => withQuote(item, quotes[item.symbol])),
    [symbols.items, quotes],
  );
  const positions = useMemo(
    () => basePositions.map((pos) => withQuotePnl(pos, quotes[pos.symbol])),
    [basePositions, quotes],
  );
  const currentSymbol =
    watchlist.find((s) => s.symbol === symbols.selectedSymbol) ?? watchlist.at(0) ?? null;
  const optionSymbol = terminalView === "options" ? chain.selectedOptionSymbol : "";
  const activeTradeSymbol = optionSymbol || (currentSymbol?.symbol ?? "");

  return (
    <div className="flex flex-col h-[calc(100vh-48px)]">
      <div
        className={`px-4 py-1.5 text-xs border-b border-border ${tradingMode === "PAPER" ? "bg-amber-500/10 text-amber-400" : "bg-loss-bg text-loss"}`}
      >
        Mode: {tradingMode}
      </div>
      <TerminalHeader
        currentSymbol={currentSymbol}
        isConnected={isConnected}
        tradingMode={tradingMode}
        onModeChange={setTradingMode}
      />

      {/* Main Content */}
      <div className="flex-1 flex overflow-hidden">
        {/* Left Sidebar - Watchlist */}
        <div className="w-64 flex-shrink-0 flex flex-col border-r border-border bg-surface">
          <WatchlistSearch onAdded={symbols.setSelectedSymbol} />
          <div className="flex-1 overflow-y-auto">
            <WatchlistTable
              rows={watchlist}
              isLoading={symbols.isLoading}
              error={symbols.error}
              selectedSymbol={symbols.selectedSymbol}
              onSelect={symbols.setSelectedSymbol}
            />
          </div>
        </div>

        {/* Center - Options Board / Positions */}
        <div className="flex-1 flex flex-col">
          <ViewToolbar
            view={terminalView}
            onViewChange={setTerminalView}
            board={chain.snapshot?.board ?? null}
          />
          <div className="flex-1 flex items-center justify-center">
            {terminalView === "options" && <OptionsView chain={chain} />}
            {terminalView === "positions" && (
              <PositionsPanel
                summary={summarizePositions(positions)}
                error={positionsQuery.isError ? "Failed to load positions" : null}
                positions={positions}
                liveStale={positionsQuery.data?.liveStale ?? false}
                squaring={squaring}
                onSquare={squarePosition}
              />
            )}
          </div>
        </div>

        {/* Right Sidebar - Order Panel */}
        <OrderTicket
          tradingMode={tradingMode}
          terminalView={terminalView}
          activeTradeSymbol={activeTradeSymbol}
          optionsUnderlying={chain.underlying}
          message={orderMessage}
          onMessage={setOrderMessage}
        />
      </div>
    </div>
  );
}
