"use client";

import {
    DynamicAreaChart,
    DynamicArea,
    DynamicXAxis,
    DynamicYAxis,
    DynamicCartesianGrid,
    DynamicTooltip,
    DynamicResponsiveContainer
} from '@/components/DynamicCharts';

interface MonitoringEquityChartProps {
    data: any[];
}

export default function MonitoringEquityChart({ data }: MonitoringEquityChartProps) {
    return (
        <DynamicResponsiveContainer width="100%" height="100%">
            <DynamicAreaChart data={data}>
                <defs>
                    <linearGradient id="equityGradient" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="5%" stopColor="#22d3ee" stopOpacity={0.1} />
                        <stop offset="95%" stopColor="#22d3ee" stopOpacity={0} />
                    </linearGradient>
                </defs>
                <DynamicCartesianGrid strokeDasharray="3 3" stroke="#ffffff05" vertical={false} />
                <DynamicXAxis
                    dataKey="date"
                    hide
                />
                <DynamicYAxis
                    domain={['auto', 'auto']}
                    orientation="right"
                    stroke="#ffffff20"
                    fontSize={10}
                    tickFormatter={(v: any) => `₹${(v / 1000).toFixed(0)}k`}
                />
                <DynamicTooltip
                    contentStyle={{ backgroundColor: '#0A0B0D', border: '1px solid #ffffff10', borderRadius: '12px' }}
                    labelStyle={{ color: '#666', fontSize: '10px' }}
                    itemStyle={{ color: '#fff', fontSize: '12px', fontWeight: 'bold' }}
                />
                <DynamicArea
                    type="monotone"
                    dataKey="equity"
                    stroke="#22d3ee"
                    strokeWidth={3}
                    fillOpacity={1}
                    fill="url(#equityGradient)"
                    animationDuration={1500}
                />
            </DynamicAreaChart>
        </DynamicResponsiveContainer>
    );
}
