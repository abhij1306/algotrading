import { useState, type ReactNode } from "react";
import { Link, useParams } from "react-router";
import { fieldClass } from "@/components/atoms/field";
import { TableCell, TableRow } from "@/components/atoms/table";
import { Text } from "@/components/atoms/text";
import { DataTable } from "@/components/molecules/data-table";
import { LabelledValue } from "@/components/molecules/labelled-value";
import { noRows, QuerySection } from "@/components/molecules/query-section";
import { BreadthChart, DrawdownChart, EquityChart } from "@/components/organisms/backtests/charts";
import {
  BucketTable,
  CsvTable,
  MetricWithDelta,
  NoOpFlag,
  Overrides,
  RunStatusBadge,
  SPLIT_LABEL,
} from "@/components/organisms/backtests/run-parts";
import { PageLayout } from "@/components/templates/page-layout";
import { formatNumber, PLACEHOLDER } from "@/lib/format";
import { useBacktestRun, useRunFile } from "@/lib/queries/backtests";
import {
  bucketRows,
  CAP_ORDER,
  cellNumber,
  distinctValues,
  dsrText,
  equitySeries,
  filterTrades,
  HEADLINE,
  LIQUIDITY_ORDER,
  REGIME_ORDER,
  runTitle,
  sweepName,
  changeText,
  longestDd,
  refusalCosts,
  REPEAT_STOPOUTS,
  stockSummary,
  topSymbols,
  topTrades,
  yearRows,
  regimeOffSpans,
  type Bucket,
  type CsvTable as CsvData,
  type Metrics,
  type Outcome,
  type Run,
  type RunFile,
  type TradeFilter,
} from "@/lib/runs";

function Block({
  title,
  note,
  children,
}: Readonly<{ title: string; note?: ReactNode; children: ReactNode }>) {
  return (
    <section aria-label={title} className="min-w-0">
      <header className="mb-3 flex flex-wrap items-baseline justify-between gap-x-4">
        <h2 className="text-lg">{title}</h2>
        {note ? <span className="text-xs text-foreground-muted">{note}</span> : null}
      </header>
      {children}
    </section>
  );
}

const runLink = (id: string) => (
  <Link to={`/hat/backtests/${id}`} className="font-mono text-accent hover:underline">
    {id}
  </Link>
);

function Header({ run }: Readonly<{ run: Run }>) {
  const { identity, data, config, binding } = run;
  return (
    <dl className="grid grid-cols-2 gap-x-8 gap-y-5 md:grid-cols-4">
      <LabelledValue label="Status">
        <RunStatusBadge status={identity.status} error={identity.error} />
      </LabelledValue>
      <LabelledValue label="Strategy">
        {identity.strategy}{" "}
        <span className="font-mono text-xs break-all text-foreground-muted">
          {identity.strategy_version}
        </span>
      </LabelledValue>
      <LabelledValue label="Sweep">
        {sweepName(identity)}{" "}
        <span className="font-mono text-xs text-foreground-muted">{identity.sweep_id}</span>
      </LabelledValue>
      <LabelledValue label="Change" className="col-span-2 md:col-span-1">
        {changeText(identity)}
      </LabelledValue>
      <LabelledValue label="Base run">
        {identity.base_run_id ? runLink(identity.base_run_id) : "none (baseline)"}
        {identity.base_run_id ? (
          <span className="mt-1 block text-xs text-foreground-muted">
            {binding.trades_changed === null
              ? "trades changed: not recorded"
              : `${formatNumber(binding.trades_changed, 0)} trades changed`}
          </span>
        ) : null}
        <span className="mt-1 block text-xs">
          <NoOpFlag binding={binding} />
        </span>
      </LabelledValue>
      <LabelledValue label="Split">
        {SPLIT_LABEL[data.split]}
        {data.validates_run_id ? (
          <span className="mt-1 block text-xs text-foreground-muted">
            validates {runLink(data.validates_run_id)}
          </span>
        ) : null}
      </LabelledValue>
      <LabelledValue label="Period">
        {data.period_start} to {data.period_end}
      </LabelledValue>
      <LabelledValue label="Universe · benchmark">
        {data.universe} · {data.benchmark}
      </LabelledValue>
      <LabelledValue label="Data snapshot">
        <span className="font-mono text-xs break-all">{data.data_snapshot_id ?? PLACEHOLDER}</span>
      </LabelledValue>
      <LabelledValue label="Engine commit">
        <span className="font-mono text-xs break-all">{identity.engine_commit ?? PLACEHOLDER}</span>
      </LabelledValue>
      <LabelledValue label="Cost model · start capital">
        {config.cost_model} · ₹{formatNumber(config.start_capital, 0)}
      </LabelledValue>
      <LabelledValue label="Created">{identity.created_at ?? PLACEHOLDER}</LabelledValue>
      <LabelledValue label="Overrides" className="col-span-2 md:col-span-2">
        <Overrides overrides={config.overrides} />
      </LabelledValue>
      <LabelledValue label="Resolved hash" className="col-span-2 md:col-span-2">
        <span className="font-mono text-xs break-all">{config.resolved_hash ?? PLACEHOLDER}</span>
      </LabelledValue>
    </dl>
  );
}

function Headline({
  metrics,
  base,
  baseError,
}: Readonly<{ metrics: Metrics; base: Metrics | undefined; baseError: Error | null }>) {
  let note: string | undefined;
  if (base) note = "small figures: change against the base run";
  else if (baseError) note = `no deltas: could not load the base run (${baseError.message})`;
  return (
    <Block title="Headline" note={note}>
      <dl className="grid grid-cols-2 gap-x-8 gap-y-5 sm:grid-cols-4 lg:grid-cols-6">
        {HEADLINE.map((def) => {
          let text: string | undefined;
          if (def.key === "longest_dd_sessions") text = longestDd(metrics);
          else if (def.key === "dsr_prob") text = dsrText(metrics);
          return (
            <LabelledValue key={def.key} label={def.label}>
              <span className="font-mono tabular-nums">
                <MetricWithDelta def={def} metrics={metrics} base={base} text={text} />
              </span>
            </LabelledValue>
          );
        })}
      </dl>
    </Block>
  );
}

/** A section fed by one CSV of the run folder, with its loading, error and empty states. */
function FileSection({
  runId,
  file,
  title,
  empty,
  meta,
  children,
}: Readonly<{
  runId: string;
  file: RunFile;
  title: string;
  empty: string;
  meta?: (data: CsvData) => ReactNode;
  children: (data: CsvData) => ReactNode;
}>) {
  const query = useRunFile(runId, file);
  return (
    <QuerySection
      title={title}
      query={query}
      isEmpty={(data) => noRows(data.rows)}
      empty={empty}
      meta={meta}
    >
      {children}
    </QuerySection>
  );
}

function EquitySection({ runId, benchmark }: Readonly<{ runId: string; benchmark: string }>) {
  return (
    <FileSection runId={runId} file="equity" title="Equity" empty="equity.csv has no sessions.">
      {(data) => {
        const points = equitySeries(data.rows);
        const spans = regimeOffSpans(points);
        return (
          <div className="space-y-6">
            <EquityChart points={points} spans={spans} benchmark={benchmark} />
            <DrawdownChart points={points} spans={spans} />
            {points.some((p) => p.breadth !== null) ? <BreadthChart points={points} /> : null}
          </div>
        );
      }}
    </FileSection>
  );
}

function excessTone(excess: number | null): string | undefined {
  if (excess === null) return undefined;
  return excess >= 0 ? "text-profit" : "text-loss";
}

const YEAR_COLUMNS = [
  { label: "Year" },
  { label: "Return %", numeric: true },
  { label: "Benchmark %", numeric: true },
  { label: "Excess pp", numeric: true },
  { label: "Max DD %", numeric: true },
  { label: "Trades", numeric: true },
  { label: "Exp. R", numeric: true },
];

function ByYear({ years }: Readonly<{ years: Run["breakdowns"]["by_year"] }>) {
  const rows = yearRows(years);
  if (rows.length === 0) return <Text tone="muted">No yearly rows in run.json.</Text>;
  return (
    <DataTable columns={YEAR_COLUMNS}>
      {rows.map((row) => (
        <TableRow key={row.year}>
          <TableCell>{row.year}</TableCell>
          <TableCell numeric>{formatNumber(row.return_pct, 1)}</TableCell>
          <TableCell numeric>{formatNumber(row.benchmark_return_pct, 1)}</TableCell>
          <TableCell numeric className={excessTone(row.excess_pct)}>
            {formatNumber(row.excess_pct, 1)}
          </TableCell>
          <TableCell numeric>{formatNumber(row.max_dd_pct, 1)}</TableCell>
          <TableCell numeric>{formatNumber(row.trades, 0)}</TableCell>
          <TableCell numeric>{formatNumber(row.expectancy_r, 2)}</TableCell>
        </TableRow>
      ))}
    </DataTable>
  );
}

function Breakdowns({ breakdowns }: Readonly<{ breakdowns: Run["breakdowns"] }>) {
  const groups: { title: string; buckets: Record<string, Bucket>; order?: readonly string[] }[] = [
    {
      title: "By regime at entry (index rule, breadth the session before)",
      buckets: breakdowns.by_regime,
      order: REGIME_ORDER,
    },
    { title: "By exit reason", buckets: breakdowns.by_exit_reason },
    { title: "By cap bucket", buckets: breakdowns.by_cap_bucket, order: CAP_ORDER },
    {
      title: "By liquidity bucket (ADV quintile)",
      buckets: breakdowns.by_liquidity_bucket,
      order: LIQUIDITY_ORDER,
    },
    { title: "By industry", buckets: breakdowns.by_industry },
  ];
  return (
    <Block title="Breakdowns">
      <div className="grid gap-x-12 gap-y-10 lg:grid-cols-2">
        {groups.map((group) => {
          const rows = bucketRows(group.buckets, group.order);
          return (
            <div key={group.title} className="min-w-0 space-y-2">
              <h3 className="text-sm">{group.title}</h3>
              {rows.length === 0 ? (
                <Text tone="muted">No data for this breakdown.</Text>
              ) : (
                <div className={rows.length > 12 ? "max-h-[420px] overflow-y-auto" : undefined}>
                  <BucketTable rows={rows} />
                </div>
              )}
            </div>
          );
        })}
      </div>
    </Block>
  );
}

const REFUSAL_COLUMNS = [
  { label: "Reason" },
  { label: "Refused", numeric: true },
  { label: "Mean fwd R 20d (gross)", numeric: true },
  { label: "Mean fwd R 60d (gross)", numeric: true },
  { label: "Would win at 60d %", numeric: true },
  { label: "With 60d data", numeric: true },
];

function Refusals({ runId, counts }: Readonly<{ runId: string; counts: Run["refusals"] }>) {
  const query = useRunFile(runId, "refusals");
  const rows = refusalCosts(counts, query.data?.rows ?? []);
  return (
    <Block
      title="Refusals"
      note="what each gate cost: the forward R, before costs, its refused entries would have made"
    >
      {query.error && !query.data ? (
        <Text role="alert" tone="loss" className="mb-3">
          Could not load refusals.csv: {query.error.message}. Counts only.
        </Text>
      ) : null}
      {rows.length === 0 ? (
        <Text tone="muted">No entry was refused.</Text>
      ) : (
        <DataTable columns={REFUSAL_COLUMNS}>
          {rows.map((row) => (
            <TableRow key={row.reason}>
              <TableCell>{row.reason}</TableCell>
              <TableCell numeric>{formatNumber(row.count, 0)}</TableCell>
              <TableCell numeric>{formatNumber(row.meanFwd20, 2)}</TableCell>
              <TableCell numeric>{formatNumber(row.meanFwd60, 2)}</TableCell>
              <TableCell numeric>{formatNumber(row.wouldWin60Pct, 1)}</TableCell>
              <TableCell numeric>{formatNumber(row.n60, 0)}</TableCell>
            </TableRow>
          ))}
        </DataTable>
      )}
    </Block>
  );
}

const TOP_TRADE_COLUMNS = [
  { label: "Symbol" },
  { label: "Entry" },
  { label: "Exit" },
  { label: "Net P&L", numeric: true },
  { label: "Share %", numeric: true },
];
const TOP_SYMBOL_COLUMNS = [
  { label: "Symbol" },
  { label: "Trades", numeric: true },
  { label: "Net P&L", numeric: true },
  { label: "Share %", numeric: true },
];

function Concentration({ runId, metrics }: Readonly<{ runId: string; metrics: Metrics }>) {
  const trades = useRunFile(runId, "trades");
  const stocks = useRunFile(runId, "stocks");
  return (
    <Block
      title="Concentration"
      note={`top 5 trades ${formatNumber(metrics.top5_trades_pnl_share_pct, 1)}% of net P&L · top 10 symbols ${formatNumber(metrics.top10_symbols_pnl_share_pct, 1)}% · CAGR without the top 5 trades ${formatNumber(metrics.cagr_ex_top5_trades_pct, 1)}% (with them ${formatNumber(metrics.cagr_pct, 1)}%)`}
    >
      <div className="grid gap-x-12 gap-y-10 lg:grid-cols-2">
        <div className="min-w-0 space-y-2">
          <h3 className="text-sm">Top 5 trades</h3>
          {trades.data ? (
            <DataTable columns={TOP_TRADE_COLUMNS}>
              {topTrades(trades.data.rows).map((row) => (
                <TableRow key={row.tradeId}>
                  <TableCell>{row.symbol}</TableCell>
                  <TableCell className="text-foreground-secondary">{row.entryDate}</TableCell>
                  <TableCell className="text-foreground-secondary">{row.exitDate}</TableCell>
                  <TableCell numeric>{formatNumber(row.netPnl, 0)}</TableCell>
                  <TableCell numeric>{formatNumber(row.sharePct, 1)}</TableCell>
                </TableRow>
              ))}
            </DataTable>
          ) : (
            <FileState query={trades} />
          )}
        </div>
        <div className="min-w-0 space-y-2">
          <h3 className="text-sm">Top 10 symbols</h3>
          {stocks.data ? (
            <DataTable columns={TOP_SYMBOL_COLUMNS}>
              {topSymbols(stocks.data.rows).map((row) => (
                <TableRow key={row.symbol}>
                  <TableCell>{row.symbol}</TableCell>
                  <TableCell numeric>{formatNumber(row.trades, 0)}</TableCell>
                  <TableCell numeric>{formatNumber(row.netPnl, 0)}</TableCell>
                  <TableCell numeric>{formatNumber(row.sharePct, 1)}</TableCell>
                </TableRow>
              ))}
            </DataTable>
          ) : (
            <FileState query={stocks} />
          )}
        </div>
      </div>
    </Block>
  );
}

function FileState({
  query,
}: Readonly<{ query: { error: Error | null; data: CsvData | undefined } }>) {
  if (query.error) {
    return (
      <Text role="alert" tone="loss">
        Could not load: {query.error.message}
      </Text>
    );
  }
  return (
    <Text tone="muted" aria-busy="true">
      Loading…
    </Text>
  );
}

const OUTCOMES: { value: Outcome; label: string }[] = [
  { value: "all", label: "All trades" },
  { value: "win", label: "Winners" },
  { value: "loss", label: "Losers" },
];

function TradesTable({ data }: Readonly<{ data: CsvData }>) {
  const [filter, setFilter] = useState<TradeFilter>({
    query: "",
    exitReason: null,
    outcome: "all",
  });
  const reasons = distinctValues(data.rows, "exit_reason");
  const rows = filterTrades(data.rows, filter);
  // run_id is the page's own run: no column for it.
  const columns = data.columns.filter((column) => column !== "run_id");
  const ordered = ["symbol", ...columns.filter((column) => column !== "symbol")];
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-4">
        <label className="flex flex-col gap-1 text-xs text-foreground-muted">
          <span>Symbol or industry</span>
          <input
            type="search"
            className={fieldClass}
            value={filter.query}
            onChange={(event) => setFilter((old) => ({ ...old, query: event.target.value }))}
          />
        </label>
        <label className="flex flex-col gap-1 text-xs text-foreground-muted">
          <span>Exit reason</span>
          <select
            className={fieldClass}
            value={filter.exitReason ?? ""}
            onChange={(event) =>
              setFilter((old) => ({ ...old, exitReason: event.target.value || null }))
            }
          >
            <option value="">All reasons</option>
            {reasons.map((reason) => (
              <option key={reason} value={reason}>
                {reason}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs text-foreground-muted">
          <span>Outcome</span>
          <select
            className={fieldClass}
            value={filter.outcome}
            onChange={(event) =>
              setFilter((old) => ({ ...old, outcome: event.target.value as Outcome }))
            }
          >
            {OUTCOMES.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
        <span className="pb-2 text-xs text-foreground-muted">
          {rows.length} of {data.rows.length} legs
        </span>
      </div>
      <CsvTable
        columns={ordered.filter((column) => data.columns.includes(column))}
        rows={rows}
        initialSort={{ key: "exit_date", dir: "asc" }}
      />
    </div>
  );
}

function StocksTable({ data }: Readonly<{ data: CsvData }>) {
  const summary = stockSummary(data.rows);
  return (
    <div className="space-y-4">
      <Text tone="muted">
        {summary.traded} symbols traded: {summary.profitable} in profit, {summary.losing} at a loss.{" "}
        {summary.repeatStopouts} stopped out {REPEAT_STOPOUTS} or more times (marked).
      </Text>
      <CsvTable
        columns={data.columns}
        rows={data.rows}
        initialSort={{ key: "net_pnl", dir: "desc" }}
        highlight={(row) => (cellNumber(row, "stopouts") ?? 0) >= REPEAT_STOPOUTS}
      />
    </div>
  );
}

function RunBody({ run }: Readonly<{ run: Run }>) {
  const runId = run.identity.run_id;
  const base = useBacktestRun(run.identity.base_run_id ?? "");
  // A failed or stopped run publishes run.json only: empty metrics and no CSV files.
  if (run.identity.status !== "complete") {
    return (
      <div className="space-y-14">
        <Header run={run} />
        <Text tone="warning">
          This run did not complete ({run.identity.error ?? "no failure code"}): its folder holds
          run.json only, with no metrics and no files.
        </Text>
      </div>
    );
  }
  return (
    <div className="space-y-14">
      <Header run={run} />
      <Headline metrics={run.metrics} base={base.data?.metrics} baseError={base.error} />
      <EquitySection runId={runId} benchmark={run.data.benchmark} />
      <Block title="By year" note={`run vs ${run.data.benchmark}`}>
        <ByYear years={run.breakdowns.by_year} />
      </Block>
      <Breakdowns breakdowns={run.breakdowns} />
      <Refusals runId={runId} counts={run.refusals} />
      <Concentration runId={runId} metrics={run.metrics} />
      <FileSection
        runId={runId}
        file="trades"
        title="Trades"
        empty="No closed trades."
        meta={(data) => `${data.rows.length} legs`}
      >
        {(data) => <TradesTable data={data} />}
      </FileSection>
      <FileSection
        runId={runId}
        file="stocks"
        title="Stocks"
        empty="No symbol produced a signal."
        meta={(data) => `${data.rows.length} symbols`}
      >
        {(data) => <StocksTable data={data} />}
      </FileSection>
    </div>
  );
}

/** One run folder: header, headline metrics, equity, breakdowns, refusals, trades and stocks. */
export default function BacktestRunPage() {
  const runId = useParams().runId ?? "";
  const run = useBacktestRun(runId);
  const identity = run.data?.identity;
  return (
    <PageLayout
      title={identity ? runTitle(identity) : "Backtest run"}
      description={runId}
      aside={
        <Link to="/hat/backtests" className="text-sm text-accent hover:underline">
          All runs
        </Link>
      }
    >
      <QuerySection title="Run" query={run} isEmpty={() => false} empty="">
        {(data) => <RunBody run={data} />}
      </QuerySection>
    </PageLayout>
  );
}
