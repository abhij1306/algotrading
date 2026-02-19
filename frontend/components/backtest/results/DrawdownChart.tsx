'use client';

import { memo, useMemo } from 'react';
import dynamic from 'next/dynamic';
import { Card } from '@/components/ui';
import { EquityPoint } from '@/lib/backtest/types';
import { ChartSkeleton } from '@/components/charts/ChartSkeleton';

// Code-split Recharts for better performance
const AreaChart = dynamic(() => import('recharts').then((mod) => mod.AreaChart), {
  ssr: false,
  loading: () => <ChartSkeleton height={250} />
});
const Area = dynamic(() => import('recharts').then((mod) => mod.Area), { ssr: false });
const XAxis = dynamic(() => import('recharts').then((mod) => mod.XAxis), { ssr: false });
const YAxis = dynamic(() => import('recharts').then((mod) => mod.YAxis), { ssr: false });
const CartesianGrid = dynamic(() => import('recharts').then((mod) => mod.CartesianGrid), { ssr: false });
const Tooltip = dynamic(() => import('recharts').then((mod) => mod.Tooltip), { ssr: false });
const ResponsiveContainer = dynamic(() => import('recharts').then((mod) => mod.ResponsiveContainer), { ssr: false });

interface DrawdownChartProps {
  equityCurve: EquityPoint[];
  height?: number;
}

export const DrawdownChart = memo(function DrawdownChart({ equityCurve, height = 250 }: DrawdownChartProps) {
  const chartData = useMemo(() => equityCurve.map((point) => ({
    date: point.date,
    drawdown: point.drawdown * 100,
  })), [equityCurve]);

  const maxDrawdown = useMemo(() => Math.min(...chartData.map((d) => d.drawdown)), [chartData]);

  const formatDate = (dateStr: string) => {
    const date = new Date(dateStr);
    return date.toLocaleDateString('en-IN', { month: 'short' });
  };

  return (
    <Card className="p-4">
      <div className="flex items-center justify-between mb-4">
        <div>
          <h3 className="text-sm font-semibold">Underwater Curve</h3>
          <p className="text-xs text-muted-foreground">
            Max Drawdown: <span className="text-red-500">{maxDrawdown.toFixed(2)}%</span>
          </p>
        </div>
      </div>

      <ResponsiveContainer width="100%" height={height}>
        <AreaChart data={chartData} margin={{ top: 10, right: 30, left: 0, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#374151" opacity={0.3} />
          <XAxis
            dataKey="date"
            tickFormatter={formatDate}
            tick={{ fontSize: 11 }}
            stroke="#6b7280"
          />
          <YAxis
            tickFormatter={(value) => `${value.toFixed(0)}%`}
            tick={{ fontSize: 11 }}
            stroke="#6b7280"
            domain={['dataMin', 0]}
          />
          <Tooltip
            content={({ active, payload, label }) => {
              if (active && payload && payload.length) {
                return (
                  <div className="bg-popover border rounded-lg p-3 shadow-lg">
                    <p className="text-sm font-medium">{formatDate(label)}</p>
                    <p className="text-sm text-red-500">
                      Drawdown: {payload[0]?.value?.toFixed(2)}%
                    </p>
                  </div>
                );
              }
              return null;
            }}
          />
          <Area
            type="monotone"
            dataKey="drawdown"
            stroke="#ef4444"
            fill="#ef4444"
            fillOpacity={0.3}
            strokeWidth={1.5}
          />
        </AreaChart>
      </ResponsiveContainer>
    </Card>
  );
});
