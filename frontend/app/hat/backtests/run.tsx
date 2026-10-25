import type { ReactNode } from "react";
import { Link, useParams } from "react-router";
import { Text } from "@/components/atoms/text";
import { LabelledValue } from "@/components/molecules/labelled-value";
import { QuerySection } from "@/components/molecules/query-section";
import {
  DrawdownChart,
  EquityChart,
  GroupChart,
  MfeScatter,
  RCurveChart,
  YearlyChart,
} from "@/components/organisms/backtests/charts";
import {
  FunnelView,
  GroupTable,
  HeadlineGrid,
  Overrides,
  RunStatus,
  TradeTable,
} from "@/components/organisms/backtests/run-parts";
import { PageLayout } from "@/components/templates/page-layout";
import {
  benchmarkName,
  equityRows,
  formatMetric,
  groupRows,
  rRows,
  runName,
  scalarMetrics,
  scatterPoints,
  type Report,
  type RunDetail,
} from "@/lib/backtests";
import { PLACEHOLDER } from "@/lib/format";
import { useBacktestRun, useBacktestTrades } from "@/lib/queries/backtests";

function Block({
  title,
  note,
  children,
}: Readonly<{ title: string; note?: string; children: ReactNode }>) {
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

function RunFacts({ detail }: Readonly<{ detail: RunDetail }>) {
  const { run } = detail;
  return (
    <dl className="grid grid-cols-2 gap-x-8 gap-y-5 md:grid-cols-4">
      <LabelledValue label="Status">
        <RunStatus status={run.status} />
      </LabelledValue>
      <LabelledValue label="Window">
        {run.period_start} to {run.period_end}
      </LabelledValue>
      <LabelledValue label="Family">
        <span className="break-all">{run.family_key}</span>
      </LabelledValue>
      <LabelledValue label="DSR · PBO">
        <span className="font-mono tabular-nums">
          {formatMetric(run.dsr, "ratio")} · {formatMetric(run.pbo, "ratio")}
        </span>
      </LabelledValue>
      <LabelledValue label="Sweep variant">{run.variant?.label ?? PLACEHOLDER}</LabelledValue>
      <LabelledValue label="Overrides" className="col-span-2 md:col-span-3">
        <Overrides overrides={run.overrides} />
      </LabelledValue>
    </dl>
  );
}

function TradeQuality({ report, trades }: Readonly<{ report: Report; trades: TradesQuery }>) {
  return (
    <Block title="Trade quality">
      <div className="grid gap-x-12 gap-y-10 lg:grid-cols-2">
        <RCurveChart rows={rRows(report)} />
        {trades.data ? (
          <MfeScatter points={scatterPoints(trades.data)} />
        ) : (
          <Text tone={trades.error ? "loss" : "muted"}>
            {trades.error ? `Could not load the trades: ${trades.error.message}` : "Loading…"}
          </Text>
        )}
      </div>
    </Block>
  );
}

type TradesQuery = ReturnType<typeof useBacktestTrades>;

function ReportView({ runId, report }: Readonly<{ runId: string; report: Report }>) {
  const trades = useBacktestTrades(runId, true);
  const rows = equityRows(report);
  const bench = report.benchmark;
  const benchmark = benchmarkName(report);
  return (
    <>
      <Block
        title="Equity and drawdown"
        note={
          bench
            ? `${benchmark}: return ${formatMetric(bench.total_return, "pct")}, CAGR ${formatMetric(bench.cagr, "pct")}, max drawdown ${formatMetric(bench.max_drawdown, "pct")}`
            : `No ${benchmark} closes for this window`
        }
      >
        <div className="grid gap-x-12 gap-y-10 lg:grid-cols-2">
          <EquityChart rows={rows} benchmark={benchmark} />
          <DrawdownChart rows={rows} benchmark={benchmark} />
          <YearlyChart yearly={report.yearly} benchmark={benchmark} />
        </div>
      </Block>
      <TradeQuality report={report} trades={trades} />
      <Block title="Breakdowns">
        <div className="grid gap-x-12 gap-y-10 lg:grid-cols-3">
          {[
            { title: "By exit reason", rows: groupRows(report.exit_reasons) },
            { title: "By hold time (sessions)", rows: groupRows(report.hold_time) },
            {
              title: "By regime at entry",
              note: `NIFTY 500 trend reading: ${report.regime_rule}`,
              rows: groupRows(report.regime),
            },
          ].map((group) => (
            <div key={group.title} className="min-w-0 space-y-4">
              <GroupChart title={group.title} rows={group.rows} />
              {group.note ? <Text tone="muted">{group.note}</Text> : null}
              <GroupTable rows={group.rows} />
            </div>
          ))}
        </div>
      </Block>
      <Block title="Signal funnel">
        <FunnelView funnel={report.funnel} />
      </Block>
      <Block title="Trades" note={trades.data ? `${trades.data.length} in exit order` : undefined}>
        {trades.data ? (
          <div className="max-h-[640px] overflow-y-auto">
            <TradeTable trades={trades.data} />
          </div>
        ) : null}
      </Block>
    </>
  );
}

function StoredMetrics({ detail }: Readonly<{ detail: RunDetail }>) {
  const rows = scalarMetrics(detail.metrics);
  return (
    <Block title="Stored metrics" note="This run has no report data">
      <Text tone="muted" className="mb-4">
        Runs recorded before report data existed, and failed or running runs, show their stored
        metrics only. Re-run the version to get its charts.
      </Text>
      <dl className="grid grid-cols-2 gap-x-8 gap-y-4 md:grid-cols-4">
        {rows.map(([key, value]) => (
          <LabelledValue key={key} label={key}>
            <span className="font-mono text-sm break-all">{value}</span>
          </LabelledValue>
        ))}
      </dl>
    </Block>
  );
}

/** One backtest run: its facts, headline metrics and, when it has report data, the charts. */
export default function BacktestRunPage() {
  const runId = useParams().runId ?? "";
  const detail = useBacktestRun(runId);
  return (
    <PageLayout
      title={detail.data ? runName(detail.data.run) : "Backtest run"}
      description={runId}
      aside={
        <Link to="/hat/backtests" className="text-sm text-accent hover:underline">
          All runs
        </Link>
      }
    >
      <QuerySection title="Run" query={detail} isEmpty={() => false} empty="">
        {(data) => (
          <div className="space-y-14">
            <RunFacts detail={data} />
            <HeadlineGrid headline={data.run.headline} />
            {data.report ? (
              <ReportView runId={runId} report={data.report} />
            ) : (
              <StoredMetrics detail={data} />
            )}
          </div>
        )}
      </QuerySection>
    </PageLayout>
  );
}
