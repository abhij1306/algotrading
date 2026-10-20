import {
  Badge,
  Button,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui";
import { formatInr, formatSignedPercent } from "@/lib/format";
import type { ScanRow } from "@/lib/queries/strategies";
import { gradeVariant } from "./strategy-view";

interface SignalTableProps {
  rows: ReadonlyArray<ScanRow>;
  selectedSymbol: string | null;
  onSelect: (symbol: string) => void;
  onQueueToggle: (row: ScanRow) => void;
  /** A strategy action is in flight; queue toggles wait for it. */
  busy: boolean;
}

/** VCP scan rows; a row selects its signal, and a breakout can be queued for entry. */
export function SignalTable({
  rows,
  selectedSymbol,
  onSelect,
  onQueueToggle,
  busy,
}: Readonly<SignalTableProps>) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Grade</TableHead>
          <TableHead>Symbol</TableHead>
          <TableHead>RS</TableHead>
          <TableHead>Contractions</TableHead>
          <TableHead>Vol Dry-up</TableHead>
          <TableHead>Pivot</TableHead>
          <TableHead>Stop %</TableHead>
          <TableHead>Action</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((row) => (
          <TableRow
            key={row.id}
            className={selectedSymbol === row.symbol ? "bg-background-tertiary" : ""}
            onClick={() => onSelect(row.symbol)}
            tabIndex={0}
            role="button"
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onSelect(row.symbol);
              }
            }}
          >
            <TableCell>
              <Badge variant={gradeVariant(row.grade)}>{row.grade}</Badge>
            </TableCell>
            <TableCell>
              <div className="font-medium">{row.symbol}</div>
              <div className="text-xs text-foreground-secondary">{row.company_name}</div>
            </TableCell>
            <TableCell>{row.rs_rating}</TableCell>
            <TableCell>{row.contraction_depths.join(" → ")}%</TableCell>
            <TableCell>{formatSignedPercent(-row.volume_dry_up_pct + 100)}</TableCell>
            <TableCell>{formatInr(row.pivot_high)}</TableCell>
            <TableCell>{formatSignedPercent(-row.stop_pct)}</TableCell>
            <TableCell>
              {row.is_breakout ? (
                <Button
                  size="sm"
                  variant={row.signal_status === "PENDING_ENTRY" ? "secondary" : "primary"}
                  disabled={busy}
                  onClick={(e) => {
                    e.stopPropagation();
                    onQueueToggle(row);
                  }}
                >
                  {row.signal_status === "PENDING_ENTRY" ? "Queued" : "Queue"}
                </Button>
              ) : (
                <Badge variant="neutral">Watch</Badge>
              )}
            </TableCell>
          </TableRow>
        ))}
        {rows.length === 0 ? (
          <TableRow>
            <TableCell colSpan={8} className="py-6 text-center text-sm text-foreground-secondary">
              No signals in this view.
            </TableCell>
          </TableRow>
        ) : null}
      </TableBody>
    </Table>
  );
}
