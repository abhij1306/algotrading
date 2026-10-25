/**
 * Data shaping for the Backtests pages: the `/api/hat/backtests` bodies turned into chart rows,
 * table rows and labels. Money, R and equity arrive as exact decimal strings and are parsed here
 * for display only; return and drawdown ratios arrive as floats (app/hat/api/backtests.py).
 */
import type { components } from "@/lib/api/schema";
import { formatNumber, PLACEHOLDER } from "@/lib/format";

type Schemas = components["schemas"];
export type RunSummary = Schemas["BacktestRunSummary"];
export type RunDetail = Schemas["BacktestRunDetail"];
export type Report = Schemas["BacktestReport"];
export type Trade = Schemas["BacktestTrade"];
export type GroupStats = Schemas["BacktestGroupStats"];
export type Funnel = Schemas["BacktestFunnel"];

/** The most runs the compare view shows side by side. */
export const MAX_COMPARE = 4;
export const BENCHMARK_NAME = "NIFTY 500";

/** A decimal string (or number) as a number for display; null when missing or not finite. */
export function num(value: unknown): number | null {
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value !== "string" || value.trim() === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

/** A JSON value as text: strings as they are, numbers and booleans printed, the rest as JSON. */
export function asText(value: unknown): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value) ?? "";
}

/** "trend-pullback-ema v1" and, for a sweep's run, its variant label. */
export function runName(run: RunSummary): string {
  const base = `${run.strategy ?? "unknown strategy"} v${run.strategy_version ?? "?"}`;
  return run.variant ? `${base} · ${run.variant.label}` : base;
}

/** What a run's overrides changed, one "path=value" line each, in the order recorded. */
export function overridesList(overrides: RunSummary["overrides"]): string[] {
  if (!overrides) return [];
  const out: string[] = [];
  const sets = overrides["set"];
  if (Array.isArray(sets)) {
    for (const item of sets) {
      if (item && typeof item === "object" && "path" in item) {
        const { path, value } = item as { path: unknown; value: unknown };
        out.push(`${String(path)}=${JSON.stringify(value)}`);
      }
    }
  }
  for (const key of ["risk_pct", "max_positions"]) {
    if (overrides[key] !== undefined && overrides[key] !== null) {
      out.push(`${key}=${asText(overrides[key])}`);
    }
  }
  return out;
}

export type Sweep = Readonly<{ id: string; label: string; runs: number }>;

/** The families and sweeps among `runs`, for the list's filters (families sorted, sweeps newest first). */
export function runFacets(runs: readonly RunSummary[]): { families: string[]; sweeps: Sweep[] } {
  const families = [...new Set(runs.map((run) => run.family_key))].sort();
  const sweeps = new Map<string, { label: string; runs: number }>();
  for (const run of runs) {
    if (!run.variant) continue;
    const found = sweeps.get(run.variant.sweep_id);
    if (found) found.runs += 1;
    else {
      const name = `${run.strategy ?? "?"} v${run.strategy_version ?? "?"}, ${run.period_start}`;
      sweeps.set(run.variant.sweep_id, { label: name, runs: 1 });
    }
  }
  return {
    families,
    sweeps: [...sweeps].map(([id, sweep]) => ({ id, ...sweep })),
  };
}

export type RunFilter = Readonly<{ family: string | null; sweep: string | null }>;

export function filterRuns(runs: readonly RunSummary[], filter: RunFilter): RunSummary[] {
  return runs.filter(
    (run) =>
      (filter.family === null || run.family_key === filter.family) &&
      (filter.sweep === null || run.variant?.sweep_id === filter.sweep),
  );
}

/** The compare selection with `id` toggled; a new id past `MAX_COMPARE` is not added. */
export function toggleSelection(selected: readonly string[], id: string): string[] {
  if (selected.includes(id)) return selected.filter((other) => other !== id);
  return selected.length >= MAX_COMPARE ? [...selected] : [...selected, id];
}

export type EquityRow = {
  date: string;
  strategy: number | null; // equity rebased to 1 at the first session
  benchmark: number | null; // NIFTY 500 rebased to 1
  drawdown: number; // <= 0, from the initial equity's running peak
  benchmarkDrawdown: number | null; // NIFTY 500's from its running peak
};

/** One row per session: equity and NIFTY 500 both rebased to 1, and both drawdowns. */
export function equityRows(report: Report): EquityRow[] {
  const first = num(report.equity[0]?.equity);
  let peak: number | null = null;
  return report.equity.map((point) => {
    const equity = num(point.equity);
    let benchmarkDrawdown: number | null = null;
    if (point.benchmark !== null) {
      peak = peak === null ? point.benchmark : Math.max(peak, point.benchmark);
      benchmarkDrawdown = peak > 0 ? point.benchmark / peak - 1 : null;
    }
    return {
      date: point.date,
      strategy: equity !== null && first ? equity / first : null,
      benchmark: point.benchmark,
      drawdown: point.drawdown,
      benchmarkDrawdown,
    };
  });
}

export function rRows(report: Report): { date: string; r: number | null }[] {
  return report.r_curve.map((point) => ({ date: point.date, r: num(point.r) }));
}

export type ScatterPoint = { mfe: number; r: number; symbol: string; exit: string };

/** Each trade's MFE (x) against its realised R (y); a trade missing either is left out. */
export function scatterPoints(trades: readonly Trade[]): ScatterPoint[] {
  const out: ScatterPoint[] = [];
  for (const trade of trades) {
    const mfe = num(trade.mfe_r);
    const r = num(trade.r_multiple);
    if (mfe === null || r === null) continue;
    out.push({ mfe, r, symbol: trade.symbol ?? trade.instrument_id, exit: trade.exit_date });
  }
  return out;
}

export type GroupRow = {
  key: string;
  trades: number;
  sumR: number | null;
  meanR: number | null;
  winRate: number;
};

/** A breakdown (exit reason, hold time, regime at entry) in the order the API sends it. */
export function groupRows(stats: readonly GroupStats[]): GroupRow[] {
  return stats.map((row) => ({
    key: row.key,
    trades: row.trades,
    sumR: num(row.sum_r),
    meanR: num(row.mean_r),
    winRate: row.win_rate,
  }));
}

export type FunnelStep = { label: string; count: number };

/** The signal funnel's main steps, watchlisted to closed trades. */
export function funnelSteps(funnel: Funnel): FunnelStep[] {
  return [
    { label: "Watchlisted", count: funnel.watchlisted },
    { label: "Signals", count: funnel.signals },
    { label: "Entered", count: funnel.entered },
    { label: "Closed trades", count: funnel.trades },
  ];
}

/** A reason breakdown (skips, rejections), largest first, ties by name. */
export function reasonRows(reasons: Readonly<Record<string, number>>): [string, number][] {
  return Object.entries(reasons).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}

/** The compare view's series key of the run at `index` ("s0" ... "s3"). */
export const seriesKey = (index: number) => `s${index}`;

/**
 * The compared runs' equity, each rebased to 1 at its own first session, on the union of their
 * dates (a run without a value on a date leaves it out). Runs without a report add no series.
 */
export function compareEquity(
  reports: readonly (Report | null)[],
): Record<string, number | string>[] {
  const byDate = new Map<string, Record<string, number | string>>();
  reports.forEach((report, index) => {
    if (!report) return;
    for (const row of equityRows(report)) {
      if (row.strategy === null) continue;
      const found = byDate.get(row.date) ?? { date: row.date };
      found[seriesKey(index)] = row.strategy;
      byDate.set(row.date, found);
    }
  });
  return [...byDate.values()].sort((a, b) => String(a.date).localeCompare(String(b.date)));
}

/** The compared runs' calendar-year returns, one row per year any run covers. */
export function compareYearly(
  reports: readonly (Report | null)[],
): Record<string, number | null>[] {
  const byYear = new Map<number, Record<string, number | null>>();
  reports.forEach((report, index) => {
    for (const row of report?.yearly ?? []) {
      const found = byYear.get(row.year) ?? { year: row.year };
      found[seriesKey(index)] = row.return;
      byYear.set(row.year, found);
    }
  });
  return [...byYear.values()].sort((a, b) => Number(a.year) - Number(b.year));
}

export type MetricFormat = "int" | "r" | "pct" | "drawdown" | "ratio" | "money" | "text";
export type MetricDef = Readonly<{ key: string; label: string; format: MetricFormat }>;

/** The headline metrics the list and the compare view show, as stored (`api.backtests.HEADLINE`). */
export const METRICS: readonly MetricDef[] = [
  { key: "trades", label: "Trades", format: "int" },
  { key: "signals", label: "Signals", format: "int" },
  { key: "expectancy_r", label: "Expectancy (R)", format: "r" },
  { key: "sum_r", label: "Sum R", format: "r" },
  { key: "win_rate", label: "Win rate", format: "pct" },
  { key: "avg_win_r", label: "Avg win (R)", format: "r" },
  { key: "avg_loss_r", label: "Avg loss (R)", format: "r" },
  { key: "avg_mfe_r", label: "Avg MFE (R)", format: "r" },
  { key: "avg_mae_r", label: "Avg MAE (R)", format: "r" },
  { key: "total_return", label: "Total return", format: "pct" },
  { key: "cagr", label: "CAGR", format: "pct" },
  { key: "max_drawdown", label: "Max drawdown", format: "drawdown" },
  { key: "sharpe_annual", label: "Sharpe (annual)", format: "ratio" },
  { key: "exposure", label: "Exposure", format: "pct" },
  { key: "net_pnl", label: "Net P&L", format: "money" },
  { key: "fees", label: "Fees", format: "money" },
  { key: "sleeve_weight", label: "Sleeve weight", format: "ratio" },
  { key: "slot_order", label: "Slot order", format: "text" },
  { key: "regime_mult", label: "Regime sizing", format: "text" },
  { key: "slippage_bps", label: "Slippage (bps)", format: "ratio" },
];

/** The stored metrics that are plain values (numbers, strings, booleans), for a run without a report. */
export function scalarMetrics(metrics: Readonly<Record<string, unknown>>): [string, string][] {
  return Object.entries(metrics)
    .filter(([, value]) => ["number", "string", "boolean"].includes(typeof value))
    .map(([key, value]) => [key, String(value)] as [string, string])
    .sort((a, b) => a[0].localeCompare(b[0]));
}

/** A stored metric for display: "--" when missing, percents from ratios, R to two places. */
export function formatMetric(value: unknown, format: MetricFormat): string {
  if (format === "text") return value === undefined || value === null ? PLACEHOLDER : asText(value);
  const number = num(value);
  if (number === null) return PLACEHOLDER;
  switch (format) {
    case "int":
      return formatNumber(number, 0);
    case "pct":
      return `${formatNumber(number * 100, 2)}%`;
    case "drawdown": // stored as a positive fraction; shown below zero, as the charts draw it
      return `${formatNumber(-Math.abs(number) * 100, 2)}%`;
    case "r":
    case "ratio":
    case "money":
      return formatNumber(number, 2);
  }
}
