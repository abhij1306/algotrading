import { useState } from "react";
import { Bell, ExternalLink, Settings } from "lucide-react";
import { Button } from "@/components/ui";
import { formatNumber } from "@/lib/format";
import type { WatchlistItem } from "@/lib/queries/terminal";
import { openTradingView } from "../lib/open-trading-view";
import type { TradingMode } from "../lib/order-defaults";
import { TIMEFRAMES, getChangeText, getChangeTone } from "../lib/terminal-view";

export function TerminalHeader({
  currentSymbol,
  isConnected,
  tradingMode,
  onModeChange: setTradingMode,
}: Readonly<{
  currentSymbol: WatchlistItem | null;
  isConnected: boolean;
  tradingMode: TradingMode;
  onModeChange: (mode: TradingMode) => void;
}>) {
  const [selectedTimeframe, setSelectedTimeframe] = useState("D");
  return (
    <header className="flex items-center justify-between px-4 py-2 border-b border-border bg-surface">
      <div className="flex items-center gap-6">
        {/* Symbol Info */}
        <div className="flex items-center gap-4">
          <div>
            <div className="flex items-center gap-2">
              <span className="font-semibold text-lg">{currentSymbol?.symbol || "--"}</span>
              {currentSymbol?.symbol && (
                <button
                  type="button"
                  onClick={() => openTradingView(currentSymbol.symbol)}
                  className="text-foreground-muted hover:text-foreground"
                  title="Open on TradingView"
                  aria-label={`Open ${currentSymbol.symbol} on TradingView`}
                >
                  <ExternalLink className="h-4 w-4" />
                </button>
              )}
              <span className="px-1.5 py-0.5 rounded text-xs bg-background-tertiary">NSE</span>
              {isConnected && <span className="w-1.5 h-1.5 rounded-full bg-profit" />}
            </div>
            <div className="text-xs text-foreground-muted truncate max-w-[150px]">
              {currentSymbol?.name || "Select a symbol"}
            </div>
          </div>
          <div className="text-right">
            <div className="font-semibold tabular-nums text-xl">
              {formatNumber(currentSymbol?.ltp)}
            </div>
            {currentSymbol && (
              <div className={`tabular-nums text-xs ${getChangeTone(currentSymbol.changePct)}`}>
                {getChangeText(currentSymbol.changePct)}
              </div>
            )}
          </div>
        </div>

        {/* Timeframes */}
        <div className="flex items-center gap-0.5">
          {TIMEFRAMES.map((tf) => (
            <Button
              key={tf}
              onClick={() => setSelectedTimeframe(tf)}
              variant={selectedTimeframe === tf ? "secondary" : "ghost"}
              size="sm"
              className="h-7 px-2 text-xs"
            >
              {tf}
            </Button>
          ))}
        </div>
      </div>

      <div className="flex items-center gap-1">
        <Button
          variant={tradingMode === "PAPER" ? "secondary" : "ghost"}
          size="sm"
          className="h-8 text-xs"
          onClick={() => setTradingMode("PAPER")}
        >
          PAPER
        </Button>
        <Button
          variant={tradingMode === "LIVE" ? "loss" : "ghost"}
          size="sm"
          className="h-8 text-xs"
          onClick={() => setTradingMode("LIVE")}
        >
          LIVE
        </Button>
        <Button variant="ghost" size="icon" className="h-8 w-8">
          <Bell className="w-4 h-4" />
        </Button>
        <Button variant="ghost" size="icon" className="h-8 w-8">
          <Settings className="w-4 h-4" />
        </Button>
      </div>
    </header>
  );
}
