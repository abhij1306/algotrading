import { useState } from "react";
import { Badge, type Tone } from "@/components/atoms/badge";
import { Table, TableBody, TableCell, TableHeader, TableRow } from "@/components/atoms/table";
import { DataTable } from "@/components/molecules/data-table";
import { SortableHead } from "@/components/molecules/sortable-head";
import { formatNumber } from "@/lib/format";
import {
  columnLabel,
  deltaTone,
  formatCell,
  formatDelta,
  formatMetric,
  metricDelta,
  metricValue,
  nextSort,
  overrideLines,
  sortRecords,
  type Binding,
  type BucketRow,
  type CsvRecord,
  type MetricDef,
  type Metrics,
  type RunStatus,
  type Sort,
  type Split,
} from "@/lib/runs";
import { cn } from "@/lib/utils";

const STATUS_TONE: Record<RunStatus, Tone> = {
  complete: "profit",
  failed: "loss",
  stopped_early: "warning",
};

/** The run's status; a failed or stopped run adds its failure code. */
export function RunStatusBadge({
  status,
  error,
}: Readonly<{ status: RunStatus; error?: string | null }>) {
  const label = status.replaceAll("_", " ");
  return (
    <Badge tone={STATUS_TONE[status] ?? "muted"}>{error ? `${label}: ${error}` : label}</Badge>
  );
}

export const SPLIT_LABEL: Record<Split, string> = { in_sample: "in-sample", hold_out: "hold-out" };

/** The plain flag of a variant whose change never triggered: its trades match the base's. */
export function NoOpFlag({ binding }: Readonly<{ binding: Binding }>) {
  if (binding.no_op !== true) return null;
  return (
    <Badge tone="warning" title="The trade list matches the base run's: the change never triggered">
      no-op: same trades as base
    </Badge>
  );
}

/** A metric's value with, below it, its change against the base run in the better/worse tone. */
export function MetricWithDelta({
  def,
  metrics,
  base,
  text,
}: Readonly<{
  def: MetricDef;
  metrics: Partial<Metrics>;
  base: Partial<Metrics> | null | undefined;
  /** The value's text when it is more than the number (longest DD, DSR). */
  text?: string;
}>) {
  const delta = metricDelta(metrics, base, def.key);
  const tone = deltaTone(delta, def.better);
  return (
    <span className="inline-flex flex-col items-end leading-tight">
      <span>{text ?? formatMetric(metricValue(metrics, def.key), def.format)}</span>
      {delta === null ? null : (
        <span
          className={cn(
            "text-xs",
            tone === "profit" && "text-profit",
            tone === "loss" && "text-loss",
            tone === "muted" && "text-foreground-muted",
          )}
        >
          {formatDelta(delta, def.format)}
        </span>
      )}
    </span>
  );
}

/** What the run's config overrides, or "none" for a baseline. */
export function Overrides({ overrides }: Readonly<{ overrides: Record<string, unknown> }>) {
  const lines = overrideLines(overrides);
  if (lines.length === 0) return <span className="text-foreground-muted">none</span>;
  return (
    <ul className="space-y-0.5 font-mono text-xs">
      {lines.map((line) => (
        <li key={line} className="break-all">
          {line}
        </li>
      ))}
    </ul>
  );
}

const BUCKET_COLUMNS = [
  { label: "Bucket" },
  { label: "Trades", numeric: true },
  { label: "Share %", numeric: true },
  { label: "Win %", numeric: true },
  { label: "Exp. R", numeric: true },
];

/** One breakdown's buckets; the extra columns show only when the breakdown has them. */
export function BucketTable({ rows }: Readonly<{ rows: readonly BucketRow[] }>) {
  const hold = rows.some((row) => row.avg_hold_sessions != null);
  const pnl = rows.some((row) => row.net_pnl != null);
  const columns = [
    ...BUCKET_COLUMNS,
    ...(hold ? [{ label: "Avg hold", numeric: true }] : []),
    ...(pnl ? [{ label: "Net P&L", numeric: true }] : []),
  ];
  return (
    <DataTable columns={columns}>
      {rows.map((row) => (
        <TableRow key={row.key}>
          <TableCell>{row.key}</TableCell>
          <TableCell numeric>{formatNumber(row.trades, 0)}</TableCell>
          <TableCell numeric>{formatNumber(row.sharePct, 1)}</TableCell>
          <TableCell numeric>{formatNumber(row.win_pct, 1)}</TableCell>
          <TableCell numeric>{formatNumber(row.expectancy_r, 2)}</TableCell>
          {hold ? <TableCell numeric>{formatNumber(row.avg_hold_sessions, 1)}</TableCell> : null}
          {pnl ? <TableCell numeric>{formatNumber(row.net_pnl, 0)}</TableCell> : null}
        </TableRow>
      ))}
    </DataTable>
  );
}

/** The most rows a CSV table draws before "Show all". */
const PAGE_ROWS = 200;

/**
 * A run CSV as a sortable table: every column, the first one sticky so the wide table scrolls
 * under it, and `highlight` rows marked in the warning tone.
 */
export function CsvTable({
  columns,
  rows,
  initialSort = null,
  highlight,
}: Readonly<{
  columns: readonly string[];
  rows: readonly CsvRecord[];
  initialSort?: Sort | null;
  highlight?: (row: CsvRecord) => boolean;
}>) {
  const [sort, setSort] = useState<Sort | null>(initialSort);
  const [all, setAll] = useState(false);
  const sorted = sortRecords(rows, sort);
  const shown = all ? sorted : sorted.slice(0, PAGE_ROWS);
  const numeric = new Set(
    columns.filter((column) => rows.some((row) => typeof row[column] === "number")),
  );
  const sticky = "sticky left-0 z-10 bg-background";
  return (
    <div className="space-y-3">
      <div className="max-h-[640px] overflow-y-auto">
        <Table>
          <TableHeader className="sticky top-0 z-20 bg-background">
            <TableRow>
              {columns.map((column, index) => (
                <SortableHead
                  key={column}
                  numeric={numeric.has(column)}
                  dir={sort?.key === column ? sort.dir : null}
                  onSort={() => setSort((old) => nextSort(old, column))}
                  className={index === 0 ? sticky : undefined}
                >
                  {columnLabel(column)}
                </SortableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {shown.map((row, rowIndex) => (
              <TableRow
                key={`${String(row[columns[0] ?? ""])}-${rowIndex}`}
                className={highlight?.(row) ? "text-warning" : undefined}
              >
                {columns.map((column, index) => (
                  <TableCell
                    key={column}
                    numeric={numeric.has(column)}
                    className={index === 0 ? sticky : undefined}
                  >
                    {formatCell(row[column])}
                  </TableCell>
                ))}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
      {sorted.length > PAGE_ROWS ? (
        <button
          type="button"
          onClick={() => setAll((old) => !old)}
          className="text-sm text-accent hover:underline"
        >
          {all ? `Show the first ${PAGE_ROWS}` : `Show all ${sorted.length} rows`}
        </button>
      ) : null}
    </div>
  );
}
