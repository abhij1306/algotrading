import {
  Badge,
  Card,
  CardHeader,
  CardTitle,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui";
import { formatNumber, formatPercent, formatSignedPercent, roundsNegative } from "@/lib/format";
import type { BacktestJob } from "@/lib/queries/backtest";

type Trade = NonNullable<BacktestJob["result"]>["trade_log"][number];

/** The most trades the log renders. */
const MAX_TRADES_SHOWN = 200;

interface TradeLogPanelProps {
  trades: ReadonlyArray<Trade>;
  winRate: number;
}

/** The run's trades by date, with the count and win rate above them. */
export function TradeLogPanel({ trades, winRate }: Readonly<TradeLogPanelProps>) {
  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden bg-background-secondary">
      <Card
        variant="void"
        className="shrink-0 rounded-none border-0 border-b border-border bg-surface"
      >
        <CardHeader className="px-4 py-3">
          <div className="flex items-center justify-between">
            <CardTitle className="text-foreground">Trade Log v2</CardTitle>
            <Badge variant="outline">{trades.length} trades</Badge>
          </div>
          <p className="text-xs text-foreground-muted">
            {formatPercent(winRate, 1)} win rate · by date
          </p>
        </CardHeader>
      </Card>

      <div className="min-h-0 flex-1 overflow-y-auto custom-scrollbar">
        {trades.length === 0 ? (
          <div className="p-4 text-sm text-foreground-secondary">
            No trades generated for this configuration.
          </div>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Symbol</TableHead>
                <TableHead>Dates</TableHead>
                <TableHead numeric>Prices</TableHead>
                <TableHead numeric>Return</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {trades.slice(0, MAX_TRADES_SHOWN).map((t, i) => (
                <TableRow key={`${t.symbol}-${t.entry_date}-${i}`}>
                  <TableCell className="font-semibold text-foreground">{t.symbol}</TableCell>
                  <TableCell>
                    <div className="mono-num text-xs text-foreground-muted">↑ {t.entry_date}</div>
                    <div className="mono-num text-xs text-foreground-muted">↓ {t.exit_date}</div>
                  </TableCell>
                  <TableCell numeric>
                    <div className="mono-num text-foreground">{formatNumber(t.entry_price)}</div>
                    <div className="mono-num text-foreground-muted">
                      {formatNumber(t.exit_price)}
                    </div>
                  </TableCell>
                  <TableCell numeric variant={roundsNegative(t.return_pct) ? "loss" : "profit"}>
                    {formatSignedPercent(t.return_pct)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </div>
    </div>
  );
}
