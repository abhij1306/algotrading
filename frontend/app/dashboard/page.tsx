import { DashboardHeader } from "@/components/dashboard/DashboardHeader";
import { DashboardEmptyState, DashboardLayout } from "@/components/dashboard/DashboardLayout";
import { IndicesPanel } from "@/components/dashboard/IndicesPanel";
import { InsightsPanel } from "@/components/dashboard/InsightsPanel";
import { useDashboardData } from "@/components/dashboard/useDashboardData";
import { WatchlistPanel } from "@/components/dashboard/WatchlistPanel";

export default function DashboardPage() {
  const data = useDashboardData();
  const { marketStatus, isConnected, indices, watchlist } = data;

  const hasData = indices.rows.length > 0 || watchlist.rows.length > 0;
  const loading = indices.loading || watchlist.loading;
  if (!hasData && !loading) {
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
      insights={
        <InsightsPanel marketStatus={marketStatus} statusLoaded={data.marketStatusLoaded} />
      }
      indices={
        <IndicesPanel data={indices} marketOpen={marketStatus.isOpen} isConnected={isConnected} />
      }
      watchlist={<WatchlistPanel data={watchlist} isLive={isConnected} />}
    />
  );
}
