import { useState } from "react";
import { Badge } from "@/components/atoms/badge";
import { Decimal } from "@/components/atoms/number";
import { TableCell, TableRow } from "@/components/atoms/table";
import { DataTable, RowOpener } from "@/components/molecules/data-table";
import { DetailList, Drawer } from "@/components/molecules/drawer";
import { LabelledValue } from "@/components/molecules/labelled-value";
import { noRows, QuerySection, rowCount } from "@/components/molecules/query-section";
import { useHatPositions, type HatPositions } from "@/lib/queries/hat";
import { formatIstDateTime } from "@/lib/time";

type Position = HatPositions[number];

const COLUMNS = [
  { label: "Symbol" },
  { label: "Bot" },
  { label: "Qty", numeric: true },
  { label: "Entry", numeric: true },
  { label: "Stop", numeric: true },
  { label: "Opened (IST)" },
];

function PositionDetails({
  position: p,
  onClose,
}: Readonly<{ position: Position; onClose: () => void }>) {
  return (
    <Drawer
      title={p.symbol}
      subtitle={`${p.bot} · ${p.is_virtual ? "virtual (shadow)" : "real"}`}
      onClose={onClose}
    >
      <DetailList>
        <LabelledValue label="Quantity open">
          <span className="font-mono tabular-nums">{p.qty_open}</span>
        </LabelledValue>
        <LabelledValue label="State">{p.state}</LabelledValue>
        <LabelledValue label="Average entry">
          <span className="font-mono tabular-nums">
            <Decimal value={p.avg_entry} />
          </span>
        </LabelledValue>
        <LabelledValue label="Initial stop">
          <span className="font-mono tabular-nums">
            <Decimal value={p.initial_stop} />
          </span>
        </LabelledValue>
        <LabelledValue label="Current stop">
          <span className="font-mono tabular-nums">
            <Decimal value={p.current_stop} />
          </span>
        </LabelledValue>
        <LabelledValue label="Opened">{formatIstDateTime(p.opened_at)} IST</LabelledValue>
      </DetailList>
    </Drawer>
  );
}

function PositionRow({
  position: p,
  onOpen,
}: Readonly<{ position: Position; onOpen: () => void }>) {
  return (
    <TableRow>
      <TableCell>
        <RowOpener label={`Details of position ${p.symbol}`} onOpen={onOpen}>
          {p.symbol}
        </RowOpener>
        {p.is_virtual ? (
          <Badge tone="mode" className="ml-3 text-xs">
            virtual
          </Badge>
        ) : null}
      </TableCell>
      <TableCell className="text-foreground-secondary">{p.bot}</TableCell>
      <TableCell numeric>{p.qty_open}</TableCell>
      <TableCell numeric>
        <Decimal value={p.avg_entry} />
      </TableCell>
      <TableCell numeric>
        <Decimal value={p.current_stop} />
      </TableCell>
      <TableCell className="tabular-nums text-foreground-secondary">
        {formatIstDateTime(p.opened_at)}
      </TableCell>
    </TableRow>
  );
}

/** Every open position, virtual (shadow) ones tagged; a row opens its details. */
export function PositionsPanel({ className }: Readonly<{ className?: string }>) {
  const positions = useHatPositions();
  const [openId, setOpenId] = useState<string | null>(null);
  const open = positions.data?.find((p) => p.id === openId);
  return (
    <QuerySection
      title="Open positions"
      query={positions}
      isEmpty={noRows}
      empty="No open positions."
      meta={rowCount}
      className={className}
    >
      {(data) => (
        <>
          <DataTable columns={COLUMNS}>
            {data.map((p) => (
              <PositionRow key={p.id} position={p} onOpen={() => setOpenId(p.id)} />
            ))}
          </DataTable>
          {open ? <PositionDetails position={open} onClose={() => setOpenId(null)} /> : null}
        </>
      )}
    </QuerySection>
  );
}
