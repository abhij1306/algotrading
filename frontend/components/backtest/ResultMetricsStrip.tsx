import { formatCompactInr, formatNumber, formatPercent, PLACEHOLDER } from "@/lib/format";
import type { BacktestJob } from "@/lib/queries/backtest";
import { returnTone, sharpeTone, winRateTone } from "./result-view";

type Metrics = NonNullable<BacktestJob["result"]>["metrics"];

function MetricCell({
  label,
  value,
  tone,
  last = false,
}: Readonly<{ label: string; value: string; tone: string; last?: boolean }>) {
  return (
    <div className={last ? "px-4 py-2" : "border-r border-border px-4 py-2"}>
      <div className="text-xxs uppercase tracking-wider text-foreground-muted">{label}</div>
      <div className={`mono-num text-2xl font-semibold ${tone}`}>{value}</div>
    </div>
  );
}

/** The headline numbers of a finished run; every cell shows the placeholder without metrics. */
export function ResultMetricsStrip({ metrics }: Readonly<{ metrics: Metrics | undefined }>) {
  return (
    <div className="grid h-16 shrink-0 grid-cols-2 border-b border-border bg-surface md:grid-cols-3 xl:grid-cols-6">
      <MetricCell
        label="Total Return"
        value={formatPercent(metrics?.total_return_pct, 1)}
        tone={returnTone(metrics?.total_return_pct ?? 0)}
      />
      <MetricCell
        label="Final Equity"
        value={formatCompactInr(metrics?.final_equity)}
        tone="text-foreground"
      />
      <MetricCell
        label="Sharpe"
        value={formatNumber(metrics?.sharpe_ratio)}
        tone={sharpeTone(metrics?.sharpe_ratio ?? 0)}
      />
      <MetricCell
        label="Max Drawdown"
        value={formatPercent(metrics?.max_drawdown_pct, 1)}
        tone="text-loss"
      />
      <MetricCell
        label="Trades"
        value={metrics ? String(metrics.total_trades) : PLACEHOLDER}
        tone="text-foreground"
      />
      <MetricCell
        label="Win Rate"
        value={formatPercent(metrics?.win_rate_pct, 1)}
        tone={winRateTone(metrics?.win_rate_pct ?? 0)}
        last
      />
    </div>
  );
}
