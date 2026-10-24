import { Badge } from "@/components/atoms/badge";
import { Decimal } from "@/components/atoms/number";
import { TableCell, TableRow } from "@/components/atoms/table";
import { DataTable } from "@/components/molecules/data-table";
import { noRows, QuerySection } from "@/components/molecules/query-section";
import { PLACEHOLDER } from "@/lib/format";
import { useHatSignals, type HatSignals } from "@/lib/queries/hat";
import { formatIstDateTime } from "@/lib/time";

type Signal = HatSignals[number];

const COLUMNS = [
  { label: "Triggered" },
  { label: "Bot" },
  { label: "Symbol" },
  { label: "Trigger", numeric: true },
  { label: "Stop", numeric: true },
  { label: "Qty", numeric: true },
  { label: "Mode" },
  { label: "State" },
  { label: "Confidence" },
  { label: "Basis" },
];

const CONFIDENCE_VARIANT = { high: "profit", medium: "warning", low: "muted" } as const;

/** Why the signal got its label: the reason code, and the backtest trades of its bucket. */
function basisText(signal: Signal): string {
  const { reason, trades } = signal.confidence_basis;
  const why = typeof reason === "string" ? reason : PLACEHOLDER;
  return typeof trades === "number" ? `${why} · ${trades} trades` : why;
}

function ConfidenceBadge({ signal }: Readonly<{ signal: Signal }>) {
  const label = signal.confidence;
  if (label == null) return <span className="text-foreground-muted">{PLACEHOLDER}</span>;
  const variant =
    label in CONFIDENCE_VARIANT
      ? CONFIDENCE_VARIANT[label as keyof typeof CONFIDENCE_VARIANT]
      : "outline";
  return <Badge variant={variant}>{label}</Badge>;
}

function SignalRow({ signal: s }: Readonly<{ signal: Signal }>) {
  return (
    <TableRow>
      <TableCell>{formatIstDateTime(s.triggered_at)}</TableCell>
      <TableCell>{s.bot}</TableCell>
      <TableCell>{s.symbol}</TableCell>
      <TableCell numeric>
        <Decimal value={s.trigger_price} />
      </TableCell>
      <TableCell numeric>
        <Decimal value={s.stop_price} />
      </TableCell>
      <TableCell numeric>{s.qty}</TableCell>
      <TableCell>{s.mode}</TableCell>
      <TableCell>{s.state}</TableCell>
      <TableCell>
        <ConfidenceBadge signal={s} />
      </TableCell>
      <TableCell className="text-foreground-muted">{basisText(s)}</TableCell>
    </TableRow>
  );
}

/** Recent signals with their mode, state and confidence label. */
export function SignalsPanel() {
  const signals = useHatSignals();
  return (
    <QuerySection title="Signals" query={signals} isEmpty={noRows} empty="No signals.">
      {(data) => (
        <DataTable columns={COLUMNS}>
          {data.map((s) => (
            <SignalRow key={s.id} signal={s} />
          ))}
        </DataTable>
      )}
    </QuerySection>
  );
}
