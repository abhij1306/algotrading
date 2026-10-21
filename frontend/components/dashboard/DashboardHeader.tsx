import { useNavigate } from "react-router";
import { Zap } from "lucide-react";
import { Button } from "@/components/ui";
import { formatNumber, formatSignedPercent, roundsNegative } from "@/lib/format";
import type { MarketIndex } from "@/lib/queries/dashboard";
import { getCompactIndexLabel, getStatusBadge, HEADER_INDEX_SYMBOLS } from "./model";

const QUICK_ACTIONS = [{ icon: Zap, label: "Trade", path: "/terminal" }];

function HeaderIndex({ idx }: Readonly<{ idx: MarketIndex }>) {
  const isUp = !roundsNegative(idx.changePercent);
  return (
    <div className="flex items-center gap-1.5">
      <span className="text-xs text-foreground-muted">{getCompactIndexLabel(idx.symbol)}</span>
      <span className="text-sm font-semibold tabular-nums">{formatNumber(idx.value)}</span>
      <span className={`text-xs tabular-nums ${isUp ? "text-profit" : "text-loss"}`}>
        {formatSignedPercent(idx.changePercent)}
      </span>
      {idx.source === "websocket" && (
        <span className="w-1 h-1 rounded-full bg-profit animate-pulse" />
      )}
    </div>
  );
}

/** Title, connection badge, the NIFTY/BANK/IT ticker, the IST clock and quick links. */
export function DashboardHeader(
  props: Readonly<{
    marketOpen: boolean;
    isConnected: boolean;
    indices: MarketIndex[];
    istTime: string;
  }>,
) {
  const { marketOpen, isConnected, indices, istTime } = props;
  const navigate = useNavigate();
  const statusBadge = getStatusBadge({ marketOpen, isConnected });
  const StatusIcon = statusBadge.icon;
  return (
    <header className="flex items-center justify-between px-4 py-2 border-b border-border bg-surface h-11">
      <div className="flex items-center gap-3">
        <h1 className="text-base font-semibold">Dashboard</h1>
        <span
          className={`flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium ${statusBadge.className}`}
        >
          <StatusIcon className="w-3 h-3" /> {statusBadge.label}
        </span>
        {indices.length > 0 && (
          <div className="flex items-center gap-3 ml-2 pl-3 border-l border-border">
            {indices
              .filter((idx) => HEADER_INDEX_SYMBOLS.has(idx.symbol))
              .map((idx) => (
                <HeaderIndex key={idx.name} idx={idx} />
              ))}
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
  );
}
