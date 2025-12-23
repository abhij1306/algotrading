"use client";

import React from 'react';
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Copy, Edit2, Play, GitBranch, Clock, DollarSign, Activity, PieChart, Lock } from "lucide-react";

interface PortfolioDetailProps {
    portfolio: any;
}

export default function PortfolioDetail({ portfolio }: PortfolioDetailProps) {
    if (!portfolio) return null;

    // Handle "INDEX" pseudo-portfolios
    const isIndex = typeof portfolio.id === 'string' && portfolio.id.startsWith('INDEX:');
    const displayId = isIndex ? portfolio.id.split(':')[1] : portfolio.id;
    const displayName = isIndex ? displayId : portfolio.name;
    const displayDesc = isIndex ? "Standard Market Benchmark Index" : (portfolio.description || "Core equity portfolio comprising selected strategies filtered for liquidity and tradeability.");

    // Mock data for Index if needed
    const composition = portfolio.composition || [];

    return (
        <div className="h-full flex flex-col bg-[#09090b] text-gray-200 font-sans overflow-y-auto custom-scrollbar">
            {/* 1. Header Section */}
            <div className="px-8 py-6 border-b border-[#27272a] flex items-start justify-between bg-[#09090b]">
                <div>
                    <div className="flex items-center gap-3 mb-2">
                        <h1 className="text-2xl font-bold text-white tracking-tight">{displayName}</h1>
                        <Badge variant="outline" className={
                            isIndex ? "bg-cyan-500/10 text-cyan-400 border-cyan-500/20" :
                                portfolio.status === 'LIVE'
                                    ? "bg-green-500/10 text-green-400 border-green-500/20"
                                    : "bg-[#27272a] text-gray-400 border-[#3f3f46]"
                        }>
                            {isIndex ? 'BENCHMARK' : portfolio.status}
                        </Badge>
                    </div>
                    <p className="text-sm text-gray-400 max-w-2xl leading-relaxed">
                        {displayDesc}
                    </p>
                    <div className="flex items-center gap-6 mt-4 text-[10px] text-gray-500 uppercase tracking-widest font-mono">
                        <div className="flex items-center gap-1.5">
                            <span className="w-1.5 h-1.5 rounded-full bg-gray-600"></span>
                            ID: {isIndex ? 'SYS' : `P-${portfolio.id}`}
                        </div>
                        <div className="flex items-center gap-1.5">
                            <span className="w-1.5 h-1.5 rounded-full bg-gray-600"></span>
                            Owner: {isIndex ? 'SYSTEM' : 'Quant_Team_Alpha'}
                        </div>
                        <div className="flex items-center gap-1.5">
                            <Clock className="w-3 h-3 text-gray-600" />
                            Last Rebalance: {isIndex ? 'Real-time' : 'Today, 08:00 EST'}
                        </div>
                    </div>
                </div>

                {!isIndex && (
                    <div className="flex items-center gap-2">
                        <Button variant="outline" size="sm" className="h-9 border-[#3f3f46] text-gray-300 bg-[#18181b] hover:bg-[#27272a] hover:text-white">
                            <Clock className="w-4 h-4 mr-2" /> Changelog
                        </Button>
                        <Button variant="outline" size="sm" className="h-9 border-[#3f3f46] text-gray-300 bg-[#18181b] hover:bg-[#27272a] hover:text-white">
                            <Copy className="w-4 h-4 mr-2" /> Duplicate
                        </Button>
                        <Button size="sm" className="h-9 bg-blue-600 hover:bg-blue-500 text-white font-semibold">
                            <Edit2 className="w-4 h-4 mr-2" /> Edit Definition
                        </Button>
                    </div>
                )}
            </div>

            {/* 2. Metrics Grid */}
            <div className="grid grid-cols-3 gap-4 px-8 py-6">
                <MetricCard
                    label={isIndex ? "Constituents" : "Strategies"}
                    value={composition.length}
                    subValue={isIndex ? "Stocks" : "+0 added today"}
                    trend="up"
                    icon={<GitBranch className="w-8 h-8 text-gray-700" />}
                />
                <MetricCard
                    label={isIndex ? "Current Level" : "Total Capital"}
                    value={isIndex ? "24,500.00" : `₹${(portfolio.initial_capital || 100000).toLocaleString()}`}
                    subValue={isIndex ? "+0.45% Today" : "Avg: ₹84.9k"}
                    icon={<DollarSign className="w-8 h-8 text-gray-700" />}
                    color={isIndex ? "text-cyan-400" : undefined}
                />
                {/* Benchmark Card Removed */}
                <MetricCard
                    label="Utilization"
                    value={isIndex ? "100%" : "8 Strategies"}
                    subValue="View dependencies"
                    icon={<PieChart className="w-8 h-8 text-gray-700" />}
                />
            </div>

            {/* 3. Main Split View */}
            <div className="grid grid-cols-3 gap-6 px-8 pb-8 flex-1 min-h-0">

                {/* Left Col: Selection Logic (Visible only for non-Indices) */}
                {!isIndex && (
                    <div className="col-span-2 bg-[#09090b] border border-[#27272a] rounded-lg flex flex-col overflow-hidden">
                        <div className="px-5 py-3 border-b border-[#27272a] flex items-center justify-between bg-[#101012]">
                            <h3 className="text-xs font-bold text-gray-400 uppercase tracking-widest flex items-center gap-2">
                                <span className="text-blue-500 font-mono">{'<>'}</span> Selection Logic
                            </h3>
                            <span className="text-[10px] text-gray-600 font-mono">Python 3.10</span>
                        </div>
                        <div className="flex-1 p-5 bg-[#0c0c0e] font-mono text-sm overflow-auto text-gray-300 leading-relaxed">
                            <p className="text-purple-400">def <span className="text-blue-400">construct_portfolio</span>(date):</p>
                            <div className="pl-4 space-y-1 mt-1 text-gray-400">
                                <p># Initial Strategy Pool</p>
                                <p>strategies = <span className="text-yellow-400">StrategyStore</span>.get_active(region=<span className="text-green-400">'IN'</span>)</p>
                                <p className="mt-2"># Apply Constraints</p>
                                {composition.map((s: any, i: number) => (
                                    <p key={i}>
                                        portfolio.add(<span className="text-green-400">"{s.strategy_id}"</span>, weight=<span className="text-orange-400">{s.weight || 0.0}</span>)
                                    </p>
                                ))}
                                {composition.length === 0 && (
                                    <p className="text-gray-600 italic"># No strategies added yet</p>
                                )}
                                <p className="mt-2"># Rebalance</p>
                                <p>return portfolio.optimize(target=<span className="text-green-400">'sharpe'</span>)</p>
                            </div>
                        </div>
                        <div className="px-4 py-2 border-t border-[#27272a] bg-[#101012] flex items-center gap-2 text-[10px] text-green-500 font-mono">
                            <div className="w-2 h-2 rounded-full bg-green-500/20 border border-green-500"></div>
                            Logic verified and compiled successfully.
                        </div>
                    </div>
                )}

                {/* Right Col: Top Constituents (Expands if Index) */}
                <div className={`${isIndex ? 'col-span-3' : 'col-span-1'} bg-[#09090b] border border-[#27272a] rounded-lg flex flex-col overflow-hidden`}>
                    <div className="px-5 py-3 border-b border-[#27272a] flex items-center justify-between bg-[#101012]">
                        <h3 className="text-xs font-bold text-gray-400 uppercase tracking-widest">Constituents</h3>
                    </div>
                    <div className="flex-1 overflow-auto">
                        <table className="w-full text-left">
                            <thead className="bg-[#101012] sticky top-0">
                                <tr className="text-[9px] text-gray-500 uppercase tracking-wider border-b border-[#27272a]">
                                    <th className="px-5 py-2 font-semibold">Name</th>
                                    <th className="px-5 py-2 font-semibold text-right">Weight</th>
                                </tr>
                            </thead>
                            <tbody className="divide-y divide-[#27272a]">
                                {composition.map((s: any, i: number) => (
                                    <tr key={i} className="group hover:bg-[#27272a]/30 transition-colors">
                                        <td className="px-5 py-3">
                                            <div className="text-xs font-bold text-gray-300">{s.strategy_id}</div>
                                        </td>
                                        <td className="px-5 py-3 text-right">
                                            <div className="text-xs font-mono text-green-400">{(parseFloat(s.weight)).toFixed(2)}%</div>
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                    <div className="px-4 py-2 border-t border-[#27272a] bg-[#101012] text-center text-[10px] text-gray-600">
                        Displaying {composition.length} assets
                    </div>
                </div>

            </div>

            {/* 4. Actions / Footer */}
            <div className="px-8 pb-8">
                <h3 className="text-sm font-bold text-gray-200 mb-4">Actions</h3>
                <div className="flex gap-4">
                    {isIndex ? (
                        <div className="p-4 bg-[#101012] border border-[#27272a] rounded-lg flex-1 flex items-center justify-center opacity-75">
                            <span className="text-xs text-gray-500 font-bold flex items-center gap-2">
                                <Lock className="w-4 h-4" /> System Managed Index (Standard)
                            </span>
                        </div>
                    ) : (
                        <>
                            <div className="p-4 bg-[#101012] border border-[#27272a] rounded-lg flex-1 hover:border-blue-500/50 transition-colors cursor-pointer group">
                                <div className="flex items-center gap-3 mb-2">
                                    <div className="w-2 h-2 rounded-full bg-blue-500"></div>
                                    <h4 className="font-bold text-white text-sm group-hover:text-blue-400">Run Backtest</h4>
                                </div>
                                <div className="w-full bg-[#27272a] h-1.5 rounded-full overflow-hidden">
                                    <div className="bg-blue-600 h-full w-[0%] group-hover:w-[100%] transition-all duration-1000"></div>
                                </div>
                                <p className="text-[10px] text-gray-500 mt-2 font-mono">Last run: Never</p>
                            </div>

                            <div className="p-4 bg-[#101012] border border-[#27272a] rounded-lg flex-1 hover:border-purple-500/50 transition-colors cursor-pointer border-dashed flex items-center justify-center">
                                <span className="text-xs text-gray-500 font-bold flex items-center gap-2">
                                    <Button variant="ghost" className="hover:bg-transparent hover:text-white p-0 h-auto">
                                        + Add Strategy
                                    </Button>
                                </span>
                            </div>
                        </>
                    )}
                </div>
            </div>
        </div>
    );
}

function MetricCard({ label, value, subValue, trend, icon, color }: any) {
    return (
        <div className="bg-[#101012] border border-[#27272a] rounded-lg p-5 relative overflow-hidden group hover:border-[#3f3f46] transition-colors">
            <div className="absolute right-4 top-4 opacity-10 group-hover:opacity-20 transition-opacity">
                {icon}
            </div>
            <p className="text-[10px] font-bold text-gray-500 uppercase tracking-widest mb-2">{label}</p>
            <div className="flex items-baseline gap-2">
                <span className={`text-2xl font-bold tracking-tight ${color || 'text-white'}`}>{value}</span>
                {trend === 'up' && <span className="text-[10px] text-green-500 font-bold">+2</span>}
            </div>
            <p className="text-[10px] text-gray-600 mt-1 font-mono">{subValue}</p>
        </div>
    )
}
