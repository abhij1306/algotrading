'use client';

import { memo, useMemo } from 'react';
import dynamic from 'next/dynamic';
import { Card } from '@/components/ui';
import { MonteCarloResult } from '@/lib/backtest/types';
import { ChartSkeleton } from '@/components/charts/ChartSkeleton';

// Code-split Recharts for better performance
const LineChart = dynamic(() => import('recharts').then((mod) => mod.LineChart), {
  ssr: false,
  loading: () => <ChartSkeleton height={350} />
});
const Line = dynamic(() => import('recharts').then((mod) => mod.Line), { ssr: false });
const Area = dynamic(() => import('recharts').then((mod) => mod.Area), { ssr: false });
const XAxis = dynamic(() => import('recharts').then((mod) => mod.XAxis), { ssr: false });
const YAxis = dynamic(() => import('recharts').then((mod) => mod.YAxis), { ssr: false });
const CartesianGrid = dynamic(() => import('recharts').then((mod) => mod.CartesianGrid), { ssr: false });
const Tooltip = dynamic(() => import('recharts').then((mod) => mod.Tooltip), { ssr: false });
const ResponsiveContainer = dynamic(() => import('recharts').then((mod) => mod.ResponsiveContainer), { ssr: false });

interface MonteCarloChartProps {
  result: MonteCarloResult;
  height?: number;
}

export const MonteCarloChart = memo(function MonteCarloChart({ result, height = 350 }: MonteCarloChartProps) {
  // Create chart data from equity curves - memoized
  const chartData = useMemo(() => result.equityCurves.median.map((_, i) => ({
    index: i,
    p5: result.equityCurves.p5[i],
    median: result.equityCurves.median[i],
    p95: result.equityCurves.p95[i],
  })), [result.equityCurves]);

  const formatCurrency = (value: number) => {
    return `₹${(value / 1000).toFixed(0)}K`;
  };

  return (
    <Card className="p-4">
      <div className="flex items-center justify-between mb-4">
        <div>
          <h3 className="text-sm font-semibold">Monte Carlo Simulation</h3>
          <p className="text-xs text-muted-foreground">
            {result.simulations.toLocaleString()} randomized paths
          </p>
        </div>
      </div>

      <ResponsiveContainer width="100%" height={height}>
        <LineChart data={chartData} margin={{ top: 10, right: 30, left: 0, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#374151" opacity={0.3} />
          <XAxis hide />
          <YAxis
            tickFormatter={formatCurrency}
            tick={{ fontSize: 11 }}
            stroke="#6b7280"
          />
          <Tooltip
            content={({ active, payload }) => {
              if (active && payload && payload.length) {
                return (
                  <div className="bg-popover border rounded-lg p-3 shadow-lg">
                    <p className="text-sm font-medium mb-2">Trade {payload[0]?.payload?.index}</p>
                    <div className="space-y-1 text-xs">
                      <p className="text-green-500">Best (95%): {formatCurrency(payload[2]?.value as number)}</p>
                      <p className="text-blue-500">Median: {formatCurrency(payload[1]?.value as number)}</p>
                      <p className="text-red-500">Worst (5%): {formatCurrency(payload[0]?.value as number)}</p>
                    </div>
                  </div>
                );
              }
              return null;
            }}
          />
          <Area
            type="monotone"
            dataKey="p95"
            stroke="transparent"
            fill="#22c55e"
            fillOpacity={0.1}
          />
          <Area
            type="monotone"
            dataKey="p5"
            stroke="transparent"
            fill="#ef4444"
            fillOpacity={0.1}
          />
          <Line
            type="monotone"
            dataKey="p5"
            stroke="#ef4444"
            strokeWidth={1}
            dot={false}
            strokeDasharray="3 3"
          />
          <Line
            type="monotone"
            dataKey="median"
            stroke="#3b82f6"
            strokeWidth={2}
            dot={false}
          />
          <Line
            type="monotone"
            dataKey="p95"
            stroke="#22c55e"
            strokeWidth={1}
            dot={false}
            strokeDasharray="3 3"
          />
        </LineChart>
      </ResponsiveContainer>

      {/* Stats */}
      <div className="grid grid-cols-3 gap-4 mt-4">
        <div className="text-center">
          <div className="text-xs text-muted-foreground">Probability of Profit</div>
          <div className="text-lg font-semibold text-green-500">
            {(result.probabilityOfProfit * 100).toFixed(0)}%
          </div>
        </div>
        <div className="text-center">
          <div className="text-xs text-muted-foreground">Probability of Ruin</div>
          <div className="text-lg font-semibold text-red-500">
            {(result.probabilityOfRuin * 100).toFixed(1)}%
          </div>
        </div>
        <div className="text-center">
          <div className="text-xs text-muted-foreground">95% Confidence</div>
          <div className="text-xs font-medium">
            {formatCurrency(result.confidenceInterval.lower)} - {formatCurrency(result.confidenceInterval.upper)}
          </div>
        </div>
      </div>
    </Card>
  );
});
