import type { ReactElement } from "react";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  seriesKey,
  type EquityRow,
  type GroupRow,
  type Report,
  type ScatterPoint,
} from "@/lib/backtests";
import { formatNumber, PLACEHOLDER } from "@/lib/format";

/** Series colours in fixed order (globals.css); NIFTY 500 TRI is the muted ink, dashed. */
export const SERIES = [
  "var(--color-chart-1)",
  "var(--color-chart-2)",
  "var(--color-chart-3)",
  "var(--color-chart-4)",
];
const BENCH = "var(--color-foreground-muted)";
const HEIGHT = 260;

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
  typeof value === "number" ? `${formatNumber(value * 100, 1)}%` : PLACEHOLDER;
const growth = (value: unknown) =>
  typeof value === "number" ? `${formatNumber((value - 1) * 100, 1)}%` : PLACEHOLDER;
const two = (value: unknown) => (typeof value === "number" ? formatNumber(value, 2) : PLACEHOLDER);
const monthYear = (date: unknown) => (typeof date === "string" ? date.slice(0, 7) : "");

/** One titled chart in a fixed-height responsive box. */
export function ChartFrame({
  title,
  note,
  children,
}: Readonly<{ title: string; note?: string; children: ReactElement }>) {
  return (
    <figure className="min-w-0">
      <figcaption className="mb-2 flex items-baseline justify-between gap-4">
        <span className="text-sm">{title}</span>
        {note ? <span className="text-xs text-foreground-muted">{note}</span> : null}
      </figcaption>
      <ResponsiveContainer width="100%" height={HEIGHT}>
        {children}
      </ResponsiveContainer>
    </figure>
  );
}

/** Equity and NIFTY 500 TRI, both rebased to the first session, as growth in percent. */
export function EquityChart({
  rows,
  benchmark,
}: Readonly<{ rows: EquityRow[]; benchmark: string }>) {
  return (
    <ChartFrame title={`Equity vs ${benchmark}`} note="growth from the first session">
      <LineChart data={rows}>
        {grid}
        <XAxis dataKey="date" {...axis} tickFormatter={monthYear} minTickGap={48} />
        <YAxis {...axis} tickFormatter={growth} width={56} domain={["auto", "auto"]} />
        <Tooltip {...tooltipStyle} formatter={growth} />
        {legend}
        <Line name="Strategy" dataKey="strategy" stroke={SERIES[0]} strokeWidth={2} dot={false} />
        <Line
          name={benchmark}
          dataKey="benchmark"
          stroke={BENCH}
          strokeWidth={2}
          strokeDasharray="4 3"
          dot={false}
        />
      </LineChart>
    </ChartFrame>
  );
}

/** Drawdown of the strategy (from its initial equity's peak) and of NIFTY 500 TRI. */
export function DrawdownChart({
  rows,
  benchmark,
}: Readonly<{ rows: EquityRow[]; benchmark: string }>) {
  return (
    <ChartFrame title={`Drawdown vs ${benchmark}`}>
      <AreaChart data={rows}>
        {grid}
        <XAxis dataKey="date" {...axis} tickFormatter={monthYear} minTickGap={48} />
        <YAxis {...axis} tickFormatter={pct} width={56} />
        <Tooltip {...tooltipStyle} formatter={pct} />
        {legend}
        <Area
          name="Strategy"
          dataKey="drawdown"
          stroke={SERIES[0]}
          fill={SERIES[0]}
          fillOpacity={0.15}
          strokeWidth={2}
        />
        <Area
          name={benchmark}
          dataKey="benchmarkDrawdown"
          stroke={BENCH}
          fill="none"
          strokeDasharray="4 3"
          strokeWidth={2}
        />
      </AreaChart>
    </ChartFrame>
  );
}

/** Calendar-year returns of the strategy beside NIFTY 500 TRI's. */
export function YearlyChart({
  yearly,
  benchmark,
}: Readonly<{ yearly: Report["yearly"]; benchmark: string }>) {
  return (
    <ChartFrame title="Yearly returns">
      <BarChart data={yearly} barGap={2}>
        {grid}
        <XAxis dataKey="year" {...axis} />
        <YAxis {...axis} tickFormatter={pct} width={56} />
        <ReferenceLine y={0} stroke="var(--color-foreground-muted)" />
        <Tooltip {...tooltipStyle} formatter={pct} cursor={{ fill: "var(--color-hover)" }} />
        {legend}
        <Bar name="Strategy" dataKey="return" fill={SERIES[0]} radius={[4, 4, 0, 0]} />
        <Bar name={benchmark} dataKey="benchmark" fill={BENCH} radius={[4, 4, 0, 0]} />
      </BarChart>
    </ChartFrame>
  );
}

/** Cumulative R in exit order. */
export function RCurveChart({ rows }: Readonly<{ rows: { date: string; r: number | null }[] }>) {
  return (
    <ChartFrame title="Cumulative R" note="by exit date">
      <LineChart data={rows}>
        {grid}
        <XAxis dataKey="date" {...axis} tickFormatter={monthYear} minTickGap={48} />
        <YAxis {...axis} tickFormatter={two} width={48} domain={["auto", "auto"]} />
        <ReferenceLine y={0} stroke="var(--color-foreground-muted)" />
        <Tooltip {...tooltipStyle} formatter={two} />
        <Line name="R" dataKey="r" stroke={SERIES[0]} strokeWidth={2} dot={false} />
      </LineChart>
    </ChartFrame>
  );
}

/** Each trade's best excursion (MFE, R) against the R it realised. */
export function MfeScatter({ points }: Readonly<{ points: ScatterPoint[] }>) {
  return (
    <ChartFrame title="MFE vs realised R" note={`${points.length} trades`}>
      <ScatterChart>
        {grid}
        <XAxis type="number" dataKey="mfe" name="MFE (R)" {...axis} tickFormatter={two} />
        <YAxis type="number" dataKey="r" name="R" {...axis} tickFormatter={two} width={48} />
        <ReferenceLine y={0} stroke="var(--color-foreground-muted)" />
        <Tooltip
          {...tooltipStyle}
          formatter={two}
          labelFormatter={() => ""}
          cursor={{ strokeDasharray: "3 3" }}
        />
        <Scatter data={points} fill={SERIES[0]} fillOpacity={0.6} />
      </ScatterChart>
    </ChartFrame>
  );
}

/** Sum of R per bucket of a breakdown (exit reason, hold time, regime at entry). */
export function GroupChart({
  title,
  note,
  rows,
}: Readonly<{ title: string; note?: string; rows: GroupRow[] }>) {
  return (
    <ChartFrame title={title} note={note}>
      <BarChart data={rows}>
        {grid}
        <XAxis dataKey="key" {...axis} interval={0} />
        <YAxis {...axis} tickFormatter={two} width={48} />
        <ReferenceLine y={0} stroke="var(--color-foreground-muted)" />
        <Tooltip {...tooltipStyle} formatter={two} cursor={{ fill: "var(--color-hover)" }} />
        <Bar name="Sum R" dataKey="sumR" fill={SERIES[0]} radius={[4, 4, 0, 0]} />
      </BarChart>
    </ChartFrame>
  );
}

/** The compared runs' equity, each rebased to its own first session. */
export function CompareEquityChart({
  rows,
  names,
}: Readonly<{ rows: Record<string, number | string>[]; names: (string | null)[] }>) {
  return (
    <ChartFrame title="Equity" note="growth from each run's first session">
      <LineChart data={rows}>
        {grid}
        <XAxis dataKey="date" {...axis} tickFormatter={monthYear} minTickGap={48} />
        <YAxis {...axis} tickFormatter={growth} width={56} domain={["auto", "auto"]} />
        <Tooltip {...tooltipStyle} formatter={growth} />
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
            />
          ),
        )}
      </LineChart>
    </ChartFrame>
  );
}

/** The compared runs' calendar-year returns side by side. */
export function CompareYearlyChart({
  rows,
  names,
}: Readonly<{ rows: Record<string, number | null>[]; names: (string | null)[] }>) {
  return (
    <ChartFrame title="Yearly returns">
      <BarChart data={rows} barGap={2}>
        {grid}
        <XAxis dataKey="year" {...axis} />
        <YAxis {...axis} tickFormatter={pct} width={56} />
        <ReferenceLine y={0} stroke="var(--color-foreground-muted)" />
        <Tooltip {...tooltipStyle} formatter={pct} cursor={{ fill: "var(--color-hover)" }} />
        {legend}
        {names.map((name, index) =>
          name === null ? null : (
            <Bar
              key={seriesKey(index)}
              name={name}
              dataKey={seriesKey(index)}
              fill={SERIES[index]}
              radius={[4, 4, 0, 0]}
            />
          ),
        )}
      </BarChart>
    </ChartFrame>
  );
}
