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

interface StrategyEquityChartProps {
    data: any[];
}

export default function StrategyEquityChart({ data }: StrategyEquityChartProps) {
    return (
        <DynamicResponsiveContainer width="100%" height="100%">
            <DynamicAreaChart data={data}>
                <defs>
                    <linearGradient id="colorEquity" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="5%" stopColor="#06b6d4" stopOpacity={0.3} />
                        <stop offset="95%" stopColor="#06b6d4" stopOpacity={0} />
                    </linearGradient>
                </defs>
                <DynamicCartesianGrid strokeDasharray="3 3" stroke="#ffffff0a" vertical={false} />
                <DynamicXAxis
                    dataKey="date"
                    hide
                />
                <DynamicYAxis
                    hide
                    domain={['auto', 'auto']}
                />
                <DynamicTooltip
                    contentStyle={{ backgroundColor: '#13151A', border: '1px solid #ffffff14', borderRadius: '8px' }}
                    itemStyle={{ color: '#06b6d4' }}
                    labelStyle={{ color: '#888' }}
                />
                <DynamicArea
                    type="monotone"
                    dataKey="equity"
                    stroke="#06b6d4"
                    fillOpacity={1}
                    fill="url(#colorEquity)"
                    strokeWidth={2}
                    dot={false}
                />
            </DynamicAreaChart>
        </DynamicResponsiveContainer>
    );
}
