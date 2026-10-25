import { Badge, type Tone } from "@/components/atoms/badge";
import { Decimal } from "@/components/atoms/number";
import { TableCell, TableRow } from "@/components/atoms/table";
import { DataTable } from "@/components/molecules/data-table";
import { LabelledValue } from "@/components/molecules/labelled-value";
import {
  formatMetric,
  funnelSteps,
  METRICS,
  overridesList,
  reasonRows,
  type Funnel,
  type GroupRow,
  type RunSummary,
  type Trade,
} from "@/lib/backtests";
import { formatNumber, PLACEHOLDER } from "@/lib/format";

const STATUS_TONE: Record<string, Tone> = { completed: "profit", failed: "loss", running: "mode" };

export function RunStatus({ status }: Readonly<{ status: string }>) {
  return <Badge tone={STATUS_TONE[status] ?? "muted"}>{status}</Badge>;
}

/** What a run's overrides changed, or "none" for a plain run. */
export function Overrides({ overrides }: Readonly<{ overrides: RunSummary["overrides"] }>) {
  const lines = overridesList(overrides);
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

/** The run's headline metrics as a label-over-value grid. */
export function HeadlineGrid({ headline }: Readonly<{ headline: Record<string, unknown> }>) {
  return (
    <dl className="grid grid-cols-2 gap-x-8 gap-y-5 sm:grid-cols-4 lg:grid-cols-6">
      {METRICS.map((metric) => (
        <LabelledValue key={metric.key} label={metric.label}>
          <span className={metric.format === "text" ? undefined : "font-mono tabular-nums"}>
            {formatMetric(headline[metric.key], metric.format)}
          </span>
        </LabelledValue>
      ))}
    </dl>
  );
}

const GROUP_COLUMNS = [
  { label: "Bucket" },
  { label: "Trades", numeric: true },
  { label: "Sum R", numeric: true },
  { label: "Mean R", numeric: true },
  { label: "Win rate", numeric: true },
];

/** A breakdown's numbers under its chart. */
export function GroupTable({ rows }: Readonly<{ rows: GroupRow[] }>) {
  return (
    <DataTable columns={GROUP_COLUMNS}>
      {rows.map((row) => (
        <TableRow key={row.key}>
          <TableCell>{row.key}</TableCell>
          <TableCell numeric>{formatNumber(row.trades, 0)}</TableCell>
          <TableCell numeric>{formatNumber(row.sumR, 2)}</TableCell>
          <TableCell numeric>{formatNumber(row.meanR, 2)}</TableCell>
          <TableCell numeric>{formatNumber(row.winRate * 100, 1)}%</TableCell>
        </TableRow>
      ))}
    </DataTable>
  );
}

function Reasons({ title, reasons }: Readonly<{ title: string; reasons: Record<string, number> }>) {
  const rows = reasonRows(reasons);
  return (
    <div>
      <h4 className="mb-1 text-xs text-foreground-muted">{title}</h4>
      {rows.length === 0 ? (
        <p className="text-sm text-foreground-muted">none</p>
      ) : (
        <ul className="space-y-0.5 text-sm">
          {rows.map(([reason, count]) => (
            <li key={reason} className="flex justify-between gap-4">
              <span className="break-all">{reason}</span>
              <span className="font-mono tabular-nums">{formatNumber(count, 0)}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** The signal funnel: watchlisted to closed trades, with the skips and rejections by reason. */
export function FunnelView({ funnel }: Readonly<{ funnel: Funnel }>) {
  return (
    <div className="space-y-5">
      <ol className="flex flex-wrap items-baseline gap-x-3 gap-y-2">
        {funnelSteps(funnel).map((step, index) => (
          <li key={step.label} className="flex items-baseline gap-3">
            {index > 0 ? (
              <span aria-hidden className="text-foreground-muted">
                →
              </span>
            ) : null}
            <span>
              <span className="block text-xs text-foreground-muted">{step.label}</span>
              <span className="font-mono text-base tabular-nums">
                {formatNumber(step.count, 0)}
              </span>
            </span>
          </li>
        ))}
      </ol>
      <div className="grid gap-6 sm:grid-cols-2">
        <Reasons title={`Skipped triggers (${funnel.skipped})`} reasons={funnel.skips} />
        <Reasons title={`Rejected signals (${funnel.rejected})`} reasons={funnel.rejections} />
      </div>
    </div>
  );
}

const TRADE_COLUMNS = [
  { label: "Symbol" },
  { label: "Entry" },
  { label: "Exit" },
  { label: "Days", numeric: true },
  { label: "Qty", numeric: true },
  { label: "Entry price", numeric: true },
  { label: "Stop", numeric: true },
  { label: "Exit reason" },
  { label: "R", numeric: true },
  { label: "MFE R", numeric: true },
  { label: "MAE R", numeric: true },
  { label: "P&L", numeric: true },
  { label: "Regime" },
];

/** Every trade of the run in exit order. */
export function TradeTable({ trades }: Readonly<{ trades: Trade[] }>) {
  return (
    <DataTable columns={TRADE_COLUMNS}>
      {trades.map((trade, index) => (
        <TableRow key={`${trade.instrument_id}-${trade.entry_date}-${index}`}>
          <TableCell>{trade.symbol ?? PLACEHOLDER}</TableCell>
          <TableCell className="text-foreground-secondary">{trade.entry_date}</TableCell>
          <TableCell className="text-foreground-secondary">{trade.exit_date}</TableCell>
          <TableCell numeric>{trade.holding_days}</TableCell>
          <TableCell numeric>{formatNumber(trade.qty, 0)}</TableCell>
          <TableCell numeric>
            <Decimal value={trade.entry_price} />
          </TableCell>
          <TableCell numeric>
            <Decimal value={trade.initial_stop} />
          </TableCell>
          <TableCell>{trade.exit_reason}</TableCell>
          <TableCell numeric>
            <Decimal value={trade.r_multiple} />
          </TableCell>
          <TableCell numeric>
            <Decimal value={trade.mfe_r} />
          </TableCell>
          <TableCell numeric>
            <Decimal value={trade.mae_r} />
          </TableCell>
          <TableCell numeric>
            <Decimal value={trade.realized_pnl} />
          </TableCell>
          <TableCell className="text-foreground-secondary">{trade.regime}</TableCell>
        </TableRow>
      ))}
    </DataTable>
  );
}
