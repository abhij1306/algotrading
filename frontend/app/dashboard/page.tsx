import type { ReactNode } from "react";
import { useState, memo, useMemo } from "react";
import { useNavigate } from "react-router";
import {
  TrendingUp,
  TrendingDown,
  Activity,
  Zap,
  Globe,
  ChevronRight,
  Target,
  BarChart3,
  Radio,
  Moon,
  ExternalLink,
} from "lucide-react";
import { Button } from "@/components/ui";
import { getISTTime, MarketStatus } from "@/lib/market-hours";
import { formatCurrency, formatPercentage, roundToDecimals } from "@/lib/utils";
import { InsightsPanel } from "@/components/dashboard/InsightsPanel";
import {
  useDashboardWatchlist,
  useMarketIndices,
  useMarketStatus,
  usePortfolioStats,
  type DashboardMode,
  type DashboardWatchlistItem as WatchlistItem,
  type MarketIndex,
} from "@/lib/queries/dashboard";
import { useQuotes, useWebSocket } from "@/lib/realtime/hooks";
import type { QuoteMap } from "@/lib/realtime/quote-store";
import { getTradingViewUrl } from "@/lib/tradingview";

type StatusBadge = {
  icon: typeof Radio;
  label: string;
  className: string;
};
type MarketSectionMeta = {
  title: string;
  subtitle: string;
};

const INITIAL_MARKET_STATUS: MarketStatus = { isOpen: false, message: "Loading market status" };
const LIVE_INDEX_SYMBOLS = new Set(["NIFTY50", "BANKNIFTY", "NIFTYIT"]);

const NO_INDICES: MarketIndex[] = [];
const NO_WATCHLIST: WatchlistItem[] = [];
const NO_SYMBOLS: string[] = [];

const QUICK_ACTIONS = [
  { icon: Zap, label: "Trade", path: "/terminal" },
  { icon: Target, label: "Backtest", path: "/backtest" },
  { icon: BarChart3, label: "Screener", path: "/screener" },
];

function formatDashboardNumber(value: number | null | undefined): string {
  if (typeof value !== "number" || Number.isNaN(value)) return "—";
  return value.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function formatDashboardPercent(value: number | null | undefined): string {
  if (typeof value !== "number" || Number.isNaN(value)) return "—";
  return `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;
}

function getStatusBadge(isLive: boolean, isDelayed: boolean): StatusBadge {
  if (isLive) {
    return { icon: Radio, label: "LIVE", className: "bg-profit-bg text-profit" };
  }
  if (isDelayed) {
    return { icon: Activity, label: "DELAYED", className: "bg-amber-500/10 text-amber-400" };
  }
  return {
    icon: Moon,
    label: "CLOSED",
    className: "bg-background-tertiary text-foreground-secondary",
  };
}

function getMarketSectionMeta(isOpen: boolean, isConnected: boolean): MarketSectionMeta {
  if (isOpen) {
    return {
      title: "Indian Markets",
      subtitle: isConnected ? "NSE Real-time" : "NSE Delayed (Disconnected)",
    };
  }
  return { title: "Global Markets", subtitle: "Yahoo Delayed" };
}

function getCompactIndexLabel(symbol: string): string {
  if (symbol === "NIFTY50") return "NIFTY";
  if (symbol === "BANKNIFTY") return "BANK";
  return "IT";
}

function getStatTrend(value: number | null | undefined): "up" | "down" {
  return typeof value === "number" && value >= 0 ? "up" : "down";
}

function roundedOr(value: number | undefined, fallback: number | null): number | null {
  return typeof value === "number" ? roundToDecimals(value, 2) : fallback;
}

/** Watchlist rows with the live last price and day percent laid over the fetched ones. */
function applyWatchlistQuotes(rows: WatchlistItem[], quotes: QuoteMap): WatchlistItem[] {
  return rows.map((item) => {
    const quote = quotes[item.symbol];
    if (!quote) return item;
    return {
      ...item,
      price: roundedOr(quote.ltp, item.price),
      changePercent: roundedOr(quote.changePct, item.changePercent),
    };
  });
}

/** Index rows with live quotes laid over them; a quoted row is marked as streaming. */
function applyIndexQuotes(rows: MarketIndex[], quotes: QuoteMap): MarketIndex[] {
  return rows.map((idx) => {
    const quote = quotes[idx.symbol];
    if (!quote) return idx;
    return {
      ...idx,
      value: roundedOr(quote.ltp, idx.value) ?? idx.value,
      changePercent: roundedOr(quote.changePct, idx.changePercent) ?? idx.changePercent,
      source: "websocket",
    };
  });
}

function PanelError({ error }: Readonly<{ error: Error }>) {
  return (
    <div role="alert" className="p-8 text-center text-loss">
      {error.message}
    </div>
  );
}

// Memoized components for performance
const StatItem = memo(function StatItem({
  label,
  value,
  change,
  trend,
  loading,
}: {
  label: string;
  value: string;
  change?: number;
  trend?: "up" | "down";
  loading?: boolean;
}) {
  if (loading) return <div className="text-foreground-muted">—</div>;
  return (
    <div className="flex items-center gap-3">
      <span className="text-sm text-foreground-muted">{label}</span>
      <span className="text-base font-semibold tabular-nums">{value}</span>
      {change !== undefined && (
        <span className={`text-sm tabular-nums ${trend === "up" ? "text-profit" : "text-loss"}`}>
          {formatPercentage(change)}
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
  const isUp = idx.changePercent >= 0;
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
          {idx.source === "websocket" && <span className="w-1.5 h-1.5 rounded-full bg-profit" />}
        </div>
      </td>
      <td className="py-2 text-right tabular-nums">{formatDashboardNumber(idx.value)}</td>
      <td className={`py-2 pr-4 text-right tabular-nums ${isUp ? "text-profit" : "text-loss"}`}>
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
  const hasPrice = typeof stock.price === "number" && Number.isFinite(stock.price);
  const hasChangePercent =
    typeof stock.changePercent === "number" && Number.isFinite(stock.changePercent);
  const isUp = hasChangePercent ? (stock.changePercent ?? 0) >= 0 : false;
  const priceLabel = hasPrice
    ? `₹${(stock.price ?? 0).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
    : "—";
  let changeClass = "text-foreground-muted";
  if (hasChangePercent) {
    changeClass = isUp ? "text-profit" : "text-loss";
  }
  let changeContent: ReactNode = "Unavailable";
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
      <td className={`py-2.5 pr-4 text-right tabular-nums ${changeClass}`}>{changeContent}</td>
    </tr>
  );
});

export default function DashboardPage() {
  const navigate = useNavigate();
  const statusQuery = useMarketStatus();
  const statsQuery = usePortfolioStats();
  const watchlistQuery = useDashboardWatchlist();
  const marketStatus = statusQuery.data?.status ?? INITIAL_MARKET_STATUS;
  const indicesQuery = useMarketIndices({
    marketOpen: marketStatus.isOpen,
    enabled: statusQuery.data !== undefined,
  });
  // Connection state only; the socket is held open during market hours.
  const { isConnected } = useWebSocket({ enabled: marketStatus.isOpen, skipStateUpdates: true });

  const [initialIstTime] = useState(getISTTime);
  const istTime = statusQuery.data?.istTime ?? initialIstTime;
  const dashboardMode: DashboardMode = indicesQuery.data?.nextMode ?? "post_market";
  const portfolioStats = statsQuery.data ?? null;
  const fetchedIndices = indicesQuery.data?.indicesData ?? NO_INDICES;
  const fetchedWatchlist = watchlistQuery.data ?? NO_WATCHLIST;
  const streaming = marketStatus.isOpen && dashboardMode === "market";
  // Live quotes for the shown rows while the market is open; the store owns the subscriptions.
  const liveSymbols = useMemo(
    () =>
      streaming
        ? [
            ...fetchedWatchlist.map((item) => item.symbol),
            ...fetchedIndices.map((idx) => idx.symbol),
          ]
        : NO_SYMBOLS,
    [streaming, fetchedWatchlist, fetchedIndices],
  );
  const quotes = useQuotes(liveSymbols);
  const marketIndices = applyIndexQuotes(fetchedIndices, quotes);
  const watchlist = applyWatchlistQuotes(fetchedWatchlist, quotes);
  const loading = {
    portfolio: statsQuery.isPending,
    indices: indicesQuery.isPending,
    watchlist: watchlistQuery.isPending,
  };
  const loadError = statsQuery.error ?? watchlistQuery.error ?? indicesQuery.error;
  const openTradingView = (symbol: string) => {
    window.open(getTradingViewUrl(symbol), "_blank", "noopener,noreferrer");
  };

  const isLive = marketStatus.isOpen && isConnected;
  const isDelayed = marketStatus.isOpen && !isConnected;
  const statusBadge = getStatusBadge(isLive, isDelayed);
  const StatusIcon = statusBadge.icon;
  const marketSectionMeta = getMarketSectionMeta(marketStatus.isOpen, isConnected);
  const renderIndicesBody = () => {
    if (loading.indices) {
      return (
        <div className="p-4 space-y-2">
          {[1, 2, 3, 4].map((i) => (
            <div key={i} className="h-8 bg-background-tertiary/30 rounded" />
          ))}
        </div>
      );
    }
    if (marketIndices.length === 0 && indicesQuery.error) {
      return <PanelError error={indicesQuery.error} />;
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
              onClick={() => void navigate(`/terminal?symbol=${idx.symbol}`)}
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
          {[1, 2, 3, 4, 5].map((i) => (
            <div key={i} className="h-8 bg-background-tertiary/30 rounded" />
          ))}
        </div>
      );
    }
    if (watchlist.length === 0 && watchlistQuery.error) {
      return <PanelError error={watchlistQuery.error} />;
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
              onClick={() => void navigate(`/terminal?symbol=${stock.symbol}`)}
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
          {loadError ? (
            <p role="alert" className="text-sm text-loss">
              {loadError.message}
            </p>
          ) : (
            <p className="text-sm text-foreground-muted">Connect to Fyers to view market data</p>
          )}
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
          <span
            className={`flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium ${statusBadge.className}`}
          >
            <StatusIcon className="w-3 h-3" /> {statusBadge.label}
          </span>
          {/* Live Indices - NIFTY & BANKNIFTY */}
          {marketIndices.length > 0 && (
            <div className="flex items-center gap-3 ml-2 pl-3 border-l border-border">
              {marketIndices
                .filter((idx) => LIVE_INDEX_SYMBOLS.has(idx.symbol))
                .map((idx) => {
                  const isUp = idx.changePercent >= 0;
                  const isLive = idx.source === "websocket";
                  const label = getCompactIndexLabel(idx.symbol);
                  return (
                    <div key={idx.name} className="flex items-center gap-1.5">
                      <span className="text-xs text-foreground-muted">{label}</span>
                      <span className="text-sm font-semibold tabular-nums">
                        {formatDashboardNumber(idx.value)}
                      </span>
                      <span
                        className={`text-xs tabular-nums ${isUp ? "text-profit" : "text-loss"}`}
                      >
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
            {QUICK_ACTIONS.map((action) => (
              <Button
                key={action.label}
                onClick={() => void navigate(action.path)}
                variant="ghost"
                size="sm"
                className="h-8"
              >
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
          value={portfolioStats ? formatCurrency(portfolioStats.totalValue, "INR", "en-IN") : "—"}
          loading={loading.portfolio}
        />
        <StatItem
          label="Day P&L"
          value={portfolioStats ? formatCurrency(portfolioStats.dayChange, "INR", "en-IN") : "—"}
          change={portfolioStats?.dayChangePercent}
          trend={getStatTrend(portfolioStats?.dayChange)}
          loading={loading.portfolio}
        />
        <StatItem
          label="Total Return"
          value={portfolioStats ? formatCurrency(portfolioStats.totalReturn, "INR", "en-IN") : "—"}
          change={portfolioStats?.totalReturnPercent}
          trend={getStatTrend(portfolioStats?.totalReturn)}
          loading={loading.portfolio}
        />
      </div>

      {/* Insights Panel - Top Gainers/Losers/Sectors */}
      <InsightsPanel marketStatus={marketStatus} />

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
                {marketStatus.isOpen && isConnected && (
                  <span className="w-1.5 h-1.5 rounded-full bg-profit" />
                )}
              </div>
              <Button
                onClick={() => void navigate("/screener")}
                variant="ghost"
                size="sm"
                className="h-6 text-xs"
              >
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
