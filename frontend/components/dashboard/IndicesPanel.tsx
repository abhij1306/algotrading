import { memo } from "react";
import { useNavigate } from "react-router";
import { Activity, Globe } from "lucide-react";
import { formatNumber, formatSignedPercent, roundsNegative } from "@/lib/format";
import type { MarketIndex } from "@/lib/queries/dashboard";
import { getMarketSectionMeta } from "./model";
import { PanelBody, TradingViewButton } from "./PanelStates";
import type { PanelData } from "./useDashboardData";

const IndexRow = memo(function IndexRow({
  idx,
  onClick,
}: Readonly<{ idx: MarketIndex; onClick: () => void }>) {
  const isUp = !roundsNegative(idx.changePercent);
  return (
    <tr onClick={onClick} className="cursor-pointer hover:bg-surface transition-colors">
      <td className="py-2 pl-4">
        <div className="flex items-center gap-2">
          <span className="font-medium">{idx.name}</span>
          <TradingViewButton symbol={idx.symbol} label={idx.name} />
          {idx.source === "websocket" && <span className="w-1.5 h-1.5 rounded-full bg-profit" />}
        </div>
      </td>
      <td className="py-2 text-right tabular-nums">{formatNumber(idx.value)}</td>
      <td className={`py-2 pr-4 text-right tabular-nums ${isUp ? "text-profit" : "text-loss"}`}>
        {formatSignedPercent(idx.changePercent)}
      </td>
    </tr>
  );
});

/** NSE indices while the market is open, the Yahoo global overview otherwise. */
export function IndicesPanel(
  props: Readonly<{ data: PanelData<MarketIndex>; marketOpen: boolean; isConnected: boolean }>,
) {
  const { data, marketOpen, isConnected } = props;
  const navigate = useNavigate();
  const meta = getMarketSectionMeta({ marketOpen, isConnected });
  return (
    <div className="lg:col-span-3 border-r border-border">
      <div className="flex items-center justify-between px-4 py-2 border-b border-border bg-surface h-9">
        <div className="flex items-center gap-2">
          {marketOpen ? (
            <Activity className="w-4 h-4 text-foreground-muted" />
          ) : (
            <Globe className="w-4 h-4 text-foreground-muted" />
          )}
          <span className="font-medium text-sm">{meta.title}</span>
        </div>
        <span className="text-xs text-foreground-muted">{meta.subtitle}</span>
      </div>
      <PanelBody
        loading={data.loading}
        skeletonRows={4}
        error={data.error}
        isEmpty={data.rows.length === 0}
        emptyLabel="No market data"
      >
        <table className="w-full">
          <thead className="text-xs text-foreground-muted border-b border-border">
            <tr>
              <th className="py-2 pl-4 text-left font-normal">Index</th>
              <th className="py-2 text-right font-normal">Value</th>
              <th className="py-2 pr-4 text-right font-normal">Change</th>
            </tr>
          </thead>
          <tbody className="text-sm">
            {data.rows.map((idx) => (
              <IndexRow
                key={idx.name}
                idx={idx}
                onClick={() => void navigate(`/terminal?symbol=${idx.symbol}`)}
              />
            ))}
          </tbody>
        </table>
      </PanelBody>
    </div>
  );
}
