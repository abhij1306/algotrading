import { DashboardHeader } from "@/components/dashboard/DashboardHeader";
import { DashboardEmptyState, DashboardLayout } from "@/components/dashboard/DashboardLayout";
import { IndicesPanel } from "@/components/dashboard/IndicesPanel";
import { InsightsPanel } from "@/components/dashboard/InsightsPanel";
import { PortfolioStatsBar } from "@/components/dashboard/PortfolioStatsBar";
import { useDashboardData } from "@/components/dashboard/useDashboardData";
import { WatchlistPanel } from "@/components/dashboard/WatchlistPanel";

export default function DashboardPage() {
  const data = useDashboardData();
  const { marketStatus, isConnected, portfolio, indices, watchlist } = data;

  const hasData = portfolio.stats || indices.rows.length > 0 || watchlist.rows.length > 0;
  if (!hasData && !portfolio.loading) {
    return <DashboardEmptyState error={data.loadError} />;
  }

  return (
    <DashboardLayout
      header={
        <DashboardHeader
          marketOpen={marketStatus.isOpen}
          isConnected={isConnected}
          indices={indices.rows}
          istTime={data.istTime}
        />
      }
      stats={<PortfolioStatsBar stats={portfolio.stats} loading={portfolio.loading} />}
      insights={<InsightsPanel marketStatus={marketStatus} />}
      indices={
        <IndicesPanel data={indices} marketOpen={marketStatus.isOpen} isConnected={isConnected} />
      }
      watchlist={<WatchlistPanel data={watchlist} isLive={isConnected} />}
    />
  );
}
