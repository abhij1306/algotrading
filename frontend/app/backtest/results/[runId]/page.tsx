import { useMemo } from "react";
import { useNavigate, useParams } from "react-router";
import { Card, CardContent, PageContainer } from "@/components/ui";
import { DrawdownCard, EquityCurveCard } from "@/components/backtest/ResultCharts";
import { ResultHeader } from "@/components/backtest/ResultHeader";
import { ResultMetricsStrip } from "@/components/backtest/ResultMetricsStrip";
import { RunSummaryBar } from "@/components/backtest/RunSummaryBar";
import { TradeLogPanel } from "@/components/backtest/TradeLogPanel";
import { buildEquityRows } from "@/components/backtest/result-view";
import { useBacktestResult } from "@/lib/queries/backtest";

const NO_POINTS: never[] = [];

export default function BacktestResultPage() {
  const { runId } = useParams<{ runId: string }>();
  const navigate = useNavigate();
  const resultQuery = useBacktestResult(runId);
  const payload = resultQuery.data;
  const result = payload?.result;
  const error = resultQuery.error?.message ?? null;

  const equityCurve = result?.equity_curve;
  const benchmarkCurve = result?.benchmark_curve;
  const equityRows = useMemo(
    () => buildEquityRows(equityCurve ?? NO_POINTS, benchmarkCurve ?? NO_POINTS),
    [equityCurve, benchmarkCurve],
  );

  return (
    <PageContainer fullWidth>
      <div className="flex h-full w-full flex-col overflow-hidden bg-background">
        <ResultHeader runId={runId} onBack={() => void navigate("/backtest")} />

        {error && <div className="bg-loss-bg p-4 text-sm text-loss">{error}</div>}

        {payload?.status === "running" && (
          <div className="p-4">
            <Card>
              <CardContent className="p-4 text-sm text-foreground-secondary">
                Backtest executing...
              </CardContent>
            </Card>
          </div>
        )}

        {payload?.status === "failed" && (
          <div className="p-4">
            <Card variant="outline" className="border-loss bg-loss-bg">
              <CardContent className="p-4 text-sm text-loss">
                {payload.error || "Backtest failed"}
              </CardContent>
            </Card>
          </div>
        )}

        {payload?.status === "completed" && (
          <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
            <ResultMetricsStrip metrics={result?.metrics} />
            <RunSummaryBar job={payload} />

            <div className="grid min-h-0 flex-1 grid-cols-[1fr_360px] overflow-hidden">
              <div className="flex h-full min-h-0 flex-col overflow-hidden border-r border-border bg-background">
                <EquityCurveCard rows={equityRows} />
                <DrawdownCard points={result?.drawdown_curve ?? NO_POINTS} />
              </div>

              <TradeLogPanel
                trades={result?.trade_log ?? NO_POINTS}
                winRate={result?.metrics.win_rate_pct ?? 0}
              />
            </div>
          </div>
        )}
      </div>
    </PageContainer>
  );
}
