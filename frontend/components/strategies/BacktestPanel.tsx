import { Button, Card, CardContent, CardHeader, CardTitle, Input } from "@/components/ui";
import type { BacktestRun, StrategyBacktestResult } from "@/lib/queries/strategies";
import { BacktestHistoryCard } from "./BacktestHistoryCard";
import { Metric } from "./Metric";
import { backtestHeadline } from "./strategy-view";

export interface BacktestFormValues {
  startDate: string;
  endDate: string;
  capital: string;
}

interface BacktestPanelProps {
  universe: string;
  form: BacktestFormValues;
  result: StrategyBacktestResult | null;
  runs: ReadonlyArray<BacktestRun>;
  /** A strategy action is in flight; a new run waits for it. */
  busy: boolean;
  onEdit: (change: Partial<BacktestFormValues>) => void;
  onRun: () => void;
}

/** The VCP backtest form, the latest result and the run history. */
export function BacktestPanel({
  universe,
  form,
  result,
  runs,
  busy,
  onEdit,
  onRun,
}: Readonly<BacktestPanelProps>) {
  const headline = backtestHeadline(result);
  return (
    <>
      <Card className="mb-4">
        <CardHeader>
          <CardTitle className="text-sm">Run VCP Backtest · {universe}</CardTitle>
        </CardHeader>
        <CardContent className="grid grid-cols-4 gap-3">
          <Input
            label="Start Date"
            type="date"
            value={form.startDate}
            onChange={(e) => onEdit({ startDate: e.target.value })}
          />
          <Input
            label="End Date"
            type="date"
            value={form.endDate}
            onChange={(e) => onEdit({ endDate: e.target.value })}
          />
          <Input
            label="Capital (₹)"
            type="number"
            value={form.capital}
            onChange={(e) => onEdit({ capital: e.target.value })}
          />
          <div className="flex items-end">
            <Button className="w-full" onClick={onRun} disabled={busy}>
              {busy ? "Running..." : "Run Backtest"}
            </Button>
          </div>
        </CardContent>
      </Card>

      {headline ? (
        <Card className="mb-4">
          <CardHeader>
            <CardTitle className="text-sm">Latest Backtest Result</CardTitle>
          </CardHeader>
          <CardContent className="grid grid-cols-3 gap-3 text-sm">
            <Metric label="Return" value={headline.returnPct} />
            <Metric label="Sharpe" value={headline.sharpe} />
            <Metric label="Trades" value={headline.trades} />
          </CardContent>
        </Card>
      ) : null}

      <BacktestHistoryCard runs={runs} />
    </>
  );
}
