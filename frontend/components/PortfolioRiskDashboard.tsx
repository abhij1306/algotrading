'use client'

import React, { useState, useEffect } from 'react';

interface RiskDashboardProps {
    portfolioId: number;
}

export default function PortfolioRiskDashboard({ portfolioId }: RiskDashboardProps) {
    const [portfolio, setPortfolio] = useState<any>(null);
    const [riskAnalysis, setRiskAnalysis] = useState<any>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');

    useEffect(() => {
        loadPortfolioData();
    }, [portfolioId]);

    const loadPortfolioData = async () => {
        setLoading(true);
        setError('');

        try {
            const portfolioRes = await fetch(`http://localhost:8000/api/portfolios/${portfolioId}`);
            if (!portfolioRes.ok) throw new Error('Failed to load portfolio');
            const portfolioData = await portfolioRes.json();
            setPortfolio(portfolioData);

            const riskRes = await fetch(`http://localhost:8000/api/portfolios/${portfolioId}/analyze`, {
                method: 'POST'
            });
            if (!riskRes.ok) throw new Error('Failed to analyze portfolio');
            const riskData = await riskRes.json();
            setRiskAnalysis(riskData);

        } catch (err: any) {
            setError(err.message || 'Failed to load data');
        } finally {
            setLoading(false);
        }
    };

    if (loading) return (
        <div className="flex h-screen items-center justify-center bg-[#0f1115] text-white">
            <div className="flex flex-col items-center gap-4">
                <div className="h-10 w-10 animate-spin rounded-full border-4 border-primary border-t-transparent"></div>
                <p className="text-text-secondary animate-pulse">Analyzing portfolio...</p>
            </div>
        </div>
    );

    if (error) return (
        <div className="flex h-screen items-center justify-center bg-[#0f1115]">
            <div className="bg-red-500/10 border border-red-500/20 p-6 rounded-xl max-w-md w-full">
                <div className="flex items-center gap-3 text-red-500 mb-2">
                    <span className="material-symbols-outlined">error</span>
                    <h3 className="font-bold">Analysis Failed</h3>
                </div>
                <p className="text-red-400/80 text-sm">{error}</p>
                <button onClick={loadPortfolioData} className="mt-4 w-full py-2 rounded-lg bg-red-500 hover:bg-red-600 text-white text-sm font-bold transition-colors">
                    Retry Analysis
                </button>
            </div>
        </div>
    );

    if (!portfolio || !riskAnalysis) return null;

    const marketRisk = riskAnalysis.market_risk || {};
    const portfolioRisk = riskAnalysis.portfolio_risk || {};

    // Calculated metrics
    const portfolioValue = portfolio.total_invested || 0; // Assuming this is current value for now, or fetch real value
    const daysPnL = 0; // Needs real data
    const daysPnLPct = 0;
    const unrealizedPnL = 0;
    const unrealizedPnLPct = 0;

    return (
        <div className="flex h-full flex-col bg-[#0f1115] text-white font-sans">
            {/* Header Area */}
            <div className="flex items-center justify-between px-6 py-4 border-b border-border-dark bg-[#0f1115]">
                <div className="flex items-center gap-4">
                    <div className="p-2 bg-primary/20 rounded-lg">
                        <span className="material-symbols-outlined text-primary">analytics</span>
                    </div>
                    <div>
                        <h1 className="text-xl font-bold leading-tight">{portfolio.portfolio_name}</h1>
                        <p className="text-text-secondary text-xs">Risk & Performance Analysis</p>
                    </div>
                </div>
                <div className="flex gap-3">
                    <button className="flex items-center gap-2 px-4 py-2 bg-card-dark border border-border-dark rounded-lg hover:bg-[#252a33] transition-colors text-sm font-medium text-gray-300 hover:text-white">
                        <span className="material-symbols-outlined text-[18px]">file_upload</span>
                        Import
                    </button>
                    <button className="flex items-center gap-2 px-4 py-2 bg-primary hover:bg-indigo-500 rounded-lg text-white text-sm font-bold shadow-lg shadow-primary/20 transition-all">
                        <span className="material-symbols-outlined text-[18px]">add</span>
                        Add Trade
                    </button>
                </div>
            </div>

            <div className="flex-1 overflow-y-auto p-6 space-y-6">

                {/* Stats Grid */}
                <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
                    <StatsCard
                        title="Total Portfolio Value"
                        value={`₹${portfolioValue.toLocaleString()}`}
                        change={1.2}
                        icon="account_balance_wallet"
                        color="primary"
                    />
                    <StatsCard
                        title="Day's P&L"
                        value={`₹${daysPnL.toLocaleString()}`}
                        change={daysPnLPct}
                        icon="show_chart"
                        color="profit"
                    />
                    <StatsCard
                        title="Unrealized P&L"
                        value={`₹${unrealizedPnL.toLocaleString()}`}
                        change={unrealizedPnLPct}
                        icon="savings"
                        color="accent-teal"
                    />
                    <StatsCard
                        title="Realized P&L (YTD)"
                        value="₹85,200"
                        change={4.5}
                        icon="pie_chart"
                        color="accent-purple"
                    />
                </div>

                {/* Main Content Grid */}
                <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
                    {/* Performance Chart Placeholder */}
                    <div className="lg:col-span-2 rounded-xl border border-border-dark bg-card-dark p-6 h-[400px] flex flex-col">
                        <div className="flex justify-between items-start mb-6">
                            <div>
                                <h3 className="text-base font-semibold">Portfolio Performance</h3>
                                <p className="text-text-secondary text-sm">vs Nifty 50 Benchmark</p>
                            </div>
                            <div className="flex gap-1 bg-[#0f1115] p-1 rounded-lg border border-border-dark">
                                {['1M', '6M', '1Y', 'YTD'].map(period => (
                                    <button key={period} className={`px-3 py-1 rounded-md text-xs font-medium transition-colors ${period === '1Y' ? 'bg-primary text-white' : 'text-text-secondary hover:text-white hover:bg-[#252a33]'}`}>
                                        {period}
                                    </button>
                                ))}
                            </div>
                        </div>
                        <div className="flex-1 flex items-center justify-center border border-dashed border-border-dark rounded-lg bg-[#0f1115]/50">
                            <p className="text-text-secondary text-sm">Chart Visualization Component</p>
                        </div>
                    </div>

                    {/* Sector Allocation */}
                    <div className="rounded-xl border border-border-dark bg-card-dark p-6 flex flex-col">
                        <h3 className="text-base font-semibold mb-1">Sector Allocation</h3>
                        <p className="text-text-secondary text-sm mb-6">Diversification Status</p>

                        <div className="flex-1 flex flex-col gap-5 justify-center">
                            {/* Mock Sectors for now */}
                            <SectorBar name="IT Services" percent={40} color="bg-primary" />
                            <SectorBar name="Banking" percent={30} color="bg-accent-purple" />
                            <SectorBar name="Pharma" percent={15} color="bg-accent-teal" />
                            <SectorBar name="Auto" percent={10} color="bg-accent-orange" />
                            <SectorBar name="Cash" percent={5} color="bg-gray-500" />
                        </div>
                    </div>
                </div>

                {/* Risk Matrix & Stats */}
                <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
                    <div className="lg:col-span-2 rounded-xl border border-border-dark bg-card-dark p-6">
                        <div className="flex justify-between items-center mb-6">
                            <h3 className="text-lg font-bold">Risk Analysis Matrix</h3>
                            <button className="text-primary text-sm font-medium hover:text-indigo-400 flex items-center gap-1">
                                Detailed Report <span className="material-symbols-outlined text-sm">arrow_forward</span>
                            </button>
                        </div>
                        <div className="grid grid-cols-1 md:grid-cols-2 gap-8">
                            {/* Matrix Visual Placeholder */}
                            <div className="aspect-[4/3] bg-[#0f1115] rounded-lg border border-border-dark relative p-4">
                                <div className="absolute inset-0 flex flex-col p-4">
                                    <div className="flex-1 flex border-b border-border-dark">
                                        <div className="flex-1 border-r border-border-dark relative bg-red-500/5">
                                            <span className="absolute top-2 left-2 text-[10px] font-bold text-red-400 opacity-60">High Risk</span>
                                        </div>
                                        <div className="flex-1 relative"></div>
                                    </div>
                                    <div className="flex-1 flex">
                                        <div className="flex-1 border-r border-border-dark relative"></div>
                                        <div className="flex-1 relative bg-blue-500/5">
                                            <span className="absolute bottom-2 right-2 text-[10px] font-bold text-blue-400 opacity-60">Low Risk</span>
                                        </div>
                                    </div>
                                </div>
                                <div className="absolute inset-0 flex items-center justify-center">
                                    <div className="h-4 w-4 rounded-full bg-primary shadow-[0_0_15px_rgba(99,102,241,0.8)] border-2 border-white animate-pulse" title="Portfolio"></div>
                                </div>
                            </div>

                            {/* Key Metrics */}
                            <div className="flex flex-col justify-center gap-6">
                                <RiskMetricRow
                                    label="Portfolio Beta"
                                    value={marketRisk.beta?.toFixed(2) || 'N/A'}
                                    status={marketRisk.beta > 1.2 ? "High Volatility" : "Moderate Volatility"}
                                    statusColor={marketRisk.beta > 1.2 ? "text-orange-400" : "text-blue-400"}
                                    icon="warning"
                                />
                                <hr className="border-border-dark" />
                                <RiskMetricRow
                                    label="Sharpe Ratio"
                                    value={marketRisk.sharpe_ratio?.toFixed(2) || 'N/A'}
                                    status="Risk-Adjusted Returns"
                                    statusColor="text-profit"
                                    icon="check_circle"
                                />
                                <hr className="border-border-dark" />
                                <RiskMetricRow
                                    label="Annual Volatility"
                                    value={`${((marketRisk.portfolio_volatility || 0) * 100).toFixed(1)}%`}
                                    status="Standard Deviation"
                                    statusColor="text-text-secondary"
                                />
                            </div>
                        </div>
                    </div>

                    {/* Alerts Panel */}
                    <div className="rounded-xl border border-border-dark bg-card-dark p-6 flex flex-col">
                        <div className="flex items-center justify-between mb-4">
                            <h3 className="text-lg font-bold">Risk Alerts</h3>
                            <span className="px-2.5 py-1 rounded-full bg-red-500/10 border border-red-500/20 text-red-400 text-xs font-medium">Auto-Generated</span>
                        </div>
                        <div className="flex-1 flex flex-col gap-3 overflow-y-auto max-h-[300px] hide-scrollbar">
                            <AlertItem type="error" title="High Concentration" message="IT Sector exposure > 40%. Consider rebalancing." />
                            <AlertItem type="warning" title="Stop Loss Near" message="TATASTEEL is 2% away from stop loss." />
                        </div>
                    </div>
                </div>

                {/* Holdings Table */}
                <div className="rounded-xl border border-border-dark bg-card-dark overflow-hidden shadow-sm">
                    <div className="px-6 py-4 border-b border-border-dark flex justify-between items-center bg-[#13161c]">
                        <h3 className="text-lg font-bold">Holdings</h3>
                        <div className="flex gap-2">
                            <input type="text" placeholder="Filter..." className="bg-[#0f1115] border border-border-dark rounded px-3 py-1.5 text-sm text-white focus:outline-none focus:border-primary w-40" />
                        </div>
                    </div>
                    <div className="overflow-x-auto">
                        <table className="w-full text-left text-sm whitespace-nowrap">
                            <thead className="bg-[#13161c] text-text-secondary font-semibold border-b border-border-dark">
                                <tr>
                                    <th className="px-6 py-3">Instrument</th>
                                    <th className="px-6 py-3 text-right">Qty</th>
                                    <th className="px-6 py-3 text-right">Avg Price</th>
                                    <th className="px-6 py-3 text-right">LTP</th>
                                    <th className="px-6 py-3 text-right">Value</th>
                                    <th className="px-6 py-3 text-right">P&L</th>
                                </tr>
                            </thead>
                            <tbody className="divide-y divide-border-dark">
                                {portfolio.positions?.map((pos: any, idx: number) => (
                                    <tr key={idx} className="hover:bg-[#1f232b] transition-colors group">
                                        <td className="px-6 py-4">
                                            <div className="flex items-center gap-3">
                                                <div className={`w-1 h-8 rounded-full ${idx % 2 === 0 ? 'bg-primary' : 'bg-accent-purple'}`}></div>
                                                <div>
                                                    <p className="font-bold text-white group-hover:text-primary transition-colors">{pos.symbol}</p>
                                                    <p className="text-xs text-text-secondary">{pos.company_name || '--'}</p>
                                                </div>
                                            </div>
                                        </td>
                                        <td className="px-6 py-4 text-right text-gray-300">{pos.quantity ?? '--'}</td>
                                        <td className="px-6 py-4 text-right text-gray-300">₹{(pos.avg_buy_price || pos.average_price)?.toFixed(2) ?? '--'}</td>
                                        <td className="px-6 py-4 text-right font-medium text-white">₹--</td>
                                        <td className="px-6 py-4 text-right font-medium text-white">₹{(pos.invested_value ?? 0).toLocaleString()}</td>
                                        <td className="px-6 py-4 text-right text-gray-400">--</td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                </div>

            </div>
        </div>
    );
}

// Components
function StatsCard({ title, value, change, icon, color }: any) {
    const isPos = change >= 0;
    return (
        <div className="flex flex-col gap-1 rounded-xl p-5 bg-card-dark border border-border-dark relative overflow-hidden group hover:border-gray-600 transition-colors">
            <div className={`absolute -right-6 -top-6 w-24 h-24 rounded-full blur-2xl opacity-10 bg-${color} group-hover:opacity-20 transition-all`}></div>
            <div className="flex justify-between items-start z-10">
                <p className="text-text-secondary text-sm font-medium">{title}</p>
                <span className={`material-symbols-outlined text-3xl opacity-20 text-${color}`}>{icon}</span>
            </div>
            <div className="flex items-end gap-2 mt-1 z-10">
                <p className="text-2xl font-bold tracking-tight text-white">{value}</p>
                <span className={`flex items-center text-xs font-bold px-1.5 py-0.5 rounded mb-1 ${isPos ? 'text-profit bg-profit/10' : 'text-loss bg-loss/10'}`}>
                    {isPos ? '+' : ''}{change}%
                </span>
            </div>
        </div>
    )
}

function SectorBar({ name, percent, color }: any) {
    return (
        <div className="group">
            <div className="flex justify-between text-sm mb-1.5">
                <span className="text-white font-medium">{name}</span>
                <span className="text-text-secondary">{percent}%</span>
            </div>
            <div className="h-2.5 w-full bg-[#0f1115] rounded-full overflow-hidden border border-border-dark/50">
                <div className={`h-full ${color} w-[${percent}%] rounded-full shadow-[0_0_10px_rgba(255,255,255,0.2)]`} style={{ width: `${percent}%` }}></div>
            </div>
        </div>
    )
}

function RiskMetricRow({ label, value, status, statusColor, icon }: any) {
    return (
        <div>
            <div className="flex justify-between items-center mb-1">
                <span className="text-text-secondary text-sm">{label}</span>
                <span className="text-white font-bold bg-white/5 px-2 py-0.5 rounded border border-white/10">{value}</span>
            </div>
            <div className={`text-xs ${statusColor} flex items-center gap-1.5 mt-1`}>
                {icon && <span className="material-symbols-outlined text-xs">{icon}</span>}
                {status}
            </div>
        </div>
    )
}

function AlertItem({ type, title, message }: any) {
    const color = type === 'error' ? 'red' : type === 'warning' ? 'orange' : 'blue';
    return (
        <div className={`flex gap-3 items-start p-3 rounded-lg bg-[#0f1115] border border-${color}-900/30 hover:border-${color}-900/50 transition-colors`}>
            <span className={`material-symbols-outlined text-${color}-400 shrink-0 mt-0.5`}>{type === 'error' ? 'error' : 'warning'}</span>
            <div>
                <p className="text-white text-sm font-medium">{title}</p>
                <p className="text-text-secondary text-xs mt-1 leading-relaxed">{message}</p>
            </div>
        </div>
    )
}
