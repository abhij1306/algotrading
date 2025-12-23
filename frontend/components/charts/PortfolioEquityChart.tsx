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

interface PortfolioEquityChartProps {
    data: any[];
}

export default function PortfolioEquityChart({ data }: PortfolioEquityChartProps) {
    return (
        <DynamicResponsiveContainer width="100%" height="100%">
            <DynamicAreaChart data={data}>
                <defs>
                    <linearGradient id="colorEquityPort" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="5%" stopColor="#8b5cf6" stopOpacity={0.3} />
                        <stop offset="95%" stopColor="#8b5cf6" stopOpacity={0} />
                    </linearGradient>
                </defs>
                <DynamicCartesianGrid strokeDasharray="3 3" stroke="#ffffff0a" vertical={false} />
                <DynamicXAxis dataKey="date" hide />
                <DynamicYAxis hide domain={['auto', 'auto']} />
                <DynamicTooltip
                    contentStyle={{ backgroundColor: '#13151A', border: '1px solid #ffffff14', borderRadius: '8px' }}
                    itemStyle={{ color: '#8b5cf6' }}
                />
                <DynamicArea
                    type="monotone"
                    dataKey="equity"
                    stroke="#8b5cf6"
                    fillOpacity={1}
                    fill="url(#colorEquityPort)"
                    strokeWidth={2}
                    dot={false}
                />
            </DynamicAreaChart>
        </DynamicResponsiveContainer>
    );
}
