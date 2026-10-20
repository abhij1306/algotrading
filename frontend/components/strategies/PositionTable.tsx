import {
  Button,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui";
import { formatInr, formatNumber, formatSignedPercent } from "@/lib/format";
import type { PositionRow } from "@/lib/queries/strategies";
import { priceStatusLabel } from "./strategy-view";

interface PositionTableProps {
  rows: ReadonlyArray<PositionRow>;
  onClose: (positionId: number) => void;
  /** A strategy action is in flight; closing twice would overwrite the recorded P&L. */
  busy: boolean;
}

/** Open strategy positions, each with a Close Now action. */
export function PositionTable({ rows, onClose, busy }: Readonly<PositionTableProps>) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Symbol</TableHead>
          <TableHead>Entry</TableHead>
          <TableHead>Stop</TableHead>
          <TableHead>LTP</TableHead>
          <TableHead>P&L</TableHead>
          <TableHead>R</TableHead>
          <TableHead>Status</TableHead>
          <TableHead>Action</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((row) => (
          <TableRow key={row.id}>
            <TableCell>{row.symbol}</TableCell>
            <TableCell>{formatInr(row.entry_price)}</TableCell>
            <TableCell>{formatInr(row.stop_price)}</TableCell>
            <TableCell>{formatInr(row.ltp)}</TableCell>
            <TableCell>
              <div>{formatInr(row.unrealized_pnl)}</div>
              <div className="text-xs text-foreground-secondary">
                {formatSignedPercent(row.unrealized_pnl_pct)}
              </div>
            </TableCell>
            <TableCell>{formatNumber(row.r_multiple)}</TableCell>
            <TableCell>
              <div>{row.status}</div>
              <div className="text-xs text-foreground-secondary">
                {priceStatusLabel(row.price_status)}
              </div>
            </TableCell>
            <TableCell>
              <Button size="sm" variant="secondary" disabled={busy} onClick={() => onClose(row.id)}>
                Close Now
              </Button>
            </TableCell>
          </TableRow>
        ))}
        {rows.length === 0 ? (
          <TableRow>
            <TableCell colSpan={8} className="py-6 text-center text-sm text-foreground-secondary">
              No open positions.
            </TableCell>
          </TableRow>
        ) : null}
      </TableBody>
    </Table>
  );
}
