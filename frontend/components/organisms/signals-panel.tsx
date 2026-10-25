import { useState } from "react";
import { Badge, type Tone } from "@/components/atoms/badge";
import { Decimal } from "@/components/atoms/number";
import { TableCell, TableRow } from "@/components/atoms/table";
import { DataTable, RowOpener } from "@/components/molecules/data-table";
import { DetailList, Drawer } from "@/components/molecules/drawer";
import { LabelledValue } from "@/components/molecules/labelled-value";
import { noRows, QuerySection } from "@/components/molecules/query-section";
import { PLACEHOLDER } from "@/lib/format";
import { useHatSignals, useSharedStale, type HatSignals } from "@/lib/queries/hat";
import { formatIstDate, formatIstDateTime } from "@/lib/time";

type Signal = HatSignals[number];

const COLUMNS = [
  { label: "Triggered (IST)" },
  { label: "Symbol" },
  { label: "Bot" },
  { label: "Trigger", numeric: true },
  { label: "Stop", numeric: true },
  { label: "Qty", numeric: true },
  { label: "Mode" },
  { label: "State" },
  { label: "Confidence" },
];

const CONFIDENCE_TONE: Record<NonNullable<Signal["confidence"]>, Tone> = {
  high: "profit",
  medium: "warning",
  low: "muted",
};

/** Why the signal got its label: the reason code, and the backtest trades of its bucket. */
function basisText(signal: Signal): string {
  const { reason, trades } = signal.confidence_basis;
  const why = reason ?? PLACEHOLDER;
  return trades === null ? why : `${why} · ${trades} trades`;
}

function ConfidenceBadge({ signal }: Readonly<{ signal: Signal }>) {
  const label = signal.confidence;
  if (label == null) return <span className="text-foreground-muted">{PLACEHOLDER}</span>;
  return <Badge tone={CONFIDENCE_TONE[label]}>{label}</Badge>;
}

function Money({ value }: Readonly<{ value: string }>) {
  return (
    <span className="font-mono tabular-nums">
      <Decimal value={value} />
    </span>
  );
}

function SignalDetails({ signal: s, onClose }: Readonly<{ signal: Signal; onClose: () => void }>) {
  return (
    <Drawer title={s.symbol} subtitle={`${s.bot} · ${s.playbook}`} onClose={onClose}>
      <DetailList>
        <LabelledValue label="Triggered">{formatIstDateTime(s.triggered_at)} IST</LabelledValue>
        <LabelledValue label="Trade date">{formatIstDate(s.trade_date)}</LabelledValue>
        <LabelledValue label="Trigger price">
          <Money value={s.trigger_price} />
        </LabelledValue>
        <LabelledValue label="Stop price">
          <Money value={s.stop_price} />
        </LabelledValue>
        <LabelledValue label="Quantity">
          <span className="font-mono tabular-nums">{s.qty}</span>
        </LabelledValue>
        <LabelledValue label="Risk amount">
          <Money value={s.risk_amount} />
        </LabelledValue>
        <LabelledValue label="Mode">
          <span className="text-mode">{s.mode}</span>
        </LabelledValue>
        <LabelledValue label="State">{s.state}</LabelledValue>
        <LabelledValue label="Confidence">
          <ConfidenceBadge signal={s} />
        </LabelledValue>
        <LabelledValue label="Basis" className="col-span-2">
          {basisText(s)}
        </LabelledValue>
      </DetailList>
    </Drawer>
  );
}

function SignalRow({ signal: s, onOpen }: Readonly<{ signal: Signal; onOpen: () => void }>) {
  return (
    <TableRow>
      <TableCell className="tabular-nums text-foreground-secondary">
        {formatIstDateTime(s.triggered_at)}
      </TableCell>
      <TableCell>
        <RowOpener label={`Details of signal ${s.symbol}`} onOpen={onOpen}>
          {s.symbol}
        </RowOpener>
      </TableCell>
      <TableCell className="text-foreground-secondary">{s.bot}</TableCell>
      <TableCell numeric>
        <Decimal value={s.trigger_price} />
      </TableCell>
      <TableCell numeric>
        <Decimal value={s.stop_price} />
      </TableCell>
      <TableCell numeric>{s.qty}</TableCell>
      <TableCell className="text-mode">{s.mode}</TableCell>
      <TableCell>{s.state}</TableCell>
      <TableCell>
        <ConfidenceBadge signal={s} />
      </TableCell>
    </TableRow>
  );
}

/** Recent signals with their mode, state and confidence; a row opens its basis and risk. */
export function SignalsPanel({ className }: Readonly<{ className?: string }>) {
  const signals = useHatSignals();
  const sharedStale = useSharedStale() !== null;
  const [openId, setOpenId] = useState<string | null>(null);
  const open = signals.data?.find((s) => s.id === openId);
  return (
    <QuerySection
      sharedStale={sharedStale}
      title="Signals"
      query={signals}
      isEmpty={noRows}
      empty="No signals."
      meta={(data) => `latest ${data.length}`}
      className={className}
    >
      {(data) => (
        <>
          <DataTable columns={COLUMNS}>
            {data.map((s) => (
              <SignalRow key={s.id} signal={s} onOpen={() => setOpenId(s.id)} />
            ))}
          </DataTable>
          {open ? <SignalDetails signal={open} onClose={() => setOpenId(null)} /> : null}
        </>
      )}
    </QuerySection>
  );
}
