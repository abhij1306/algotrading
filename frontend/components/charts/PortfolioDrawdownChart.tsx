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

interface PortfolioDrawdownChartProps {
    data: any[];
}

export default function PortfolioDrawdownChart({ data }: PortfolioDrawdownChartProps) {
    return (
        <DynamicResponsiveContainer width="100%" height="100%">
            <DynamicAreaChart data={data}>
                <defs>
                    <linearGradient id="colorDDPort" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="5%" stopColor="#ef4444" stopOpacity={0.1} />
                        <stop offset="95%" stopColor="#ef4444" stopOpacity={0} />
                    </linearGradient>
                </defs>
                <DynamicCartesianGrid strokeDasharray="3 3" stroke="#ffffff0a" vertical={false} />
                <DynamicXAxis dataKey="date" hide />
                <DynamicYAxis hide domain={['-100', '0']} />
                <DynamicTooltip
                    contentStyle={{ backgroundColor: '#13151A', border: '1px solid #ffffff14', borderRadius: '8px' }}
                    itemStyle={{ color: '#ef4444' }}
                    formatter={(value: any) => [value != null ? `${Number(value).toFixed(2)}%` : 'N/A', 'Drawdown']}
                />
                <DynamicArea
                    type="stepAfter"
                    dataKey="drawdown"
                    stroke="#ef4444"
                    fillOpacity={1}
                    fill="url(#colorDDPort)"
                    strokeWidth={1.5}
                    dot={false}
                />
            </DynamicAreaChart>
        </DynamicResponsiveContainer>
    );
}
