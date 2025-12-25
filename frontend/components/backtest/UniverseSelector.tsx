"use client";

import React, { useState, useEffect } from "react";
import { Database, TrendingUp, Target, Activity, Layers, Check, Info } from "lucide-react";

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

// Enhanced universe profiles
const UNIVERSE_PROFILES: Record<string, {
    liquidity: string;
    liquidityScore: number;
    volatility: string;
    volatilityScore: number;
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
        avgMarketCap: "₹45k Cr"
    },
    "NIFTY100_MEAN_REV": {
        liquidity: "MODERATE",
        liquidityScore: 65,
        volatility: "LOW",
        volatilityScore: 25,
        rebalance: "MONTHLY",
        designedFor: "Mean Rev",
        icon: Target,
        color: "purple",
        constituents: 50,
        avgMarketCap: "₹28k Cr"
    },
    "NIFTY50_ONLY": {
        liquidity: "HIGH",
        liquidityScore: 90,
        volatility: "MODERATE",
        volatilityScore: 40,
        rebalance: "NONE",
        designedFor: "Gap Rev",
        icon: Activity,
        color: "emerald",
        constituents: 50,
        avgMarketCap: "₹120k Cr"
    },
    "NIFTY-INDEX": {
        liquidity: "VERY HIGH",
        liquidityScore: 100,
        volatility: "MODERATE",
        volatilityScore: 38,
        rebalance: "NONE",
        designedFor: "Index",
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
        designedFor: "Index",
        icon: Database,
        color: "orange",
        constituents: 1,
        avgMarketCap: "Index"
    },
    // Adding entry for seeded indices to use defaults gracefully or add specific ones
    "NIFTY_MIDCAP_100": {
        liquidity: "MED",
        liquidityScore: 60,
        volatility: "HIGH",
        volatilityScore: 70,
        rebalance: "SEMI",
        designedFor: "Growth",
        icon: TrendingUp,
        color: "orange",
        constituents: 100,
        avgMarketCap: "₹30k Cr"
    },
    "NIFTY_SMALLCAP_100": {
        liquidity: "LOW",
        liquidityScore: 40,
        volatility: "V.HIGH",
        volatilityScore: 90,
        rebalance: "SEMI",
        designedFor: "Aggressive",
        icon: TrendingUp,
        color: "rose",
        constituents: 100,
        avgMarketCap: "₹10k Cr"
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

    useEffect(() => {
        fetch("http://localhost:9000/api/portfolio-backtest/universes")
            .then(res => res.json())
            .then(json => {
                if (json.system_universes) setData(json);
                setLoading(false);
            })
            .catch(console.error);
    }, []);

    useEffect(() => {
        if (selectedId) {
            fetch(`http://localhost:9000/api/portfolio/compatible-strategies/${selectedId}`)
                .then(res => res.json())
                .then(data => {
                    const compatible = data.compatible_strategies || [];
                    if (onCompatibleStrategiesUpdate) onCompatibleStrategiesUpdate(compatible);
                })
                .catch(console.error);
        }
    }, [selectedId, onCompatibleStrategiesUpdate]);

    const allUniverses = [...data.system_universes, ...(data.user_portfolios || [])];

    if (loading) return (
        <div className="flex gap-4 overflow-hidden">
            {[1, 2, 3, 4].map(i => (
                <div key={i} className="min-w-[240px] h-32 bg-[#0A0A0A] border border-white/5 rounded-xl animate-pulse"></div>
            ))}
        </div>
    );

    return (
        <div className="w-full overflow-x-auto pb-6 [&::-webkit-scrollbar]:h-1.5 [&::-webkit-scrollbar-track]:bg-transparent [&::-webkit-scrollbar-thumb]:bg-white/10 [&::-webkit-scrollbar-thumb]:rounded-full hover:[&::-webkit-scrollbar-thumb]:bg-white/20 transition-colors">
            <div className="flex gap-4 min-w-max px-1">
                {allUniverses.map((universe) => {
                    const profile = UNIVERSE_PROFILES[universe.id];
                    const Icon = profile?.icon || Database;
                    const isSelected = selectedId === universe.id;
                    const colorMap: Record<string, string> = {
                        cyan: "text-cyan-400 bg-cyan-950/30 border-cyan-500/50",
                        purple: "text-purple-400 bg-purple-950/30 border-purple-500/50",
                        emerald: "text-emerald-400 bg-emerald-950/30 border-emerald-500/50",
                        blue: "text-blue-400 bg-blue-950/30 border-blue-500/50",
                        orange: "text-orange-400 bg-orange-950/30 border-orange-500/50",
                        rose: "text-rose-400 bg-rose-950/30 border-rose-500/50",
                    };
                    const theme = profile ? colorMap[profile.color] || colorMap.cyan : colorMap.cyan;

                    return (
                        <button
                            key={universe.id}
                            onClick={() => onSelect(universe.id)}
                            className={`
                                group relative w-[280px] flex-shrink-0 p-5 rounded-2xl border transition-all duration-300 text-left flex flex-col justify-between h-[160px]
                                ${isSelected
                                    ? `bg-[#0A0A0A] ${theme.split(' ')[2]} shadow-[0_0_30px_-10px_rgba(0,0,0,0.8)] ring-1 ring-white/10`
                                    : "bg-black/40 border-white/5 hover:border-white/20 hover:bg-white/5"
                                }
                            `}
                        >
                            {/* Header */}
                            <div className="flex items-start justify-between w-full">
                                <div className="flex items-center gap-3">
                                    <div className={`p-2 rounded-lg transition-colors ${isSelected ? theme.split(' ')[1] : "bg-white/5"}`}>
                                        <Icon className={`w-5 h-5 ${isSelected ? theme.split(' ')[0] : "text-gray-500"}`} />
                                    </div>
                                    <div>
                                        <div className={`text-xs font-bold text-white uppercase tracking-wider truncate max-w-[140px] ${isSelected ? "text-white" : "text-gray-400"}`}>
                                            {universe.name.replace(/_/g, ' ')}
                                        </div>
                                        <div className="text-[10px] text-gray-600 font-mono mt-0.5">
                                            {profile?.constituents || 1} Constituents
                                        </div>
                                    </div>
                                </div>
                                {isSelected && <div className={`w-5 h-5 rounded-full flex items-center justify-center ${theme.split(' ')[1]}`}><Check className={`w-3 h-3 ${theme.split(' ')[0]}`} /></div>}
                            </div>

                            {/* Metrics Strip */}
                            {profile ? (
                                <div className="space-y-3 mt-4">
                                    {/* Liquidity/Vol Mini Bars */}
                                    <div className="flex flex-col gap-1.5">
                                        <div className="flex justify-between items-center text-[9px] uppercase tracking-wider text-gray-500">
                                            <span>Liq</span>
                                            <div className="w-16 h-1 bg-white/10 rounded-full overflow-hidden">
                                                <div className={`h-full ${theme.split(' ')[0].replace('text-', 'bg-')}`} style={{ width: `${profile.liquidityScore}%` }}></div>
                                            </div>
                                        </div>
                                        <div className="flex justify-between items-center text-[9px] uppercase tracking-wider text-gray-500">
                                            <span>Vol</span>
                                            <div className="w-16 h-1 bg-white/10 rounded-full overflow-hidden">
                                                <div className="h-full bg-gray-500" style={{ width: `${profile.volatilityScore}%` }}></div>
                                            </div>
                                        </div>
                                    </div>

                                    <div className="flex items-center gap-2 pt-3 border-t border-white/5">
                                        <span className={`px-2 py-0.5 rounded text-[9px] font-bold uppercase tracking-wider ${theme.split(' ')[1]} ${theme.split(' ')[0]}`}>
                                            {profile.designedFor}
                                        </span>
                                        <div className="ml-auto text-[10px] text-gray-500 font-mono">
                                            {profile.avgMarketCap}
                                        </div>
                                    </div>
                                </div>
                            ) : (
                                <div className="mt-auto">
                                    <p className="text-[10px] text-gray-600 line-clamp-2">
                                        {universe.description || "System generated universe."}
                                    </p>
                                </div>
                            )}

                            {/* Hover Glow */}
                            <div className={`absolute inset-0 rounded-2xl transition-opacity duration-300 opacity-0 group-hover:opacity-100 pointer-events-none ${isSelected ? "hidden" : "radial-gradient-glow"}`}></div>
                        </button>
                    );
                })}
            </div>
        </div>
    );
}
