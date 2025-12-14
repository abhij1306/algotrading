'use client'

import React from 'react';
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer } from 'recharts';

interface PerformanceChartProps {
    data: {
        dates: string[];
        portfolioReturns: number[];
        benchmarkReturns: number[];
    };
    selectedPeriod?: '1M' | '3M' | '6M' | '1Y' | 'YTD';
}

export default function PerformanceChart({ data, selectedPeriod = '1Y' }: PerformanceChartProps) {
    // Transform data for Recharts format
    const chartData = data.dates.map((date, index) => ({
        date: new Date(date).toLocaleDateString('en-US', { month: 'short', day: 'numeric' }),
        portfolio: data.portfolioReturns[index],
        benchmark: data.benchmarkReturns[index]
    }));

    // Custom tooltip
    const CustomTooltip = ({ active, payload }: any) => {
        if (active && payload && payload.length) {
            return (
                <div className="bg-card-dark border border-border-light rounded-lg p-3 shadow-xl">
                    <p className="text-xs text-text-secondary mb-2">{payload[0].payload.date}</p>
                    <div className="space-y-1">
                        <div className="flex items-center gap-2">
                            <div className="w-3 h-3 rounded-full bg-primary"></div>
                            <span className="text-sm text-white">Portfolio: {payload[0].value.toFixed(2)}%</span>
                        </div>
                        <div className="flex items-center gap-2">
                            <div className="w-3 h-3 rounded-full bg-text-muted"></div>
                            <span className="text-sm text-white">Nifty 50: {payload[1]?.value.toFixed(2)}%</span>
                        </div>
                    </div>
                </div>
            );
        }
        return null;
    };

    return (
        <div className="w-full h-full">
            <ResponsiveContainer width="100%" height="100%">
                <LineChart data={chartData} margin={{ top: 5, right: 30, left: 0, bottom: 5 }}>
                    <defs>
                        <linearGradient id="portfolioGradient" x1="0" y1="0" x2="0" y2="1">
                            <stop offset="5%" stopColor="#6366f1" stopOpacity={0.3} />
                            <stop offset="95%" stopColor="#6366f1" stopOpacity={0} />
                        </linearGradient>
                    </defs>
                    <CartesianGrid strokeDasharray="3 3" stroke="#252932" opacity={0.3} />
                    <XAxis
                        dataKey="date"
                        stroke="#6b7280"
                        style={{ fontSize: '12px' }}
                        tickLine={false}
                    />
                    <YAxis
                        stroke="#6b7280"
                        style={{ fontSize: '12px' }}
                        tickLine={false}
                        tickFormatter={(value) => `${value}%`}
                    />
                    <Tooltip content={<CustomTooltip />} />
                    <Legend
                        wrapperStyle={{ fontSize: '14px', color: '#9ca3af' }}
                        iconType="circle"
                    />
                    <Line
                        type="monotone"
                        dataKey="portfolio"
                        stroke="#6366f1"
                        strokeWidth={3}
                        dot={false}
                        name="Portfolio Returns"
                        activeDot={{ r: 6, fill: '#6366f1' }}
                    />
                    <Line
                        type="monotone"
                        dataKey="benchmark"
                        stroke="#6b7280"
                        strokeWidth={2}
                        strokeDasharray="5 5"
                        dot={false}
                        name="Nifty 50"
                        activeDot={{ r: 4, fill: '#6b7280' }}
                    />
                </LineChart>
            </ResponsiveContainer>
        </div>
    );
}
