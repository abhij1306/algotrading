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

interface RiskAnalysisChartProps {
    data: any[];
}

export default function RiskAnalysisChart({ data }: RiskAnalysisChartProps) {
    if (!data || data.length === 0) {
        return (
            <div className="flex items-center justify-center h-full">
                <p className="text-gray-600 text-[10px] font-mono">NO DATA AVAILABLE</p>
            </div>
        );
    }

    return (
        <DynamicResponsiveContainer width="100%" height="100%">
            <DynamicAreaChart data={data}>
                <defs>
                    <linearGradient id="colorValue" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="5%" stopColor="#06b6d4" stopOpacity={0.3} />
                        <stop offset="95%" stopColor="#06b6d4" stopOpacity={0} />
                    </linearGradient>
                </defs>
                <DynamicCartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" vertical={false} />
                <DynamicXAxis
                    dataKey="date"
                    hide={false}
                    tick={{ fill: '#6b7280', fontSize: 10 }}
                    axisLine={false}
                    tickLine={false}
                    minTickGap={30}
                    tickFormatter={(val: any) => {
                        const d = new Date(val);
                        return `${d.getDate()} ${d.toLocaleString('default', { month: 'short' })}`;
                    }}
                />
                <DynamicYAxis
                    hide={false}
                    orientation="right"
                    tick={{ fill: '#6b7280', fontSize: 10 }}
                    axisLine={false}
                    tickLine={false}
                    tickFormatter={(val: any) => `${val.toFixed(0)}%`}
                />
                <DynamicTooltip
                    contentStyle={{ backgroundColor: '#0A0A0A', borderColor: 'rgba(255,255,255,0.1)', borderRadius: '8px' }}
                    itemStyle={{ color: '#fff', fontSize: '12px' }}
                    labelStyle={{ color: '#9ca3af', fontSize: '10px', marginBottom: '4px' }}
                    formatter={(value: any) => [`${parseFloat(value).toFixed(2)}%`, 'Return']}
                />
                <DynamicArea
                    type="monotone"
                    dataKey="value"
                    stroke="#06b6d4"
                    fillOpacity={1}
                    fill="url(#colorValue)"
                    strokeWidth={2}
                    name="Portfolio"
                />
                <DynamicArea
                    type="monotone"
                    dataKey="benchmark"
                    stroke="#6b7280"
                    fill="transparent"
                    strokeDasharray="5 5"
                    strokeWidth={1}
                    name="Nifty 50"
                />
            </DynamicAreaChart>
        </DynamicResponsiveContainer>
    );
}
