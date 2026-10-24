import { Badge } from "@/components/atoms/badge";
import { Decimal } from "@/components/atoms/number";
import { TableCell, TableRow } from "@/components/atoms/table";
import { DataTable } from "@/components/molecules/data-table";
import { noRows, QuerySection } from "@/components/molecules/query-section";
import { useHatPositions, type HatPositions } from "@/lib/queries/hat";
import { formatIstDateTime } from "@/lib/time";

const COLUMNS = [
  { label: "Bot" },
  { label: "Symbol" },
  { label: "Qty", numeric: true },
  { label: "Entry", numeric: true },
  { label: "Stop", numeric: true },
  { label: "Opened" },
];

function PositionRow({ position: p }: Readonly<{ position: HatPositions[number] }>) {
  return (
    <TableRow>
      <TableCell>{p.bot}</TableCell>
      <TableCell>
        {p.symbol}
        {p.is_virtual ? (
          <Badge variant="muted" className="ml-2">
            virtual
          </Badge>
        ) : null}
      </TableCell>
      <TableCell numeric>{p.qty_open}</TableCell>
      <TableCell numeric>
        <Decimal value={p.avg_entry} />
      </TableCell>
      <TableCell numeric>
        <Decimal value={p.current_stop} />
      </TableCell>
      <TableCell>{formatIstDateTime(p.opened_at)}</TableCell>
    </TableRow>
  );
}

/** Every open position, virtual (shadow) ones tagged. */
export function PositionsPanel() {
  const positions = useHatPositions();
  return (
    <QuerySection
      title="Open positions"
      query={positions}
      isEmpty={noRows}
      empty="No open positions."
    >
      {(data) => (
        <DataTable columns={COLUMNS}>
          {data.map((p) => (
            <PositionRow key={p.id} position={p} />
          ))}
        </DataTable>
      )}
    </QuerySection>
  );
}
