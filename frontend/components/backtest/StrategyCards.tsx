"use client";

import React, { useState, useEffect } from "react";
import { Target, TrendingUp, ArrowUpDown, BarChart3, ChevronDown, ChevronRight, Activity } from "lucide-react";

interface StrategyContract {
    strategy_id: string;
    allowed_universes: string[];
    timeframe: string;
    holding_period: string;
    regime: string;
    when_loses: string;
    description: string;
}

interface StrategyConfig {
    id: string;
    params: Record<string, number>;
    enabled: boolean;
}

interface StrategyCardsProps {
    selection: StrategyConfig[];
    onChange: (selection: StrategyConfig[]) => void;
    compatibleStrategies?: string[];
}

const STRATEGY_ICONS: Record<string, any> = {
    "INTRADAY_MOMENTUM": TrendingUp,
    "INTRADAY_MEAN_REVERSION": Target,
    "OVERNIGHT_GAP": ArrowUpDown,
    "INDEX_MEAN_REVERSION": BarChart3
};

export default function StrategyCards({ selection, onChange, compatibleStrategies = [] }: StrategyCardsProps) {
    const [contracts, setContracts] = useState<StrategyContract[]>([]);
    const [expandedAdvanced, setExpandedAdvanced] = useState<string | null>(null);

    useEffect(() => {
        // Fetch strategy contracts from API
        fetch("http://localhost:8000/api/portfolio/strategy-contracts")
            .then((res) => res.json())
            .then((data) => setContracts(data.contracts || []))
            .catch((err) => console.error("Failed to fetch contracts", err));
    }, []);

    const toggleStrategy = (strategyId: string) => {
        const existing = selection.find((s) => s.id === strategyId);
        if (existing) {
            onChange(selection.map((s) => (s.id === strategyId ? { ...s, enabled: !s.enabled } : s)));
        } else {
            // Get default params from contract or use empty
            onChange([...selection, { id: strategyId, params: {}, enabled: true }]);
        }
    };

    const updateParam = (strategyId: string, key: string, value: number) => {
        onChange(
            selection.map((s) =>
                s.id === strategyId ? { ...s, params: { ...s.params, [key]: value } } : s
            )
        );
    };

    const isCompatible = (strategyId: string) => {
        if (!compatibleStrategies.length) return true; // No restriction if no universe selected
        return compatibleStrategies.includes(strategyId);
    };

    return (
        <div className="grid grid-cols-2 gap-3">
            {contracts.map((contract) => {
                const Icon = STRATEGY_ICONS[contract.strategy_id] || Activity;
                const config = selection.find((s) => s.id === contract.strategy_id);
                const isEnabled = config?.enabled || false;
                const compatible = isCompatible(contract.strategy_id);
                const isAdvancedExpanded = expandedAdvanced === contract.strategy_id;

                return (
                    <div
                        key={contract.strategy_id}
                        className={`p-3 rounded border transition-all ${!compatible
                            ? "bg-[#0A0A0A] border-white/5 opacity-50 cursor-not-allowed"
                            : isEnabled
                                ? "bg-cyan-600/10 border-cyan-500/30"
                                : "bg-[#0A0A0A] border-white/5 hover:border-white/10 cursor-pointer"
                            }`}
                    >
                        {/* Header */}
                        <div
                            className="flex items-start justify-between mb-2 cursor-pointer"
                            onClick={() => compatible && toggleStrategy(contract.strategy_id)}
                        >
                            <div className="flex items-center gap-2">
                                <Icon className={`w-3.5 h-3.5 ${isEnabled && compatible ? "text-cyan-400" : "text-gray-600"}`} />
                                <div>
                                    <div className={`text-[10px] font-bold uppercase tracking-wider ${isEnabled && compatible ? "text-cyan-400" : "text-gray-500"}`}>
                                        {contract.strategy_id.replace(/_/g, ' ')}
                                    </div>
                                    <div className="text-[8px] text-gray-700 uppercase">{contract.regime}</div>
                                </div>
                            </div>
                            <div className={`w-4 h-4 rounded-sm border flex items-center justify-center text-[8px] ${isEnabled && compatible ? "bg-cyan-600 border-cyan-500 text-white" : "bg-[#050505] border-white/10 text-gray-700"
                                }`}>
                                {isEnabled && compatible ? "✓" : ""}
                            </div>
                        </div>

                        {/* Timeframe & Holding (Read-only) */}
                        <div className="flex gap-2 mb-2">
                            <div className="flex-1 text-center py-1 bg-[#050505] border border-white/5 rounded">
                                <div className="text-[7px] text-gray-700 uppercase">Timeframe</div>
                                <div className="text-[9px] font-mono text-gray-400">{contract.timeframe}</div>
                            </div>
                            <div className="flex-1 text-center py-1 bg-[#050505] border border-white/5 rounded">
                                <div className="text-[7px] text-gray-700 uppercase">Holding</div>
                                <div className="text-[9px] font-mono text-gray-400">{contract.holding_period.replace('_', '-')}</div>
                            </div>
                        </div>

                        {/* Description */}
                        <p className="text-[9px] text-gray-500 leading-tight mb-2">{contract.description}</p>

                        {/* When it loses */}
                        {compatible && (
                            <div className="p-2 bg-rose-900/10 border border-rose-500/20 rounded mb-2">
                                <div className="text-[8px] text-rose-400 font-bold uppercase mb-1">When it typically loses:</div>
                                <p className="text-[8px] text-rose-300 leading-tight">{contract.when_loses}</p>
                            </div>
                        )}

                        {/* Advanced Parameters (Collapsed by default) */}
                        {isEnabled && compatible && (
                            <div className="pt-2 border-t border-white/5">
                                <button
                                    onClick={(e) => {
                                        e.stopPropagation();
                                        setExpandedAdvanced(isAdvancedExpanded ? null : contract.strategy_id);
                                    }}
                                    className="flex items-center gap-1 text-[9px] text-gray-600 hover:text-cyan-400 transition-colors uppercase tracking-wider font-semibold"
                                >
                                    {isAdvancedExpanded ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
                                    Advanced Parameters
                                </button>

                                {isAdvancedExpanded && (
                                    <div className="mt-2 space-y-1.5 p-2 bg-[#050505] border border-white/10 rounded" onClick={(e) => e.stopPropagation()}>
                                        <div className="text-[8px] text-yellow-400 mb-1">⚠️ Do not change unless researching</div>
                                        {/* Params would go here - for now showing placeholder */}
                                        <div className="text-[8px] text-gray-600">Parameters loaded from contract defaults</div>
                                    </div>
                                )}
                            </div>
                        )}

                        {/* Incompatible badge */}
                        {!compatible && (
                            <div className="mt-2 text-[8px] text-gray-700 text-center uppercase tracking-wider">
                                Incompatible with selected universe
                            </div>
                        )}
                    </div>
                );
            })}
        </div>
    );
}
