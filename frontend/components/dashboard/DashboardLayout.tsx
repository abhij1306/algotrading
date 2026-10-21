import type { ReactNode } from "react";
import { Activity } from "lucide-react";

/** The dashboard bands: header, insights, then the indices and watchlist grid. */
export function DashboardLayout(
  props: Readonly<{
    header: ReactNode;
    insights: ReactNode;
    indices: ReactNode;
    watchlist: ReactNode;
  }>,
) {
  return (
    <div className="flex flex-col h-screen">
      {props.header}
      {props.insights}
      <div className="flex-1 overflow-auto">
        <div className="grid grid-cols-1 lg:grid-cols-5 h-full">
          {props.indices}
          {props.watchlist}
        </div>
      </div>
    </div>
  );
}

/** Shown when no panel has anything: the load failure, or a hint to connect Fyers. */
export function DashboardEmptyState({ error }: Readonly<{ error: Error | null }>) {
  return (
    <div className="flex items-center justify-center h-full">
      <div className="text-center">
        <Activity className="w-10 h-10 mx-auto text-foreground-muted mb-3" />
        <h2 className="text-base font-medium">No Data Available</h2>
        {error ? (
          <p role="alert" className="text-sm text-loss">
            {error.message}
          </p>
        ) : (
          <p className="text-sm text-foreground-muted">Connect to Fyers to view market data</p>
        )}
      </div>
    </div>
  );
}
