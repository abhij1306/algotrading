"use client";

import React, { useState, useEffect } from "react";
import UniverseSelector from "@/components/backtest/UniverseSelector";
import StrategyCards from "@/components/backtest/StrategyCards";
import AllocationConfig from "@/components/backtest/AllocationConfig";
import { Play, Loader2, Layers, Zap, Settings2, ShieldCheck, ChevronRight, LayoutDashboard, Terminal, Calendar } from "lucide-react";

interface StrategyConfig {
    id: string;
    params: Record<string, number>;
    enabled: boolean;
}

interface PolicySettings {
    correlationPenalty: "low" | "medium" | "high";
    cautiousThreshold: number;
    defensiveThreshold: number;
    defensiveAction: string;
}

export default function PortfolioResearchSystem() {
    // Core State
    const [universeId, setUniverseId] = useState("");
    const [compatibleStrategies, setCompatibleStrategies] = useState<string[]>([]);
    const [strategies, setStrategies] = useState<StrategyConfig[]>([]);

    // Config State
    const [allocationMethod, setAllocationMethod] = useState("INVERSE_VOLATILITY");
    const [lookback, setLookback] = useState(30);
    const [policies, setPolicies] = useState<any[]>([]);
    const [selectedPolicyId, setSelectedPolicyId] = useState<string>("");

    useEffect(() => {
        fetch("http://localhost:9000/api/portfolio/strategies/policy")
            .then(res => res.json())
            .then(data => {
                setPolicies(data);
                if (data.length > 0) setSelectedPolicyId(data[0].id);
            })
            .catch(err => console.error("Failed to fetch policies", err));
    }, []);

    // Date Range State
    const [startDate, setStartDate] = useState(() => {
        const d = new Date();
        d.setFullYear(d.getFullYear() - 1);
        return d.toISOString().split("T")[0];
    });
    const [endDate, setEndDate] = useState(() => new Date().toISOString().split("T")[0]);

    // Execution State
    const [runId, setRunId] = useState<string | null>(null);
    const [loading, setLoading] = useState(false);
    const [activeSection, setActiveSection] = useState<"universe" | "strategy" | "config">("universe");

    const runBacktest = async () => {
        if (!universeId) {
            alert("Please select a universe first.");
            return;
        }
        const enabledStrats = strategies.filter((s) => s.enabled);
        if (enabledStrats.length === 0) {
            alert("Please enable at least one strategy.");
            return;
        }

        setLoading(true);

        try {
            const response = await fetch("http://localhost:9000/api/portfolio-backtest/run", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    universe_id: universeId,
                    strategies: enabledStrats.map((s) => ({ id: s.id, params: s.params })),
                    allocation_method: allocationMethod,
                    lookback: lookback,
                    policy_id: selectedPolicyId,
                    start_date: startDate,
                    end_date: endDate
                })
            });

            const data = await response.json();
            if (data.run_id) {
                setRunId(data.run_id);
                // In a real app, we might redirect to a results page here
                alert(`Backtest Started! Run ID: ${data.run_id}`);
            }
        } catch (err) {
            console.error("Backtest failed", err);
            alert("Backtest failed to execute.");
        } finally {
            setLoading(false);
        }
    };

    return (
        <div className="h-screen overflow-y-auto bg-[#000000] text-gray-200 font-sans selection:bg-cyan-500/30">
            {/* 1. Ultra-Minimal Header */}
            <header className="px-12 py-5 border-b border-white/5 bg-black/50 backdrop-blur-md sticky top-0 z-50">
                <div className="flex items-center justify-between max-w-[1920px] mx-auto">
                    <div className="flex items-center gap-4">
                        <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-cyan-900/40 to-black border border-white/10 flex items-center justify-center">
                            <Terminal className="w-5 h-5 text-cyan-400" />
                        </div>
                        <div>
                            <h1 className="text-xl font-bold tracking-tight text-white">
                                Quant Lab <span className="text-cyan-500">.Pro</span>
                            </h1>
                            <div className="flex items-center gap-2 text-[10px] uppercase tracking-widest text-gray-500 font-medium">
                                <span>Build</span>
                                <span className="text-gray-700">•</span>
                                <span>Test</span>
                                <span className="text-gray-700">•</span>
                                <span>Deploy</span>
                            </div>
                        </div>
                    </div>

                    <div className="flex items-center gap-6">
                        <div className="flex items-center gap-2 px-3 py-1.5 rounded-full bg-white/5 border border-white/5">
                            <div className={`w-1.5 h-1.5 rounded-full ${universeId ? 'bg-emerald-500' : 'bg-gray-600'}`}></div>
                            <span className="text-xs font-mono text-gray-400">{universeId || "NO UNIVERSE"}</span>
                        </div>
                        <div className="h-8 w-px bg-white/10"></div>
                        <div className="text-right">
                            <div className="text-[10px] text-gray-500 uppercase">System Status</div>
                            <div className="text-xs text-emerald-500 font-mono font-bold">ONLINE</div>
                        </div>
                    </div>
                </div>
            </header>

            <main className="max-w-[1920px] mx-auto p-12 space-y-16 pb-40">

                {/* 2. Universe Selection */}
                <section>
                    <div className="flex items-end justify-between mb-8">
                        <div>
                            <h2 className="text-3xl font-light text-white mb-2">Investment <span className="font-bold text-gray-500">Universe</span></h2>
                            <p className="text-sm text-gray-500 max-w-xl">
                                Select the asset pool for your strategy. The universe defines the constituent stocks and benchmark index for performance comparison.
                            </p>
                        </div>
                        <div className="text-right hidden xl:block">
                            <div className="text-[10px] uppercase tracking-widest text-gray-600 mb-1">Available Universes</div>
                            <div className="text-2xl font-mono text-white">12</div>
                        </div>
                    </div>

                    <UniverseSelector
                        onSelect={(id) => setUniverseId(id)}
                        selectedId={universeId}
                        onCompatibleStrategiesUpdate={setCompatibleStrategies}
                    />
                </section>

                {/* 2.5 Time Horizon Selection */}
                <section className="pt-8 border-t border-white/5">
                    <div className="flex items-end justify-between mb-8">
                        <div>
                            <h2 className="text-3xl font-light text-white mb-2">Time <span className="font-bold text-gray-500">Horizon</span></h2>
                            <p className="text-sm text-gray-500 max-w-xl">
                                Define the temporal window for the simulation. Longer horizons provide better statistical significance, while shorter ones isolate recent market regimes.
                            </p>
                        </div>
                        <div className="flex gap-2">
                            {['3M', '6M', '1Y', 'YTD', 'MAX'].map((preset) => (
                                <button
                                    key={preset}
                                    onClick={() => {
                                        const end = new Date();
                                        const start = new Date();
                                        if (preset === '3M') start.setMonth(start.getMonth() - 3);
                                        else if (preset === '6M') start.setMonth(start.getMonth() - 6);
                                        else if (preset === '1Y') start.setFullYear(start.getFullYear() - 1);
                                        else if (preset === 'YTD') start.setMonth(0, 1);
                                        else if (preset === 'MAX') start.setFullYear(start.getFullYear() - 5);

                                        setStartDate(start.toISOString().split("T")[0]);
                                        setEndDate(end.toISOString().split("T")[0]);
                                    }}
                                    className="px-4 py-1.5 rounded-lg bg-white/5 border border-white/10 text-[10px] font-bold tracking-widest uppercase hover:bg-white/10 hover:border-white/20 transition-all text-gray-400 hover:text-white"
                                >
                                    {preset}
                                </button>
                            ))}
                        </div>
                    </div>

                    <div className="grid grid-cols-1 md:grid-cols-2 gap-6 bg-[#050505] border border-white/5 rounded-2xl p-8">
                        <div className="space-y-2">
                            <label className="text-[10px] uppercase tracking-widest text-gray-500 font-bold flex items-center gap-2">
                                <Calendar className="w-3 h-3 text-cyan-500" />
                                Start Date
                            </label>
                            <input
                                type="date"
                                value={startDate}
                                onChange={(e) => setStartDate(e.target.value)}
                                className="w-full bg-black border border-white/10 rounded-xl px-4 py-3 text-white focus:outline-none focus:border-cyan-500/50 transition-all font-mono"
                            />
                        </div>
                        <div className="space-y-2">
                            <label className="text-[10px] uppercase tracking-widest text-gray-500 font-bold flex items-center gap-2">
                                <Calendar className="w-3 h-3 text-emerald-500" />
                                End Date
                            </label>
                            <input
                                type="date"
                                value={endDate}
                                onChange={(e) => setEndDate(e.target.value)}
                                className="w-full bg-black border border-white/10 rounded-xl px-4 py-3 text-white focus:outline-none focus:border-emerald-500/50 transition-all font-mono"
                            />
                        </div>
                    </div>
                </section>

                {/* 3. Strategy Composition */}
                <section>
                    <div className="flex items-end justify-between mb-8 pt-8 border-t border-white/5">
                        <div>
                            <h2 className="text-3xl font-light text-white mb-2">Alpha <span className="font-bold text-gray-500">Composition</span></h2>
                            <p className="text-sm text-gray-500 max-w-xl">
                                Construct your portfolio by selecting from independent alpha sources. Strategies are automatically filtered for compatibility with the selected universe.
                            </p>
                        </div>
                        <div className="flex items-center gap-6">
                            <div className="flex items-center gap-4">
                                <div>
                                    <div className="text-[10px] uppercase tracking-widest text-gray-500 mb-1">Risk Policy</div>
                                    <select
                                        value={selectedPolicyId}
                                        onChange={(e) => setSelectedPolicyId(e.target.value)}
                                        className="bg-black border border-white/10 rounded-lg px-3 py-1.5 text-xs text-white focus:outline-none focus:border-cyan-500 font-mono"
                                    >
                                        {policies.map(p => (
                                            <option key={p.id} value={p.id}>{p.name}</option>
                                        ))}
                                    </select>
                                </div>
                                <div className="h-10 w-px bg-white/5"></div>
                                <div>
                                    <div className="text-[10px] uppercase tracking-widest text-gray-500 mb-1">Active Strategies</div>
                                    <div className="text-2xl font-mono text-cyan-400">{strategies.filter(s => s.enabled).length}</div>
                                </div>
                            </div>
                        </div>
                    </div>

                    <StrategyCards
                        selection={strategies}
                        onChange={setStrategies}
                        compatibleStrategies={compatibleStrategies}
                    />
                </section>

                {/* 4. Configuration Panel */}
                <section className="pt-8 border-t border-white/5">
                    <div className="bg-[#050505] border border-white/5 rounded-2xl p-8 transition-all hover:border-white/10">
                        <div className="flex items-start gap-6">
                            <div className="p-3 rounded-xl bg-purple-900/10 border border-purple-500/20 text-purple-400">
                                <Settings2 className="w-6 h-6" />
                            </div>
                            <div className="flex-1">
                                <h3 className="text-xl font-bold text-white mb-2">Portfolio Construction Policy</h3>
                                <p className="text-sm text-gray-500 mb-6">
                                    Define how capital is allocated across selected strategies and set risk management constraints.
                                </p>
                                <AllocationConfig
                                    method={allocationMethod}
                                    onChange={setAllocationMethod}
                                    lookback={lookback}
                                    onLookbackChange={setLookback}
                                />
                            </div>
                        </div>
                    </div>
                </section>

                {/* 5. Execution & Summary */}
                <section className="pt-8 border-t border-white/5 pb-20">
                    <div className="flex items-center justify-between bg-[#111] p-6 rounded-2xl border border-white/10">
                        <div className="flex items-center gap-12">
                            <div>
                                <div className="text-[10px] uppercase tracking-widest text-gray-500 mb-1">Target Universe</div>
                                <div className="text-lg font-mono text-white font-bold">{universeId ? universeId.replace(/_/g, ' ') : "---"}</div>
                            </div>
                            <div>
                                <div className="text-[10px] uppercase tracking-widest text-gray-500 mb-1">Active Strategies</div>
                                <div className="text-lg font-mono text-white font-bold">{strategies.filter(s => s.enabled).length} <span className="text-sm font-normal text-gray-600">/ {strategies.length}</span></div>
                            </div>
                            <div className="h-10 w-px bg-white/5"></div>
                            <div>
                                <div className="text-[10px] uppercase tracking-widest text-gray-500 mb-1">Period</div>
                                <div className="text-lg font-mono text-white font-bold">
                                    {startDate} <span className="text-xs text-gray-600 mx-1">TO</span> {endDate}
                                </div>
                            </div>
                        </div>

                        <button
                            onClick={runBacktest}
                            disabled={loading || !universeId || strategies.filter(s => s.enabled).length === 0}
                            className={`
                                relative overflow-hidden group px-12 py-4 rounded-xl font-bold uppercase tracking-widest transition-all duration-300
                                ${loading || !universeId || strategies.filter(s => s.enabled).length === 0
                                    ? "bg-white/5 text-gray-600 cursor-not-allowed"
                                    : "bg-emerald-500 text-black hover:scale-[1.02] shadow-[0_0_30px_-5px_rgba(16,185,129,0.4)]"
                                }
                            `}
                        >
                            <div className="relative z-10 flex items-center gap-3">
                                {loading ? (
                                    <>
                                        <Loader2 className="w-5 h-5 animate-spin" />
                                        <span>Running Engine...</span>
                                    </>
                                ) : (
                                    <>
                                        <span>Execute Simulation</span>
                                        <Play className="w-5 h-5 fill-current" />
                                    </>
                                )}
                            </div>
                        </button>
                    </div>
                </section>
            </main>
        </div >
    );
}
