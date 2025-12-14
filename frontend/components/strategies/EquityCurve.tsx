"use client";

import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer } from 'recharts';

interface EquityCurveProps {
    equityCurve: any[];
    initialCapital: number;
}

export default function EquityCurve({ equityCurve, initialCapital }: EquityCurveProps) {
    if (!equityCurve || equityCurve.length === 0) {
        return null;
    }

    // Format data for chart
    const chartData = equityCurve.map((point, index) => ({
        index,
        timestamp: point.timestamp,
        time: new Date(point.timestamp.endsWith('Z') ? point.timestamp : point.timestamp + 'Z').toLocaleString('en-IN', {
            day: '2-digit',
            month: 'short',
            timeZone: 'Asia/Kolkata'
        }),
        equity: point.equity,
        drawdown: point.drawdown
    }));

    const maxEquity = Math.max(...equityCurve.map(p => p.equity));
    const minEquity = Math.min(...equityCurve.map(p => p.equity));

    return (
        <div className="bg-slate-800/50 rounded-lg p-6 border border-slate-700">
            <div className="flex items-center justify-between mb-4">
                <h3 className="text-sm font-semibold text-slate-400 flex items-center">
                    <svg className="w-4 h-4 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M7 12l3-3 3 3 4-4M8 21l4-4 4 4M3 4h18M4 4h16v12a1 1 0 01-1 1H5a1 1 0 01-1-1V4z" />
                    </svg>
                    EQUITY CURVE
                </h3>
                <div className="flex gap-4">
                    <div className="text-right">
                        <div className="text-xs text-slate-500">Initial Capital</div>
                        <div className="text-sm text-white font-medium">₹{initialCapital.toLocaleString()}</div>
                    </div>
                    <div className="text-right">
                        <div className="text-xs text-slate-500">Final Equity</div>
                        <div className="text-sm text-white font-medium">
                            ₹{Math.floor(equityCurve[equityCurve.length - 1]?.equity).toLocaleString('en-IN', { maximumFractionDigits: 0 })}
                        </div>
                    </div>
                </div>
            </div>

            <ResponsiveContainer width="100%" height={300}>
                <LineChart data={chartData} margin={{ top: 5, right: 20, left: 0, bottom: 5 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
                    <XAxis
                        dataKey="time"
                        stroke="#94a3b8"
                        tick={{ fill: '#94a3b8', fontSize: 12 }}
                        tickMargin={10}
                        interval="preserveStartEnd"
                    />
                    <YAxis
                        stroke="#94a3b8"
                        tick={{ fill: '#94a3b8', fontSize: 12 }}
                        tickFormatter={(value) => `₹${(value / 1000).toFixed(0)}k`}
                        domain={[Math.floor(minEquity * 0.98), Math.ceil(maxEquity * 1.02)]}
                    />
                    <Tooltip
                        contentStyle={{
                            backgroundColor: '#1e293b',
                            border: '1px solid #334155',
                            borderRadius: '8px',
                            color: '#fff'
                        }}
                        formatter={(value: any) => [`₹${Number(value).toLocaleString('en-IN', { maximumFractionDigits: 0 })}`, 'Equity']}
                        labelStyle={{ color: '#94a3b8' }}
                    />
                    <Legend
                        wrapperStyle={{ color: '#94a3b8', fontSize: '12px' }}
                        iconType="line"
                    />
                    <Line
                        type="monotone"
                        dataKey="equity"
                        stroke="#10b981"
                        strokeWidth={2}
                        dot={false}
                        name="Portfolio Equity"
                        animationDuration={500}
                    />
                </LineChart>
            </ResponsiveContainer>
        </div>
    );
}
