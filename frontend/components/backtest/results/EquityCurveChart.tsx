'use client';

import { useState } from 'react';
import dynamic from 'next/dynamic';
import { Card } from '@/components/ui';
import { EquityPoint } from '@/lib/backtest/types';
import { CHART_COLORS } from '@/lib/backtest/constants';
import { ChartSkeleton } from '@/components/charts/ChartSkeleton';

// Code-split Recharts for better performance
const ComposedChart = dynamic(() => import('recharts').then((mod) => mod.ComposedChart), {
  ssr: false,
  loading: () => <ChartSkeleton height={400} />
});
const Line = dynamic(() => import('recharts').then((mod) => mod.Line), { ssr: false });
const Area = dynamic(() => import('recharts').then((mod) => mod.Area), { ssr: false });
const XAxis = dynamic(() => import('recharts').then((mod) => mod.XAxis), { ssr: false });
const YAxis = dynamic(() => import('recharts').then((mod) => mod.YAxis), { ssr: false });
const CartesianGrid = dynamic(() => import('recharts').then((mod) => mod.CartesianGrid), { ssr: false });
const Tooltip = dynamic(() => import('recharts').then((mod) => mod.Tooltip), { ssr: false });
const ResponsiveContainer = dynamic(() => import('recharts').then((mod) => mod.ResponsiveContainer), { ssr: false });
const ReferenceLine = dynamic(() => import('recharts').then((mod) => mod.ReferenceLine), { ssr: false });

interface EquityCurveChartProps {
  equityCurve: EquityPoint[];
  benchmarkCurve?: { date: string; value: number }[];
  initialCapital: number;
  height?: number;
}

export function EquityCurveChart({
  equityCurve,
  benchmarkCurve,
  initialCapital,
  height = 400,
}: EquityCurveChartProps) {
  const [showBenchmark, setShowBenchmark] = useState(true);

  // Merge equity and benchmark data
  const chartData = equityCurve.map((point) => {
    const benchmarkPoint = benchmarkCurve?.find((b) => b.date === point.date);
    return {
      date: point.date,
      equity: point.equity,
      benchmark: benchmarkPoint?.value || null,
      drawdown: point.drawdown * 100,
    };
  });

  const formatCurrency = (value: number) => {
    return `₹${(value / 1000).toFixed(0)}K`;
  };

  const formatDate = (dateStr: string) => {
    const date = new Date(dateStr);
    return date.toLocaleDateString('en-IN', { month: 'short', day: 'numeric' });
  };

  const finalReturn = ((equityCurve[equityCurve.length - 1]?.equity - initialCapital) / initialCapital) * 100;

  return (
    <Card className="p-4">
      <div className="flex items-center justify-between mb-4">
        <div>
          <h3 className="text-lg font-semibold">Equity Curve</h3>
          <p className="text-sm text-muted-foreground">
            Final Return: <span className={finalReturn >= 0 ? 'text-green-500' : 'text-red-500'}>
              {finalReturn >= 0 ? '+' : ''}{finalReturn.toFixed(2)}%
            </span>
          </p>
        </div>
        <div className="flex gap-4">
          <label className="flex items-center gap-2 text-sm cursor-pointer">
            <div className="w-3 h-3 rounded-full" style={{ backgroundColor: CHART_COLORS.equity }} />
            <span>Strategy</span>
          </label>
          {benchmarkCurve && (
            <label className="flex items-center gap-2 text-sm cursor-pointer">
              <input
                type="checkbox"
                checked={showBenchmark}
                onChange={(e) => setShowBenchmark(e.target.checked)}
                className="rounded"
              />
              <div className="w-3 h-3 rounded-full" style={{ backgroundColor: CHART_COLORS.benchmark }} />
              <span>Benchmark</span>
            </label>
          )}
        </div>
      </div>

      <ResponsiveContainer width="100%" height={height}>
        <ComposedChart data={chartData} margin={{ top: 10, right: 30, left: 0, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#374151" opacity={0.3} />
          <XAxis
            dataKey="date"
            tickFormatter={formatDate}
            tick={{ fontSize: 11 }}
            stroke="#6b7280"
          />
          <YAxis
            tickFormatter={formatCurrency}
            tick={{ fontSize: 11 }}
            stroke="#6b7280"
            domain={['auto', 'auto']}
          />
          <Tooltip
            content={({ active, payload, label }) => {
              if (active && payload && payload.length) {
                return (
                  <div className="bg-popover border rounded-lg p-3 shadow-lg">
                    <p className="text-sm font-medium mb-2">{formatDate(label)}</p>
                    <div className="space-y-1">
                      <p className="text-sm" style={{ color: CHART_COLORS.equity }}>
                        Strategy: {formatCurrency(payload[0]?.value as number)}
                      </p>
                      {showBenchmark && payload[1]?.value && (
                        <p className="text-sm" style={{ color: CHART_COLORS.benchmark }}>
                          Benchmark: {formatCurrency(payload[1]?.value as number)}
                        </p>
                      )}
                      <p className="text-xs text-muted-foreground">
                        Drawdown: {(payload[0]?.payload?.drawdown)?.toFixed(2)}%
                      </p>
                    </div>
                  </div>
                );
              }
              return null;
            }}
          />
          <ReferenceLine
            y={initialCapital}
            stroke="#6b7280"
            strokeDasharray="5 5"
            label={{ value: 'Initial', position: 'right', fontSize: 10 }}
          />
          <Area
            type="monotone"
            dataKey="equity"
            stroke={CHART_COLORS.equity}
            fill={CHART_COLORS.equity}
            fillOpacity={0.1}
            strokeWidth={2}
            dot={false}
            activeDot={{ r: 4, strokeWidth: 0 }}
          />
          {showBenchmark && benchmarkCurve && (
            <Line
              type="monotone"
              dataKey="benchmark"
              stroke={CHART_COLORS.benchmark}
              strokeWidth={1.5}
              dot={false}
              strokeDasharray="5 5"
            />
          )}
        </ComposedChart>
      </ResponsiveContainer>
    </Card>
  );
}
