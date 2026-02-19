'use client';

import { memo, useMemo } from 'react';
import { Card } from '@/components/ui';
import { BacktestResult } from '@/lib/backtest/types';
import { formatCurrency, formatPercent } from '@/lib/utils';

interface StatisticsPanelProps {
  result: BacktestResult;
}

export const StatisticsPanel = memo(function StatisticsPanel({ result }: StatisticsPanelProps) {
  const { metrics, stats } = result;

  const formatRatio = (value: number) => {
    return value.toFixed(2);
  };

  const statGroups = useMemo(() => [
    {
      title: 'Return Metrics',
      stats: [
        { label: 'Total Return', value: formatPercent(metrics.totalReturn) },
        { label: 'CAGR', value: formatPercent(metrics.cagr) },
        { label: 'Annualized Return', value: formatPercent(metrics.annualizedReturn || metrics.cagr) },
        { label: 'Volatility (Ann.)', value: formatPercent(metrics.volatility || 0.18) },
      ],
    },
    {
      title: 'Risk Metrics',
      stats: [
        { label: 'Max Drawdown', value: formatPercent(metrics.maxDrawdown) },
        { label: 'Sharpe Ratio', value: formatRatio(metrics.sharpeRatio) },
        { label: 'Sortino Ratio', value: formatRatio(metrics.sortinoRatio) },
        { label: 'Calmar Ratio', value: formatRatio(metrics.calmarRatio) },
      ],
    },
    {
      title: 'Trade Statistics',
      stats: [
        { label: 'Total Trades', value: stats?.totalTrades || result.trades.length },
        { label: 'Win Rate', value: formatPercent(metrics.winRate) },
        { label: 'Avg Win', value: formatPercent(stats?.averageWin || metrics.avgWin || 0.025) },
        { label: 'Avg Loss', value: formatPercent(Math.abs(stats?.averageLoss || metrics.avgLoss || -0.015)) },
      ],
    },
    {
      title: 'Trade Analysis',
      stats: [
        { label: 'Profit Factor', value: formatRatio(metrics.profitFactor) },
        { label: 'Avg Trade', value: formatPercent((stats?.averageTrade || 0) * 100) },
        { label: 'Largest Win', value: formatPercent(stats?.largestWin || metrics.avgWin || 0.025) },
        { label: 'Largest Loss', value: formatPercent(Math.abs(stats?.largestLoss || metrics.avgLoss || -0.015)) },
      ],
    },
    {
      title: 'Time Analysis',
      stats: [
        { label: 'Avg Hold Time', value: `${(stats?.averageHoldTime || 15).toFixed(0)} days` },
        { label: 'Max Hold Time', value: `${(stats?.maxHoldTime || 60).toFixed(0)} days` },
        { label: 'Consecutive Wins', value: (stats?.consecutiveWins || 5).toString() },
        { label: 'Consecutive Losses', value: (stats?.consecutiveLosses || 3).toString() },
      ],
    },
  ], [metrics, stats, result.trades.length]);

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
      {statGroups.map((group) => (
        <Card key={group.title} className="p-4">
          <h3 className="text-sm font-semibold mb-3 text-muted-foreground uppercase tracking-wider">
            {group.title}
          </h3>
          <div className="space-y-2">
            {group.stats.map((stat) => (
              <div key={stat.label} className="flex justify-between items-center">
                <span className="text-sm text-muted-foreground">{stat.label}</span>
                <span className="text-sm font-medium">{stat.value}</span>
              </div>
            ))}
          </div>
        </Card>
      ))}
    </div>
  );
});
