import { useState } from "react";
import { Link } from "react-router";
import { buttonVariants } from "@/components/atoms/button";
import { fieldClass } from "@/components/atoms/field";
import { TableCell, TableRow } from "@/components/atoms/table";
import { DataTable } from "@/components/molecules/data-table";
import { noRows, QuerySection } from "@/components/molecules/query-section";
import { Overrides, RunStatus } from "@/components/organisms/backtests/run-parts";
import {
  filterRuns,
  formatMetric,
  MAX_COMPARE,
  runFacets,
  runName,
  toggleSelection,
  type RunSummary,
} from "@/lib/backtests";
import { useBacktestRuns } from "@/lib/queries/backtests";
import { cn } from "@/lib/utils";

const COLUMNS = [
  { label: "Compare" },
  { label: "Run" },
  { label: "Window" },
  { label: "Status" },
  { label: "Overrides" },
  { label: "Trades", numeric: true },
  { label: "Exp. R", numeric: true },
  { label: "CAGR", numeric: true },
  { label: "Max DD", numeric: true },
  { label: "DSR", numeric: true },
  { label: "PBO", numeric: true },
];

function RunRow({
  run,
  checked,
  onToggle,
}: Readonly<{ run: RunSummary; checked: boolean; onToggle: () => void }>) {
  const name = runName(run);
  return (
    <TableRow>
      <TableCell>
        <input
          type="checkbox"
          checked={checked}
          onChange={onToggle}
          aria-label={`Compare ${name}`}
          className="size-4 accent-accent"
        />
      </TableCell>
      <TableCell>
        <Link to={`/hat/backtests/${run.id}`} className="hover:text-accent">
          {name}
        </Link>
        {run.has_report ? null : (
          <span className="ml-2 text-xs text-foreground-muted">metrics only</span>
        )}
      </TableCell>
      <TableCell className="text-foreground-secondary">
        {run.period_start} to {run.period_end}
      </TableCell>
      <TableCell>
        <RunStatus status={run.status} />
      </TableCell>
      <TableCell className="max-w-72 whitespace-normal">
        <Overrides overrides={run.overrides} />
      </TableCell>
      <TableCell numeric>{formatMetric(run.headline["trades"], "int")}</TableCell>
      <TableCell numeric>{formatMetric(run.headline["expectancy_r"], "r")}</TableCell>
      <TableCell numeric>{formatMetric(run.headline["cagr"], "pct")}</TableCell>
      <TableCell numeric>{formatMetric(run.headline["max_drawdown"], "drawdown")}</TableCell>
      <TableCell numeric>{formatMetric(run.dsr, "ratio")}</TableCell>
      <TableCell numeric>{formatMetric(run.pbo, "ratio")}</TableCell>
    </TableRow>
  );
}

function Filters({
  runs,
  family,
  sweep,
  onFamily,
  onSweep,
}: Readonly<{
  runs: RunSummary[];
  family: string | null;
  sweep: string | null;
  onFamily: (value: string | null) => void;
  onSweep: (value: string | null) => void;
}>) {
  const facets = runFacets(runs);
  return (
    <div className="flex flex-wrap items-end gap-4">
      <label className="flex flex-col gap-1 text-xs text-foreground-muted">
        Family
        <select
          className={fieldClass}
          value={family ?? ""}
          onChange={(event) => onFamily(event.target.value || null)}
        >
          <option value="">All families</option>
          {facets.families.map((key) => (
            <option key={key} value={key}>
              {key}
            </option>
          ))}
        </select>
      </label>
      <label className="flex flex-col gap-1 text-xs text-foreground-muted">
        Sweep
        <select
          className={fieldClass}
          value={sweep ?? ""}
          onChange={(event) => onSweep(event.target.value || null)}
        >
          <option value="">All runs</option>
          {facets.sweeps.map((item) => (
            <option key={item.id} value={item.id}>
              {item.label} ({item.runs} variants)
            </option>
          ))}
        </select>
      </label>
    </div>
  );
}

/** The recorded runs, newest first, filtered by family and sweep; two to four tick for compare. */
export function RunsList({ className }: Readonly<{ className?: string }>) {
  const runs = useBacktestRuns();
  const [family, setFamily] = useState<string | null>(null);
  const [sweep, setSweep] = useState<string | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const canCompare = selected.length >= 2;
  return (
    <QuerySection
      title="Runs"
      query={runs}
      isEmpty={noRows}
      empty="No backtest runs yet."
      meta={(data) => `${filterRuns(data, { family, sweep }).length} of ${data.length}`}
      className={className}
    >
      {(data) => (
        <div className="space-y-4">
          <div className="flex flex-wrap items-end justify-between gap-4">
            <Filters
              runs={data}
              family={family}
              sweep={sweep}
              onFamily={setFamily}
              onSweep={setSweep}
            />
            <div className="flex items-center gap-3">
              <span className="text-xs text-foreground-muted">
                {selected.length} of {MAX_COMPARE} picked
              </span>
              <Link
                to={`/hat/backtests/compare?ids=${selected.join(",")}`}
                aria-disabled={!canCompare}
                className={cn(
                  buttonVariants({ variant: "primary", size: "sm" }),
                  !canCompare && "pointer-events-none opacity-50",
                )}
              >
                Compare
              </Link>
            </div>
          </div>
          <DataTable columns={COLUMNS}>
            {filterRuns(data, { family, sweep }).map((run) => (
              <RunRow
                key={run.id}
                run={run}
                checked={selected.includes(run.id)}
                onToggle={() => setSelected((old) => toggleSelection(old, run.id))}
              />
            ))}
          </DataTable>
        </div>
      )}
    </QuerySection>
  );
}
