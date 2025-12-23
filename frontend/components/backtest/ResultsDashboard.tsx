"use client";

import React from "react";
import {
    DynamicAreaChart,
    DynamicArea,
    DynamicXAxis,
    DynamicYAxis,
    DynamicCartesianGrid,
    DynamicTooltip,
    DynamicResponsiveContainer
} from "@/components/DynamicCharts";
import { TrendingUp, TrendingDown, Activity, Target, Award, Percent } from "lucide-react";

interface ResultsDashboardProps {
    metrics: any;
    equityCurve: any[];
    config: any;
    strategyBreakdown?: any;
}

export default function ResultsDashboard({ metrics, equityCurve, config, strategyBreakdown }: ResultsDashboardProps) {
    if (!metrics || !equityCurve.length) return null;

    const summaryItems = [
        { label: "CAGR", value: (metrics.cagr * 100).toFixed(2) + "%", color: "text-cyan-400", icon: TrendingUp },
        { label: "Sharpe", value: metrics.sharpe.toFixed(2), color: "text-blue-400", icon: Award },
        { label: "Sortino", value: metrics.sortino.toFixed(2), color: "text-purple-400", icon: Target },
        { label: "MaxDD", value: (metrics.max_drawdown * 100).toFixed(2) + "%", color: "text-rose-400", icon: TrendingDown },
        { label: "Vol", value: (metrics.volatility * 100).toFixed(2) + "%", color: "text-gray-400", icon: Activity },
        { label: "Win%", value: (metrics.win_rate * 100).toFixed(1) + "%", color: "text-emerald-400", icon: Percent },
    ];

    // Calculate monthly returns for heatmap
    const monthlyReturns = calculateMonthlyReturns(equityCurve);

    return (
        <div className="space-y-3">
            {/* Compact Metrics Grid */}
            <div className="grid grid-cols-6 gap-2">
                {summaryItems.map((item) => {
                    const Icon = item.icon;
                    return (
                        <div key={item.label} className="bg-[#0A0A0A] border border-white/5 rounded p-2 text-center">
                            <div className="flex items-center justify-center gap-1 mb-1">
                                <Icon className="w-2.5 h-2.5 text-gray-700" />
                                <div className="text-[9px] text-gray-600 uppercase tracking-wider font-bold">{item.label}</div>
                            </div>
                            <div className={`text-xs font-bold font-mono ${item.color}`}>{item.value}</div>
                        </div>
                    );
                })}
            </div>

            {/* Strategy Breakdown - if multi-strategy */}
            {strategyBreakdown && Object.keys(strategyBreakdown).length > 1 && (
                <div className="bg-[#0A0A0A] border border-white/5 rounded p-3">
                    <h3 className="text-[10px] text-gray-600 uppercase tracking-wider font-bold mb-2">Strategy Performance</h3>
                    <div className="grid grid-cols-4 gap-2">
                        {Object.entries(strategyBreakdown).map(([name, perf]: [string, any]) => (
                            <div key={name} className="text-center p-2 bg-[#050505] rounded border border-white/5">
                                <div className="text-[9px] text-gray-500 mb-1">{name.replace('Strategy', '')}</div>
                                <div className={`text-xs font-mono font-bold ${perf.return > 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                                    {(perf.return * 100).toFixed(1)}%
                                </div>
                                <div className="text-[8px] text-gray-700 mt-0.5">SR: {perf.sharpe?.toFixed(2) || 'N/A'}</div>
                            </div>
                        ))}
                    </div>
                </div>
            )}

            {/* Equity Curve */}
            <div className="bg-[#0A0A0A] border border-white/5 rounded p-3">
                <h3 className="text-[10px] text-gray-600 uppercase tracking-wider font-bold mb-3">Portfolio Equity</h3>
                <div className="h-64 w-full">
                    <DynamicResponsiveContainer width="100%" height="100%">
                        <DynamicAreaChart data={equityCurve}>
                            <defs>
                                <linearGradient id="colorEquityPort" x1="0" y1="0" x2="0" y2="1">
                                    <stop offset="5%" stopColor="#8b5cf6" stopOpacity={0.3} />
                                    <stop offset="95%" stopColor="#8b5cf6" stopOpacity={0} />
                                </linearGradient>
                            </defs>
                            <DynamicArea
                                type="monotone"
                                dataKey="equity"
                                stroke="#06b6d4"
                                strokeWidth={1.5}
                                fillOpacity={1}
                            />
                        </DynamicAreaChart>
                    </DynamicResponsiveContainer>
                </div>
            </div>

            {/* Monthly Returns Heatmap */}
            {monthlyReturns.length > 0 && (
                <div className="bg-[#0A0A0A] border border-white/5 rounded p-3">
                    <h3 className="text-[10px] text-gray-600 uppercase tracking-wider font-bold mb-3">Monthly Returns</h3>
                    <div className="flex flex-wrap gap-1">
                        {monthlyReturns.map((m, idx) => (
                            <div
                                key={idx}
                                className={`px-2 py-1 rounded text-[9px] font-mono ${m.return > 0.05 ? 'bg-emerald-600/30 text-emerald-400' :
                                    m.return > 0 ? 'bg-emerald-600/10 text-emerald-500' :
                                        m.return < -0.05 ? 'bg-rose-600/30 text-rose-400' :
                                            'bg-rose-600/10 text-rose-500'
                                    }`}
                                title={m.month}
                            >
                                {m.month.substring(0, 3)}: {(m.return * 100).toFixed(1)}%
                            </div>
                        ))}
                    </div>
                </div>
            )}

            {/* Drawdown Chart */}
            <div className="bg-[#0A0A0A] border border-white/5 rounded p-3">
                <h3 className="text-[10px] text-gray-600 uppercase tracking-wider font-bold mb-3">Drawdown</h3>
                <div className="h-[120px] w-full">
                    <DynamicResponsiveContainer width="100%" height="100%">
                        <DynamicAreaChart data={equityCurve}>
                            <DynamicCartesianGrid strokeDasharray="3 3" stroke="#1e293b" vertical={false} strokeOpacity={0.1} />
                            <DynamicXAxis dataKey="date" hide />
                            <DynamicYAxis
                                stroke="#475569"
                                fontSize={9}
                                tickFormatter={(val: any) => (val * 100).toFixed(1) + "%"}
                                tick={{ fill: '#64748b' }}
                            />
                            <DynamicTooltip
                                formatter={(val: number | undefined) => val ? (val * 100).toFixed(2) + "%" : "0%"}
                                contentStyle={{
                                    backgroundColor: "#0A0A0A",
                                    border: "1px solid rgba(255,255,255,0.1)",
                                    borderRadius: "6px",
                                    fontSize: "10px"
                                }}
                            />
                            <DynamicArea
                                type="monotone"
                                dataKey="drawdown"
                                stroke="#f43f5e"
                                fill="#f43f5e"
                                fillOpacity={0.15}
                                strokeWidth={1.5}
                            />
                        </DynamicAreaChart>
                    </DynamicResponsiveContainer>
                </div>
            </div>
        </div>
    );
}

// Helper function to calculate monthly returns
function calculateMonthlyReturns(equityCurve: any[]): any[] {
    const monthlyData: Record<string, { start: number; end: number }> = {};

    equityCurve.forEach((point) => {
        const month = point.date.substring(0, 7); // YYYY-MM
        if (!monthlyData[month]) {
            monthlyData[month] = { start: point.equity, end: point.equity };
        }
        monthlyData[month].end = point.equity;
    });

    return Object.entries(monthlyData).map(([month, data]) => ({
        month,
        return: (data.end - data.start) / data.start
    }));
}
