import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui";
import dynamic from "@/lib/dynamic";
import type { SignalDetail } from "@/lib/queries/strategies";

const DynamicSignalChart = dynamic(() =>
  import("recharts").then((mod) => {
    const {
      Bar,
      CartesianGrid,
      ComposedChart,
      Legend,
      Line,
      ResponsiveContainer,
      Tooltip,
      XAxis,
      YAxis,
    } = mod;
    return function SignalChart({ data }: Readonly<{ data: SignalDetail["chart"] }>) {
      if (data.length === 0) {
        return (
          <div className="flex h-full items-center justify-center text-sm text-foreground-secondary">
            Select a signal to load chart data.
          </div>
        );
      }
      return (
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={data} margin={{ top: 12, right: 24, left: 0, bottom: 12 }}>
            <CartesianGrid stroke="var(--color-border)" strokeDasharray="3 3" />
            <XAxis dataKey="date" hide />
            <YAxis
              yAxisId="price"
              tick={{ fontSize: 11, fill: "var(--color-foreground-secondary)" }}
            />
            <YAxis
              yAxisId="volume"
              orientation="right"
              tick={{ fontSize: 11, fill: "var(--color-foreground-muted)" }}
            />
            <Tooltip
              contentStyle={{
                backgroundColor: "var(--color-elevated)",
                border: "1px solid var(--color-border)",
                borderRadius: "var(--radius-md)",
                fontSize: "12px",
              }}
            />
            <Legend />
            <Bar yAxisId="volume" dataKey="volume" fill="rgba(89, 141, 250, 0.18)" name="Volume" />
            <Line
              yAxisId="price"
              type="monotone"
              dataKey="close"
              stroke="#f97316"
              dot={false}
              strokeWidth={2.2}
              name="Close"
            />
            <Line
              yAxisId="price"
              type="monotone"
              dataKey="ema_21"
              stroke="#fafafa"
              dot={false}
              strokeDasharray="4 4"
              name="EMA 21"
            />
            <Line
              yAxisId="price"
              type="monotone"
              dataKey="ma_50"
              stroke="#3b82f6"
              dot={false}
              name="MA 50"
            />
            <Line
              yAxisId="price"
              type="monotone"
              dataKey="ma_150"
              stroke="#f59e0b"
              dot={false}
              name="MA 150"
            />
            <Line
              yAxisId="price"
              type="monotone"
              dataKey="ma_200"
              stroke="#ef4444"
              dot={false}
              name="MA 200"
            />
          </ComposedChart>
        </ResponsiveContainer>
      );
    };
  }),
);

const NO_POINTS: SignalDetail["chart"] = [];

/** Price, moving averages and volume for the selected signal; recharts loads on demand. */
export function SignalChartCard({ data }: Readonly<{ data: SignalDetail["chart"] | undefined }>) {
  return (
    <Card variant="void" className="m-4 flex min-h-0 flex-1 flex-col">
      <CardHeader>
        <CardTitle className="text-sm">Chart</CardTitle>
      </CardHeader>
      <CardContent className="min-h-0 flex-1">
        <div className="h-full min-h-[360px]">
          <DynamicSignalChart data={data ?? NO_POINTS} />
        </div>
      </CardContent>
    </Card>
  );
}
