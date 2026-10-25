import { Link, useSearchParams } from "react-router";
import { Text } from "@/components/atoms/text";
import { TableCell, TableHead, TableRow } from "@/components/atoms/table";
import { Table, TableBody, TableHeader } from "@/components/atoms/table";
import {
  CompareEquityChart,
  CompareYearlyChart,
  SERIES,
} from "@/components/organisms/backtests/charts";
import { Overrides } from "@/components/organisms/backtests/run-parts";
import { PageLayout } from "@/components/templates/page-layout";
import {
  compareEquity,
  compareYearly,
  formatMetric,
  MAX_COMPARE,
  METRICS,
  runName,
  type RunDetail,
} from "@/lib/backtests";
import { useBacktestRunsById } from "@/lib/queries/backtests";

function Swatch({ index }: Readonly<{ index: number }>) {
  return (
    <span
      aria-hidden
      className="mr-2 inline-block size-2.5 rounded-full align-middle"
      style={{ background: SERIES[index] }}
    />
  );
}

function MetricsTable({ details }: Readonly<{ details: RunDetail[] }>) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Metric</TableHead>
          {details.map((detail, index) => (
            <TableHead key={detail.run.id} numeric>
              <Swatch index={index} />
              <Link to={`/hat/backtests/${detail.run.id}`} className="hover:text-accent">
                {runName(detail.run)}
              </Link>
            </TableHead>
          ))}
        </TableRow>
      </TableHeader>
      <TableBody>
        <TableRow>
          <TableCell className="text-foreground-muted">Window</TableCell>
          {details.map((d) => (
            <TableCell key={d.run.id} numeric>
              {d.run.period_start} to {d.run.period_end}
            </TableCell>
          ))}
        </TableRow>
        {METRICS.map((metric) => (
          <TableRow key={metric.key}>
            <TableCell className="text-foreground-muted">{metric.label}</TableCell>
            {details.map((d) => (
              <TableCell key={d.run.id} numeric>
                {formatMetric(d.run.headline[metric.key], metric.format)}
              </TableCell>
            ))}
          </TableRow>
        ))}
        {(["dsr", "pbo"] as const).map((key) => (
          <TableRow key={key}>
            <TableCell className="text-foreground-muted">{key.toUpperCase()}</TableCell>
            {details.map((d) => (
              <TableCell key={d.run.id} numeric>
                {formatMetric(d.run[key], "ratio")}
              </TableCell>
            ))}
          </TableRow>
        ))}
        <TableRow>
          <TableCell className="align-top text-foreground-muted">Overrides</TableCell>
          {details.map((d) => (
            <TableCell key={d.run.id} className="align-top whitespace-normal">
              <Overrides overrides={d.run.overrides} />
            </TableCell>
          ))}
        </TableRow>
      </TableBody>
    </Table>
  );
}

/** Two to four runs side by side: headline metrics, equity and yearly returns. */
export default function BacktestComparePage() {
  const [params] = useSearchParams();
  const ids = (params.get("ids") ?? "").split(",").filter(Boolean).slice(0, MAX_COMPARE);
  const queries = useBacktestRunsById(ids);
  const failed = queries.find((query) => query.error);
  const details = queries.flatMap((query) => (query.data ? [query.data] : []));
  const loaded = details.length === ids.length;
  const reports = details.map((detail) => detail.report);
  // A run without report data keeps its colour slot but draws no series.
  const names = details.map((detail) => (detail.report ? runName(detail.run) : null));
  const missing = details.filter((detail) => !detail.report).map((d) => runName(d.run));
  return (
    <PageLayout
      title="Compare runs"
      description={`${ids.length} runs`}
      aside={
        <Link to="/hat/backtests" className="text-sm text-accent hover:underline">
          All runs
        </Link>
      }
    >
      {ids.length < 2 ? (
        <Text tone="muted">Pick two to four runs in the list to compare them.</Text>
      ) : failed?.error ? (
        <Text role="alert" tone="loss">
          Could not load a run: {failed.error.message}
        </Text>
      ) : !loaded ? (
        <Text tone="muted" aria-busy="true">
          Loading…
        </Text>
      ) : (
        <div className="space-y-14">
          <MetricsTable details={details} />
          {missing.length > 0 ? (
            <Text tone="muted">No report data, so no curves, for: {missing.join(", ")}.</Text>
          ) : null}
          <div className="grid gap-x-12 gap-y-10 lg:grid-cols-2">
            <CompareEquityChart rows={compareEquity(reports)} names={names} />
            <CompareYearlyChart rows={compareYearly(reports)} names={names} />
          </div>
        </div>
      )}
    </PageLayout>
  );
}
