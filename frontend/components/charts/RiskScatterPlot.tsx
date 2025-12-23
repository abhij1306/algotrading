'use client'

import React from 'react';
import {
    DynamicScatterChart,
    DynamicScatter,
    DynamicXAxis,
    DynamicYAxis,
    DynamicCartesianGrid,
    DynamicTooltip,
    DynamicResponsiveContainer,
    DynamicCell
} from "@/components/DynamicCharts";

interface RiskScatterPlotProps {
    data: Array<{
        symbol: string;
        volatility: number;
        return: number;
        weight: number;
    }>;
}

export default function RiskScatterPlot({ data }: RiskScatterPlotProps) {
    // Calculate median values for quadrant lines
    const medianReturn = data.length > 0
        ? data.map(d => d.return).sort((a, b) => a - b)[Math.floor(data.length / 2)]
        : 0;
    const medianVol = data.length > 0
        ? data.map(d => d.volatility).sort((a, b) => a - b)[Math.floor(data.length / 2)]
        : 0;

    const CustomTooltip = ({ active, payload }: any) => {
        if (active && payload && payload.length) {
            const data = payload[0].payload;
            return (
                <div className="bg-card-dark border border-border-light rounded-lg p-3 shadow-xl">
                    <p className="text-sm text-white font-bold mb-2">{data.symbol}</p>
                    <div className="space-y-1 text-xs">
                        <div className="flex justify-between gap-4">
                            <span className="text-text-secondary">Return:</span>
                            <span className={`font-medium ${data.return >= 0 ? 'text-profit' : 'text-loss'}`}>
                                {data.return.toFixed(2)}%
                            </span>
                        </div>
                        <div className="flex justify-between gap-4">
                            <span className="text-text-secondary">Volatility:</span>
                            <span className="text-white font-medium">{data.volatility.toFixed(2)}%</span>
                        </div>
                        <div className="flex justify-between gap-4">
                            <span className="text-text-secondary">Weight:</span>
                            <span className="text-white font-medium">{data.weight.toFixed(1)}%</span>
                        </div>
                    </div>
                </div>
            );
        }
        return null;
    };

    // Color based on quadrant
    const getColor = (d: any) => {
        if (d.return >= medianReturn && d.volatility <= medianVol) return '#10b981'; // Growth/Low Vol (green)
        if (d.return >= medianReturn && d.volatility > medianVol) return '#3b82f6';  // Growth/High Vol (blue)
        if (d.return < medianReturn && d.volatility <= medianVol) return '#eab308';  // Risk/Low Vol (yellow)
        return '#ef4444'; // Risk/High Vol (red)
    };

    // Assuming chartData is derived from data, and each item in chartData has 'std', 'cagr', and 'color' properties.
    // For this change, we'll map the existing 'data' to 'chartData' structure as implied by the instruction.
    const chartData = data.map(d => ({
        std: d.volatility, // Map volatility to std
        cagr: d.return,    // Map return to cagr
        symbol: d.symbol,
        weight: d.weight,
        color: getColor(d) // Use the existing getColor logic
    }));

    return (
        <div className="h-[250px] w-full">
            <DynamicResponsiveContainer width="100%" height="100%">
                <DynamicScatterChart margin={{ top: 20, right: 20, bottom: 20, left: 20 }}>
                    <DynamicCartesianGrid strokeDasharray="3 3" stroke="#252932" />
                    <DynamicXAxis
                        type="number"
                        dataKey="std"
                        name="Risk (Std Dev)"
                        stroke="#6b7280"
                        fontSize={12}
                        tickFormatter={(val: any) => `${val}%`}
                        label={{ value: 'Risk (Std Dev)', position: 'bottom', offset: 0, fill: '#6b7280', fontSize: 12 }}
                    />
                    <DynamicYAxis
                        type="number"
                        dataKey="cagr"
                        name="Return (CAGR)"
                        stroke="#6b7280"
                        fontSize={12}
                        tickFormatter={(val: any) => `${val}%`}
                        label={{ value: 'Return (CAGR)', angle: -90, position: 'left', fill: '#6b7280', fontSize: 12 }}
                    />
                    <DynamicTooltip content={<CustomTooltip />} cursor={{ strokeDasharray: '3 3' }} />

                    {/* Reference lines for quadrants (removed as per instruction, but keeping the median calculations) */}
                    {/* If ReferenceLine is needed, it would need to be imported from DynamicCharts or handled differently */}
                    {/* <ReferenceLine x={medianVol} stroke="#6b7280" strokeDasharray="3 3" opacity={0.5} />
                    <ReferenceLine y={medianReturn} stroke="#6b7280" strokeDasharray="3 3" opacity={0.5} /> */}

                    <DynamicScatter name="Portfolios" data={chartData} fill="#8884d8">
                        {chartData.map((entry: any, index: number) => (
                            <DynamicCell
                                key={`cell-${index}`}
                                fill={entry.color}
                            // The instruction removed 'r' prop, but if sizing by weight is desired, it would need to be re-added
                            // r={6 + (entry.weight * 0.3)}
                            />
                        ))}
                    </DynamicScatter>
                </DynamicScatterChart>
            </DynamicResponsiveContainer>
        </div>
    );
}
