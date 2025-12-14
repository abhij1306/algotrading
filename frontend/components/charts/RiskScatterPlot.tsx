'use client'

import React from 'react';
import { ScatterChart, Scatter, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Cell, ReferenceLine } from 'recharts';

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

    return (
        <div className="w-full h-full">
            <ResponsiveContainer width="100%" height="100%">
                <ScatterChart margin={{ top: 20, right: 20, bottom: 20, left: 20 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#252932" opacity={0.3} />
                    <XAxis
                        type="number"
                        dataKey="volatility"
                        name="Volatility"
                        stroke="#6b7280"
                        style={{ fontSize: '12px' }}
                        tickLine={false}
                        label={{ value: 'Volatility (%)', position: 'insideBottom', offset: -10, fill: '#9ca3af' }}
                        tickFormatter={(value) => `${value}%`}
                    />
                    <YAxis
                        type="number"
                        dataKey="return"
                        name="Return"
                        stroke="#6b7280"
                        style={{ fontSize: '12px' }}
                        tickLine={false}
                        label={{ value: 'Annual Return (%)', angle: -90, position: 'insideLeft', fill: '#9ca3af' }}
                        tickFormatter={(value) => `${value}%`}
                    />
                    <Tooltip content={<CustomTooltip />} />

                    {/* Reference lines for quadrants */}
                    <ReferenceLine x={medianVol} stroke="#6b7280" strokeDasharray="3 3" opacity={0.5} />
                    <ReferenceLine y={medianReturn} stroke="#6b7280" strokeDasharray="3 3" opacity={0.5} />

                    <Scatter
                        name="Stocks"
                        data={data}
                        shape="circle"
                    >
                        {data.map((entry, index) => (
                            <Cell
                                key={`cell-${index}`}
                                fill={getColor(entry)}
                                r={6 + (entry.weight * 0.3)} // Size based on weight
                            />
                        ))}
                    </Scatter>
                </ScatterChart>
            </ResponsiveContainer>
        </div>
    );
}
