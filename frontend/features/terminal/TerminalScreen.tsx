import { useState } from "react";
import { usePositionsBook, type PositionItem } from "@/lib/queries/terminal";
import { useConnectionStatus } from "@/lib/realtime/hooks";
import { OrderTicket } from "./components/OrderTicket";
import { PositionsPanel } from "./components/PositionsPanel";
import { TerminalHeader } from "./components/TerminalHeader";
import { WatchlistSearch } from "./components/WatchlistSearch";
import { WatchlistTable } from "./components/WatchlistTable";
import { useLiveRows } from "./hooks/use-live-rows";
import { useSquareAction } from "./hooks/use-square-action";
import { useTerminalSymbols } from "./hooks/use-terminal-symbols";
import { DEFAULT_TRADING_MODE, type TradingMode } from "./lib/order-defaults";
import { currentRow, summarizePositions, type TerminalView } from "./lib/terminal-view";

const NO_POSITIONS: PositionItem[] = [];

/** The terminal: watchlist, positions, and the order ticket. */
export function TerminalScreen() {
  const isConnected = useConnectionStatus() === "connected";
  const [tradingMode, setTradingMode] = useState<TradingMode>(DEFAULT_TRADING_MODE);
  const terminalView: TerminalView = "positions";
  const [orderMessage, setOrderMessage] = useState<string | null>(null);
  const symbols = useTerminalSymbols();
  const positionsQuery = usePositionsBook(true);
  const { squaring, squarePosition } = useSquareAction(setOrderMessage);
  const { watchlist, positions } = useLiveRows({
    watchlist: symbols.items,
    selectedSymbol: symbols.selectedSymbol,
    positions: positionsQuery.data?.rows ?? NO_POSITIONS,
  });
  const currentSymbol = currentRow(watchlist, symbols.selectedSymbol);
  const activeTradeSymbol = currentSymbol?.symbol ?? "";

  return (
    <div className="flex flex-col h-[calc(100vh-48px)]">
      <div
        className={`px-4 py-1.5 text-xs border-b border-border ${tradingMode === "PAPER" ? "bg-amber-500/10 text-amber-400" : "bg-loss-bg text-loss"}`}
      >
        Mode: {tradingMode}
      </div>
      <TerminalHeader
        currentSymbol={currentSymbol}
        isConnected={isConnected}
        tradingMode={tradingMode}
        onModeChange={setTradingMode}
      />

      {/* Main Content */}
      <div className="flex-1 flex overflow-hidden">
        {/* Left Sidebar - Watchlist */}
        <div className="w-64 flex-shrink-0 flex flex-col border-r border-border bg-surface">
          <WatchlistSearch onAdded={symbols.setSelectedSymbol} />
          <div className="flex-1 overflow-y-auto">
            <WatchlistTable
              rows={watchlist}
              isLoading={symbols.isLoading}
              error={symbols.error}
              selectedSymbol={symbols.selectedSymbol}
              onSelect={symbols.setSelectedSymbol}
            />
          </div>
        </div>

        {/* Center - Positions */}
        <div className="flex-1 flex flex-col">
          <div className="flex-1 flex items-center justify-center">
            <PositionsPanel
              summary={summarizePositions(positions)}
              error={positionsQuery.isError ? "Failed to load positions" : null}
              positions={positions}
              liveStale={positionsQuery.data?.liveStale ?? false}
              squaring={squaring}
              onSquare={squarePosition}
            />
          </div>
        </div>

        {/* Right Sidebar - Order Panel */}
        <OrderTicket
          tradingMode={tradingMode}
          terminalView={terminalView}
          activeTradeSymbol={activeTradeSymbol}
          message={orderMessage}
          onMessage={setOrderMessage}
        />
      </div>
    </div>
  );
}
