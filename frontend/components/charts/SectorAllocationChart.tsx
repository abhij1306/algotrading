'use client'

import React from 'react';
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Cell } from 'recharts';

interface SectorAllocationChartProps {
    data: Array<{
        name: string;
        allocation: number;
    }>;
}

const SECTOR_COLORS: Record<string, string> = {
    'Information Technology': '#3b82f6',
    'Banking & Finance': '#8b5cf6',
    'Pharmaceuticals': '#10b981',
    'Automobile': '#f97316',
    'Energy': '#eab308',
    'FMCG': '#ec4899',
    'Metals & Mining': '#6366f1',
    'Telecom': '#14b8a6',
    'Unknown': '#6b7280',
};

export default function SectorAllocationChart({ data }: SectorAllocationChartProps) {
    // Sort by allocation descending
    const sortedData = [...data].sort((a, b) => b.allocation - a.allocation);

    const CustomTooltip = ({ active, payload }: any) => {
        if (active && payload && payload.length) {
            return (
                <div className="bg-card-dark border border-border-light rounded-lg p-3 shadow-xl">
                    <p className="text-sm text-white font-medium">{payload[0].payload.name}</p>
                    <p className="text-lg text-primary font-bold">{payload[0].value.toFixed(1)}%</p>
                </div>
            );
        }
        return null;
    };

    return (
        <div className="w-full h-full">
            <ResponsiveContainer width="100%" height="100%">
                <BarChart
                    data={sortedData}
                    layout="vertical"
                    margin={{ top: 5, right: 30, left: 100, bottom: 5 }}
                >
                    <CartesianGrid strokeDasharray="3 3" stroke="#252932" opacity={0.3} />
                    <XAxis
                        type="number"
                        stroke="#6b7280"
                        style={{ fontSize: '12px' }}
                        tickLine={false}
                        tickFormatter={(value) => `${value}%`}
                    />
                    <YAxis
                        dataKey="name"
                        type="category"
                        stroke="#6b7280"
                        style={{ fontSize: '12px' }}
                        tickLine={false}
                        width={90}
                    />
                    <Tooltip content={<CustomTooltip />} />
                    <Bar
                        dataKey="allocation"
                        radius={[0, 4, 4, 0]}
                        label={{
                            position: 'right',
                            fill: '#9ca3af',
                            fontSize: 12,
                            formatter: (value: number) => `${value.toFixed(1)}%`
                        }}
                    >
                        {sortedData.map((entry, index) => (
                            <Cell
                                key={`cell-${index}`}
                                fill={SECTOR_COLORS[entry.name] || SECTOR_COLORS['Unknown']}
                            />
                        ))}
                    </Bar>
                </BarChart>
            </ResponsiveContainer>
        </div>
    );
}
