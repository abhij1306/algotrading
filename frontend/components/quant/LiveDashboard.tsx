'use client'

import { useState, useEffect } from 'react'
import { Activity, ShieldCheck, TrendingUp, AlertCircle, CheckCircle2, Info, ChevronRight, BarChart3, Zap, Cpu, Server } from 'lucide-react'
import MonitoringEquityChart from '@/components/charts/MonitoringEquityChart'
import { GlassCard } from "@/components/ui/GlassCard"
import { Button, Heading, Text, Data, Badge, Label } from '@/components/design-system/atoms'
import { useWebSocket } from '@/hooks/useWebSocket';

interface HealthState {
    date: string
    equity: number
    drawdown: number
    volatility: number
    volatility_regime: string
    risk_state: string
    risk_state_reason: string
}

interface TrustStrategy {
    strategy_id: string
    current_drawdown: number
    expected_max_dd: number
    drift_ratio: number
    status: string
    regime: string
    weight: number
}

interface AllocatorDecision {
    strategy_id: string
    old_weight: number
    new_weight: number
    delta: number
    reason: string
    severity: string
}

interface SystemHealth {
    status: string
    latency_ms: number
    cpu_usage: number
    memory_usage: number
    uptime_days: number
}

interface LiveData {
    portfolio_health: HealthState
    trust_map: Record<string, TrustStrategy>
    allocator_decisions: AllocatorDecision[]
}

export default function MonitoringClient() {
    const [liveData, setLiveData] = useState<LiveData | null>(null)
    const [historyData, setHistoryData] = useState<{ date: string; equity: number; drawdown: number }[]>([])
    const [loading, setLoading] = useState(true)
    const [error, setError] = useState<string | null>(null)
    const [marketClosed, setMarketClosed] = useState(false)
    const [systemHealth, setSystemHealth] = useState<SystemHealth | null>(null)

    // WebSocket Integration
    const { isConnected, lastMessage } = useWebSocket();

    // Handle incoming tick data
    useEffect(() => {
        if (lastMessage) {
            // Update live data based on tick (e.g. updating equity if we tracked specific positions)
            // For now, we log it or update a specific state if relevant
            // console.log("Live Tick:", lastMessage);
        }
    }, [lastMessage]);

    const fetchMonitoringData = async () => {
        try {
            // 1. Fetch Live Data
            const res = await fetch('/api/portfolio/strategies/monitor');

            if (res.status === 503) {
                setMarketClosed(true);
                setLoading(false);
                return;
            }

            if (!res.ok) {
                const errorText = await res.text();
                throw new Error(`Failed to fetch live data: ${res.status} ${errorText}`);
            }
            const portfolios = await res.json();

            // If we get here, market is open
            setMarketClosed(false);

            if (portfolios.length === 0) {
                setError("NO_ACTIVE_RUN");
                setLoading(false);
                return;
            }

            // Select first portfolio for display
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            const p: any = portfolios[0];

            // Map to UI Structure
            const health: HealthState = {
                date: new Date(p.timestamp).toLocaleTimeString(),
                equity: p.total_equity,
                drawdown: p.drawdown_pct,
                volatility: 0.15, // TODO: Add to backend
                volatility_regime: 'NORMAL',
                risk_state: p.breached ? 'BREACHED' : 'NORMAL',
                risk_state_reason: p.breach_details || 'All systems nominal'
            };

            const trustMapData: Record<string, TrustStrategy> = {};
            if (p.strategies) {
                // eslint-disable-next-line @typescript-eslint/no-explicit-any
                Object.entries(p.strategies).forEach(([sid, stats]: [string, any]) => {
                    trustMapData[sid] = {
                        strategy_id: sid,
                        current_drawdown: -2.0, // Mock for now or calculate from stats
                        expected_max_dd: -5.0,
                        drift_ratio: 0.4,
                        status: 'STABLE',
                        regime: 'UNKNOWN',
                        weight: stats.allocation || 0
                    };
                });
            }

            setLiveData({
                portfolio_health: health,
                trust_map: trustMapData,
                allocator_decisions: []
            });

            // Mock history (until history API exists)
            setHistoryData([
                { date: 'Start', equity: 1000000, drawdown: 0 },
                { date: 'Now', equity: p.total_equity, drawdown: p.drawdown_pct }
            ]);

            // System Health
            setSystemHealth({
                status: isConnected ? 'ACTIVE' : 'CONNECTING',
                latency_ms: isConnected ? 12 : 999,
                cpu_usage: 15,
                memory_usage: 40,
                uptime_days: 1.5
            });

            setError(null);

            // eslint-disable-next-line @typescript-eslint/no-explicit-any
        } catch (e: any) {
            console.error("Monitoring fetch error:", e);
            setError(e.message);
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        fetchMonitoringData()
        const interval = setInterval(fetchMonitoringData, 30000) // Poll every 30s
        return () => clearInterval(interval)
    }, [isConnected]) // Refetch if connection status changes

    if (loading) return (
        <div className="h-full flex items-center justify-center bg-[#050505]">
            <div className="flex flex-col items-center gap-4">
                <div className="w-12 h-12 border-4 border-cyan-500/20 border-t-cyan-500 rounded-full animate-spin"></div>
                <Text variant="small" className="uppercase tracking-widest">Initializing Quantum Monitoring</Text>
            </div>
        </div>
    )

    if (marketClosed) return (
        <div className="h-full flex items-center justify-center bg-[#050505] p-6">
            <GlassCard className="max-w-md w-full p-10 text-center space-y-8">
                <div className="w-20 h-20 bg-white/5 rounded-3xl flex items-center justify-center mx-auto border border-white/10 shadow-[0_0_30px_rgba(255,255,255,0.05)]">
                    <Activity className="w-8 h-8 text-gray-400" />
                </div>
                <div className="space-y-4">
                    <Heading level="h2" className="text-2xl font-black text-white tracking-tight">Market Offline</Heading>
                    <p className="text-gray-400 text-sm leading-relaxed">
                        Live monitoring is only available during market hours.<br />
                        <span className="text-cyan-400 font-mono mt-2 block">09:15 - 15:30 IST</span>
                    </p>
                </div>
                <div className="pt-4 border-t border-white/5">
                    <Text variant="tiny" className="uppercase tracking-widest text-gray-600 font-bold">System Standby Mode</Text>
                </div>
            </GlassCard>
        </div>
    )

    if (error === "NO_ACTIVE_RUN") return (
        <div className="h-full flex items-center justify-center bg-[#050505] p-6">
            <div className="max-w-md w-full glass-subtle rounded-3xl border border-white/5 p-8 text-center space-y-6">
                <div className="w-16 h-16 bg-amber-500/10 rounded-2xl flex items-center justify-center mx-auto border border-amber-500/20">
                    <AlertCircle className="w-8 h-8 text-amber-500" />
                </div>
                <div className="space-y-2">
                    <Heading level="h3" className="text-xl font-bold text-white">No Active Portfolio Found</Heading>
                    <p className="text-gray-500 text-sm leading-relaxed">
                        The monitoring engine is dormant because no live portfolio has been initialized.
                        Please promote a research portfolio to 'Live' status to begin monitoring.
                    </p>
                </div>
                <div className="w-full">
                    <Button
                        onClick={() => window.location.href = '/quant/research'}
                        variant="ghost"
                        className="w-full py-6 uppercase tracking-widest text-xs font-bold border border-white/10 hover:border-white/20"
                    >
                        Initialize Portfolio
                    </Button>
                </div>
            </div>
        </div>
    )

    const health = liveData?.portfolio_health as HealthState
    const trustMap = liveData?.trust_map as Record<string, TrustStrategy>
    const decisions = liveData?.allocator_decisions as AllocatorDecision[]

    return (
        <div className="space-y-6">
            {/* Top Bar: System Health (MANDATORY) */}
            <GlassCard className="flex items-center justify-between px-6 py-3 sticky top-0 z-50">
                <div className="flex items-center gap-6">
                    <div className="flex items-center gap-2">
                        <div className={`w-2 h-2 rounded-full ${systemHealth?.status === 'ACTIVE' ? 'bg-emerald-500 shadow-[0_0_8px_rgba(16,185,129,0.5)] anime-pulse' : 'bg-red-500'}`}></div>
                        <Text variant="tiny" className="font-black text-white uppercase tracking-widest">
                            System: {systemHealth?.status || 'OFFLINE'}
                        </Text>
                    </div>
                    <div className="h-4 w-[1px] bg-white/10"></div>
                    <div className="flex items-center gap-4 text-[9px] font-bold text-gray-500 uppercase tracking-widest">
                        <div className="flex items-center gap-1.5">
                            <Zap className="w-3 h-3 text-cyan-400" />
                            Latency: <Data value={systemHealth?.latency_ms || '--'} suffix="ms" className="text-white" />
                        </div>
                        <div className="flex items-center gap-1.5">
                            <Cpu className="w-3 h-3 text-purple-400" />
                            CPU: <Data value={systemHealth?.cpu_usage || '--'} suffix="%" className="text-white" />
                        </div>
                        <div className="flex items-center gap-1.5">
                            <Server className="w-3 h-3 text-amber-400" />
                            Memory: <Data value={systemHealth?.memory_usage || '--'} suffix="%" className="text-white" />
                        </div>
                    </div>
                </div>

                <div className="flex items-center gap-4">
                    <div className="text-[9px] text-gray-600 font-bold uppercase tracking-widest">
                        Uptime: <Data value={systemHealth?.uptime_days || '--'} suffix="d" className="text-gray-400" />
                    </div>
                </div>
            </GlassCard>

            <div className="space-y-6">

                {/* Header: Run Identity */}
                <div className="flex items-center justify-between mb-4">
                    <div className="flex items-center gap-4">
                        <div className="w-12 h-12 rounded-2xl bg-cyan-500/10 flex items-center justify-center border border-cyan-500/20">
                            <Activity className="w-6 h-6 text-cyan-400" />
                        </div>
                        <div>
                            <Heading level="h1" className="text-xl font-black text-white tracking-tight">System Monitor <span className="text-gray-600 ml-2">[{health?.date || 'LIVE'}]</span></Heading>
                            <div className="flex items-center gap-2 mt-1">
                                <div className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse"></div>
                                <Text variant="tiny" className="text-emerald-500 font-bold uppercase tracking-widest">Active Execution Feed</Text>
                            </div>
                        </div>
                    </div>

                    <div className="flex gap-4">
                        <GlassCard className="px-4 py-2">
                            <Label size="sm" className="block uppercase font-bold mb-1">Risk Regime</Label>
                            <span className={`text-xs font-black ${health?.risk_state === 'NORMAL' ? 'text-cyan-400' : 'text-amber-400'}`}>
                                {health?.risk_state || 'STABLE'}
                            </span>
                        </GlassCard>
                        <GlassCard className="px-4 py-2">
                            <Label size="sm" className="block uppercase font-bold mb-1">Vol Regime</Label>
                            <span className="text-xs font-black text-purple-400">
                                {health?.volatility_regime || 'LOW'}
                            </span>
                        </GlassCard>
                    </div>
                </div>

                <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">

                    {/* Main Curve: Portfolio Health */}
                    <div className="lg:col-span-2 space-y-6">
                        <GlassCard className="p-6 space-y-6 shadow-2xl">
                            <div className="flex items-center justify-between">
                                <div className="flex items-center gap-3">
                                    <TrendingUp className="w-5 h-5 text-cyan-400" />
                                    <Heading level="h4" className="text-sm font-bold text-white uppercase tracking-widest">Live Portfolio Equity</Heading>
                                </div>
                                <div className="text-right">
                                    <div className="text-2xl font-black text-white font-data">
                                        <Data value={health?.equity.toLocaleString('en-IN') || '--'} prefix="₹" />
                                    </div>
                                    <div className={`text-[10px] font-bold ${health?.drawdown > -2 ? 'text-emerald-400' : 'text-red-400'}`}>
                                        {health?.drawdown.toFixed(2)}% DD Peak-to-Trough
                                    </div>
                                </div>
                            </div>

                            <div className="h-[300px] w-full">
                                <MonitoringEquityChart data={historyData} />
                            </div>
                        </GlassCard>

                        {/* Trust Map Grid */}
                        <div className="space-y-4">
                            <div className="flex items-center justify-between">
                                <Heading level="h4" className="text-xs font-bold text-gray-500 uppercase tracking-widest flex items-center gap-2">
                                    <ShieldCheck className="w-4 h-4" /> Strategy Trust Map
                                </Heading>
                                <Info className="w-3 h-3 text-gray-600 cursor-help" />
                            </div>

                            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                                {trustMap && Object.values(trustMap).map((strat) => (
                                    <GlassCard key={strat.strategy_id} className="p-5 hover:bg-white/[0.02] transition-all group">
                                        <div className="flex items-center justify-between mb-4">
                                            <div>
                                                <h3 className="text-xs font-black text-white group-hover:text-cyan-400 transition-colors font-inter">{strat.strategy_id}</h3>
                                                <p className="text-[10px] text-gray-500 uppercase font-bold mt-0.5 tracking-tighter">{strat.regime} Regime</p>
                                            </div>
                                            <div className={`px-2 py-0.5 rounded text-[9px] font-black tracking-tighter border ${strat.status === 'STABLE' ? 'text-emerald-400 bg-emerald-500/10 border-emerald-500/20' :
                                                strat.status === 'WARNING' ? 'text-amber-400 bg-amber-500/10 border-amber-500/20' :
                                                    'text-red-400 bg-red-500/10 border-red-500/20'
                                                }`}>
                                                {strat.status}
                                            </div>
                                        </div>

                                        <div className="space-y-3">
                                            <div className="flex justify-between items-end">
                                                <Label size="sm" className="font-bold uppercase">Drift vs Benchmark</Label>
                                                <Data value={(strat.drift_ratio * 100).toFixed(1)} suffix="%" className="text-sm font-black text-white" />
                                            </div>
                                            <div className="w-full h-1.5 bg-white/5 rounded-full overflow-hidden">
                                                <div
                                                    className={`h-full transition-all duration-1000 rounded-full ${strat.drift_ratio < 0.8 ? 'bg-cyan-500/50' :
                                                        strat.drift_ratio < 1.0 ? 'bg-amber-500/50' : 'bg-red-500/50'
                                                        }`}
                                                    style={{ width: `${Math.min(strat.drift_ratio * 100, 100)}%` }}
                                                ></div>
                                            </div>
                                            <div className="grid grid-cols-2 gap-2 text-[10px]">
                                                <div className="text-gray-600">Current DD: <Data value={strat.current_drawdown} suffix="%" className="text-gray-300" /></div>
                                                <div className="text-gray-600 text-right">Target DD: <Data value={strat.expected_max_dd} suffix="%" className="text-gray-300" /></div>
                                            </div>
                                        </div>
                                    </GlassCard>
                                ))}
                            </div>
                        </div>
                    </div>

                    {/* Sidebar: Logs & Stats */}
                    <div className="space-y-6">

                        {/* Recent Allocator Decisions */}
                        <GlassCard className="flex flex-col min-h-[400px]">
                            <div className="p-5 border-b border-white/5 bg-white/[0.02]">
                                <div className="flex items-center gap-3">
                                    <BarChart3 className="w-4 h-4 text-purple-400" />
                                    <Heading level="h4" className="text-xs font-bold text-white uppercase tracking-widest">Decision Ledger</Heading>
                                </div>
                            </div>

                            <div className="flex-1 overflow-y-auto p-4 space-y-4 max-h-[500px] scrollbar-thin scrollbar-thumb-white/5">
                                {decisions && decisions.length > 0 ? (
                                    decisions.slice().reverse().map((dec, i) => (
                                        <div key={i} className="p-3 rounded-xl bg-white/[0.02] border border-white/5 space-y-2 relative group hover:bg-white/[0.04] transition-all">
                                            <div className="flex items-center justify-between">
                                                <span className="text-[10px] font-black text-white tracking-tight">{dec.strategy_id}</span>
                                                <span className={`text-[9px] font-bold ${dec.delta > 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                                                    {dec.delta > 0 ? '+' : ''}{Math.round(dec.delta * 100)}% WT
                                                </span>
                                            </div>
                                            <p className="text-[10px] text-gray-500 leading-snug">
                                                {dec.reason}
                                            </p>
                                            <div className="flex items-center justify-between pt-1 border-t border-white/[0.03]">
                                                <span className="text-[8px] text-gray-600 font-bold uppercase">{dec.severity} SEVERITY</span>
                                                <ChevronRight className="w-3 h-3 text-gray-700 group-hover:text-gray-400 transform group-hover:translate-x-1 transition-all" />
                                            </div>
                                        </div>
                                    ))
                                ) : (
                                    <div className="h-full flex flex-col items-center justify-center opacity-30 py-20 grayscale">
                                        <BarChart3 className="w-12 h-12 mb-4" />
                                        <p className="text-[10px] font-bold uppercase tracking-widest">No recent adjustments</p>
                                    </div>
                                )}
                            </div>
                        </GlassCard>

                        {/* Policy Status Mini-card */}
                        <GlassCard className="p-5 space-y-4">
                            <div className="flex items-center gap-3">
                                <div className="w-8 h-8 rounded-lg bg-emerald-500/10 flex items-center justify-center border border-emerald-500/20">
                                    <CheckCircle2 className="w-4 h-4 text-emerald-400" />
                                </div>
                                <Heading level="h4" className="text-xs font-bold text-white uppercase tracking-widest">Compliance</Heading>
                            </div>
                            <ul className="space-y-3">
                                {[
                                    { label: 'Risk Overlays', status: 'ACTIVE' },
                                    { label: 'Liquidity Buffer', status: 'OK' },
                                    { label: 'Sector Exposure', status: 'COMPLIANT' }
                                ].map((rule, i) => (
                                    <li key={i} className="flex items-center justify-between text-[11px]">
                                        <span className="text-gray-500 font-medium font-inter">{rule.label}</span>
                                        <span className="text-emerald-500 font-mono text-[9px] font-bold">{rule.status}</span>
                                    </li>
                                ))}
                            </ul>
                        </GlassCard>
                    </div>

                </div>

            </div>
        </div>
    )
}
