'use client'

import React, { useState, useEffect, useRef } from 'react';
import { AreaChart, Area, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from 'recharts';
import { ChevronDown } from 'lucide-react';

interface RiskDashboardProps {
    portfolioId: number;
    portfolios?: any[];
    onSelectPortfolio?: (id: number) => void;
}

export default function PortfolioRiskDashboard({ portfolioId, portfolios = [], onSelectPortfolio }: RiskDashboardProps) {
    const [portfolio, setPortfolio] = useState<any>(null);
    const [riskAnalysis, setRiskAnalysis] = useState<any>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [chartData, setChartData] = useState<any[]>([]);
    const [timeRange, setTimeRange] = useState<'1M' | '6M' | '1Y'>('1Y'); // New State
    const fileInputRef = useRef<HTMLInputElement>(null);

    useEffect(() => {
        if (portfolioId) {
            loadPortfolioData();
        }
    }, [portfolioId, timeRange]); // Reload when range changes

    const loadPortfolioData = async () => {
        try {
            setLoading(true);
            setError('');
            const portfolioRes = await fetch(`http://localhost:8000/api/portfolios/${portfolioId}`);
            if (!portfolioRes.ok) throw new Error('Failed to load portfolio');
            const portfolioData = await portfolioRes.json();
            setPortfolio(portfolioData);

            if (portfolioData.positions && portfolioData.positions.length > 0) {
                const lookbackDays = timeRange === '1M' ? 30 : timeRange === '6M' ? 180 : 365;
                const riskRes = await fetch(`http://localhost:8000/api/portfolios/${portfolioId}/analyze?lookback_days=${lookbackDays}`, {
                    method: 'POST'
                });
                if (!riskRes.ok) throw new Error('Failed to analyze portfolio');
                const riskData = await riskRes.json();
                setRiskAnalysis(riskData);

                // Process Chart Data
                if (riskData.charts?.performance?.dates?.length > 0) {
                    const dates = riskData.charts.performance.dates;
                    const pRet = riskData.charts.performance.portfolioReturns;
                    const bRet = riskData.charts.performance.benchmarkReturns;

                    const formatted = dates.map((d: string, i: number) => ({
                        date: d,
                        value: pRet[i] || 0,
                        benchmark: bRet[i] || 0
                    }));
                    setChartData(formatted);
                } else {
                    // No chart data available
                    setChartData([]);
                }

            } else {
                setRiskAnalysis(null); // No positions to analyze
            }

        } catch (err: any) {
            setError(err.message || 'Failed to load data');
        } finally {
            setLoading(false);
        }
    };

    const handleImportClick = () => {
        fileInputRef.current?.click();
    };

    const handleFileChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
        const file = e.target.files?.[0];
        if (!file) return;

        const reader = new FileReader();
        reader.onload = async (event) => {
            try {
                const text = event.target?.result as string;
                // Basic CSV Parser: Symbol,Quantity,BuyPrice
                const lines = text.split('\n');
                const positions = lines.slice(1).map(line => {
                    const [symbol, qty, price] = line.split(',').map(s => s.trim());
                    if (!symbol) return null;
                    return {
                        symbol: symbol.toUpperCase(),
                        quantity: parseFloat(qty) || 0,
                        avg_buy_price: parseFloat(price) || 0,
                        invested_value: (parseFloat(qty) || 0) * (parseFloat(price) || 0)
                    };
                }).filter(p => p !== null);

                if (positions.length === 0) throw new Error("No valid positions found in CSV");

                // Create new portfolio with these positions (or add to current if endpoint supported adding)
                // For now, we'll create a NEW portfolio named "Imported Portfolio"
                const res = await fetch('http://localhost:8000/api/portfolios', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        portfolio_name: `Imported_${new Date().toISOString().split('T')[0]}`,
                        positions: positions
                    })
                });

                if (!res.ok) throw new Error("Failed to import portfolio");

                // Reload to show the new portfolio (In a real app, we'd switch the view ID)
                alert("Portfolio Imported! Please select it from the menu.");
                window.location.reload();

            } catch (err: any) {
                alert(`Import Failed: ${err.message}`);
            }
        };
        reader.readAsText(file);
    };

    if (loading) return (
        <div className="flex h-full items-center justify-center text-white">
            <div className="flex flex-col items-center gap-4">
                <div className="h-8 w-8 animate-spin rounded-full border-2 border-cyan-500 border-t-transparent shadow-[0_0_15px_rgba(6,182,212,0.5)]"></div>
                <p className="text-cyan-400 font-medium tracking-wider text-[10px] animate-pulse">ANALYZING PORTFOLIO RISK...</p>
            </div>
        </div>
    );

    if (error) return (
        <div className="flex h-full items-center justify-center">
            <div className="bg-red-500/5 border border-red-500/20 p-4 rounded-xl max-w-sm w-full backdrop-blur-md">
                <p className="text-red-400 text-xs font-mono mb-2">{error}</p>
                <button onClick={loadPortfolioData} className="w-full py-1.5 rounded bg-red-500/10 hover:bg-red-500/20 text-red-500 text-xs font-bold transition-all">TRY AGAIN</button>
            </div>
        </div>
    );

    if (!portfolio) return null;

    // Data Processing
    const positions = portfolio.positions || [];
    let totalInvested = 0;
    let totalCurrentValue = 0;

    positions.forEach((pos: any) => {
        const qty = parseFloat(pos.quantity) || 0;
        const avg = parseFloat(pos.avg_buy_price || pos.average_price) || 0;
        const ltp = parseFloat(pos.ltp) || avg;
        totalInvested += qty * avg;
        totalCurrentValue += qty * ltp;
    });

    const totalUnrealizedPnL = totalCurrentValue - totalInvested;
    const unrealizedPnLPct = totalInvested > 0 ? (totalUnrealizedPnL / totalInvested) * 100 : 0;
    const marketRisk = riskAnalysis?.market_risk || {};
    const sectorData = riskAnalysis?.charts?.sectors || [];

    return (
        <div className="flex h-full flex-col font-sans text-gray-200">
            {/* Hidden File Input */}
            <input
                type="file"
                ref={fileInputRef}
                onChange={handleFileChange}
                className="hidden"
                accept=".csv"
            />

            {/* Compact Header */}
            <div className="flex items-center justify-between px-4 py-2 border-b border-white/5 bg-[#050505]/30 h-12">
                <div className="flex items-center gap-3">
                    <div className="p-1.5 bg-gradient-to-br from-cyan-500/10 to-purple-500/10 border border-white/5 rounded-md">
                        <span className="material-symbols-outlined text-cyan-400 text-sm">analytics</span>
                    </div>
                    <div>
                        <div>
                            {/* Portfolio Switcher in Header */}
                            <div className="relative group">
                                <button className="flex items-center gap-2 text-sm font-bold text-white hover:text-cyan-400 transition-colors">
                                    {portfolio.portfolio_name}
                                    <ChevronDown className="w-3 h-3 text-gray-500 group-hover:text-cyan-400" />
                                </button>
                                {/* Dropdown */}
                                <select
                                    className="absolute inset-0 w-full h-full opacity-0 cursor-pointer"
                                    value={portfolioId}
                                    onChange={(e) => onSelectPortfolio && onSelectPortfolio(parseInt(e.target.value))}
                                >
                                    {portfolios.map((p: any) => (
                                        <option key={p.id} value={p.id}>{p.portfolio_name}</option>
                                    ))}
                                </select>
                            </div>
                            <p className="text-gray-500 text-[10px] font-mono tracking-wide">RISK INTELLIGENCE</p>
                        </div>
                    </div>
                </div>
                <div className="flex gap-2">
                    <button
                        onClick={handleImportClick}
                        className="flex items-center gap-1.5 px-3 py-1 bg-[#0A0A0A] border border-white/10 rounded-md hover:bg-white/5 transition-colors text-[10px] font-bold text-gray-400 hover:text-white tracking-wide"
                    >
                        <span className="material-symbols-outlined text-[14px]">file_upload</span>
                        IMPORT CSV
                    </button>
                    <button className="flex items-center gap-1.5 px-3 py-1 bg-gradient-to-r from-cyan-600 to-cyan-500 hover:from-cyan-500 hover:to-cyan-400 rounded-md text-white text-[10px] font-bold shadow-lg shadow-cyan-500/20 transition-all tracking-wide">
                        <span className="material-symbols-outlined text-[14px]">add</span>
                        ADD TRADE
                    </button>
                </div>
            </div>

            <div className="flex-1 overflow-y-auto p-4 space-y-4 scrollbar-thin scrollbar-thumb-white/10 scrollbar-track-transparent">

                {/* Dense Stats Grid */}
                <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
                    <StatsCard
                        title="PORTFOLIO VALUE"
                        value={`₹${totalCurrentValue.toLocaleString('en-IN', { maximumFractionDigits: 0 })}`}
                        change={unrealizedPnLPct.toFixed(2)}
                        icon="account_balance_wallet"
                        color="cyan"
                    />
                    <StatsCard
                        title="NET P&L"
                        value={`₹${totalUnrealizedPnL.toLocaleString('en-IN', { maximumFractionDigits: 0 })}`}
                        change={unrealizedPnLPct.toFixed(2)}
                        icon="savings"
                        color="purple"
                    />
                    <StatsCard
                        title="SHARPE RATIO"
                        value={marketRisk.sharpe_ratio?.toFixed(2) || 'N/A'}
                        change={0} // Placeholder
                        icon="show_chart"
                        color="green"
                    />
                    <StatsCard
                        title="ANNUAL VOLATILITY"
                        value={marketRisk.portfolio_volatility ? `${(marketRisk.portfolio_volatility * 100).toFixed(1)}%` : 'N/A'}
                        change={0}
                        icon="pie_chart"
                        color="orange"
                    />
                </div>

                {/* Main Content Grid */}
                <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
                    {/* Performance Chart Placeholder */}
                    <div className="lg:col-span-2 rounded-xl border border-white/5 bg-[#0A0A0A]/50 backdrop-blur-md p-4 h-[300px] flex flex-col relative overflow-hidden group">
                        <div className="flex justify-between items-start mb-4 relative z-10">
                            <div>
                                <h3 className="text-xs font-bold text-gray-300 uppercase tracking-wider">Performance Curve</h3>
                                <p className="text-gray-600 text-[10px] font-mono mt-0.5">BENCHMARK: NIFTY 50</p>
                            </div>
                            <div className="flex gap-1 bg-[#050505] p-0.5 rounded border border-white/5">
                                {['1M', '6M', '1Y'].map(period => (
                                    <button key={period} className={`px-2 py-0.5 rounded text-[10px] font-bold transition-colors ${period === '1Y' ? 'bg-white/10 text-white shadow-sm' : 'text-gray-500 hover:text-white'}`}>
                                        {period}
                                    </button>
                                ))}
                            </div>
                        </div>
                        <div className="flex-1 w-full h-full relative z-10">
                            {chartData.length > 0 ? (
                                <ResponsiveContainer width="100%" height="100%">
                                    <AreaChart data={chartData}>
                                        <defs>
                                            <linearGradient id="colorValue" x1="0" y1="0" x2="0" y2="1">
                                                <stop offset="5%" stopColor="#06b6d4" stopOpacity={0.3} />
                                                <stop offset="95%" stopColor="#06b6d4" stopOpacity={0} />
                                            </linearGradient>
                                        </defs>
                                        <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" vertical={false} />
                                        <XAxis
                                            dataKey="date"
                                            hide={false}
                                            tick={{ fill: '#6b7280', fontSize: 10 }}
                                            axisLine={false}
                                            tickLine={false}
                                            minTickGap={30}
                                            tickFormatter={(val) => {
                                                const d = new Date(val);
                                                return `${d.getDate()} ${d.toLocaleString('default', { month: 'short' })}`;
                                            }}
                                        />
                                        <YAxis
                                            hide={false}
                                            orientation="right"
                                            tick={{ fill: '#6b7280', fontSize: 10 }}
                                            axisLine={false}
                                            tickLine={false}
                                            tickFormatter={(val) => `${val.toFixed(0)}%`}
                                        />
                                        <Tooltip
                                            contentStyle={{ backgroundColor: '#0A0A0A', borderColor: 'rgba(255,255,255,0.1)', borderRadius: '8px' }}
                                            itemStyle={{ color: '#fff', fontSize: '12px' }}
                                            labelStyle={{ color: '#9ca3af', fontSize: '10px', marginBottom: '4px' }}
                                            formatter={(value: any) => [`${parseFloat(value).toFixed(2)}%`, 'Return']}
                                        />
                                        <Area
                                            type="monotone"
                                            dataKey="value"
                                            stroke="#06b6d4"
                                            fillOpacity={1}
                                            fill="url(#colorValue)"
                                            strokeWidth={2}
                                            name="Portfolio"
                                        />
                                        <Area
                                            type="monotone"
                                            dataKey="benchmark"
                                            stroke="#6b7280"
                                            fill="transparent"
                                            strokeDasharray="5 5"
                                            strokeWidth={1}
                                            name="Nifty 50"
                                        />
                                    </AreaChart>
                                </ResponsiveContainer>
                            ) : (
                                <div className="flex items-center justify-center h-full">
                                    <p className="text-gray-600 text-[10px] font-mono">NO DATA AVAILABLE</p>
                                </div>
                            )}
                        </div>
                    </div>

                    {/* Sector Allocation - Dynamic */}
                    <div className="rounded-xl border border-white/5 bg-[#0A0A0A]/50 backdrop-blur-md p-4 flex flex-col relative overflow-hidden">
                        <h3 className="text-xs font-bold text-gray-300 uppercase tracking-wider mb-1">Sector Allocation</h3>
                        <p className="text-gray-600 text-[10px] font-mono mb-4">DIVERSIFICATION SCORE: {marketRisk.concentration ? (10 - marketRisk.concentration * 10).toFixed(1) : 'N/A'}/10</p>

                        <div className="flex-1 flex flex-col gap-3 overflow-y-auto pr-1 scrollbar-thin scrollbar-thumb-white/10">
                            {sectorData.length > 0 ? (
                                sectorData.map((s: any, idx: number) => (
                                    <SectorBar
                                        key={idx}
                                        name={s.name}
                                        percent={s.allocation}
                                        color={idx % 2 === 0 ? 'bg-cyan-500' : 'bg-purple-500'}
                                    />
                                ))
                            ) : (
                                <div className="text-center py-8 text-gray-600 text-[10px]">NO SECTOR DATA</div>
                            )}
                        </div>
                    </div>
                </div>

                {/* Risk Matrix & Stats */}
                <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
                    <div className="lg:col-span-2 rounded-xl border border-white/5 bg-[#0A0A0A]/50 backdrop-blur-md p-4">
                        <div className="flex justify-between items-center mb-4">
                            <h3 className="text-xs font-bold text-gray-300 uppercase tracking-wider">Risk Matrix</h3>
                        </div>
                        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                            {/* Matrix Visual Placeholder */}
                            <div className="aspect-[21/9] bg-[#050505] rounded-lg border border-white/5 relative p-2">
                                <div className="absolute inset-0 flex flex-col p-2">
                                    <div className="flex-1 flex border-b border-white/5">
                                        <div className="flex-1 border-r border-white/5 relative bg-red-500/5">
                                            <span className="absolute top-1 left-1 text-[8px] font-bold text-red-500/50 uppercase">HIGH</span>
                                        </div>
                                        <div className="flex-1 relative"></div>
                                    </div>
                                    <div className="flex-1 flex">
                                        <div className="flex-1 border-r border-white/5 relative"></div>
                                        <div className="flex-1 relative bg-green-500/5">
                                            <span className="absolute bottom-1 right-1 text-[8px] font-bold text-green-500/50 uppercase">LOW</span>
                                        </div>
                                    </div>
                                </div>
                                <div className="absolute inset-0 flex items-center justify-center">
                                    <div className="h-3 w-3 rounded-full bg-cyan-500 shadow-[0_0_15px_rgba(6,182,212,0.6)] border border-white animate-pulse" title="Portfolio Position"></div>
                                </div>
                            </div>

                            {/* Key Metrics */}
                            <div className="flex flex-col justify-center gap-4">
                                <RiskMetricRow
                                    label="PORTFOLIO BETA"
                                    value={marketRisk.beta?.toFixed(2) || 'N/A'}
                                    status={marketRisk.beta > 1.2 ? "HIGH VOL" : "MODERATE"}
                                    statusColor={marketRisk.beta > 1.2 ? "text-orange-400" : "text-cyan-400"}
                                />
                                <hr className="border-white/5" />
                                <RiskMetricRow
                                    label="VAR (95%)"
                                    value={marketRisk.var_95 ? `${(marketRisk.var_95 * 100).toFixed(2)}%` : 'N/A'}
                                    status="DOWNSIDE RISK"
                                    statusColor="text-red-400"
                                />
                            </div>
                        </div>
                    </div>

                    {/* Holdings Summary */}
                    <div className="rounded-xl border border-white/5 bg-[#0A0A0A]/50 backdrop-blur-md p-4 flex flex-col">
                        <div className="flex items-center justify-between mb-2">
                            <h3 className="text-xs font-bold text-gray-300 uppercase tracking-wider">Top Holdings</h3>
                        </div>    <div className="flex-1 overflow-y-auto max-h-[200px] hide-scrollbar space-y-0.5">
                            {positions.slice(0, 5).map((pos: any, idx: number) => (
                                <div key={idx} className="flex justify-between items-center py-2 border-b border-white/5 last:border-0 hover:bg-white/5 px-2 rounded transition-colors cursor-pointer">
                                    <div>
                                        <div className="font-bold text-xs text-gray-200">{pos.symbol}</div>
                                        <div className="text-[10px] text-gray-600 font-mono">{(pos.allocation_pct || 0).toFixed(1)}%</div>
                                    </div>
                                    <div className="text-right">
                                        <div className="font-bold text-xs text-gray-300">₹{parseFloat(pos.invested_value).toLocaleString('en-IN', { maximumFractionDigits: 0 })}</div>
                                        {pos.pnl && (
                                            <div className={`text-[10px] font-bold ${pos.pnl >= 0 ? 'text-green-500' : 'text-red-500'}`}>
                                                {pos.pnl >= 0 ? '+' : ''}{((pos.pnl / pos.invested_value) * 100).toFixed(1)}%
                                            </div>
                                        )}
                                    </div>
                                </div>
                            ))}
                        </div>
                    </div>
                </div>
            </div>
        </div>
    );
}

// Components
function StatsCard({ title, value, change, icon, color }: any) {
    const isPos = parseFloat(change) >= 0;
    const colorClasses = {
        cyan: 'text-cyan-400 bg-cyan-500/10 border-cyan-500/20',
        purple: 'text-purple-400 bg-purple-500/10 border-purple-500/20',
        green: 'text-green-400 bg-green-500/10 border-green-500/20',
        orange: 'text-orange-400 bg-orange-500/10 border-orange-500/20'
    }[color as string] || 'text-gray-400 bg-gray-500/10 border-gray-500/20';

    return (
        <div className="flex flex-col gap-1 rounded-lg p-3 bg-[#0A0A0A]/50 backdrop-blur-md border border-white/5 relative overflow-hidden group hover:border-white/10 transition-colors">
            <div className={`absolute -right-4 -top-4 w-16 h-16 rounded-full blur-xl opacity-10 bg-${color}-500 group-hover:opacity-20 transition-all`}></div>
            <div className="flex justify-between items-start z-10">
                <p className="text-gray-500 text-[10px] font-bold uppercase tracking-widest">{title}</p>
                <span className={`material-symbols-outlined text-base opacity-50 ${colorClasses.split(' ')[0]}`}>{icon}</span>
            </div>
            <div className="flex items-end gap-2 mt-1 z-10">
                <p className="text-lg font-bold tracking-tight text-white font-mono">{value}</p>
                {change !== 0 && (
                    <span className={`flex items-center text-[9px] font-bold px-1 py-0.5 rounded mb-0.5 ${isPos ? 'text-green-400 bg-green-500/10' : 'text-red-400 bg-red-500/10'}`}>
                        {isPos ? '+' : ''}{change}%
                    </span>
                )}
            </div>
        </div>
    )
}

function SectorBar({ name, percent, color }: any) {
    return (
        <div className="group w-full">
            <div className="flex justify-between text-[10px] font-bold uppercase tracking-wider mb-1">
                <span className="text-gray-400 truncate max-w-[100px]">{name}</span>
                <span className="text-gray-500 font-mono">{percent}%</span>
            </div>
            <div className="h-1 bg-[#050505] rounded-full overflow-hidden border border-white/5">
                <div className={`h-full ${color} rounded-full shadow-[0_0_5px_rgba(255,255,255,0.2)]`} style={{ width: `${percent}%` }}></div>
            </div>
        </div>
    )
}

function RiskMetricRow({ label, value, status, statusColor }: any) {
    return (
        <div>
            <div className="flex justify-between items-center mb-0.5">
                <span className="text-gray-500 text-[10px] font-bold uppercase tracking-wider">{label}</span>
                <span className="text-gray-200 font-bold bg-white/5 px-1.5 py-0.5 rounded border border-white/10 font-mono text-xs">{value}</span>
            </div>
            {status && (
                <div className={`text-[9px] font-bold uppercase tracking-wide ${statusColor} flex items-center gap-1 mt-0.5`}>
                    {status}
                </div>
            )}
        </div>
    )
}
