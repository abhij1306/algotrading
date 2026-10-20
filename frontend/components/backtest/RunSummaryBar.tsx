import { Badge } from "@/components/ui";
import type { BacktestJob } from "@/lib/queries/backtest";
import { runStatusVariant } from "@/lib/status-variants";

/** Status, instrument, date range and scope of a run, in one line. */
export function RunSummaryBar({ job }: Readonly<{ job: BacktestJob }>) {
  return (
    <div className="flex h-9 shrink-0 items-center gap-3 border-b border-border bg-background-secondary px-4 text-xs text-foreground-secondary">
      <Badge variant={runStatusVariant(job.status)}>{job.status}</Badge>
      <span className="h-3 w-px bg-border" />
      <span>
        Instrument{" "}
        <strong className="text-foreground">{job.params.instrument_type.toUpperCase()}</strong>
      </span>
      <span className="h-3 w-px bg-border" />
      <span>
        Range{" "}
        <strong className="mono-num text-foreground">
          {job.params.start_date} → {job.params.end_date}
        </strong>
      </span>
      <span className="h-3 w-px bg-border" />
      <span>
        Scope{" "}
        <strong className="text-foreground">
          {job.result?.selection.scope || job.params.selection.mode}
        </strong>
      </span>
    </div>
  );
}
