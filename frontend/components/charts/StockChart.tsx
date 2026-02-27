"use client";

import React, { useEffect, useState } from "react";
import dynamic from "next/dynamic";
import { formatPrice, formatPercent, formatCompact } from "@/lib/utils";
import { getErrorMessage } from "@/lib/type-guards";

interface StockChartProps {
  symbol: string;
}

interface PriceData {
  date: string;
  close: number;
  open: number;
  high: number;
  low: number;
  volume: number;
}

// Dynamic import for recharts to reduce initial bundle size
const DynamicLineChart = dynamic(
  () => import('recharts').then((mod) => {
    const { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } = mod;

    return function LineChartComponent({
      data,
      minPrice,
      maxPrice,
      isPositive
    }: {
      data: PriceData[];
      minPrice: number;
      maxPrice: number;
      isPositive: boolean;
    }) {
      return (
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data} margin={{ top: 5, right: 20, bottom: 5, left: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border)" />
            <XAxis
              dataKey="date"
              stroke="var(--color-foreground-tertiary)"
              tick={{ fill: "var(--color-foreground-tertiary)", fontSize: 12 }}
              tickFormatter={(date) => {
                const d = new Date(date);
                return `${d.getDate()}/${d.getMonth() + 1}`;
              }}
            />
            <YAxis
              stroke="var(--color-foreground-tertiary)"
              tick={{ fill: "var(--color-foreground-tertiary)", fontSize: 12 }}
              domain={[minPrice * 0.98, maxPrice * 1.02]}
              tickFormatter={(value) => `₹${formatPrice(value)}`}
            />
            <Tooltip
              contentStyle={{
                backgroundColor: "var(--color-background-tertiary)",
                border: "1px solid var(--color-border)",
                borderRadius: "8px",
                padding: "12px",
              }}
              labelStyle={{ color: "var(--color-foreground)", marginBottom: "8px" }}
              itemStyle={{ color: "var(--color-primary)" }}
              formatter={(value) => [`₹${formatPrice(Number(value) || 0)}`, "Close"]}
              labelFormatter={(label) => {
                const d = new Date(label);
                return d.toLocaleDateString("en-IN", {
                  year: "numeric",
                  month: "short",
                  day: "numeric",
                });
              }}
            />
            <Line
              type="monotone"
              dataKey="close"
              stroke={isPositive ? "var(--color-profit)" : "var(--color-loss)"}
              strokeWidth={2}
              dot={false}
              animationDuration={300}
            />
          </LineChart>
        </ResponsiveContainer>
      );
    };
  }),
  {
    loading: () => (
      <div className="flex-1 flex items-center justify-center">
        <div className="w-full h-full animate-pulse rounded-md bg-background-secondary" />
      </div>
    ),
    ssr: false
  }
);

export default function StockChart({ symbol }: Readonly<StockChartProps>) {
  const [data, setData] = useState<PriceData[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!symbol) return;

    let mounted = true;

    const fetchData = async () => {
      setLoading(true);
      setError(null);
      try {
        // Fetch last 90 days of data
        const endDate = new Date();
        const startDate = new Date();
        startDate.setDate(startDate.getDate() - 90);

        const res = await fetch(
          `/api/market/historical?symbol=${symbol}&start_date=${startDate.toISOString().split("T")[0]}&end_date=${endDate.toISOString().split("T")[0]}`
        );

        if (!mounted) return;

        if (res.status === 404 || res.status === 204) {
          if (!mounted) return;
          setData([]);
          setError("No historical data available for this symbol");
          setLoading(false);
          return;
        }
        const priceData = await res.json();

        if (!mounted) return;

        if (!priceData || priceData.length === 0) {
          if (!mounted) return;
          setData([]);
          setError("No data available");
          setLoading(false);
          return;
        }
        setData(priceData);
        setLoading(false);
      } catch (err: unknown) {
        if (!mounted) return;
        setError(getErrorMessage(err));
        setLoading(false);
      }
    };

    fetchData();

    return () => {
      mounted = false;
    };
  }, [symbol]);

  if (loading) {
    return (
      <div className="h-full w-full flex items-center justify-center bg-background">
        <div className="text-muted-foreground">Loading chart data...</div>
      </div>
    );
  }

  if (error || data.length === 0) {
    return (
      <div className="h-full w-full flex flex-col items-center justify-center bg-background p-8">
        <div className="text-muted-foreground mb-2">📊 No Chart Data Available</div>
        <div className="text-xs text-muted">
          {error || "Historical data not found for " + symbol}
        </div>
        <div className="text-xs text-muted mt-2">Check if symbol exists in database</div>
      </div>
    );
  }

  const minPrice = Math.min(...data.map((d) => d.low));
  const maxPrice = Math.max(...data.map((d) => d.high));
  const currentPrice = data[data.length - 1]?.close || 0;
  const firstPrice = data[0]?.close || 0;
  const priceChange = currentPrice - firstPrice;
  const priceChangePercent = (priceChange / firstPrice) * 100;
  const isPositive = priceChange >= 0;

  return (
    <div className="h-full w-full bg-background flex flex-col p-4">
      {/* Header */}
      <div className="mb-4">
        <div className="flex items-baseline gap-3">
          <div className="text-2xl font-semibold text-foreground">{symbol}</div>
          <div className="text-xl font-semibold text-foreground">₹{formatPrice(currentPrice)}</div>
          <div className={`text-sm ${isPositive ? "text-green-500" : "text-red-500"}`}>
            {formatPercent(priceChangePercent)}
          </div>
        </div>
        <div className="text-xs text-muted-foreground mt-1">Last 90 days • Daily</div>
      </div>

      {/* Chart */}
      <div className="flex-1">
        <DynamicLineChart
          data={data}
          minPrice={minPrice}
          maxPrice={maxPrice}
          isPositive={isPositive}
        />
      </div>

      {/* Footer Stats */}
      <div className="mt-4 grid grid-cols-4 gap-4 text-xs">
        <div>
          <div className="text-muted-foreground">High</div>
          <div className="text-foreground font-semibold">₹{formatPrice(maxPrice)}</div>
        </div>
        <div>
          <div className="text-muted-foreground">Low</div>
          <div className="text-foreground font-semibold">₹{formatPrice(minPrice)}</div>
        </div>
        <div>
          <div className="text-muted-foreground">Volume</div>
          <div className="text-foreground font-semibold">
            {formatCompact(data[data.length - 1]?.volume)}
          </div>
        </div>
        <div>
          <div className="text-muted-foreground">Data Points</div>
          <div className="text-foreground font-semibold">{data.length}</div>
        </div>
      </div>
    </div>
  );
}
