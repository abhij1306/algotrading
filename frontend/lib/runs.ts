/**
 * Backtest runs in the Result Standard v1.4 (docs/backtests/result-standard.md): the run.json shape and
 * the pure shaping the Backtests pages draw from (deltas, grouping, sorting, CSV parsing and the
 * summaries of the run's CSV files). Units follow the standard: % as 0 to 100, R as a multiple,
 * money in rupees.
 */
import type { components } from "@/lib/api/schema";
import { formatNumber, PLACEHOLDER } from "@/lib/format";

export type Num = number | null;
export type RunStatus = "complete" | "failed" | "stopped_early";
export type Split = "in_sample" | "hold_out";

export type Identity = {
  run_id: string;
  strategy: string;
  strategy_version: string;
  engine_commit: string | null;
  sweep_id: string | null;
  sweep_name?: string | null;
  variant: string | null;
  base_run_id: string | null;
  change: string | null;
  created_at: string | null;
  status: RunStatus;
  /** The failure code of a failed or stopped run. */
  error?: string | null;
  for_promotion?: boolean;
};

export type AuditStatus = "pass" | "warn" | "fail";
/** An audit document's worst status and digest, as run.json and the runs list reference it. */
export type AuditRef = { digest: string; status: AuditStatus };

export type RunData = {
  universe: string;
  data_snapshot_id: string | null;
  split: Split;
  validates_run_id?: string | null;
  period_start: string;
  period_end: string;
  benchmark: string;
  /** The data audit the run read (absent before v1.2). */
  audit?: AuditRef;
};

export type RunConfig = {
  overrides: Record<string, unknown>;
  resolved: Record<string, unknown>;
  resolved_hash: string | null;
  cost_model: string;
  start_capital: number;
  rf_pct?: number;
};

export type Metrics = {
  trades: number;
  signals: number;
  win_pct: Num;
  avg_win_r: Num;
  avg_loss_r: Num;
  expectancy_r: Num;
  avg_mfe_r: Num;
  avg_mae_r?: Num;
  cagr_pct: Num;
  max_dd_pct: Num;
  mar: Num;
  sharpe: Num;
  sortino?: Num;
  invested_pct: Num;
  avg_open_positions?: Num;
  avg_dd_pct?: Num;
  longest_dd_sessions: Num;
  longest_dd_recovered: boolean | null;
  longest_dd_start?: string | null;
  longest_dd_end?: string | null;
  dsr_prob: Num;
  dsr_n_trials: Num;
  dsr_n_trials_effective?: Num;
  pbo: Num;
  total_return_pct: Num;
  final_equity: Num;
  fees: Num;
  cost_share_pct: Num;
  benchmark_cagr_pct: Num;
  avg_hold_sessions_win?: Num;
  avg_hold_sessions_loss?: Num;
  max_consecutive_losses?: Num;
  top5_trades_pnl_share_pct: Num;
  top10_symbols_pnl_share_pct?: Num;
  cagr_ex_top5_trades_pct: Num;
  max_position_pct_adv?: Num;
  symbols_traded: Num;
  trades_over_5pct_adv?: Num;
};

export type Binding = {
  trades_changed: Num;
  trade_list_hash: string | null;
  no_op: boolean | null;
};

export type Bucket = {
  trades: number;
  win_pct: Num;
  expectancy_r: Num;
  avg_hold_sessions?: Num;
  net_pnl?: Num;
};

export type YearRow = {
  year: number;
  return_pct: Num;
  max_dd_pct: Num;
  trades: number;
  expectancy_r: Num;
  benchmark_return_pct: Num;
};

export type Breakdowns = {
  by_year: YearRow[];
  by_regime: Record<string, Bucket>;
  by_exit_reason: Record<string, Bucket>;
  by_industry: Record<string, Bucket>;
  by_cap_bucket: Record<string, Bucket>;
  by_liquidity_bucket: Record<string, Bucket>;
};

/** One run.json. */
export type Run = {
  format_version: string;
  identity: Identity;
  data: RunData;
  config: RunConfig;
  metrics: Metrics;
  refusals: Record<string, number>;
  binding: Binding;
  breakdowns: Breakdowns;
};

/**
 * One row of the runs list: the generated `BacktestRunItem` names the fields (the API types each
 * as an open object), run.schema.json types them.
 */
export type RunSummary = Pick<
  Run,
  Exclude<keyof components["schemas"]["BacktestRunItem"], "audit">
> & {
  /** The run audit's status and digest; null for a run without `audit.json`. */
  audit?: AuditRef | null;
};

/** One finding of an audit check: a code, its severity and how many items it found. */
export type AuditFinding = Readonly<{ code: string; severity: AuditStatus; count: number }>;
/** An audit document (`audit.json`, `data_audit.json`): its checks in order. */
export type AuditDoc = Readonly<{
  status: AuditStatus;
  digest: string;
  checks: readonly Readonly<{ name: string; status: AuditStatus; findings: AuditFinding[] }>[];
}>;

export type AuditRow = Readonly<{
  check: string;
  code: string;
  severity: AuditStatus;
  count: number;
}>;

/** An audit's findings that are not a pass, one row each, failures first, in check order. */
export function auditRows(doc: AuditDoc): AuditRow[] {
  const rows = doc.checks.flatMap((check) =>
    check.findings
      .filter((f) => f.severity !== "pass")
      .map((f) => ({ check: check.name, code: f.code, severity: f.severity, count: f.count })),
  );
  return [
    ...rows.filter((r) => r.severity === "fail"),
    ...rows.filter((r) => r.severity !== "fail"),
  ];
}

/** A run's variant label, or its id's tail when the run sets none. */
export const runName = (identity: Identity) => identity.variant ?? identity.run_id.slice(-8);
export const runTitle = (identity: Identity) => `${identity.strategy} · ${runName(identity)}`;
/** The run's one-line change; a run with no base and no change is a baseline. */
export function changeText(identity: Identity): string {
  if (identity.change) return identity.change;
  return identity.base_run_id === null ? "baseline" : "change not stated";
}
/** The sweep group of a run; runs that set no sweep share one group. */
export const NO_SWEEP = "";
export const sweepId = (identity: Identity) => identity.sweep_id ?? NO_SWEEP;
export const sweepName = (identity: Identity) =>
  identity.sweep_name ?? identity.sweep_id ?? "No sweep";

/** The CSV files of a run folder, by their route name. */
export const RUN_FILES = ["trades", "equity", "refusals", "signals", "stocks"] as const;
export type RunAudit = "audit" | "data_audit";
export type RunFile = (typeof RUN_FILES)[number];

export const MAX_COMPARE = 4;

// ---- metrics, formatting and deltas ----------------------------------------------------------

type NumericMetric = {
  [K in keyof Metrics]-?: Metrics[K] extends Num | number | undefined
    ? Exclude<Metrics[K], undefined> extends Num
      ? K
      : never
    : never;
}[keyof Metrics];

export type MetricFormat = "int" | "pct" | "r" | "ratio" | "prob" | "money";
/** Which way is better for a metric; null when neither is (trades, invested %). */
export type Better = "higher" | "lower" | null;
export type MetricDef = Readonly<{
  key: NumericMetric;
  label: string;
  format: MetricFormat;
  better: Better;
}>;

const metric = (key: NumericMetric, label: string, format: MetricFormat, better: Better) =>
  ({ key, label, format, better }) as const;

/** The columns of the runs view, in order; the first `STICKY_METRICS` stay put on scroll. */
export const RUN_COLUMNS: readonly MetricDef[] = [
  metric("expectancy_r", "Exp. R", "r", "higher"),
  metric("cagr_pct", "CAGR %", "pct", "higher"),
  metric("max_dd_pct", "Max DD %", "pct", "lower"),
  metric("mar", "MAR", "ratio", "higher"),
  metric("trades", "Trades", "int", null),
  metric("win_pct", "Win %", "pct", "higher"),
  metric("longest_dd_sessions", "Longest DD", "int", "lower"),
  metric("invested_pct", "Invested %", "pct", null),
  metric("dsr_prob", "DSR prob", "prob", "higher"),
  metric("cagr_ex_top5_trades_pct", "CAGR ex top 5 %", "pct", "higher"),
];
export const STICKY_METRICS = 4;

/** The headline metrics of a run's page and of the compare view. */
export const HEADLINE: readonly MetricDef[] = [
  metric("trades", "Trades", "int", null),
  metric("signals", "Signals", "int", null),
  metric("win_pct", "Win %", "pct", "higher"),
  metric("expectancy_r", "Expectancy R", "r", "higher"),
  metric("avg_win_r", "Avg win R", "r", "higher"),
  metric("avg_loss_r", "Avg loss R", "r", "higher"),
  metric("avg_mfe_r", "Avg MFE R", "r", null),
  metric("avg_mae_r", "Avg MAE R", "r", null),
  metric("cagr_pct", "CAGR %", "pct", "higher"),
  metric("benchmark_cagr_pct", "Benchmark CAGR %", "pct", null),
  metric("total_return_pct", "Total return %", "pct", "higher"),
  metric("max_dd_pct", "Max DD %", "pct", "lower"),
  metric("mar", "MAR", "ratio", "higher"),
  metric("sharpe", "Sharpe", "ratio", "higher"),
  metric("sortino", "Sortino", "ratio", "higher"),
  metric("longest_dd_sessions", "Longest DD (sessions)", "int", "lower"),
  metric("dsr_prob", "DSR prob", "prob", "higher"),
  metric("pbo", "PBO", "prob", "lower"),
  metric("avg_dd_pct", "Avg DD %", "pct", "lower"),
  metric("invested_pct", "Invested %", "pct", null),
  metric("avg_open_positions", "Avg open positions", "ratio", null),
  metric("final_equity", "Final equity", "money", "higher"),
  metric("fees", "Fees", "money", "lower"),
  metric("cost_share_pct", "Cost share %", "pct", "lower"),
  metric("avg_hold_sessions_win", "Avg hold, winners", "ratio", null),
  metric("avg_hold_sessions_loss", "Avg hold, losers", "ratio", null),
  metric("max_consecutive_losses", "Max losing streak", "int", "lower"),
  metric("symbols_traded", "Symbols traded", "int", null),
  metric("top5_trades_pnl_share_pct", "Top 5 trades P&L %", "pct", "lower"),
  metric("top10_symbols_pnl_share_pct", "Top 10 symbols P&L %", "pct", "lower"),
  metric("cagr_ex_top5_trades_pct", "CAGR ex top 5 %", "pct", "higher"),
  metric("max_position_pct_adv", "Max position % ADV", "pct", "lower"),
  metric("trades_over_5pct_adv", "Trades over 5% ADV", "int", "lower"),
];

const DECIMALS: Record<MetricFormat, number> = {
  int: 0,
  pct: 1,
  r: 2,
  ratio: 2,
  prob: 2,
  money: 0,
};

export function metricValue(metrics: Partial<Metrics>, key: NumericMetric): Num {
  const value = metrics[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export function formatMetric(value: Num | undefined, format: MetricFormat): string {
  if (format === "money" && typeof value === "number") return `₹${formatNumber(value, 0)}`;
  return formatNumber(value, DECIMALS[format]);
}

/** The change of a metric against the base run; null when either side is missing. */
export function metricDelta(
  run: Partial<Metrics>,
  base: Partial<Metrics> | null | undefined,
  key: NumericMetric,
): Num {
  if (!base) return null;
  const a = metricValue(run, key);
  const b = metricValue(base, key);
  return a === null || b === null ? null : a - b;
}

/** "+1.2 pp", "−0.05", or "" when there is no delta. A delta that rounds to zero shows "±0". */
export function formatDelta(delta: Num, format: MetricFormat): string {
  if (delta === null) return "";
  const text = formatNumber(Math.abs(delta), DECIMALS[format]);
  const zero = Number(text.replaceAll(",", "")) === 0;
  let sign = "−";
  if (zero) sign = "±";
  else if (delta > 0) sign = "+";
  const unit = format === "pct" ? " pp" : "";
  return `${sign}${text}${unit}`;
}

export type DeltaTone = "profit" | "loss" | "muted";

/** Green when the delta goes the better way, red when worse, muted for neutral or zero. */
export function deltaTone(delta: Num, better: Better): DeltaTone {
  if (delta === null || better === null || delta === 0) return "muted";
  return delta > 0 === (better === "higher") ? "profit" : "loss";
}

/** "739 open" or "412 recovered": the longest drawdown with whether it ended. */
export function longestDd(metrics: Partial<Metrics>): string {
  const sessions = formatNumber(metrics.longest_dd_sessions, 0);
  if (sessions === PLACEHOLDER) return sessions;
  if (metrics.longest_dd_recovered === true) return `${sessions} recovered`;
  if (metrics.longest_dd_recovered === false) return `${sessions} open`;
  return sessions;
}

/** "0.33 of 12 trials"; DSR means nothing without the trial count, so it says when that is missing. */
export function dsrText(metrics: Partial<Metrics>): string {
  const prob = formatNumber(metrics.dsr_prob, 2);
  if (prob === PLACEHOLDER) return prob;
  const trials = metrics.dsr_n_trials;
  return typeof trials === "number" ? `${prob} of ${trials}` : `${prob}, trials unknown`;
}

// ---- runs: filters, grouping, sorting ----------------------------------------------------------

export type RunFilter = Readonly<{
  strategy: string | null;
  split: Split | null;
  sweep: string | null;
}>;

export const NO_FILTER: RunFilter = { strategy: null, split: null, sweep: null };

export function filterRuns(runs: readonly RunSummary[], filter: RunFilter): RunSummary[] {
  return runs.filter(
    (run) =>
      (filter.strategy === null || run.identity.strategy === filter.strategy) &&
      (filter.split === null || run.data.split === filter.split) &&
      (filter.sweep === null || sweepId(run.identity) === filter.sweep),
  );
}

export type SweepOption = Readonly<{ id: string; name: string; runs: number }>;

/** The strategies and sweeps in the list, in first-seen (newest first) order. */
export function runFacets(runs: readonly RunSummary[]): {
  strategies: string[];
  sweeps: SweepOption[];
} {
  const strategies: string[] = [];
  const sweeps = new Map<string, { id: string; name: string; runs: number }>();
  for (const { identity } of runs) {
    if (!strategies.includes(identity.strategy)) strategies.push(identity.strategy);
    const id = sweepId(identity);
    const sweep = sweeps.get(id);
    if (sweep) sweep.runs += 1;
    else sweeps.set(id, { id, name: sweepName(identity), runs: 1 });
  }
  return { strategies, sweeps: [...sweeps.values()] };
}

export type SortDir = "asc" | "desc";
export type Sort<K extends string = string> = Readonly<{ key: K; dir: SortDir }>;
export type RunSortKey = NumericMetric | "variant";

/** Clicking a column sorts it descending first (best numbers on top), then flips. */
export function nextSort<K extends string>(current: Sort<K> | null, key: K): Sort<K> {
  if (current?.key !== key) return { key, dir: "desc" };
  return { key, dir: current.dir === "desc" ? "asc" : "desc" };
}

/** Compare two cells: numbers numerically, text by locale; missing values always sort last. */
export function compareCells(a: Cell | undefined, b: Cell | undefined, dir: SortDir): number {
  const missingA = a === null || a === undefined || a === "";
  const missingB = b === null || b === undefined || b === "";
  if (missingA || missingB) return Number(missingA) - Number(missingB);
  const order =
    typeof a === "number" && typeof b === "number" ? a - b : String(a).localeCompare(String(b));
  return dir === "asc" ? order : -order;
}

function runSortValue(run: RunSummary, key: RunSortKey): Cell {
  return key === "variant" ? runName(run.identity) : metricValue(run.metrics, key);
}

export type RunRow = Readonly<{
  run: RunSummary;
  /** The run it is compared against, when that run is in the list. */
  base: RunSummary | null;
  /** A hold-out run listed under the in-sample run it validates. */
  validates: RunSummary | null;
}>;
export type SweepGroup = Readonly<{ id: string; name: string; rows: RunRow[] }>;
export type StrategyGroup = Readonly<{ strategy: string; sweeps: SweepGroup[] }>;

/**
 * The shown runs grouped by strategy, then sweep, both in first-seen (newest first) order. Rows
 * keep the list order or follow `sort`; a hold-out run whose validated run is shown sits right
 * after that run, in its group. `all` is the whole list, where the base runs are looked up.
 */
export function groupRuns(
  shown: readonly RunSummary[],
  all: readonly RunSummary[],
  sort: Sort<RunSortKey> | null = null,
): StrategyGroup[] {
  const byId = new Map(all.map((run) => [run.identity.run_id, run]));
  const shownIds = new Set(shown.map((run) => run.identity.run_id));
  const nestedUnder = (run: RunSummary) => {
    const target = run.data.validates_run_id;
    const nests = run.data.split === "hold_out" && target && target !== run.identity.run_id;
    return nests && shownIds.has(target) ? target : null;
  };
  const holdOuts = new Map<string, RunSummary[]>();
  const anchors: RunSummary[] = [];
  for (const run of shown) {
    const target = nestedUnder(run);
    if (target === null) anchors.push(run);
    else holdOuts.set(target, [...(holdOuts.get(target) ?? []), run]);
  }
  const strategies = new Map<string, Map<string, { name: string; runs: RunSummary[] }>>();
  for (const run of anchors) {
    const { strategy } = run.identity;
    const id = sweepId(run.identity);
    const sweeps = strategies.get(strategy) ?? new Map();
    strategies.set(strategy, sweeps);
    const sweep = sweeps.get(id) ?? { name: sweepName(run.identity), runs: [] };
    sweeps.set(id, sweep);
    sweep.runs.push(run);
  }
  const row = (run: RunSummary, validates: RunSummary | null): RunRow => ({
    run,
    base: run.identity.base_run_id ? (byId.get(run.identity.base_run_id) ?? null) : null,
    validates,
  });
  return [...strategies].map(([strategy, sweeps]) => ({
    strategy,
    sweeps: [...sweeps].map(([id, sweep]) => {
      const ordered = sort
        ? sweep.runs.toSorted((a, b) =>
            compareCells(runSortValue(a, sort.key), runSortValue(b, sort.key), sort.dir),
          )
        : sweep.runs;
      return {
        id,
        name: sweep.name,
        rows: ordered.flatMap((run) => [
          row(run, null),
          ...(holdOuts.get(run.identity.run_id) ?? []).map((holdOut) => row(holdOut, run)),
        ]),
      };
    }),
  }));
}

/** Tick a run for compare; at most MAX_COMPARE stay ticked, the oldest pick drops out. */
export function toggleSelection(selected: readonly string[], id: string): string[] {
  if (selected.includes(id)) return selected.filter((other) => other !== id);
  return [...selected, id].slice(-MAX_COMPARE);
}

// ---- CSV ------------------------------------------------------------------------------------------

/** RFC 4180 rows: quoted cells with doubled quotes, CRLF or LF line ends, no blank rows. */
export function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = "";
  let quoted = false;
  const source = text.startsWith("﻿") ? text.slice(1) : text;
  const endRow = () => {
    row.push(cell);
    if (row.length > 1 || row[0] !== "") rows.push(row);
    row = [];
    cell = "";
  };
  for (let i = 0; i < source.length; i++) {
    const ch = source[i];
    if (quoted) {
      if (ch !== '"') cell += ch;
      else if (source[i + 1] === '"') {
        cell += '"';
        i++;
      } else quoted = false;
    } else if (ch === '"') quoted = true;
    else if (ch === ",") {
      row.push(cell);
      cell = "";
    } else if (ch === "\n" || ch === "\r") {
      if (ch === "\r" && source[i + 1] === "\n") i++;
      endRow();
    } else cell += ch;
  }
  if (cell !== "" || row.length > 0) endRow();
  return rows;
}

export type Cell = string | number | boolean | null;
export type CsvRecord = Readonly<Record<string, Cell>>;
export type CsvTable = Readonly<{ columns: string[]; rows: CsvRecord[] }>;

const TEXT_COLUMNS = new Set([
  "run_id",
  "trade_id",
  "symbol",
  "industry",
  "exit_reason",
  "cap_bucket",
  "cap_bucket_mode",
  "index_membership",
  "reason",
  "refusal_reason",
  "first_trade",
  "last_trade",
  "liquidity_bucket",
  "instrument_id",
  "playbook",
  "engine_exit_reason",
  "auction_risk",
]);
const BOOL_COLUMNS = new Set(["index_regime_on", "taken"]);
const MISSING = new Set(["", "null", "none", "nan"]);

/** One cell typed by its column: text, true/false, a finite number, or null when empty. */
export function parseCell(column: string, raw: string): Cell {
  const value = raw.trim();
  if (MISSING.has(value.toLowerCase())) return null;
  if (TEXT_COLUMNS.has(column) || column === "date" || column.endsWith("_date")) return value;
  if (BOOL_COLUMNS.has(column)) {
    const lower = value.toLowerCase();
    if (lower === "true" || lower === "1") return true;
    if (lower === "false" || lower === "0") return false;
    return null;
  }
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : value;
}

/** A CSV file as its header and typed records. */
export function parseCsvTable(text: string): CsvTable {
  const [header = [], ...body] = parseCsv(text);
  const columns = header.map((name) => name.trim());
  const rows = body.map((cells) =>
    Object.fromEntries(columns.map((column, i) => [column, parseCell(column, cells[i] ?? "")])),
  );
  return { columns, rows };
}

export const cellNumber = (row: CsvRecord, column: string): Num => {
  const value = row[column];
  return typeof value === "number" ? value : null;
};
export const cellText = (row: CsvRecord, column: string): string => {
  const value = row[column];
  return value === null || value === undefined ? "" : String(value);
};

function mean(values: readonly Num[]): { mean: Num; n: number } {
  const present = values.filter((value): value is number => value !== null);
  if (present.length === 0) return { mean: null, n: 0 };
  return {
    mean: present.reduce((sum, value) => sum + value, 0) / present.length,
    n: present.length,
  };
}

const sum = (values: readonly Num[]) => values.reduce<number>((total, v) => total + (v ?? 0), 0);

// ---- equity ---------------------------------------------------------------------------------------

export type EquityPoint = Readonly<{
  date: string;
  /** Growth of equity from the first session, %. */
  growth: Num;
  /** Growth of the benchmark from its first close in the window, %. */
  benchmark: Num;
  /** Drawdown as a fall below zero, %. */
  drawdown: Num;
  breadth: Num;
  regimeOn: boolean | null;
}>;

/** equity.csv as chart points: equity and benchmark rebased to the first session. */
export function equitySeries(rows: readonly CsvRecord[]): EquityPoint[] {
  const firstEquity = rows.map((row) => cellNumber(row, "equity")).find((v) => v !== null && v > 0);
  const firstBench = rows
    .map((row) => cellNumber(row, "benchmark_tri"))
    .find((v) => v !== null && v > 0);
  const growth = (value: Num, first: Num | undefined) =>
    value === null || !first ? null : (value / first - 1) * 100;
  return rows.map((row) => {
    const drawdown = cellNumber(row, "drawdown_pct");
    const regime = row["index_regime_on"];
    return {
      date: cellText(row, "date"),
      growth: growth(cellNumber(row, "equity"), firstEquity),
      benchmark: growth(cellNumber(row, "benchmark_tri"), firstBench),
      drawdown: drawdown === null ? null : -Math.abs(drawdown),
      breadth: cellNumber(row, "breadth_pct"),
      regimeOn: typeof regime === "boolean" ? regime : null,
    };
  });
}

export type Span = Readonly<{ start: string; end: string }>;

/** The stretches of sessions with the index regime off, as first and last date. */
export function regimeOffSpans(points: readonly EquityPoint[]): Span[] {
  const spans: Span[] = [];
  let start: string | null = null;
  let last = "";
  for (const point of points) {
    if (point.regimeOn === false) {
      start ??= point.date;
      last = point.date;
    } else if (start !== null) {
      spans.push({ start, end: last });
      start = null;
    }
  }
  if (start !== null) spans.push({ start, end: last });
  return spans;
}

export const seriesKey = (index: number) => `s${index}`;

/** Several runs' growth curves merged on date, one key per run (`s0`, `s1`, ...). */
export function compareEquity(
  series: readonly (readonly EquityPoint[] | null)[],
): Record<string, string | number | null>[] {
  const byDate = new Map<string, Record<string, string | number | null>>();
  series.forEach((points, index) => {
    for (const point of points ?? []) {
      const row = byDate.get(point.date) ?? { date: point.date };
      row[seriesKey(index)] = point.growth;
      byDate.set(point.date, row);
    }
  });
  return [...byDate.values()].sort((a, b) => String(a.date).localeCompare(String(b.date)));
}

// ---- breakdowns -----------------------------------------------------------------------------------

export type BucketRow = Readonly<Bucket & { key: string; sharePct: Num }>;

/**
 * A breakdown's buckets as rows with each one's share of the trades: in `order` when given
 * (cap and liquidity buckets), else most trades first.
 */
export function bucketRows(
  buckets: Readonly<Record<string, Bucket>>,
  order?: readonly string[],
): BucketRow[] {
  const total = Object.values(buckets).reduce((n, bucket) => n + bucket.trades, 0);
  const rows = Object.entries(buckets).map(([key, bucket]) => ({
    ...bucket,
    key,
    sharePct: total > 0 ? (bucket.trades / total) * 100 : null,
  }));
  if (order) {
    const rank = (key: string) => {
      const index = order.indexOf(key);
      return index === -1 ? order.length : index;
    };
    return rows.sort((a, b) => rank(a.key) - rank(b.key) || a.key.localeCompare(b.key));
  }
  return rows.sort((a, b) => b.trades - a.trades || a.key.localeCompare(b.key));
}

export const CAP_ORDER = ["large", "mid", "small", "micro"] as const;
export const LIQUIDITY_ORDER = ["q1", "q2", "q3", "q4", "q5"] as const;
export const REGIME_ORDER = [
  "index_on",
  "index_off",
  "index_unknown",
  "breadth_lt40",
  "breadth_40_50",
  "breadth_50_60",
  "breadth_ge60",
  "breadth_unknown",
] as const;

/** by_year with the strategy's margin over the benchmark each year. */
export function yearRows(years: readonly YearRow[]): (YearRow & { excess_pct: Num })[] {
  return years
    .map((year) => ({
      ...year,
      excess_pct:
        year.return_pct === null || year.benchmark_return_pct === null
          ? null
          : year.return_pct - year.benchmark_return_pct,
    }))
    .sort((a, b) => a.year - b.year);
}

// ---- refusals ---------------------------------------------------------------------------------------

export type RefusalCost = Readonly<{
  reason: string;
  /** From run.json; the CSV's row count when run.json has no count for the reason. */
  count: number;
  meanFwd20: Num;
  n20: number;
  meanFwd60: Num;
  n60: number;
  /** Share of the refused entries with a 60-session forward R above zero, %. */
  wouldWin60Pct: Num;
}>;

/**
 * What each gate cost: per refusal reason, the mean forward R (before costs) the refused entries
 * would have made at 20 and 60 sessions (refusals.csv), most refusals first.
 */
export function refusalCosts(
  counts: Readonly<Record<string, number>>,
  rows: readonly CsvRecord[],
): RefusalCost[] {
  const byReason = new Map<string, CsvRecord[]>();
  for (const row of rows) {
    const reason = cellText(row, "reason");
    byReason.set(reason, [...(byReason.get(reason) ?? []), row]);
  }
  const reasons = new Set([...Object.keys(counts), ...byReason.keys()]);
  return [...reasons]
    .map((reason) => {
      const refused = byReason.get(reason) ?? [];
      const fwd20 = mean(refused.map((row) => cellNumber(row, "fwd_r_20d_gross")));
      const fwd60Values = refused.map((row) => cellNumber(row, "fwd_r_60d_gross"));
      const fwd60 = mean(fwd60Values);
      const wins = fwd60Values.filter((v) => v !== null && v > 0).length;
      return {
        reason,
        count: counts[reason] ?? refused.length,
        meanFwd20: fwd20.mean,
        n20: fwd20.n,
        meanFwd60: fwd60.mean,
        n60: fwd60.n,
        wouldWin60Pct: fwd60.n > 0 ? (wins / fwd60.n) * 100 : null,
      };
    })
    .sort((a, b) => b.count - a.count || a.reason.localeCompare(b.reason));
}

// ---- trades, stocks and concentration ---------------------------------------------------------------

export type Outcome = "all" | "win" | "loss";
export type TradeFilter = Readonly<{ query: string; exitReason: string | null; outcome: Outcome }>;

/** Trades whose symbol or industry holds `query`, with that exit reason and outcome. */
export function filterTrades(rows: readonly CsvRecord[], filter: TradeFilter): CsvRecord[] {
  const query = filter.query.trim().toLowerCase();
  return rows.filter((row) => {
    if (filter.exitReason !== null && row["exit_reason"] !== filter.exitReason) return false;
    const r = cellNumber(row, "r_net");
    if (filter.outcome === "win" && !(r !== null && r > 0)) return false;
    if (filter.outcome === "loss" && !(r !== null && r <= 0)) return false;
    if (query === "") return true;
    return (
      cellText(row, "symbol").toLowerCase().includes(query) ||
      cellText(row, "industry").toLowerCase().includes(query)
    );
  });
}

export function sortRecords(rows: readonly CsvRecord[], sort: Sort | null): CsvRecord[] {
  if (sort === null) return [...rows];
  return rows.toSorted((a, b) => compareCells(a[sort.key], b[sort.key], sort.dir));
}

/** The distinct values of a text column, sorted, for a filter's options. */
export function distinctValues(rows: readonly CsvRecord[], column: string): string[] {
  return [...new Set(rows.map((row) => cellText(row, column)).filter(Boolean))].sort((a, b) =>
    a.localeCompare(b),
  );
}

export type TopTrade = Readonly<{
  tradeId: string;
  symbol: string;
  entryDate: string;
  exitDate: string;
  netPnl: number;
  sharePct: Num;
}>;

/**
 * The `n` best trades by net P&L with their share of the run's net P&L. A partial exit's legs
 * share a trade id and count as one trade.
 */
export function topTrades(rows: readonly CsvRecord[], n: number = 5): TopTrade[] {
  const total = sum(rows.map((row) => cellNumber(row, "net_pnl")));
  const trades = new Map<string, { symbol: string; entry: string; exit: string; pnl: number }>();
  rows.forEach((row, index) => {
    const id = cellText(row, "trade_id") || `row-${index}`;
    const trade = trades.get(id) ?? {
      symbol: cellText(row, "symbol"),
      entry: cellText(row, "entry_date"),
      exit: "",
      pnl: 0,
    };
    trade.pnl += cellNumber(row, "net_pnl") ?? 0;
    const exit = cellText(row, "exit_date");
    if (exit > trade.exit) trade.exit = exit;
    trades.set(id, trade);
  });
  return [...trades]
    .map(([tradeId, trade]) => ({
      tradeId,
      symbol: trade.symbol,
      entryDate: trade.entry,
      exitDate: trade.exit,
      netPnl: trade.pnl,
      sharePct: total > 0 ? (trade.pnl / total) * 100 : null,
    }))
    .sort((a, b) => b.netPnl - a.netPnl)
    .slice(0, n);
}

export type TopSymbol = Readonly<{
  symbol: string;
  trades: Num;
  netPnl: number;
  sharePct: Num;
}>;

/** The `n` symbols with the most net P&L (stocks.csv) and their share of the run's net P&L. */
export function topSymbols(stocks: readonly CsvRecord[], n: number = 10): TopSymbol[] {
  const total = sum(stocks.map((row) => cellNumber(row, "net_pnl")));
  return stocks
    .map((row) => ({
      symbol: cellText(row, "symbol"),
      trades: cellNumber(row, "trades"),
      netPnl: cellNumber(row, "net_pnl") ?? 0,
    }))
    .sort((a, b) => b.netPnl - a.netPnl)
    .slice(0, n)
    .map((row) => ({ ...row, sharePct: total > 0 ? (row.netPnl / total) * 100 : null }));
}

/** A symbol stopped out this many times or more is a repeat stop-out. */
export const REPEAT_STOPOUTS = 2;

export type StockSummary = Readonly<{
  traded: number;
  profitable: number;
  losing: number;
  repeatStopouts: number;
}>;

/** Who carries the P&L: symbols traded, in profit, in loss, and repeat stop-outs (stocks.csv). */
export function stockSummary(stocks: readonly CsvRecord[]): StockSummary {
  const traded = stocks.filter((row) => (cellNumber(row, "trades") ?? 0) > 0);
  const pnl = (row: CsvRecord) => cellNumber(row, "net_pnl") ?? 0;
  return {
    traded: traded.length,
    profitable: traded.filter((row) => pnl(row) > 0).length,
    losing: traded.filter((row) => pnl(row) < 0).length,
    repeatStopouts: stocks.filter((row) => (cellNumber(row, "stopouts") ?? 0) >= REPEAT_STOPOUTS)
      .length,
  };
}

/** A CSV column name as a header label: "prior_stopouts_symbol_90d" → "prior stopouts symbol 90d". */
export const columnLabel = (column: string) => column.replaceAll("_", " ");

/** A typed cell for a table: integers whole, other numbers to two places, flags as yes or no. */
export function formatCell(value: Cell | undefined): string {
  if (value === null || value === undefined || value === "") return PLACEHOLDER;
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (typeof value === "number") return formatNumber(value, Number.isInteger(value) ? 0 : 2);
  return value;
}

/** Overrides as "path = value" lines. */
export function overrideLines(overrides: Readonly<Record<string, unknown>>): string[] {
  return Object.entries(overrides).map(
    ([key, value]) => `${key} = ${typeof value === "string" ? value : JSON.stringify(value)}`,
  );
}
