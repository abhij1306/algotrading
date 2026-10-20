import { CandlestickChart, ListOrdered } from "lucide-react";
import { Button } from "@/components/ui";
import { formatNumber } from "@/lib/format";
import type { OptionsBoard } from "@/lib/queries/terminal";
import type { TerminalView } from "../lib/terminal-view";

export function ViewToolbar({
  view,
  onViewChange: setTerminalView,
  board,
}: Readonly<{
  view: TerminalView;
  onViewChange: (view: TerminalView) => void;
  board: OptionsBoard | null;
}>) {
  return (
    <div className="h-9 flex items-center justify-between px-3 border-b border-border">
      <div className="flex items-center gap-2">
        <Button
          variant={view === "options" ? "secondary" : "ghost"}
          size="sm"
          className="h-7 gap-1 text-xs"
          onClick={() => setTerminalView("options")}
        >
          <CandlestickChart className="w-4 h-4" /> Option Chain
        </Button>
        <Button
          variant={view === "positions" ? "secondary" : "ghost"}
          size="sm"
          className="h-7 gap-1 text-xs"
          onClick={() => setTerminalView("positions")}
        >
          <ListOrdered className="w-4 h-4" /> Positions
        </Button>
      </div>
      {/* Orders are placed from the ticket only: one Buy/Sell pair, one LIVE confirmation path. */}
      {view === "options" && board && (
        <span className="text-xs text-foreground-muted">
          {board.underlying} Spot: {formatNumber(board.spot_price)}
        </span>
      )}
    </div>
  );
}
