import { useMemo, type ReactNode } from "react";
import type { MarketStatus } from "@/lib/market-hours";
import {
  getInsightSymbols,
  mergeCommodities,
  useCommodities,
  useMarketInsights,
  usePostMarketInsights,
  type CommodityData,
  type MarketInsights,
  type PostMarketData,
  type SectorData,
  type StockMover,
} from "@/lib/queries/dashboard";
import { useQuotes } from "@/lib/realtime/hooks";
import type { QuoteMap } from "@/lib/realtime/quote-store";
import { MarketHoursInsights } from "./MarketHoursInsights";
import { PostMarketInsights } from "./PostMarketInsights";

interface InsightsPanelProps {
  marketStatus: MarketStatus;
  /** Fetch nothing until the real market status is known, so no closed-market call runs early. */
  statusLoaded: boolean;
}

/** One insights mode's data with its loading flag and failure text. */
interface InsightsView<Data> {
  data: Data | null;
  loading: boolean;
  error: string | null;
}

const NO_SYMBOLS: string[] = [];
const NO_COMMODITIES: CommodityData[] = [];

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

/** The post-market snapshot plus the commodity rows (Crude Oil) the overview does not carry. */
function withCommodities(
  data: PostMarketData | undefined,
  extra: readonly CommodityData[],
): PostMarketData | null {
  if (!data) return null;
  return { ...data, commodities: mergeCommodities(data.commodities, extra) };
}

/** The fetched movers and sectors with any live quote laid over them. */
function applyQuotes(data: MarketInsights, quotes: QuoteMap): MarketInsights {
  return {
    topGainers: data.topGainers.map((row) => moverWithQuote(row, quotes)),
    topLosers: data.topLosers.map((row) => moverWithQuote(row, quotes)),
    sectorPerformance: data.sectorPerformance.map((row) => sectorWithQuote(row, quotes)),
  };
}

function failureText(label: string, failure: Error | null): string | null {
  return failure ? `${label}: ${failure.message}` : null;
}

/** Live top movers and sectors with ticks laid over them, while `enabled` (market hours). */
function useMarketHoursView(enabled: boolean): InsightsView<MarketInsights> {
  const query = useMarketInsights(enabled);
  const fetched = enabled ? (query.data ?? null) : null;
  const liveSymbols = useMemo(() => (fetched ? getInsightSymbols(fetched) : NO_SYMBOLS), [fetched]);
  const quotes = useQuotes(liveSymbols);
  return {
    data: fetched ? applyQuotes(fetched, quotes) : null,
    loading: query.isPending,
    error: failureText("Failed to load market insights", query.error),
  };
}

/** The post-market snapshot with the extra commodity rows, while `enabled` (after the close). */
function usePostMarketView(enabled: boolean): InsightsView<PostMarketData> {
  const query = usePostMarketInsights(enabled);
  // Optional extra rows: a failed commodities fetch leaves the overview's own rows on screen.
  const extra = useCommodities(enabled).data ?? NO_COMMODITIES;
  return {
    data: enabled ? withCommodities(query.data, extra) : null,
    loading: query.isPending,
    error: failureText("Failed to load post-market data", query.error),
  };
}

export function InsightsPanel({ marketStatus, statusLoaded }: Readonly<InsightsPanelProps>) {
  const isOpen = marketStatus.isOpen;
  const marketView = useMarketHoursView(statusLoaded && isOpen);
  const postMarketView = usePostMarketView(statusLoaded && !isOpen);
  const view = isOpen ? marketView : postMarketView;

  if (view.loading && !view.data) {
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

  if (view.error) {
    return <div className="p-4 text-center text-sm text-loss">{view.error}</div>;
  }

  let insights: ReactNode = null;
  if (isOpen && marketView.data) {
    insights = <MarketHoursInsights data={marketView.data} />;
  } else if (!isOpen && postMarketView.data) {
    insights = <PostMarketInsights data={postMarketView.data} />;
  }

  return <div className="transition-opacity duration-300">{insights}</div>;
}
