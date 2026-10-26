import type { ReactNode } from "react";
import { Link, useSearchParams } from "react-router";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/atoms/table";
import { Text } from "@/components/atoms/text";
import { CompareEquityChart, SERIES } from "@/components/organisms/backtests/charts";
import { MetricWithDelta, NoOpFlag, SPLIT_LABEL } from "@/components/organisms/backtests/run-parts";
import { formatNumber, PLACEHOLDER } from "@/lib/format";
import { PageLayout } from "@/components/templates/page-layout";
import { useBacktestRunsById, useRunFiles } from "@/lib/queries/backtests";
import {
  compareEquity,
  dsrText,
  equitySeries,
  HEADLINE,
  longestDd,
  MAX_COMPARE,
  type Run,
} from "@/lib/runs";

function Swatch({ index }: Readonly<{ index: number }>) {
  return (
    <span
      aria-hidden
      className="mr-2 inline-block size-2.5 rounded-full align-middle"
      style={{ background: SERIES[index] }}
    />
  );
}

function bindingText(run: Run): string {
  if (run.identity.base_run_id === null) return "baseline";
  const changed = run.binding.trades_changed;
  return changed === null ? PLACEHOLDER : `${formatNumber(changed, 0)} trades changed`;
}

function FactRow({
  label,
  runs,
  cell,
}: Readonly<{ label: string; runs: readonly Run[]; cell: (run: Run) => ReactNode }>) {
  return (
    <TableRow>
      <TableCell className="align-top text-foreground-muted">{label}</TableCell>
      {runs.map((run) => (
        <TableCell key={run.identity.run_id} className="align-top whitespace-normal">
          {cell(run)}
        </TableCell>
      ))}
    </TableRow>
  );
}

function MetricsTable({ runs }: Readonly<{ runs: readonly Run[] }>) {
  const first = runs[0];
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Metric</TableHead>
          {runs.map((run, index) => (
            <TableHead key={run.identity.run_id}>
              <Swatch index={index} />
              <Link to={`/hat/backtests/${run.identity.run_id}`} className="hover:text-accent">
                {run.identity.strategy} · {run.identity.variant}
              </Link>
            </TableHead>
          ))}
        </TableRow>
      </TableHeader>
      <TableBody>
        <FactRow label="Change" runs={runs} cell={(run) => run.identity.change || "baseline"} />
        <FactRow
          label="Split · period"
          runs={runs}
          cell={(run) =>
            `${SPLIT_LABEL[run.data.split]} · ${run.data.period_start} to ${run.data.period_end}`
          }
        />
        <FactRow
          label="Binding"
          runs={runs}
          cell={(run) =>
            run.binding.no_op === true ? <NoOpFlag binding={run.binding} /> : bindingText(run)
          }
        />
        {HEADLINE.map((def) => (
          <TableRow key={def.key}>
            <TableCell className="text-foreground-muted">{def.label}</TableCell>
            {runs.map((run, index) => {
              let text: string | undefined;
              if (def.key === "longest_dd_sessions") text = longestDd(run.metrics);
              else if (def.key === "dsr_prob") text = dsrText(run.metrics);
              return (
                <TableCell key={run.identity.run_id} numeric>
                  <MetricWithDelta
                    def={def}
                    metrics={run.metrics}
                    base={index === 0 ? null : first?.metrics}
                    text={text}
                  />
                </TableCell>
              );
            })}
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

function Curves({ ids, names }: Readonly<{ ids: readonly string[]; names: string[] }>) {
  const files = useRunFiles(ids, "equity");
  const failed = files.find((file) => file.error && !file.data);
  if (failed?.error) {
    return (
      <Text role="alert" tone="loss">
        Could not load an equity.csv: {failed.error.message}
      </Text>
    );
  }
  if (files.some((file) => !file.data)) {
    return (
      <Text tone="muted" aria-busy="true">
        Loading the equity curves…
      </Text>
    );
  }
  const series = files.map((file) => (file.data ? equitySeries(file.data.rows) : null));
  return <CompareEquityChart rows={compareEquity(series)} names={names} />;
}

/** Two to four runs side by side: metrics with deltas against the first, and equity curves. */
export default function BacktestComparePage() {
  const [params] = useSearchParams();
  const ids = (params.get("ids") ?? "").split(",").filter(Boolean).slice(0, MAX_COMPARE);
  const queries = useBacktestRunsById(ids);
  // Only a run that never loaded is a failure: a failed refetch keeps its last good read.
  const failed = queries.find((query) => query.error && query.data === undefined);
  const runs = queries.flatMap((query) => (query.data ? [query.data] : []));
  let notice: ReactNode = null;
  if (ids.length < 2) {
    notice = <Text tone="muted">Pick two to four runs in the list to compare them.</Text>;
  } else if (failed?.error) {
    notice = (
      <Text role="alert" tone="loss">
        Could not load a run: {failed.error.message}
      </Text>
    );
  } else if (runs.length < ids.length) {
    notice = (
      <Text tone="muted" aria-busy="true">
        Loading…
      </Text>
    );
  }
  return (
    <PageLayout
      title="Compare runs"
      description={`${ids.length} runs; deltas against the first`}
      aside={
        <Link to="/hat/backtests" className="text-sm text-accent hover:underline">
          All runs
        </Link>
      }
    >
      {notice ?? (
        <div className="space-y-14">
          <MetricsTable runs={runs} />
          <Curves
            ids={ids}
            names={runs.map((run) => `${run.identity.strategy} · ${run.identity.variant}`)}
          />
        </div>
      )}
    </PageLayout>
  );
}
