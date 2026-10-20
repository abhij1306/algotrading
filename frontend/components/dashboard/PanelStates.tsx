import type { ReactNode } from "react";
import { ExternalLink } from "lucide-react";
import { getTradingViewUrl } from "@/lib/tradingview";

/** Placeholder rows while a dashboard table loads. */
export function PanelSkeleton({ rows }: Readonly<{ rows: number }>) {
  return (
    <div className="p-4 space-y-2">
      {Array.from({ length: rows }, (_, i) => i + 1).map((row) => (
        <div key={row} className="h-8 bg-background-tertiary/30 rounded" />
      ))}
    </div>
  );
}

export function PanelError({ error }: Readonly<{ error: Error }>) {
  return (
    <div role="alert" className="p-8 text-center text-loss">
      {error.message}
    </div>
  );
}

export function PanelEmpty({ children }: Readonly<{ children: ReactNode }>) {
  return <div className="p-8 text-center text-foreground-muted">{children}</div>;
}

/** Loading, then a failure with no rows, then no rows, then the table. */
export function PanelBody(
  props: Readonly<{
    loading: boolean;
    skeletonRows: number;
    error: Error | null;
    isEmpty: boolean;
    emptyLabel: string;
    children: ReactNode;
  }>,
) {
  if (props.loading) return <PanelSkeleton rows={props.skeletonRows} />;
  if (props.isEmpty && props.error) return <PanelError error={props.error} />;
  if (props.isEmpty) return <PanelEmpty>{props.emptyLabel}</PanelEmpty>;
  return <>{props.children}</>;
}

export function openTradingView(symbol: string): void {
  window.open(getTradingViewUrl(symbol), "_blank", "noopener,noreferrer");
}

/** Opens the symbol on TradingView without triggering the row's own click. */
export function TradingViewButton({ symbol, label }: Readonly<{ symbol: string; label: string }>) {
  return (
    <button
      type="button"
      onClick={(event) => {
        event.stopPropagation();
        openTradingView(symbol);
      }}
      className="text-foreground-muted hover:text-foreground"
      title="Open on TradingView"
      aria-label={`Open ${label} on TradingView`}
    >
      <ExternalLink className="h-3.5 w-3.5" />
    </button>
  );
}
