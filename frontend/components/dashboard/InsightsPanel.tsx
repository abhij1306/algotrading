import { useMemo, type ReactNode } from "react";
import type { MarketStatus } from "@/lib/market-hours";
import {
  getInsightSymbols,
  useMarketInsights,
  usePostMarketInsights,
  type MarketInsights,
  type SectorData,
  type StockMover,
} from "@/lib/queries/dashboard";
import { useQuotes } from "@/lib/realtime/hooks";
import type { QuoteMap } from "@/lib/realtime/quote-store";
import { MarketHoursInsights } from "./MarketHoursInsights";
import { PostMarketInsights } from "./PostMarketInsights";

interface InsightsPanelProps {
  marketStatus: MarketStatus;
}

const NO_SYMBOLS: string[] = [];

function moverWithQuote(row: StockMover, quotes: QuoteMap): StockMover {
  const quote = quotes[row.symbol];
  if (!quote) return row;
  return {
    ...row,
    price: quote.ltp ?? row.price,
    changePercent: quote.changePct ?? row.changePercent,
  };
}

function sectorWithQuote(row: SectorData, quotes: QuoteMap): SectorData {
  const quote = quotes[row.symbol];
  if (!quote) return row;
  return {
    ...row,
    value: quote.ltp ?? row.value,
    changePercent: quote.changePct ?? row.changePercent,
  };
}

/** The fetched movers and sectors with any live quote laid over them. */
function applyQuotes(data: MarketInsights, quotes: QuoteMap): MarketInsights {
  return {
    topGainers: data.topGainers.map((row) => moverWithQuote(row, quotes)),
    topLosers: data.topLosers.map((row) => moverWithQuote(row, quotes)),
    sectorPerformance: data.sectorPerformance.map((row) => sectorWithQuote(row, quotes)),
  };
}

export function InsightsPanel({ marketStatus }: Readonly<InsightsPanelProps>) {
  const marketQuery = useMarketInsights(marketStatus.isOpen);
  const postMarketQuery = usePostMarketInsights(!marketStatus.isOpen);
  const fetchedInsights = marketStatus.isOpen ? (marketQuery.data ?? null) : null;
  // Live ticks for the shown movers and sectors, only during market hours.
  const liveSymbols = useMemo(
    () => (fetchedInsights ? getInsightSymbols(fetchedInsights) : NO_SYMBOLS),
    [fetchedInsights],
  );
  const quotes = useQuotes(liveSymbols);
  const marketInsights = fetchedInsights ? applyQuotes(fetchedInsights, quotes) : null;
  const postMarketData = marketStatus.isOpen ? null : (postMarketQuery.data ?? null);
  const loading = marketStatus.isOpen ? marketQuery.isPending : postMarketQuery.isPending;
  const failure = marketStatus.isOpen ? marketQuery.error : postMarketQuery.error;
  const failureLabel = marketStatus.isOpen
    ? "Failed to load market insights"
    : "Failed to load post-market data";
  const error = failure ? `${failureLabel}: ${failure.message}` : null;

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
