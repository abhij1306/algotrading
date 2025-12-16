'use client'

import React from 'react';
import { PieChart, Pie, Cell, ResponsiveContainer, Tooltip, Legend } from 'recharts';

interface SectorAllocationChartProps {
    data: Array<{
        name: string;
        allocation: number;
    }>;
}

const SECTOR_COLORS: Record<string, string> = {
    'Information Technology': '#3b82f6',
    'Banking': '#8b5cf6',
    'Financial Services': '#10b981',
    'Oil & Gas': '#f97316',
    'Metals': '#eab308',
    'Automobile': '#ec4899',
    'Pharmaceuticals': '#6366f1',
    'FMCG': '#14b8a6',
    'Power': '#f59e0b',
    'Infrastructure & Capital Goods': '#06b6d4',
    'Chemicals': '#a855f7',
    'Telecom': '#84cc16',
    'Cement': '#f43f5e',
    'Consumer Durables': '#0ea5e9',
    'Media & Entertainment': '#d946ef',
    'Real Estate': '#22c55e',
    'Unknown': '#6b7280',
};

// Generate additional colors for sectors not in the predefined list
const generateColor = (index: number) => {
    const colors = [
        '#3b82f6', '#8b5cf6', '#10b981', '#f97316', '#eab308',
        '#ec4899', '#6366f1', '#14b8a6', '#f59e0b', '#06b6d4',
        '#a855f7', '#84cc16', '#f43f5e', '#0ea5e9', '#d946ef', '#22c55e'
    ];
    return colors[index % colors.length];
};

export default function SectorAllocationChart({ data }: SectorAllocationChartProps) {
    // Sort by allocation descending
    const sortedData = [...data].sort((a, b) => b.allocation - a.allocation);

    const CustomTooltip = ({ active, payload }: any) => {
        if (active && payload && payload.length) {
            return (
                <div className="bg-card-dark border border-border-light rounded-lg p-3 shadow-xl">
                    <p className="text-sm text-white font-medium">{payload[0].name}</p>
                    <p className="text-lg text-primary font-bold">{payload[0].value.toFixed(1)}%</p>
                </div>
            );
        }
        return null;
    };

    const renderCustomLabel = ({ cx, cy, midAngle, innerRadius, outerRadius, percent }: any) => {
        // Only show label if percentage is > 5%
        if (percent < 0.05) return null;

        const RADIAN = Math.PI / 180;
        const radius = innerRadius + (outerRadius - innerRadius) * 0.5;
        const x = cx + radius * Math.cos(-midAngle * RADIAN);
        const y = cy + radius * Math.sin(-midAngle * RADIAN);

        return (
            <text
                x={x}
                y={y}
                fill="white"
                textAnchor={x > cx ? 'start' : 'end'}
                dominantBaseline="central"
                className="text-xs font-semibold"
            >
                {`${(percent * 100).toFixed(1)}%`}
            </text>
        );
    };

    const CustomLegend = ({ payload }: any) => {
        return (
            <div className="flex flex-wrap gap-2 justify-center mt-4">
                {payload.map((entry: any, index: number) => (
                    <div key={`legend-${index}`} className="flex items-center gap-1.5 text-xs">
                        <div
                            className="w-3 h-3 rounded-sm"
                            style={{ backgroundColor: entry.color }}
                        />
                        <span className="text-gray-300">{entry.value}</span>
                    </div>
                ))}
            </div>
        );
    };

    return (
        <div className="w-full h-full">
            <ResponsiveContainer width="100%" height="100%">
                <PieChart>
                    <Pie
                        data={sortedData}
                        cx="50%"
                        cy="45%"
                        labelLine={false}
                        label={renderCustomLabel}
                        outerRadius={100}
                        fill="#8884d8"
                        dataKey="allocation"
                    >
                        {sortedData.map((entry, index) => (
                            <Cell
                                key={`cell-${index}`}
                                fill={SECTOR_COLORS[entry.name] || generateColor(index)}
                            />
                        ))}
                    </Pie>
                    <Tooltip content={<CustomTooltip />} />
                    <Legend content={<CustomLegend />} />
                </PieChart>
            </ResponsiveContainer>
        </div>
    );
}
