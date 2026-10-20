import { memo, type ReactNode } from "react";
import { useNavigate } from "react-router";
import { ChevronRight, TrendingDown, TrendingUp } from "lucide-react";
import { Button } from "@/components/ui";
import { formatInr, formatSignedPercent, roundsNegative } from "@/lib/format";
import type { DashboardWatchlistItem } from "@/lib/queries/dashboard";
import { PanelBody, TradingViewButton } from "./PanelStates";
import type { PanelData } from "./useDashboardData";

const WatchlistRow = memo(function WatchlistRow({
  stock,
  onClick,
}: Readonly<{ stock: DashboardWatchlistItem; onClick: () => void }>) {
  const hasChangePercent =
    typeof stock.changePercent === "number" && Number.isFinite(stock.changePercent);
  const isUp = hasChangePercent && !roundsNegative(stock.changePercent);
  let changeClass = "text-foreground-muted";
  if (hasChangePercent) {
    changeClass = isUp ? "text-profit" : "text-loss";
  }
  let changeContent: ReactNode = "Unavailable";
  if (hasChangePercent) {
    changeContent = (
      <div className="flex items-center justify-end gap-1">
        {isUp ? <TrendingUp className="w-3 h-3" /> : <TrendingDown className="w-3 h-3" />}
        {formatSignedPercent(stock.changePercent)}
      </div>
    );
  }
  return (
    <tr onClick={onClick} className="cursor-pointer hover:bg-surface transition-colors">
      <td className="py-2.5 pl-4">
        <div className="flex items-center gap-1">
          <span className="font-medium">{stock.symbol}</span>
          <TradingViewButton symbol={stock.symbol} label={stock.symbol} />
        </div>
      </td>
      <td className="py-2.5 text-right tabular-nums">{formatInr(stock.price)}</td>
      <td className={`py-2.5 pr-4 text-right tabular-nums ${changeClass}`}>{changeContent}</td>
    </tr>
  );
});

/** The first watchlist rows, with a live dot while quotes stream. */
export function WatchlistPanel(
  props: Readonly<{ data: PanelData<DashboardWatchlistItem>; isLive: boolean }>,
) {
  const { data, isLive } = props;
  const navigate = useNavigate();
  return (
    <div className="lg:col-span-2">
      <div className="flex items-center justify-between px-4 py-2 border-b border-border bg-surface h-9">
        <div className="flex items-center gap-2">
          <TrendingUp className="w-4 h-4 text-foreground-muted" />
          <span className="font-medium text-sm">Watchlist</span>
          {isLive && <span className="w-1.5 h-1.5 rounded-full bg-profit" />}
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
      <PanelBody
        loading={data.loading}
        skeletonRows={5}
        error={data.error}
        isEmpty={data.rows.length === 0}
        emptyLabel="No stocks in watchlist"
      >
        <table className="w-full">
          <thead className="text-xs text-foreground-muted border-b border-border">
            <tr>
              <th className="py-2 pl-4 text-left font-normal">Symbol</th>
              <th className="py-2 text-right font-normal">Price</th>
              <th className="py-2 pr-4 text-right font-normal">Change</th>
            </tr>
          </thead>
          <tbody className="text-sm">
            {data.rows.map((stock) => (
              <WatchlistRow
                key={stock.symbol}
                stock={stock}
                onClick={() => void navigate(`/terminal?symbol=${stock.symbol}`)}
              />
            ))}
          </tbody>
        </table>
      </PanelBody>
    </div>
  );
}
