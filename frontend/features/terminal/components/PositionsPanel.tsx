import { Button } from "@/components/ui";
import { formatNumber } from "@/lib/format";
import type { PositionItem } from "@/lib/queries/terminal";
import { pnlTone, type PositionsSummary } from "../lib/terminal-view";

function PnlValue({ value }: Readonly<{ value: number }>) {
  return <span className={`${pnlTone(value)} tabular-nums`}>{formatNumber(value)}</span>;
}

export function PositionsPanel({
  summary: positionsSummary,
  error: positionsError,
  positions,
  liveStale,
  squaring,
  onSquare: squarePosition,
}: Readonly<{
  summary: PositionsSummary;
  error: string | null;
  positions: PositionItem[];
  /** The broker sync failed, so LIVE positions are unknown or the last synced values. */
  liveStale: boolean;
  /** A square-off is in flight; a second click could send a duplicate exit. */
  squaring: boolean;
  onSquare: (row: PositionItem) => void;
}>) {
  return (
    <div className="h-full w-full overflow-auto p-2">
      <div className="mb-2 flex items-center gap-4 text-xs">
        <span className="text-foreground-muted">
          Live Net P&L: <PnlValue value={positionsSummary.net_pnl_live} />
        </span>
        <span className="text-foreground-muted">
          Paper Net P&L: <PnlValue value={positionsSummary.net_pnl_paper} />
        </span>
        <span className="text-foreground-muted">
          Total: <PnlValue value={positionsSummary.net_pnl_total} />
        </span>
      </div>
      {positionsError && <div className="mb-2 text-xs text-loss">{positionsError}</div>}
      {liveStale && (
        <div className="mb-2 text-xs text-amber-400">
          Broker unavailable: live positions could not be refreshed. Any shown are the last synced
          values and cannot be squared until it reconnects.
        </div>
      )}
      <table className="w-full text-xs">
        <thead className="sticky top-0 bg-surface border-b border-border text-foreground-muted">
          <tr>
            <th className="py-1 text-left font-normal">Symbol</th>
            <th className="py-1 text-left font-normal">Mode</th>
            <th className="py-1 text-left font-normal">Side</th>
            <th className="py-1 text-right font-normal">Qty</th>
            <th className="py-1 text-right font-normal">Entry</th>
            <th className="py-1 text-right font-normal">LTP</th>
            <th className="py-1 text-right font-normal">Unrealized</th>
            <th className="py-1 text-right font-normal">Net P&L</th>
            <th className="py-1 text-right font-normal">Action</th>
          </tr>
        </thead>
        <tbody>
          {positions.map((pos) => (
            <tr
              key={`${pos.mode}-${pos.id}`}
              className={`border-b border-border/60 ${pos.stale || positionsError ? "opacity-50" : ""}`}
            >
              <td className="py-1 pr-2">{pos.symbol}</td>
              <td className="py-1 pr-2">{pos.mode || "LIVE"}</td>
              <td className="py-1 pr-2">{pos.net_qty >= 0 ? "LONG" : "SHORT"}</td>
              <td className="py-1 pr-2 text-right tabular-nums">{Math.abs(pos.net_qty)}</td>
              <td className="py-1 pr-2 text-right tabular-nums">{formatNumber(pos.entry_price)}</td>
              <td className="py-1 pr-2 text-right tabular-nums">
                {formatNumber(pos.current_price)}
              </td>
              <td className={`py-1 pr-2 text-right tabular-nums ${pnlTone(pos.unrealized_pnl)}`}>
                {formatNumber(pos.unrealized_pnl)}
              </td>
              <td className={`py-1 pr-2 text-right tabular-nums ${pnlTone(pos.net_pnl)}`}>
                {formatNumber(pos.net_pnl)}
              </td>
              <td className="py-1 pr-2 text-right">
                <Button
                  size="sm"
                  variant="secondary"
                  className="h-6 px-2 text-xs"
                  disabled={squaring || pos.stale}
                  onClick={() => squarePosition(pos)}
                >
                  Square
                </Button>
              </td>
            </tr>
          ))}
          {positions.length === 0 && (
            <tr>
              <td colSpan={9} className="py-8 text-center text-foreground-muted">
                No open positions
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
