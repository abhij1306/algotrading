import { memo } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui";
import dynamic from "@/lib/dynamic";
import type { EquityRow } from "./result-view";

interface DrawdownPoint {
  date: string;
  drawdown_pct: number;
}

function ChartSkeleton() {
  return (
    <div className="flex h-full items-center justify-center">
      <div className="h-full w-full animate-pulse rounded-md bg-background-secondary" />
    </div>
  );
}

// recharts loads on demand to keep it out of the initial bundle.
const DynamicEquityChart = dynamic(
  () =>
    import("recharts").then((mod) => {
      const { Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } = mod;

      return function EquityChartComponent({ data }: Readonly<{ data: EquityRow[] }>) {
        if (data.length === 0) {
          return (
            <div className="flex h-full items-center justify-center rounded-md border border-border bg-background-secondary text-sm text-foreground-muted">
              No equity curve data available for this run.
            </div>
          );
        }

        return (
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={data} margin={{ top: 10, right: 30, left: 60, bottom: 40 }}>
              <XAxis
                dataKey="date"
                height={60}
                angle={-45}
                textAnchor="end"
                stroke="var(--color-foreground-muted)"
                tick={{ fill: "var(--color-foreground-muted)", fontSize: 10 }}
                axisLine={{ stroke: "var(--color-border)" }}
                tickLine={{ stroke: "var(--color-border)" }}
              />
              <YAxis
                width={60}
                stroke="var(--color-foreground-muted)"
                tick={{ fill: "var(--color-foreground-secondary)", fontSize: 11 }}
                axisLine={{ stroke: "var(--color-border)" }}
                tickLine={{ stroke: "var(--color-border)" }}
              />
              <Tooltip
                contentStyle={{
                  backgroundColor: "var(--color-elevated)",
                  border: "1px solid var(--color-border)",
                  borderRadius: "var(--radius-md)",
                  color: "var(--color-foreground)",
                  fontSize: "12px",
                }}
              />
              <Line
                type="monotone"
                dataKey="equity"
                stroke="var(--color-primary)"
                strokeWidth={2.5}
                dot={false}
                name="Portfolio"
              />
              <Line
                type="monotone"
                dataKey="benchmark"
                stroke="var(--color-foreground-muted)"
                strokeWidth={1.8}
                strokeDasharray="5 5"
                dot={false}
                name="Benchmark"
              />
            </LineChart>
          </ResponsiveContainer>
        );
      };
    }),
  { loading: ChartSkeleton },
);

const DynamicDrawdownChart = dynamic(
  () =>
    import("recharts").then((mod) => {
      const { Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } = mod;

      return function DrawdownChartComponent({ data }: Readonly<{ data: DrawdownPoint[] }>) {
        if (data.length === 0) {
          return (
            <div className="flex h-full items-center justify-center rounded-md border border-border bg-background-secondary text-xs text-foreground-muted">
              No drawdown data
            </div>
          );
        }

        return (
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={data}>
              <XAxis
                dataKey="date"
                tick={{ fontSize: 10, fill: "var(--color-foreground-muted)" }}
                hide
              />
              <YAxis
                tick={{ fontSize: 10, fill: "var(--color-foreground-secondary)" }}
                width={40}
              />
              <Tooltip
                contentStyle={{
                  backgroundColor: "var(--color-surface)",
                  border: "1px solid var(--color-border)",
                  borderRadius: "var(--radius-md)",
                  fontSize: "11px",
                }}
              />
              <Line
                type="monotone"
                dataKey="drawdown_pct"
                stroke="var(--color-loss)"
                dot={false}
                strokeWidth={2}
              />
            </LineChart>
          </ResponsiveContainer>
        );
      };
    }),
  { loading: ChartSkeleton },
);

// Memoized so a result poll that changes nothing does not redraw the charts.
const EquityChart = memo(DynamicEquityChart);
const DrawdownChart = memo(DynamicDrawdownChart);

/** The portfolio equity curve against the benchmark. */
export function EquityCurveCard({ rows }: Readonly<{ rows: EquityRow[] }>) {
  return (
    <Card
      variant="void"
      className="shrink-0 rounded-none border-0 border-b border-border bg-surface"
    >
      <CardHeader className="flex-row items-center justify-between px-4 py-3">
        <CardTitle className="text-foreground">Equity Curve vs Benchmark</CardTitle>
        <div className="flex items-center gap-4 text-sm">
          <span className="flex items-center gap-2 text-primary">
            <span className="h-0.5 w-5 bg-primary" />
            <span>Portfolio</span>
          </span>
          <span className="flex items-center gap-2 text-foreground-muted">
            <span className="h-0.5 w-5 bg-foreground-muted" />
            <span>Benchmark</span>
          </span>
        </div>
      </CardHeader>
      <CardContent className="p-4" style={{ height: "360px" }}>
        <EquityChart data={rows} />
      </CardContent>
    </Card>
  );
}

/** The drawdown curve under the equity chart. */
export function DrawdownCard({ points }: Readonly<{ points: DrawdownPoint[] }>) {
  return (
    <Card
      variant="void"
      className="shrink-0 rounded-none border-0 border-b border-border bg-surface"
    >
      <CardHeader className="px-4 py-3">
        <CardTitle className="text-sm uppercase tracking-wider text-foreground-muted">
          Drawdown
        </CardTitle>
      </CardHeader>
      <CardContent className="h-24 p-3">
        <DrawdownChart data={points} />
      </CardContent>
    </Card>
  );
}
