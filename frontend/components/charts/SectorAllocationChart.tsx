'use client'

import React from 'react';
import { DynamicPieChart, DynamicPie, DynamicCell, DynamicResponsiveContainer, DynamicTooltip, DynamicLegend } from "@/components/DynamicCharts";

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

    // Prepare chartData for the new Pie component structure
    const chartData = sortedData.map((entry, index) => ({
        name: entry.name,
        value: entry.allocation,
        color: SECTOR_COLORS[entry.name] || generateColor(index),
    }));

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

    // The renderCustomLabel and CustomLegend components are no longer directly used
    // as the new DynamicLegend handles its own content and Pie component doesn't use custom labels in this new config.

    return (
        <div className="w-full h-full relative">
            <DynamicResponsiveContainer width="100%" height="100%">
                <DynamicPieChart>
                    <DynamicPie
                        data={chartData}
                        cx="50%"
                        cy="50%"
                        innerRadius={60}
                        outerRadius={80}
                        paddingAngle={5}
                        dataKey="value"
                    >
                        {chartData.map((entry: any, index: number) => (
                            <DynamicCell key={`cell-${index}`} fill={entry.color} stroke="none" />
                        ))}
                    </DynamicPie>
                    <DynamicTooltip content={<CustomTooltip />} />
                    <DynamicLegend
                        layout="vertical"
                        verticalAlign="middle"
                        align="right"
                        iconType="circle"
                        content={({ payload }: any) => (
                            <ul className="list-none space-y-1">
                                {payload.map((entry: any, index: number) => (
                                    <li key={`item-${index}`} className="flex items-center gap-2 text-xs text-text-muted">
                                        <span className="w-2 h-2 rounded-full" style={{ backgroundColor: entry.color }}></span>
                                        <span>{entry.value}</span>
                                    </li>
                                ))}
                            </ul>
                        )}
                    />
                </DynamicPieChart>
            </DynamicResponsiveContainer>
        </div>
    );
}
