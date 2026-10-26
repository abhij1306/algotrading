import type { ReactElement } from "react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ReferenceArea,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { formatNumber, PLACEHOLDER } from "@/lib/format";
import { seriesKey, type EquityPoint, type Span } from "@/lib/runs";

/** Series colours in fixed order (globals.css); the benchmark is the muted ink, dashed. */
export const SERIES = [
  "var(--color-chart-1)",
  "var(--color-chart-2)",
  "var(--color-chart-3)",
  "var(--color-chart-4)",
];
const BENCH = "var(--color-foreground-muted)";
// Sessions with the index regime off: a light wash of the muted ink behind the series.
const REGIME_OFF = "var(--color-foreground-muted)";
const REGIME_OFF_OPACITY = 0.12;

const axis = {
  stroke: "var(--color-line)",
  tick: { fill: "var(--color-foreground-muted)", fontSize: 12 },
  tickLine: false,
} as const;
const grid = <CartesianGrid stroke="var(--color-line)" strokeDasharray="0" vertical={false} />;
const tooltipStyle = {
  contentStyle: {
    background: "var(--color-background)",
    border: "1px solid var(--color-line)",
    borderRadius: 6,
    fontSize: 13,
  },
  labelStyle: { color: "var(--color-foreground-muted)" },
  itemStyle: { color: "var(--color-foreground)" },
} as const;
// Legend text stays in text ink; the swatch beside it carries the series colour.
const legend = (
  <Legend
    wrapperStyle={{ fontSize: 13 }}
    formatter={(value: string) => (
      <span style={{ color: "var(--color-foreground-secondary)" }}>{value}</span>
    )}
  />
);

const pct = (value: unknown) =>
  typeof value === "number" ? `${formatNumber(value, 1)}%` : PLACEHOLDER;
const monthYear = (date: unknown) => (typeof date === "string" ? date.slice(0, 7) : "");
const xAxis = <XAxis dataKey="date" {...axis} tickFormatter={monthYear} minTickGap={48} />;

function regimeShading(spans: readonly Span[]) {
  return spans.map((span) => (
    <ReferenceArea
      key={span.start}
      x1={span.start}
      x2={span.end}
      fill={REGIME_OFF}
      fillOpacity={REGIME_OFF_OPACITY}
      strokeOpacity={0}
      ifOverflow="hidden"
    />
  ));
}

/** One titled chart in a fixed-height responsive box. */
export function ChartFrame({
  title,
  note,
  height = 280,
  children,
}: Readonly<{ title: string; note?: string; height?: number; children: ReactElement }>) {
  return (
    <figure className="min-w-0">
      <figcaption className="mb-2 flex flex-wrap items-baseline justify-between gap-x-4">
        <span className="text-sm">{title}</span>
        {note ? <span className="text-xs text-foreground-muted">{note}</span> : null}
      </figcaption>
      <ResponsiveContainer width="100%" height={height}>
        {children}
      </ResponsiveContainer>
    </figure>
  );
}

const SHADING_NOTE = "shaded: index regime off";

/** Equity against the benchmark, both as growth from the first session; regime-off shaded. */
export function EquityChart({
  points,
  spans,
  benchmark,
}: Readonly<{ points: readonly EquityPoint[]; spans: readonly Span[]; benchmark: string }>) {
  const shading = spans.length > 0 ? `, ${SHADING_NOTE}` : "";
  return (
    <ChartFrame title={`Equity vs ${benchmark}`} note={`growth from the first session${shading}`}>
      <LineChart data={points as EquityPoint[]} syncId="run">
        {grid}
        {regimeShading(spans)}
        {xAxis}
        <YAxis {...axis} tickFormatter={pct} width={56} domain={["auto", "auto"]} />
        <Tooltip {...tooltipStyle} formatter={pct} />
        {legend}
        <Line
          name="Strategy"
          dataKey="growth"
          stroke={SERIES[0]}
          strokeWidth={2}
          dot={false}
          isAnimationActive={false}
        />
        <Line
          name={benchmark}
          dataKey="benchmark"
          stroke={BENCH}
          strokeWidth={2}
          strokeDasharray="4 3"
          dot={false}
          isAnimationActive={false}
        />
      </LineChart>
    </ChartFrame>
  );
}

/** The strategy's drawdown under the equity chart, on the same dates. */
export function DrawdownChart({
  points,
  spans,
}: Readonly<{ points: readonly EquityPoint[]; spans: readonly Span[] }>) {
  return (
    <ChartFrame title="Drawdown" height={160}>
      <AreaChart data={points as EquityPoint[]} syncId="run">
        {grid}
        {regimeShading(spans)}
        {xAxis}
        <YAxis {...axis} tickFormatter={pct} width={56} />
        <Tooltip {...tooltipStyle} formatter={pct} />
        <Area
          name="Drawdown"
          dataKey="drawdown"
          stroke="var(--color-loss)"
          fill="var(--color-loss)"
          fillOpacity={0.15}
          strokeWidth={1.5}
          isAnimationActive={false}
        />
      </AreaChart>
    </ChartFrame>
  );
}

/** Market breadth (equity.csv `breadth_pct`) on the same dates. */
export function BreadthChart({ points }: Readonly<{ points: readonly EquityPoint[] }>) {
  return (
    <ChartFrame title="Market breadth" height={120}>
      <AreaChart data={points as EquityPoint[]} syncId="run">
        {grid}
        {xAxis}
        <YAxis {...axis} tickFormatter={pct} width={56} domain={[0, 100]} ticks={[0, 50, 100]} />
        <ReferenceLine y={50} stroke="var(--color-foreground-muted)" strokeDasharray="3 3" />
        <Tooltip {...tooltipStyle} formatter={pct} />
        <Area
          name="Breadth"
          dataKey="breadth"
          stroke={SERIES[2]}
          fill={SERIES[2]}
          fillOpacity={0.15}
          strokeWidth={1.5}
          isAnimationActive={false}
        />
      </AreaChart>
    </ChartFrame>
  );
}

/** The compared runs' equity, each as growth from its own first session. */
export function CompareEquityChart({
  rows,
  names,
}: Readonly<{ rows: Record<string, number | string | null>[]; names: (string | null)[] }>) {
  return (
    <ChartFrame title="Equity" note="growth from each run's first session" height={340}>
      <LineChart data={rows}>
        {grid}
        {xAxis}
        <YAxis {...axis} tickFormatter={pct} width={56} domain={["auto", "auto"]} />
        <Tooltip {...tooltipStyle} formatter={pct} />
        {legend}
        {names.map((name, index) =>
          name === null ? null : (
            <Line
              key={seriesKey(index)}
              name={name}
              dataKey={seriesKey(index)}
              stroke={SERIES[index]}
              strokeWidth={2}
              dot={false}
              isAnimationActive={false}
              connectNulls
            />
          ),
        )}
      </LineChart>
    </ChartFrame>
  );
}
