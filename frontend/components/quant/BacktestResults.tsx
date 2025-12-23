"use client";

import React from 'react';
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { ResponsiveContainer, LineChart, Line, XAxis, YAxis, Tooltip, CartesianGrid } from 'recharts';

interface BacktestResultsProps {
    data: any; // The JSON response from /run-simulation
}

// Helper for currency formatting
const formatCurrency = (val: number) =>
    new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR', maximumFractionDigits: 0 }).format(val);

export default function BacktestResults({ data }: BacktestResultsProps) {
    if (!data) return null;

    const { metrics, daily_equity } = data;

    // Transform daily_equity for chart if needed (already array of objects)
    const chartData = daily_equity?.map((d: any) => ({
        ...d,
        date: d.date.split('T')[0] // Ensure YYYY-MM-DD
    })) || [];

    const finalEquity = chartData.length > 0 ? chartData[chartData.length - 1].equity : 0;
    const initialEquity = chartData.length > 0 ? chartData[0].equity : 0;
    const totalReturn = initialEquity > 0 ? ((finalEquity - initialEquity) / initialEquity) * 100 : 0;

    return (
        <div className="space-y-6 animate-in fade-in duration-500">
            {/* Top Metrics Row */}
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                <MetricCard
                    label="Final Equity"
                    value={formatCurrency(finalEquity)}
                    subValue={`${totalReturn > 0 ? '+' : ''}${totalReturn.toFixed(2)}%`}
                    color={totalReturn >= 0 ? "text-green-500" : "text-red-500"}
                />
                <MetricCard
                    label="CAGR"
                    value={`${(metrics.cagr * 100 || 0).toFixed(1)}%`}
                    subValue="Annualized Return"
                />
                <MetricCard
                    label="Sharpe Ratio"
                    value={metrics.sharpe_ratio?.toFixed(2) || "0.00"}
                    subValue="Risk Adjusted"
                />
                <MetricCard
                    label="Max Drawdown"
                    value={`${(metrics.max_drawdown * 100 || 0).toFixed(1)}%`}
                    subValue="Peak to Trough"
                    color="text-red-400"
                />
            </div>

            {/* Equity Curve Chart */}
            <Card className="h-[400px] w-full">
                <CardHeader>
                    <CardTitle>Portfolio Equity Curve</CardTitle>
                </CardHeader>
                <CardContent className="h-[320px] w-full">
                    <ResponsiveContainer width="100%" height="100%">
                        <LineChart data={chartData}>
                            <CartesianGrid strokeDasharray="3 3" opacity={0.1} />
                            <XAxis
                                dataKey="date"
                                tick={{ fontSize: 12 }}
                                tickFormatter={(val) => val.slice(5)} // Show MM-DD
                                minTickGap={30}
                            />
                            <YAxis
                                domain={['auto', 'auto']}
                                tickFormatter={(val) => (val / 100000).toFixed(1) + 'L'}
                                tick={{ fontSize: 12 }}
                            />
                            <Tooltip
                                contentStyle={{ backgroundColor: '#1a1a1a', border: '1px solid #333' }}
                                labelStyle={{ color: '#888' }}
                                formatter={(value: number) => [formatCurrency(value), 'Equity']}
                            />
                            <Line
                                type="monotone"
                                dataKey="equity"
                                stroke="#10b981"
                                strokeWidth={2}
                                dot={false}
                                activeDot={{ r: 4 }}
                            />
                        </LineChart>
                    </ResponsiveContainer>
                </CardContent>
            </Card>
        </div>
    );
}

function MetricCard({ label, value, subValue, color }: any) {
    return (
        <Card>
            <CardContent className="p-6">
                <p className="text-sm font-medium text-muted-foreground">{label}</p>
                <h3 className={`text-2xl font-bold mt-2 ${color || 'text-foreground'}`}>{value}</h3>
                {subValue && <p className="text-xs text-muted-foreground mt-1">{subValue}</p>}
            </CardContent>
        </Card>
    );
}
