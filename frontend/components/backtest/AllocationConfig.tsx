"use client";

import React, { useState } from "react";
import { Scale, TrendingDown, Network, Shield, ChevronDown, ChevronUp } from "lucide-react";

const ALLOCATION_METHODS = [
    {
        id: "EQUAL_WEIGHT",
        name: "Equal Trust",
        icon: Scale,
        optimizes: "Simplicity & Stability",
        helps: "When you have high confidence in all strategies equally",
        fails: "Ignores individual strategy risk profiles",
        color: "blue"
    },
    {
        id: "INVERSE_VOLATILITY",
        name: "Risk Normalized",
        icon: TrendingDown,
        optimizes: "Risk-Adjusted Allocation",
        helps: "Weights strategies inversely to their historical volatility targets",
        fails: "Past volatility != future volatility",
        color: "cyan"
    },
    {
        id: "CORRELATION_PENALIZED",
        name: "Correlation Penalized",
        icon: Network,
        optimizes: "Portfolio Diversification",
        helps: "Reduces exposure to clusters with high pair-wise correlation",
        fails: "Correlations are unstable in crisis periods",
        color: "purple"
    },
    {
        id: "CAPITAL_PROTECTION",
        name: "Capital Protection",
        icon: Shield,
        optimizes: "Drawdown Minimization",
        helps: "Prioritizes capital preservation over alpha generation during stress",
        fails: "May underperform in strong trending markets",
        color: "emerald"
    }
];

interface PolicySettings {
    correlationPenalty: "low" | "medium" | "high";
    cautiousThreshold: number;
    defensiveThreshold: number;
    defensiveAction: string;
}

interface AllocationConfigProps {
    method: string;
    onChange: (method: string) => void;
    lookback: number;
    onLookbackChange: (val: number) => void;
    policySettings?: PolicySettings;
    onPolicySettingsChange?: (settings: PolicySettings) => void;
}

export default function AllocationConfig({
    method,
    onChange,
    lookback,
    onLookbackChange,
    policySettings = {
        correlationPenalty: "medium",
        cautiousThreshold: -8,
        defensiveThreshold: -15,
        defensiveAction: "scale_60"
    },
    onPolicySettingsChange
}: AllocationConfigProps) {
    const [showPolicySettings, setShowPolicySettings] = useState(false);

    const updateSetting = (key: keyof PolicySettings, value: any) => {
        if (onPolicySettingsChange) {
            onPolicySettingsChange({ ...policySettings, [key]: value });
        }
    };

    return (
        <div className="space-y-4">
            {/* Allocation Method Cards */}
            <div>
                <h4 className="text-xs font-medium text-gray-400 mb-3">Allocation Method</h4>
                <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                    {ALLOCATION_METHODS.map((m) => {
                        const Icon = m.icon;
                        const isSelected = method === m.id;
                        const colorMap: Record<string, string> = {
                            blue: isSelected ? "border-blue-500/30 bg-blue-600/10 text-blue-400" : "",
                            cyan: isSelected ? "border-cyan-500/30 bg-cyan-600/10 text-cyan-400" : "",
                            purple: isSelected ? "border-purple-500/30 bg-purple-600/10 text-purple-400" : "",
                            emerald: isSelected ? "border-emerald-500/30 bg-emerald-600/10 text-emerald-400" : ""
                        };

                        return (
                            <button
                                key={m.id}
                                onClick={() => onChange(m.id)}
                                className={`group relative p-4 rounded-lg border transition-all text-left ${isSelected
                                    ? colorMap[m.color]
                                    : "bg-[#0A0A0A] border-white/5 hover:border-white/10 hover:bg-white/5"
                                    }`}
                            >
                                {/* Icon */}
                                <div className={`w-8 h-8 rounded-md flex items-center justify-center mb-3 ${isSelected
                                    ? `bg-${m.color}-500/20`
                                    : "bg-white/5"
                                    }`}>
                                    <Icon className={`w-4 h-4 ${isSelected ? `text-${m.color}-400` : "text-gray-600"
                                        }`} />
                                </div>

                                {/* Name */}
                                <div className={`text-xs font-bold uppercase tracking-wide mb-1 ${isSelected ? "text-white" : "text-gray-400"
                                    }`}>
                                    {m.name}
                                </div>

                                {/* What it optimizes */}
                                <div className="text-[9px] text-gray-600 mb-2">{m.optimizes}</div>

                                {/* Helps/Fails on hover or when selected */}
                                {isSelected && (
                                    <div className="mt-3 pt-3 border-t border-white/10 space-y-1.5 text-[8px]">
                                        <div>
                                            <span className="text-emerald-400 font-semibold">When it helps: </span>
                                            <span className="text-gray-500">{m.helps}</span>
                                        </div>
                                        <div>
                                            <span className="text-rose-400 font-semibold">When it fails: </span>
                                            <span className="text-gray-500">{m.fails}</span>
                                        </div>
                                    </div>
                                )}
                            </button>
                        );
                    })}
                </div>
            </div>

            {/* Policy Settings (Collapsed by default) */}
            <div className="bg-[#0A0A0A] border border-white/5 rounded-lg overflow-hidden">
                <button
                    onClick={() => setShowPolicySettings(!showPolicySettings)}
                    className="w-full px-4 py-3 flex items-center justify-between text-left hover:bg-white/5 transition-colors"
                >
                    <div className="flex items-center gap-2">
                        <div className="text-xs font-medium text-gray-400 uppercase tracking-wider">Policy Settings</div>
                        <div className="text-[9px] text-gray-600">(Fine-tune sensitivity parameters)</div>
                    </div>
                    {showPolicySettings ? <ChevronUp className="w-4 h-4 text-gray-600" /> : <ChevronDown className="w-4 h-4 text-gray-600" />}
                </button>

                {showPolicySettings && (
                    <div className="px-4 pb-4 space-y-4 border-t border-white/5">
                        {/* Lookback Window */}
                        <div className="pt-4">
                            <label className="text-xs text-gray-400 mb-2 block">Allocator Lookback Window</label>
                            <div className="flex items-center gap-3">
                                <input
                                    type="range"
                                    min="14"
                                    max="90"
                                    value={lookback}
                                    onChange={(e) => onLookbackChange(parseInt(e.target.value))}
                                    className="flex-1 h-1 bg-white/10 rounded-full appearance-none cursor-pointer [&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:w-3 [&::-webkit-slider-thumb]:h-3 [&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:bg-cyan-500"
                                />
                                <div className="flex items-center gap-1 px-3 py-1.5 bg-[#050505] border border-white/10 rounded-md">
                                    <span className="text-sm font-mono text-cyan-400 font-bold">{lookback}</span>
                                    <span className="text-xs text-gray-600">days</span>
                                </div>
                            </div>
                            <div className="flex justify-between mt-1 text-[8px] text-gray-700">
                                <span>14d (Reactive)</span>
                                <span>90d (Conservative)</span>
                            </div>
                        </div>

                        {/* Correlation Penalty Strength */}
                        <div>
                            <label className="text-xs text-gray-400 mb-2 block">Correlation Penalty Strength</label>
                            <div className="grid grid-cols-3 gap-2">
                                {(["low", "medium", "high"] as const).map(level => (
                                    <button
                                        key={level}
                                        onClick={() => updateSetting("correlationPenalty", level)}
                                        className={`px-3 py-2 rounded text-xs transition-colors capitalize ${policySettings.correlationPenalty === level
                                                ? "bg-cyan-600/20 border-cyan-500/30 text-cyan-400 border"
                                                : "bg-[#050505] border border-white/10 text-gray-400 hover:border-white/20"
                                            }`}
                                    >
                                        {level}
                                    </button>
                                ))}
                            </div>
                        </div>

                        {/* Portfolio DD Thresholds */}
                        <div>
                            <label className="text-xs text-gray-400 mb-2 block">Portfolio Drawdown Thresholds</label>
                            <div className="space-y-2">
                                <div className="flex items-center justify-between p-2 bg-[#050505] border border-yellow-500/20 rounded">
                                    <span className="text-xs text-yellow-400 uppercase tracking-wide">Cautious</span>
                                    <div className="flex items-center gap-2">
                                        <input
                                            type="number"
                                            value={policySettings.cautiousThreshold}
                                            onChange={(e) => updateSetting("cautiousThreshold", parseFloat(e.target.value))}
                                            step="0.5"
                                            className="w-16 bg-black/50 border border-white/10 rounded px-2 py-1 text-xs text-right text-yellow-400 font-mono"
                                        />
                                        <span className="text-xs text-gray-600">%</span>
                                    </div>
                                </div>
                                <div className="flex items-center justify-between p-2 bg-[#050505] border border-red-500/20 rounded">
                                    <span className="text-xs text-red-400 uppercase tracking-wide">Defensive</span>
                                    <div className="flex items-center gap-2">
                                        <input
                                            type="number"
                                            value={policySettings.defensiveThreshold}
                                            onChange={(e) => updateSetting("defensiveThreshold", parseFloat(e.target.value))}
                                            step="0.5"
                                            className="w-16 bg-black/50 border border-white/10 rounded px-2 py-1 text-xs text-right text-red-400 font-mono"
                                        />
                                        <span className="text-xs text-gray-600">%</span>
                                    </div>
                                </div>
                            </div>
                        </div>

                        {/* Defensive Action */}
                        <div>
                            <label className="text-xs text-gray-400 mb-2 block">Defensive Action</label>
                            <select
                                value={policySettings.defensiveAction}
                                onChange={(e) => updateSetting("defensiveAction", e.target.value)}
                                className="w-full px-3 py-2 bg-[#050505] border border-white/10 rounded text-xs text-gray-300"
                            >
                                <option value="scale_60">Scale exposure to 60%</option>
                                <option value="scale_40">Scale exposure to 40%</option>
                                <option value="freeze">Freeze new positions</option>
                                <option value="exit_all">Exit all positions</option>
                            </select>
                        </div>

                        <div className="pt-2 border-t border-white/5 text-[9px] text-yellow-400 italic">
                            ⚠️ Only adjust these if you understand the implications. Default values are tested.
                        </div>
                    </div>
                )}
            </div>
        </div>
    );
}
