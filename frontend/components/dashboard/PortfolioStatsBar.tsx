import { memo } from "react";
import { formatInr, formatSignedPercent, roundsNegative } from "@/lib/format";
import type { PortfolioStats } from "@/lib/queries/dashboard";

const StatItem = memo(function StatItem({
  label,
  value,
  change,
  loading,
}: Readonly<{
  label: string;
  value: string;
  /** Percent with the rupee amount it describes, which sets the tone. */
  change?: { percent: number; amount: number };
  loading: boolean;
}>) {
  if (loading) return <div className="text-foreground-muted">—</div>;
  return (
    <div className="flex items-center gap-3">
      <span className="text-sm text-foreground-muted">{label}</span>
      <span className="text-base font-semibold tabular-nums">{value}</span>
      {change !== undefined && (
        <span
          className={`text-sm tabular-nums ${roundsNegative(change.amount) ? "text-loss" : "text-profit"}`}
        >
          {formatSignedPercent(change.percent)}
        </span>
      )}
    </div>
  );
});

/** Portfolio value, day P&L and total return. */
export function PortfolioStatsBar(
  props: Readonly<{ stats: PortfolioStats | null; loading: boolean }>,
) {
  const { stats, loading } = props;
  return (
    <div className="flex items-center gap-8 px-4 py-2 border-b border-border h-11">
      <StatItem label="Portfolio" value={formatInr(stats?.totalValue)} loading={loading} />
      <StatItem
        label="Day P&L"
        value={formatInr(stats?.dayChange)}
        change={stats ? { percent: stats.dayChangePercent, amount: stats.dayChange } : undefined}
        loading={loading}
      />
      <StatItem
        label="Total Return"
        value={formatInr(stats?.totalReturn)}
        change={
          stats ? { percent: stats.totalReturnPercent, amount: stats.totalReturn } : undefined
        }
        loading={loading}
      />
    </div>
  );
}
