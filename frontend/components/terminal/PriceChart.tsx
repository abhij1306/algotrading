'use client';

import {
  Bar,
  CartesianGrid,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

interface CandlePoint {
  ts: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  ema20: number;
  ema50: number;
}

interface PriceChartProps {
  data: CandlePoint[];
}

export function PriceChart({ data }: PriceChartProps) {
  return (
    <div className="w-full h-full p-2">
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={data}>
          <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border)" />
          <XAxis
            dataKey="ts"
            tickFormatter={(value: string) => new Date(value).toLocaleDateString('en-IN', { day: '2-digit', month: 'short' })}
            minTickGap={24}
            tick={{ fontSize: 11, fill: 'var(--color-foreground-muted)' }}
          />
          <YAxis
            yAxisId="price"
            domain={['dataMin', 'dataMax']}
            tick={{ fontSize: 11, fill: 'var(--color-foreground-muted)' }}
            width={60}
          />
          <YAxis yAxisId="volume" orientation="right" tick={false} width={30} />
          <Tooltip
            formatter={(value: number, name: string) => {
              if (name === 'volume') return [Math.round(value).toLocaleString('en-IN'), 'Volume'];
              return [Number(value).toFixed(2), name.toUpperCase()];
            }}
            labelFormatter={(label: string) => new Date(label).toLocaleString('en-IN')}
          />
          <Bar yAxisId="volume" dataKey="volume" fill="var(--color-border)" opacity={0.35} />
          <Line yAxisId="price" type="monotone" dataKey="close" stroke="var(--color-primary)" dot={false} strokeWidth={1.8} />
          <Line yAxisId="price" type="monotone" dataKey="ema20" stroke="var(--color-profit)" dot={false} strokeWidth={1.6} />
          <Line yAxisId="price" type="monotone" dataKey="ema50" stroke="var(--color-foreground-muted)" dot={false} strokeWidth={1.6} />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}
