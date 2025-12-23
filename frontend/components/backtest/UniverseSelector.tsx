"use client";

import React, { useState, useEffect } from "react";
import { Database, Droplets, Activity, TrendingUp, Target, AlertCircle, Check, Info, Calendar, Layers } from "lucide-react";

interface Universe {
    id: string;
    name: string;
    description?: string;
}

interface UniverseSelectorProps {
    onSelect: (universeId: string) => void;
    selectedId?: string;
    onCompatibleStrategiesUpdate?: (strategies: string[]) => void;
}

// Enhanced universe profiles with comprehensive metadata
const UNIVERSE_PROFILES: Record<string, {
    liquidity: string;
    liquidityScore: number; // 0-100
    volatility: string;
    volatilityScore: number; // 0-100
    rebalance: string;
    designedFor: string;
    icon: any;
    color: string;
    constituents: number;
    avgMarketCap: string;
}> = {
    "NIFTY100_LIQUID_50": {
        liquidity: "HIGH",
        liquidityScore: 85,
        volatility: "MODERATE",
        volatilityScore: 45,
        rebalance: "MONTHLY",
        designedFor: "Momentum",
        icon: TrendingUp,
        color: "cyan",
        constituents: 50,
        avgMarketCap: "₹45,000 Cr"
    },
    "NIFTY100_MEAN_REV": {
        liquidity: "MODERATE",
        liquidityScore: 65,
        volatility: "LOW",
        volatilityScore: 25,
        rebalance: "MONTHLY",
        designedFor: "Mean Reversion",
        icon: Target,
        color: "purple",
        constituents: 50,
        avgMarketCap: "₹28,000 Cr"
    },
    "NIFTY50_ONLY": {
        liquidity: "HIGH",
        liquidityScore: 90,
        volatility: "MODERATE",
        volatilityScore: 40,
        rebalance: "NONE",
        designedFor: "Gap Reversion",
        icon: Activity,
        color: "emerald",
        constituents: 50,
        avgMarketCap: "₹1,20,000 Cr"
    },
    "NIFTY-INDEX": {
        liquidity: "VERY HIGH",
        liquidityScore: 100,
        volatility: "MODERATE",
        volatilityScore: 38,
        rebalance: "NONE",
        designedFor: "Index Strategies",
        icon: Database,
        color: "blue",
        constituents: 1,
        avgMarketCap: "Index"
    },
    "BANKNIFTY-INDEX": {
        liquidity: "VERY HIGH",
        liquidityScore: 98,
        volatility: "HIGH",
        volatilityScore: 65,
        rebalance: "NONE",
        designedFor: "Index Strategies",
        icon: Database,
        color: "orange",
        constituents: 1,
        avgMarketCap: "Index"
    }
};

export default function UniverseSelector({
    onSelect,
    selectedId,
    onCompatibleStrategiesUpdate
}: UniverseSelectorProps) {
    const [data, setData] = useState<{
        system_universes: Universe[];
        user_portfolios: Universe[];
    }>({ system_universes: [], user_portfolios: [] });
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);

    useEffect(() => {
        fetch("http://localhost:8000/api/portfolio-backtest/universes")
            .then((res) => {
                if (!res.ok) throw new Error(`API Error: ${res.status}`);
                return res.json();
            })
            .then((json) => {
                if (json.system_universes) {
                    setData(json);
                } else {
                    throw new Error("Invalid response format");
                }
                setLoading(false);
            })
            .catch((err) => {
                console.error("Failed to fetch universes", err);
                setError(err.message);
                setLoading(false);
            });
    }, []);

    useEffect(() => {
        if (selectedId) {
            fetch(`http://localhost:8000/api/portfolio/compatible-strategies/${selectedId}`)
                .then((res) => res.json())
                .then((data) => {
                    const compatible = data.compatible_strategies || [];
                    if (onCompatibleStrategiesUpdate) {
                        onCompatibleStrategiesUpdate(compatible);
                    }
                })
                .catch((err) => console.error("Failed to fetch compatible strategies", err));
        }
    }, [selectedId, onCompatibleStrategiesUpdate]);

    if (loading) return (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {[1, 2, 3].map(i => (
                <div key={i} className="h-40 bg-[#0A0A0A] border border-white/5 rounded-lg animate-pulse"></div>
            ))}
        </div>
    );

    if (error) return (
        <div className="px-4 py-3 bg-red-900/10 border border-red-500/20 rounded-lg text-red-400 text-sm flex items-center gap-2">
            <AlertCircle className="w-4 h-4" />
            Failed to load universes: {error}
        </div>
    );

    const allUniverses = [...data.system_universes, ...(data.user_portfolios || [])];

    if (allUniverses.length === 0) {
        return (
            <div className="px-4 py-6 bg-yellow-900/10 border border-yellow-500/20 rounded-lg text-yellow-400 text-sm flex items-center gap-2">
                <Info className="w-4 h-4" />
                No universes available. Please seed universes first.
            </div>
        );
    }

    return (
        <div className="space-y-3">
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                {allUniverses.map((universe) => {
                    const profile = UNIVERSE_PROFILES[universe.id];
                    const Icon = profile?.icon || Database;
                    const isSelected = selectedId === universe.id;
                    const colorMap: Record<string, { border: string; bg: string; text: string; icon: string }> = {
                        cyan: { border: "border-cyan-500/30", bg: "bg-cyan-600/10", text: "text-cyan-400", icon: "bg-cyan-500/20" },
                        purple: { border: "border-purple-500/30", bg: "bg-purple-600/10", text: "text-purple-400", icon: "bg-purple-500/20" },
                        emerald: { border: "border-emerald-500/30", bg: "bg-emerald-600/10", text: "text-emerald-400", icon: "bg-emerald-500/20" },
                        blue: { border: "border-blue-500/30", bg: "bg-blue-600/10", text: "text-blue-400", icon: "bg-blue-500/20" },
                        orange: { border: "border-orange-500/30", bg: "bg-orange-600/10", text: "text-orange-400", icon: "bg-orange-500/20" }
                    };
                    const colors = profile ? colorMap[profile.color] : colorMap.cyan;

                    return (
                        <button
                            key={universe.id}
                            onClick={() => onSelect(universe.id)}
                            className={`group relative p-5 rounded-lg border-2 transition-all text-left overflow-hidden ${isSelected
                                ? profile
                                    ? `${colors.border} ${colors.bg} shadow-lg ring-1 ring-white/10`
                                    : "border-cyan-500/30 bg-cyan-600/10 shadow-lg ring-1 ring-white/10"
                                : "bg-[#0A0A0A] border-white/5 hover:border-white/10 hover:bg-white/5"
                                }`}
                        >
                            {/* Header with Icon */}
                            <div className="flex items-start justify-between mb-4">
                                <div className="flex items-center gap-3">
                                    <div className={`w-12 h-12 rounded-lg flex items-center justify-center transition-all ${isSelected ? (profile ? colors.icon : "bg-cyan-500/20") : "bg-white/5"
                                        }`}>
                                        <Icon className={`w-6 h-6 ${isSelected ? (profile ? colors.text : "text-cyan-400") : "text-gray-600"}`} />
                                    </div>
                                    <div>
                                        <div className={`text-sm font-bold uppercase tracking-wide transition-colors ${isSelected ? (profile ? colors.text : "text-cyan-400") : "text-gray-300"
                                            }`}>
                                            {universe.name.replace(/_/g, ' ')}
                                        </div>
                                        {profile ? (
                                            <div className="flex items-center gap-1.5 mt-1">
                                                <Layers className="w-3 h-3 text-gray-600" />
                                                <span className="text-[10px] text-gray-500">{profile.constituents} stocks</span>
                                            </div>
                                        ) : (
                                            <div className="text-[10px] text-gray-600 mt-1">System Universe</div>
                                        )}
                                    </div>
                                </div>
                                {isSelected && (
                                    <div className={`w-6 h-6 rounded-full border flex items-center justify-center ${profile ? `${colors.bg} ${colors.border}` : "bg-cyan-600/10 border-cyan-500/30"
                                        }`}>
                                        <Check className={`w-4 h-4 ${profile ? colors.text : "text-cyan-400"}`} />
                                    </div>
                                )}
                            </div>

                            {/* Profile Metrics */}
                            {profile && (
                                <div className="space-y-3">
                                    {/* Liquidity Bar */}
                                    <div>
                                        <div className="flex justify-between items-center mb-1.5">
                                            <span className="text-[9px] text-gray-600 uppercase tracking-wider font-semibold">Liquidity Profile</span>
                                            <span className={`text-[10px] font-mono font-bold ${isSelected ? colors.text : "text-gray-400"}`}>
                                                {profile.liquidity}
                                            </span>
                                        </div>
                                        <div className="h-1.5 bg-white/5 rounded-full overflow-hidden">
                                            <div
                                                className={`h-full rounded-full transition-all ${isSelected ? colors.bg.replace('/10', '/30') : "bg-white/10"}`}
                                                style={{ width: `${profile.liquidityScore}%` }}
                                            ></div>
                                        </div>
                                    </div>

                                    {/* Volatility Bar */}
                                    <div>
                                        <div className="flex justify-between items-center mb-1.5">
                                            <span className="text-[9px] text-gray-600 uppercase tracking-wider font-semibold">Volatility Profile</span>
                                            <span className={`text-[10px] font-mono font-bold ${isSelected ? colors.text : "text-gray-400"}`}>
                                                {profile.volatility}
                                            </span>
                                        </div>
                                        <div className="h-1.5 bg-white/5 rounded-full overflow-hidden">
                                            <div
                                                className={`h-full rounded-full transition-all ${isSelected ? "bg-rose-500/30" : "bg-rose-500/10"}`}
                                                style={{ width: `${profile.volatilityScore}%` }}
                                            ></div>
                                        </div>
                                    </div>

                                    {/* Additional Info Grid */}
                                    <div className="grid grid-cols-2 gap-2 pt-2 border-t border-white/5">
                                        <div>
                                            <div className="text-[8px] text-gray-600 uppercase tracking-wider mb-0.5 flex items-center gap-1">
                                                <Calendar className="w-2.5 h-2.5" /> Rebalance
                                            </div>
                                            <div className={`text-[10px] font-medium ${isSelected ? "text-white" : "text-gray-300"}`}>
                                                {profile.rebalance}
                                            </div>
                                        </div>
                                        <div>
                                            <div className="text-[8px] text-gray-600 uppercase tracking-wider mb-0.5">Avg Mkt Cap</div>
                                            <div className={`text-[10px] font-medium font-mono ${isSelected ? "text-white" : "text-gray-300"}`}>
                                                {profile.avgMarketCap}
                                            </div>
                                        </div>
                                    </div>

                                    {/* Designed For Badge */}
                                    <div className={`mt-3 px-2.5 py-1.5 rounded-md text-center border ${isSelected
                                        ? `${colors.border} ${colors.bg}`
                                        : "border-white/10 bg-white/5"
                                        }`}>
                                        <div className="text-[8px] text-gray-600 uppercase tracking-wider mb-0.5">Optimized For</div>
                                        <div className={`text-[11px] font-bold ${isSelected ? colors.text : "text-gray-300"}`}>
                                            {profile.designedFor}
                                        </div>
                                    </div>
                                </div>
                            )}

                            {/* No Profile - Show Basic Info */}
                            {!profile && (
                                <div className="mt-3">
                                    <div className="px-3 py-2 bg-white/5 border border-white/10 rounded-md text-center">
                                        <div className="text-[10px] text-gray-500">
                                            {universe.description || "Universe available for selection"}
                                        </div>
                                    </div>
                                </div>
                            )}

                            {/* Selected Glow Effect */}
                            {isSelected && (
                                <div className={`absolute inset-0 ${colors.bg} opacity-5 pointer-events-none`}></div>
                            )}
                        </button>
                    );
                })}
            </div>

            {/* Selection Summary */}
            {selectedId && (
                <div className="flex items-center gap-2 px-4 py-2.5 bg-white/5 border border-white/10 rounded-lg">
                    <Check className="w-4 h-4 text-emerald-400" />
                    <span className="text-xs text-gray-400">Selected:</span>
                    <span className="text-sm font-medium text-white">{selectedId.replace(/_/g, ' ')}</span>
                    <span className="text-xs text-gray-600 ml-auto">Compatible strategies will load below</span>
                </div>
            )}
        </div>
    );
}
