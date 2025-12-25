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

interface AllocationConfigProps {
    method: string;
    onChange: (method: string) => void;
    lookback: number;
    onLookbackChange: (val: number) => void;
}

export default function AllocationConfig({
    method,
    onChange,
    lookback,
    onLookbackChange,
}: AllocationConfigProps) {
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

            {/* Lookback Window (Stand-alone now) */}
            <div className="bg-[#0A0A0A] border border-white/5 rounded-lg p-4">
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
        </div>
    );
}
