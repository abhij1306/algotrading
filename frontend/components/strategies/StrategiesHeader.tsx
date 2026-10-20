import { Badge, Button } from "@/components/ui";
import type { StrategyStatus } from "@/lib/queries/strategies";

interface StrategiesHeaderProps {
  status: StrategyStatus | null;
  showAll: boolean;
  /** A strategy action is in flight; scan and halt wait for it. */
  busy: boolean;
  onToggleShowAll: () => void;
  onRunScan: () => void;
  onToggleHalt: () => void;
}

/** Title, regime and halt state, and the page-wide strategy actions. */
export function StrategiesHeader({
  status,
  showAll,
  busy,
  onToggleShowAll,
  onRunScan,
  onToggleHalt,
}: Readonly<StrategiesHeaderProps>) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border bg-surface px-4 py-3">
      <div>
        <h1 className="text-xl font-semibold text-foreground">Strategies</h1>
        <p className="text-sm text-foreground-secondary">
          VCP scanner, queue management, open positions, and backtest workflow for the selected
          universe.
        </p>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant={status?.regime?.regime === "BULL" ? "profit" : "warning"}>
          {status?.regime?.regime ?? "--"} · Risk {status?.regime?.risk_pct ?? "--"}%
        </Badge>
        <Badge variant={status?.halted ? "loss" : "neutral"}>
          {status?.halted ? "HALTED" : "ACTIVE"}
        </Badge>
        <Button variant="secondary" size="sm" onClick={onToggleShowAll}>
          {showAll ? "Hide C Grade" : "Show All"}
        </Button>
        <Button variant="secondary" size="sm" onClick={onRunScan} disabled={busy}>
          {busy ? "Running..." : "Run Scan Now"}
        </Button>
        <Button
          variant={status?.halted ? "secondary" : "destructive"}
          size="sm"
          onClick={onToggleHalt}
          disabled={busy}
        >
          {status?.halted ? "Resume" : "Halt"}
        </Button>
      </div>
    </div>
  );
}
