'use client'

import React, { useEffect, useState } from "react";
import {
    DynamicAreaChart,
    DynamicArea,
    DynamicXAxis,
    DynamicYAxis,
    DynamicCartesianGrid,
    DynamicTooltip,
    DynamicResponsiveContainer,
    DynamicBarChart,
    DynamicBar,
    DynamicReferenceLine,
    DynamicScatter,
    DynamicCell,
    DynamicLineChart,
    DynamicLine
} from "@/components/DynamicCharts";
import { TrendingUp, AlertOctagon, Timer, BarChart, ChevronDown } from "lucide-react";

interface ResearchDashboardProps {
    runId: string;
}

export default function ResearchDashboard({ runId }: ResearchDashboardProps) {
    const [data, setData] = useState<any>(null);
    const [loading, setLoading] = useState(true);
    const [selectedStrategy, setSelectedStrategy] = useState<string | null>(null);

    useEffect(() => {
        if (!runId) return;

        setLoading(true);
        fetch(`http://localhost:8000/api/portfolio/research-data/${runId}`)
            .then(res => res.json())
            .then(json => {
                setData(json);
                // Auto-select first strategy if available
                if (json.strategies && Object.keys(json.strategies).length > 0) {
                    setSelectedStrategy(Object.keys(json.strategies)[0]);
                }
                setLoading(false);
            })
            .catch(err => {
                console.error("Research data fetch failed", err);
                setLoading(false);
            });
    }, [runId]);

    if (loading) {
        return (
            <div className="space-y-6 animate-pulse">
                <div className="flex justify-between">
                    <div className="h-8 w-48 bg-white/5 rounded" />
                    <div className="h-8 w-32 bg-white/5 rounded" />
                </div>
                <div className="h-[250px] w-full bg-white/5 rounded-lg border border-white/5" />
                <div className="grid grid-cols-2 gap-6">
                    <div className="h-56 bg-white/5 rounded-lg" />
                    <div className="h-56 bg-white/5 rounded-lg" />
                </div>
            </div>
        );
    }

    if (!data || !data.portfolio) {
        return <div className="text-center py-20 text-gray-500">No data available.</div>;
    }

    // Format data for Recharts
    const portfolioChartData = data.dates.map((date: string, i: number) => ({
        date,
        equity: data.portfolio.equity[i],
        drawdown: data.portfolio.drawdown[i] * 100
    }));

    const maxDD = data.portfolio.metrics.max_drawdown * 100;
    const strategyList = Object.keys(data.strategies || {});
    const selectedStrategyData = selectedStrategy && data.strategies[selectedStrategy];

    // Strategy chart data
    let strategyChartData = null;
    if (selectedStrategyData) {
        strategyChartData = data.dates.slice(0, selectedStrategyData.equity.length).map((date: string, i: number) => ({
            date,
            equity: selectedStrategyData.equity[i],
            drawdown: selectedStrategyData.drawdown[i]
        }));
    }

    return (
        <div className="space-y-6">
            {/* Header */}
            <div className="flex items-center justify-between">
                <div>
                    <h2 className="text-lg font-medium text-white">Research Analysis</h2>
                    <p className="text-xs text-gray-500 mt-1">Historical behavior and risk metrics • Run {runId.slice(0, 8)}</p>
                </div>

                {/* Strategy Selector */}
                {strategyList.length > 0 && (
                    <div className="relative">
                        <select
                            value={selectedStrategy || ""}
                            onChange={(e) => setSelectedStrategy(e.target.value)}
                            className="px-4 py-2 bg-[#0A0A0A] border border-white/10 rounded text-sm text-gray-200 pr-8 appearance-none cursor-pointer hover:border-white/20 transition-colors"
                        >
                            <option value="" disabled>Select Strategy</option>
                            {strategyList.map(id => (
                                <option key={id} value={id}>{id.replace(/_/g, ' ')}</option>
                            ))}
                        </select>
                        <ChevronDown className="absolute right-2 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-500 pointer-events-none" />
                    </div>
                )}
            </div>

            {/* Portfolio Overview */}
            <div className="bg-[#0A0A0A] border border-white/5 rounded-lg p-5">
                <h3 className="text-xs font-bold text-gray-500 uppercase tracking-wider mb-4 flex items-center gap-2">
                    <TrendingUp className="w-4 h-4" /> Portfolio Equity Curve
                </h3>
                <div className="h-[250px] w-full">
                    <DynamicResponsiveContainer width="100%" height="100%">
                        <DynamicAreaChart data={portfolioChartData}>
                            <defs>
                                <linearGradient id="colorEquity" x1="0" y1="0" x2="0" y2="1">
                                    <stop offset="5%" stopColor="#8b5cf6" stopOpacity={0.3} />
                                    <stop offset="95%" stopColor="#8b5cf6" stopOpacity={0} />
                                </linearGradient>
                            </defs>
                            <DynamicCartesianGrid strokeDasharray="3 3" stroke="#252932" />
                            <DynamicXAxis
                                dataKey="date"
                                stroke="#6b7280"
                                fontSize={12}
                                tickLine={false}
                                axisLine={false}
                            />
                            <DynamicYAxis
                                stroke="#6b7280"
                                fontSize={12}
                                tickLine={false}
                                axisLine={false}
                                tickFormatter={(val: any) => `₹${(val / 100000).toFixed(1)}L`}
                            />
                            <DynamicTooltip
                                contentStyle={{ backgroundColor: '#1f2937', border: 'none', borderRadius: '0.5rem' }}
                                itemStyle={{ color: '#8b5cf6' }}
                            />
                            <DynamicArea
                                type="monotone"
                                dataKey="equity"
                                stroke="#8b5cf6"
                                fillOpacity={1}
                                fill="url(#colorEquity)"
                            />
                        </DynamicAreaChart>
                    </DynamicResponsiveContainer>
                </div>
                <div className="mt-4 grid grid-cols-2 gap-4 text-sm">
                    <div>
                        <div className="text-gray-500 text-xs mb-1">Max Drawdown</div>
                        <div className="text-rose-400 font-mono font-bold">{maxDD.toFixed(2)}%</div>
                    </div>
                    <div>
                        <div className="text-gray-500 text-xs mb-1">Final Equity</div>
                        <div className="text-cyan-400 font-mono font-bold">₹{data.portfolio.metrics.current_equity.toFixed(0)}</div>
                    </div>
                </div>
            </div>

            {/* Strategy-Specific Analysis */}
            {selectedStrategyData && strategyChartData && (
                <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
                    {/* Strategy Equity */}
                    <div className="bg-[#0A0A0A] border border-white/5 rounded-lg p-5">
                        <h3 className="text-xs font-bold text-gray-500 uppercase tracking-wider mb-4">
                            {selectedStrategy?.replace(/_/g, ' ')} - Equity
                        </h3>
                        <div className="h-56">
                            <DynamicResponsiveContainer width="100%" height="100%">
                                <DynamicLineChart data={strategyChartData}>
                                    <DynamicCartesianGrid strokeDasharray="3 3" stroke="#ffffff10" vertical={false} />
                                    <DynamicXAxis dataKey="date" stroke="#525252" fontSize={10} hide />
                                    <DynamicYAxis stroke="#525252" fontSize={10} tickFormatter={(val: any) => `₹${(val / 1000).toFixed(0)}k`} />
                                    <DynamicTooltip contentStyle={{ backgroundColor: '#000', borderColor: '#333', fontSize: 11 }} />
                                    <DynamicLine type="monotone" dataKey="equity" stroke="#10b981" strokeWidth={2} dot={false} />
                                </DynamicLineChart>
                            </DynamicResponsiveContainer>
                        </div>
                    </div>

                    {/* Strategy Drawdown */}
                    <div className="bg-[#0A0A0A] border border-white/5 rounded-lg p-5">
                        <h3 className="text-xs font-bold text-gray-500 uppercase tracking-wider mb-4 flex items-center gap-2">
                            <BarChart className="w-4 h-4 text-rose-500" /> Drawdown Profile
                        </h3>
                        <div className="h-56">
                            <DynamicResponsiveContainer width="100%" height="100%">
                                <DynamicAreaChart data={strategyChartData}>
                                    <DynamicCartesianGrid strokeDasharray="3 3" stroke="#ffffff10" vertical={false} />
                                    <DynamicXAxis dataKey="date" stroke="#525252" fontSize={10} hide />
                                    <DynamicYAxis stroke="#525252" fontSize={10} tickFormatter={(val: any) => `${val.toFixed(1)}%`} />
                                    <DynamicTooltip
                                        contentStyle={{ backgroundColor: '#000', borderColor: '#333', fontSize: 11 }}
                                        formatter={(val: any) => [`${val.toFixed(2)}%`, 'DD']}
                                    />
                                    <DynamicArea
                                        type="step"
                                        dataKey="drawdown"
                                        stroke="#f43f5e"
                                        fill="#f43f5e"
                                        fillOpacity={0.2}
                                        strokeWidth={2}
                                    />
                                </DynamicAreaChart>
                            </DynamicResponsiveContainer>
                        </div>
                    </div>

                    {/* Risk Summary */}
                    <div className="lg:col-span-2 bg-[#0A0A0A] border border-white/5 rounded-lg p-5">
                        <h3 className="text-xs font-bold text-gray-500 uppercase tracking-wider mb-4 flex items-center gap-2">
                            <AlertOctagon className="w-4 h-4 text-orange-400" /> Risk Metrics
                        </h3>
                        <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
                            <div>
                                <div className="text-xs text-gray-500 mb-1">Max DD</div>
                                <div className="text-lg font-mono text-rose-400 font-bold">
                                    {selectedStrategyData.metrics.max_drawdown.toFixed(2)}%
                                </div>
                            </div>
                            <div>
                                <div className="text-xs text-gray-500 mb-1">95th %ile DD</div>
                                <div className="text-lg font-mono text-orange-400 font-bold">
                                    {selectedStrategyData.metrics.p95_dd.toFixed(2)}%
                                </div>
                            </div>
                            <div>
                                <div className="text-xs text-gray-500 mb-1">Longest DD</div>
                                <div className="text-lg font-mono text-gray-300 font-bold">
                                    {selectedStrategyData.metrics.longest_dd_days} days
                                </div>
                            </div>
                            <div>
                                <div className="text-xs text-gray-500 mb-1 flex items-center gap-1">
                                    <Timer className="w-3 h-3" /> DD Speed
                                </div>
                                <div className={`text-lg font-bold ${selectedStrategyData.metrics.dd_speed === 'FAST' ? 'text-rose-400' : 'text-yellow-400'}`}>
                                    {selectedStrategyData.metrics.dd_speed}
                                </div>
                            </div>
                            <div>
                                <div className="text-xs text-gray-500 mb-1">Portfolio Corr</div>
                                <div className="text-lg font-mono text-cyan-400 font-bold">
                                    {selectedStrategyData.metrics.correlation_to_portfolio.toFixed(2)}
                                </div>
                            </div>
                        </div>
                        <div className="mt-4 p-3 bg-white/5 border border-white/10 rounded text-sm text-gray-300">
                            {selectedStrategyData.metrics.dd_speed === 'FAST' ? (
                                <>This strategy has a <span className="text-rose-400 font-bold">FAST</span> drawdown profile - drops sharply but may recover slowly. Use tighter risk limits.</>
                            ) : (
                                <>This strategy has a <span className="text-yellow-400 font-bold">SLOW</span> drawdown profile - gradual deterioration with longer recovery. Monitor trend breaks.</>
                            )}
                        </div>
                    </div>
                </div>
            )}

            {/* No Strategies */}
            {strategyList.length === 0 && (
                <div className="text-center py-12 text-gray-600 italic">
                    No per-strategy breakdowns available for this run.
                </div>
            )}
        </div>
    );
}
