"use client";

import React, { useState, useEffect } from "react";
import UniverseSelector from "@/components/backtest/UniverseSelector";
import StrategyCards from "@/components/backtest/StrategyCards";
import AllocationConfig from "@/components/backtest/AllocationConfig";
import PortfolioHealth from "@/components/backtest/PortfolioHealth";
import StrategyTrustMap from "@/components/backtest/StrategyTrustMap";
import AllocatorExplanation from "@/components/backtest/AllocatorExplanation";
import ResearchDashboard from "@/components/backtest/ResearchDashboard";
import { PlayCircle, Loader, TestTube, Activity, BarChart3 } from "lucide-react";

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

type InternalTab = "tester" | "live_monitor" | "research";

export default function PortfolioResearchSystem() {
    // Internal navigation (Tester | Live Monitor | Research)
    const [activeTab, setActiveTab] = useState<InternalTab>("tester");
    const [runId, setRunId] = useState<string | null>(null);
    const [liveData, setLiveData] = useState<any>(null);

    // Setup/Tester state
    const [universeId, setUniverseId] = useState("");
    const [compatibleStrategies, setCompatibleStrategies] = useState<string[]>([]);
    const [strategies, setStrategies] = useState<StrategyConfig[]>([]);
    const [allocationMethod, setAllocationMethod] = useState("INVERSE_VOLATILITY");
    const [lookback, setLookback] = useState(30);
    const [policySettings, setPolicySettings] = useState<PolicySettings>({
        correlationPenalty: "medium",
        cautiousThreshold: -8,
        defensiveThreshold: -15,
        defensiveAction: "scale_60"
    });
    const [showConfirmModal, setShowConfirmModal] = useState(false);
    const [loading, setLoading] = useState(false);

    // Fetch live state when runId exists and on Live Monitor tab
    useEffect(() => {
        if (runId && activeTab === "live_monitor") {
            fetch(`http://localhost:8000/api/portfolio/live-state?run_id=${runId}`)
                .then(res => {
                    if (!res.ok) throw new Error("Failed to fetch live state");
                    return res.json();
                })
                .then(data => setLiveData(data))
                .catch(err => console.error(err));
        }
    }, [runId, activeTab]);

    const executeBacktest = () => {
        if (!universeId) {
            alert("Please select a universe first.");
            return;
        }
        const enabledStrats = strategies.filter((s) => s.enabled);
        if (enabledStrats.length === 0) {
            alert("Please enable at least one strategy.");
            return;
        }
        // Show confirmation modal
        setShowConfirmModal(true);
    };

    const runBacktest = async () => {
        setShowConfirmModal(false);
        setLoading(true);

        const enabledStrats = strategies.filter((s) => s.enabled);

        try {
            const response = await fetch("http://localhost:8000/api/portfolio-backtest/run", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    universe_id: universeId,
                    strategies: enabledStrats.map((s) => ({ id: s.id, params: s.params })),
                    allocation_method: allocationMethod,
                    lookback: lookback,
                    policy_settings: policySettings,
                    start_date: "2024-01-01",
                    end_date: "2024-12-31"
                })
            });

            const data = await response.json();
            if (data.run_id) {
                setRunId(data.run_id);
                setActiveTab("live_monitor"); // Auto-switch to Live Monitor
            }
        } catch (err) {
            console.error("Backtest failed", err);
            alert("Backtest failed to execute.");
        } finally {
            setLoading(false);
        }
    };

    const internalTabs = [
        { id: "tester" as InternalTab, label: "Tester", icon: TestTube, description: "Backtest + Replay" },
        { id: "live_monitor" as InternalTab, label: "Live Monitor", icon: Activity, description: "Default Daily View" },
        { id: "research" as InternalTab, label: "Research", icon: BarChart3, description: "Analysis Only" }
    ];

    return (
        <div className="h-full flex flex-col bg-[#050505] text-gray-200">
            {/* Internal Tab Navigation (Tester | Live Monitor | Research) */}
            <div className="flex items-center gap-1 px-6 py-3 bg-[#0A0A0A] border-b border-white/5">
                {internalTabs.map((tab) => {
                    const Icon = tab.icon;
                    return (
                        <button
                            key={tab.id}
                            onClick={() => setActiveTab(tab.id)}
                            className={`px-4 py-2.5 rounded-md text-xs font-medium transition-all flex items-center gap-2 ${activeTab === tab.id
                                ? "bg-white/10 text-white border border-white/10"
                                : "text-gray-500 hover:text-gray-300 hover:bg-white/5"
                                }`}
                        >
                            <Icon className="w-4 h-4" />
                            <div className="flex flex-col items-start">
                                <span className="uppercase tracking-wide">{tab.label}</span>
                                <span className="text-[9px] opacity-60 font-normal">{tab.description}</span>
                            </div>
                        </button>
                    );
                })}

                {runId && (
                    <div className="ml-auto px-3 py-1.5 bg-emerald-500/10 border border-emerald-500/20 text-emerald-400 text-xs rounded-full font-mono">
                        Active Run: {runId.slice(0, 8)}
                    </div>
                )}
            </div>

            {/* Tab Content */}
            <div className="flex-1 overflow-y-auto p-6 scrollbar-thin scrollbar-thumb-white/10 scrollbar-track-transparent">

                {/* TESTER TAB */}
                {activeTab === "tester" && (
                    <div className="max-w-5xl mx-auto space-y-8 animate-in fade-in duration-300">
                        {/* Header */}
                        <div>
                            <h2 className="text-xl font-medium text-white mb-2">Backtest Setup</h2>
                            <p className="text-sm text-gray-500">
                                Create one frozen experiment to understand behavior — not to tune.
                            </p>
                        </div>

                        {/* A. Universe Selection */}
                        <section className="space-y-4">
                            <div className="flex items-center gap-3">
                                <div className="w-7 h-7 rounded-full bg-cyan-500/10 flex items-center justify-center border border-cyan-500/20 text-cyan-400 text-sm font-bold">
                                    1
                                </div>
                                <div>
                                    <h3 className="text-sm font-medium text-gray-200">Universe Selection</h3>
                                    <p className="text-xs text-gray-600">Selecting a universe filters strategies. Incompatible strategies never appear.</p>
                                </div>
                            </div>
                            <UniverseSelector
                                onSelect={(id) => setUniverseId(id)}
                                selectedId={universeId}
                                onCompatibleStrategiesUpdate={setCompatibleStrategies}
                            />
                        </section>

                        {/* B. Strategy Selection */}
                        <section className="space-y-4">
                            <div className="flex items-center gap-3">
                                <div className="w-7 h-7 rounded-full bg-cyan-500/10 flex items-center justify-center border border-cyan-500/20 text-cyan-400 text-sm font-bold">
                                    2
                                </div>
                                <div>
                                    <h3 className="text-sm font-medium text-gray-200">Strategy Selection</h3>
                                    <p className="text-xs text-gray-600">
                                        Card-based tiles with mandatory "WHEN IT LOSES" warnings. Simple toggle only.
                                    </p>
                                </div>
                            </div>
                            <StrategyCards
                                selection={strategies}
                                onChange={setStrategies}
                                compatibleStrategies={compatibleStrategies}
                            />
                        </section>

                        {/* C. Portfolio Policy */}
                        <section className="space-y-4">
                            <div className="flex items-center gap-3">
                                <div className="w-7 h-7 rounded-full bg-cyan-500/10 flex items-center justify-center border border-cyan-500/20 text-cyan-400 text-sm font-bold">
                                    3
                                </div>
                                <div>
                                    <h3 className="text-sm font-medium text-gray-200">Portfolio Policy</h3>
                                    <p className="text-xs text-gray-600">
                                        Allocation method and risk thresholds. This is the only place to adjust behavior.
                                    </p>
                                </div>
                            </div>
                            <AllocationConfig
                                method={allocationMethod}
                                onChange={setAllocationMethod}
                                lookback={lookback}
                                onLookbackChange={setLookback}
                            />
                        </section>

                        {/* D. Execute Backtest */}
                        <div className="flex justify-center pt-6 border-t border-white/5">
                            <button
                                onClick={runBacktest}
                                disabled={loading}
                                className={`px-8 py-3 rounded-md font-medium text-sm flex items-center gap-2 transition-all shadow-lg uppercase tracking-wide ${loading
                                    ? "bg-white/5 text-gray-500 cursor-not-allowed"
                                    : "bg-cyan-600 hover:bg-cyan-500 text-white shadow-cyan-900/20"
                                    }`}
                            >
                                {loading ? <Loader className="w-4 h-4 animate-spin" /> : <PlayCircle className="w-4 h-4" />}
                                {loading ? "Executing..." : "Execute Backtest"}
                            </button>
                        </div>
                    </div>
                )}

                {/* LIVE MONITOR TAB */}
                {activeTab === "live_monitor" && (
                    <div className="max-w-7xl mx-auto space-y-6 animate-in fade-in duration-300">
                        {!runId ? (
                            /* No Active Run */
                            <div className="flex flex-col items-center justify-center h-[60vh] text-center space-y-4">
                                <div className="w-20 h-20 rounded-full bg-white/5 flex items-center justify-center">
                                    <Activity className="w-10 h-10 text-gray-600" />
                                </div>
                                <div>
                                    <h3 className="text-lg font-medium text-gray-300">No Active Run</h3>
                                    <p className="text-sm text-gray-500 mt-1 max-w-md">
                                        Execute a backtest from the Tester tab to populate Live Monitor.
                                    </p>
                                </div>
                                <button
                                    onClick={() => setActiveTab("tester")}
                                    className="px-4 py-2 bg-cyan-600 hover:bg-cyan-500 text-white text-sm font-medium rounded-md transition-colors"
                                >
                                    Go to Tester
                                </button>
                            </div>
                        ) : !liveData ? (
                            /* Loading */
                            <div className="flex items-center justify-center h-[60vh]">
                                <Loader className="w-8 h-8 text-cyan-500 animate-spin" />
                            </div>
                        ) : (
                            /* Live Monitor Content */
                            <>
                                {/* Header */}
                                <div className="flex items-center justify-between mb-4">
                                    <div>
                                        <h2 className="text-xl font-medium text-white">Live Monitor</h2>
                                        <div className="flex items-center gap-2 text-sm text-gray-500 mt-1">
                                            <span className="w-2 h-2 rounded-full bg-emerald-500 animate-pulse"></span>
                                            Mode: Backtest • Last Updated: {liveData.portfolio_health.date}
                                        </div>
                                    </div>
                                </div>

                                <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
                                    {/* Left: Portfolio Health */}
                                    <div className="space-y-6">
                                        <PortfolioHealth
                                            equity={liveData.portfolio_health.equity}
                                            drawdown={liveData.portfolio_health.drawdown}
                                            volatilityRegime={liveData.portfolio_health.volatility_regime}
                                            riskState={liveData.portfolio_health.risk_state}
                                        />

                                        {/* Market Context (if available) */}
                                        {liveData.market_context && Object.keys(liveData.market_context).length > 1 && (
                                            <div className="bg-[#0A0A0A] border border-white/5 rounded-lg p-5">
                                                <h3 className="text-xs font-bold text-gray-500 uppercase tracking-wider mb-4">
                                                    Market Context
                                                </h3>
                                                <div className="space-y-3">
                                                    <div className="flex justify-between items-center text-sm">
                                                        <span className="text-gray-400">Volatility Regime</span>
                                                        <span className="font-mono font-medium text-white">
                                                            {liveData.market_context.volatility_regime}
                                                        </span>
                                                    </div>
                                                    <div className="h-px bg-white/5"></div>
                                                    <p className="text-xs text-gray-600 italic">
                                                        Context only — not signals.
                                                    </p>
                                                </div>
                                            </div>
                                        )}
                                    </div>

                                    {/* Right: Trust Map & Allocator Explanation */}
                                    <div className="lg:col-span-2 space-y-6">
                                        <StrategyTrustMap
                                            data={Object.entries(liveData.strategy_weights || {}).map(([id, weight]: [string, any]) => ({
                                                strategy: id,
                                                weight: weight,
                                                return_30d: 0, // Placeholder as liveData format is unknown
                                                drawdown: 0,   // Placeholder
                                                status: "normal",
                                                reason: liveData.allocator_decisions?.[id] || "Active"
                                            }))}
                                        />
                                        <AllocatorExplanation
                                            changes={liveData.allocator_decisions}
                                            date={liveData.portfolio_health.date}
                                        />
                                    </div>
                                </div>
                            </>
                        )}
                    </div>
                )}

                {/* RESEARCH TAB */}
                {activeTab === "research" && (
                    <div className="max-w-7xl mx-auto animate-in fade-in duration-300">
                        {!runId ? (
                            <div className="flex flex-col items-center justify-center h-[60vh] text-center space-y-4">
                                <div className="w-20 h-20 rounded-full bg-white/5 flex items-center justify-center">
                                    <BarChart3 className="w-10 h-10 text-gray-600" />
                                </div>
                                <div>
                                    <h3 className="text-lg font-medium text-gray-300">No Research Data</h3>
                                    <p className="text-sm text-gray-500 mt-1 max-w-md">
                                        Execute a backtest to populate research analytics.
                                    </p>
                                </div>
                            </div>
                        ) : (
                            <ResearchDashboard runId={runId} />
                        )}
                    </div>
                )}
            </div>
        </div>
    );
}
