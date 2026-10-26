import { useState, type CSSProperties } from "react";
import { Link } from "react-router";
import { Badge } from "@/components/atoms/badge";
import { buttonVariants } from "@/components/atoms/button";
import { fieldClass } from "@/components/atoms/field";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/atoms/table";
import { Text } from "@/components/atoms/text";
import { QuerySection } from "@/components/molecules/query-section";
import { SortableHead } from "@/components/molecules/sortable-head";
import {
  MetricWithDelta,
  NoOpFlag,
  RunStatusBadge,
  SPLIT_LABEL,
} from "@/components/organisms/backtests/run-parts";
import { useBacktestRuns } from "@/lib/queries/backtests";
import {
  dsrText,
  filterRuns,
  groupRuns,
  longestDd,
  MAX_COMPARE,
  nextSort,
  NO_FILTER,
  RUN_COLUMNS,
  runFacets,
  STICKY_METRICS,
  toggleSelection,
  type MetricDef,
  type RunFilter,
  type RunRow,
  type RunSortKey,
  type RunSummary,
  type Sort,
  type Split,
  type SweepGroup,
} from "@/lib/runs";
import { cn } from "@/lib/utils";

// The pick box, the variant and the first STICKY_METRICS metrics stay put while the rest scroll
// (from md up; a phone scrolls the whole row).
const PICK_WIDTH = 40;
const VARIANT_WIDTH = 240;
const METRIC_WIDTH = 104;
const stickyLeft = (column: number) =>
  column === 0 ? 0 : PICK_WIDTH + (column > 1 ? VARIANT_WIDTH + (column - 2) * METRIC_WIDTH : 0);
const stickyWidth = (column: number) => {
  if (column === 0) return PICK_WIDTH;
  return column === 1 ? VARIANT_WIDTH : METRIC_WIDTH;
};
const LAST_STICKY = STICKY_METRICS + 1;

function stickyProps(column: number): { className?: string; style?: CSSProperties } {
  if (column > LAST_STICKY) return {};
  const width = stickyWidth(column);
  return {
    className: cn(
      "bg-background md:sticky md:z-10",
      column === LAST_STICKY && "md:border-r md:border-line",
    ),
    style: { left: stickyLeft(column), width, minWidth: width, maxWidth: width },
  };
}

function metricText(def: MetricDef, run: RunSummary): string | undefined {
  if (def.key === "longest_dd_sessions") return longestDd(run.metrics);
  if (def.key === "dsr_prob") return dsrText(run.metrics);
  return undefined;
}

function VariantCell({ row }: Readonly<{ row: RunRow }>) {
  const { identity, binding, data } = row.run;
  return (
    <div className={cn("min-w-0 whitespace-normal", row.validates && "pl-4")}>
      <Link
        to={`/hat/backtests/${identity.run_id}`}
        className="font-medium break-words hover:text-accent"
      >
        {row.validates ? <span className="text-foreground-muted">↳ </span> : null}
        {identity.variant}
      </Link>
      <p className="text-xs break-words text-foreground-secondary" title={identity.change}>
        {identity.change || "baseline"}
      </p>
      <div className="mt-0.5 flex flex-wrap gap-x-3 text-xs">
        {data.split === "hold_out" ? <Badge tone="mode">{SPLIT_LABEL.hold_out}</Badge> : null}
        {identity.status === "complete" ? null : <RunStatusBadge status={identity.status} />}
        <NoOpFlag binding={binding} />
      </div>
    </div>
  );
}

function SweepTable({
  sweep,
  sort,
  onSort,
  selected,
  onToggle,
}: Readonly<{
  sweep: SweepGroup;
  sort: Sort<RunSortKey> | null;
  onSort: (key: RunSortKey) => void;
  selected: readonly string[];
  onToggle: (id: string) => void;
}>) {
  const dir = (key: RunSortKey) => (sort?.key === key ? sort.dir : null);
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead {...stickyProps(0)}>
            <span className="sr-only">Compare</span>
          </TableHead>
          <SortableHead {...stickyProps(1)} dir={dir("variant")} onSort={() => onSort("variant")}>
            Variant · change
          </SortableHead>
          {RUN_COLUMNS.map((def, index) => (
            <SortableHead
              key={def.key}
              numeric
              {...stickyProps(index + 2)}
              dir={dir(def.key)}
              onSort={() => onSort(def.key)}
            >
              {def.label}
            </SortableHead>
          ))}
          <TableHead>Period</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {sweep.rows.map((row) => {
          const { identity, data, metrics } = row.run;
          const picked = selected.includes(identity.run_id);
          return (
            <TableRow key={identity.run_id}>
              <TableCell {...stickyProps(0)} className={cn(stickyProps(0).className, "align-top")}>
                <input
                  type="checkbox"
                  checked={picked}
                  onChange={() => onToggle(identity.run_id)}
                  aria-label={`Compare ${identity.variant}`}
                  className="size-4 accent-accent"
                />
              </TableCell>
              <TableCell {...stickyProps(1)} className={cn(stickyProps(1).className, "align-top")}>
                <VariantCell row={row} />
              </TableCell>
              {RUN_COLUMNS.map((def, index) => (
                <TableCell
                  key={def.key}
                  numeric
                  {...stickyProps(index + 2)}
                  className={cn(stickyProps(index + 2).className, "align-top")}
                >
                  <MetricWithDelta
                    def={def}
                    metrics={metrics}
                    base={row.base?.metrics}
                    text={metricText(def, row.run)}
                  />
                </TableCell>
              ))}
              <TableCell className="align-top text-xs text-foreground-secondary">
                {data.period_start} to {data.period_end}
              </TableCell>
            </TableRow>
          );
        })}
      </TableBody>
    </Table>
  );
}

function Select({
  label,
  value,
  onChange,
  options,
  all,
}: Readonly<{
  label: string;
  value: string | null;
  onChange: (value: string | null) => void;
  options: readonly { value: string; label: string }[];
  all: string;
}>) {
  return (
    <label className="flex flex-col gap-1 text-xs text-foreground-muted">
      <span>{label}</span>
      <select
        className={fieldClass}
        value={value ?? ""}
        onChange={(event) => onChange(event.target.value || null)}
      >
        <option value="">{all}</option>
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  );
}

const SWEEP_COMMAND = `python -m app.hat.backtest --strategy-version <v> --universe nifty500 \\
  --from <d> --to <d> --variants sweep.yaml`;

/** No run folder yet: how to start a sweep. */
export function EmptyRuns() {
  return (
    <div className="max-w-3xl space-y-3">
      <Text>No backtest runs yet.</Text>
      <Text tone="muted">
        Start a sweep from <code className="font-mono">backend/</code> (runbook, Backtest research
        variants), or with New run or sweep above. Each variant writes its run folder under{" "}
        <code className="font-mono">HAT_DATA_ROOT/backtests/runs/</code> and shows here.
      </Text>
      <pre className="overflow-x-auto rounded-md bg-subtle px-4 py-3 font-mono text-xs">
        {SWEEP_COMMAND}
      </pre>
    </div>
  );
}

function Runs({ runs }: Readonly<{ runs: RunSummary[] }>) {
  const [filter, setFilter] = useState<RunFilter>(NO_FILTER);
  const [sort, setSort] = useState<Sort<RunSortKey> | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const facets = runFacets(runs);
  const groups = groupRuns(filterRuns(runs, filter), runs, sort);
  const canCompare = selected.length >= 2;
  const set = (patch: Partial<RunFilter>) => setFilter((old) => ({ ...old, ...patch }));
  return (
    <div className="space-y-8">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="flex flex-wrap items-end gap-4">
          <Select
            label="Strategy"
            value={filter.strategy}
            onChange={(strategy) => set({ strategy })}
            options={facets.strategies.map((s) => ({ value: s, label: s }))}
            all="All strategies"
          />
          <Select
            label="Split"
            value={filter.split}
            onChange={(split) => set({ split: split as Split | null })}
            options={(["in_sample", "hold_out"] as const).map((s) => ({
              value: s,
              label: SPLIT_LABEL[s],
            }))}
            all="Both splits"
          />
          <Select
            label="Sweep"
            value={filter.sweep}
            onChange={(sweep) => set({ sweep })}
            options={facets.sweeps.map((s) => ({
              value: s.id,
              label: `${s.name} (${s.runs})`,
            }))}
            all="All sweeps"
          />
        </div>
        <div className="flex items-center gap-3">
          <span className="text-xs text-foreground-muted">
            {selected.length} of {MAX_COMPARE} picked
          </span>
          <Link
            to={`/hat/backtests/compare?ids=${selected.map(encodeURIComponent).join(",")}`}
            aria-disabled={!canCompare}
            tabIndex={canCompare ? undefined : -1}
            className={cn(
              buttonVariants({ variant: "primary", size: "sm" }),
              !canCompare && "pointer-events-none opacity-50",
            )}
          >
            Compare
          </Link>
        </div>
      </div>
      {groups.length === 0 ? <Text tone="muted">No runs match the filters.</Text> : null}
      {groups.map((group) => (
        <section key={group.strategy} aria-label={group.strategy} className="space-y-8">
          <h3 className="text-lg">{group.strategy}</h3>
          {group.sweeps.map((sweep) => (
            <div key={sweep.id} className="space-y-2">
              <h4 className="flex flex-wrap items-baseline gap-x-3 text-sm">
                <span>{sweep.name}</span>
                <span className="font-mono text-xs text-foreground-muted">{sweep.id}</span>
                <span className="text-xs text-foreground-muted">
                  {sweep.rows.length} {sweep.rows.length === 1 ? "run" : "runs"}
                </span>
              </h4>
              <SweepTable
                sweep={sweep}
                sort={sort}
                onSort={(key) => setSort((old) => nextSort(old, key))}
                selected={selected}
                onToggle={(id) => setSelected((old) => toggleSelection(old, id))}
              />
            </div>
          ))}
        </section>
      ))}
      <Text tone="muted" className="text-xs">
        Small figures under a value are its change against the variant's base run, green when
        better. Longest DD is in sessions; DSR prob shows the trials it counts.
      </Text>
    </div>
  );
}

/** The run folders grouped by strategy and sweep, with filters, sorting and compare picks. */
export function RunsView() {
  const runs = useBacktestRuns();
  return (
    <QuerySection
      title="Runs"
      query={runs}
      isEmpty={() => false}
      empty=""
      meta={(data) => (data.length > 0 ? `${data.length} runs` : null)}
    >
      {(data) => (data.length === 0 ? <EmptyRuns /> : <Runs runs={data} />)}
    </QuerySection>
  );
}
