import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui";
import { formatNumber, formatSignedPercent } from "@/lib/format";
import type { BacktestRun } from "@/lib/queries/strategies";

/** Earlier VCP backtest runs for the universe, newest first as the backend sends them. */
export function BacktestHistoryCard({ runs }: Readonly<{ runs: ReadonlyArray<BacktestRun> }>) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-sm">History</CardTitle>
      </CardHeader>
      <CardContent className="p-0">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Run</TableHead>
              <TableHead>Universe</TableHead>
              <TableHead>Period</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Return</TableHead>
              <TableHead>Sharpe</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {runs.map((run) => (
              <TableRow key={run.run_id}>
                <TableCell>{run.name || run.run_id}</TableCell>
                <TableCell>{run.universe || "--"}</TableCell>
                <TableCell>
                  {run.start_date} → {run.end_date}
                </TableCell>
                <TableCell>{run.status}</TableCell>
                <TableCell>{formatSignedPercent(run.metrics?.total_return_pct)}</TableCell>
                <TableCell>{formatNumber(run.metrics?.sharpe_ratio)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}
