import { memo } from "react";
import { ExternalLink, Loader2 } from "lucide-react";
import { formatNumber } from "@/lib/format";
import type { WatchlistItem } from "@/lib/queries/terminal";
import { openTradingView } from "../lib/open-trading-view";
import { getChangeText, getChangeTone } from "../lib/terminal-view";

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
        <div className="tabular-nums font-medium">{formatNumber(stock.ltp)}</div>
        <div className={`tabular-nums text-xs ${getChangeTone(stock.changePct)}`}>
          {getChangeText(stock.changePct)}
        </div>
      </td>
    </tr>
  );
});

export function WatchlistTable({
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
