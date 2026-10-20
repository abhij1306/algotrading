import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui";
import { formatInr, PLACEHOLDER } from "@/lib/format";
import type { ScanRow } from "@/lib/queries/strategies";
import { Metric } from "./Metric";

/** The selected signal's base, levels and planned position. */
export function SignalDetailCard({ signal }: Readonly<{ signal: ScanRow | null }>) {
  return (
    <Card variant="void" className="m-4 mb-0">
      <CardHeader>
        <CardTitle className="text-sm">
          {signal ? `${signal.symbol} · ${signal.grade}-Grade` : "Signal Detail"}
        </CardTitle>
      </CardHeader>
      <CardContent className="grid grid-cols-2 gap-3 text-sm">
        <Metric label="RS Rating" value={signal ? String(signal.rs_rating) : PLACEHOLDER} />
        <Metric
          label="Contractions"
          value={signal ? signal.contraction_depths.map((d) => `${d}%`).join(" → ") : PLACEHOLDER}
        />
        <Metric label="Pivot High" value={formatInr(signal?.pivot_high)} />
        <Metric label="Stop Level" value={formatInr(signal?.stop_level)} />
        <Metric label="Position Value" value={formatInr(signal?.planned_position_value)} />
        <Metric label="2R Target" value={formatInr(signal?.two_r_target)} />
      </CardContent>
    </Card>
  );
}
