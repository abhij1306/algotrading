"use client";

import React from "react";
import { MessageSquare, ArrowRight } from "lucide-react";

interface AllocatorChange {
    strategy: string;
    old_weight: number;
    new_weight: number;
    delta: number;
    reason: string;
    recovery_condition: string;
    severity: string;
}

interface AllocatorExplanationProps {
    changes?: AllocatorChange[];
    date?: string;
}

const DEFAULT_CHANGES: AllocatorChange[] = [
    {
        strategy: "Gap Reversion",
        old_weight: 0.20,
        new_weight: 0.15,
        delta: -0.05,
        reason: "Drawdown exceeded -8% threshold",
        recovery_condition: "Will recover when DD improves to -5%",
        severity: "CAUTIOUS"
    },
    {
        strategy: "Index Mean Reversion",
        old_weight: 0.10,
        new_weight: 0.05,
        delta: -0.05,
        reason: "Only 2 trades in last 10 days (low activity)",
        recovery_condition: "Will recover when activity picks up",
        severity: "CAUTIOUS"
    }
];

export default function AllocatorExplanation({
    changes = DEFAULT_CHANGES,
    date = "2024-12-21"
}: AllocatorExplanationProps) {

    if (changes.length === 0) {
        return (
            <div className="bg-[#0A0A0A] border border-white/5 rounded p-4">
                <div className="flex items-center gap-2 mb-3">
                    <MessageSquare className="w-3.5 h-3.5 text-gray-600" />
                    <h3 className="text-[10px] font-bold text-gray-400 uppercase tracking-wider">
                        What Changed Today
                    </h3>
                </div>
                <div className="text-[10px] text-gray-500 text-center py-4">
                    No significant weight changes today. System is protecting capital.
                </div>
            </div>
        );
    }

    const defensiveCount = changes.filter(c => c.severity === "DEFENSIVE").length;

    return (
        <div className="bg-[#0A0A0A] border border-white/5 rounded p-4">
            <div className="flex items-center gap-2 mb-3">
                <MessageSquare className="w-3.5 h-3.5 text-gray-600" />
                <h3 className="text-[10px] font-bold text-gray-400 uppercase tracking-wider">
                    What Changed Today
                </h3>
                <span className="text-[8px] text-gray-700 font-mono">{date}</span>
            </div>

            <div className="space-y-3">
                {changes.map((change, idx) => (
                    <div key={idx} className="p-3 bg-[#050505] border border-white/10 rounded">
                        {/* Strategy & Weight Change */}
                        <div className="flex items-center justify-between mb-2">
                            <div className="flex items-center gap-2">
                                <span className="text-[10px] font-mono font-semibold text-gray-300">{change.strategy}</span>
                                <span className="text-[8px] text-gray-600">weight {change.delta > 0 ? 'increased' : 'reduced'}</span>
                            </div>
                            <div className="flex items-center gap-1.5 text-[10px] font-mono">
                                <span className="text-gray-500">{(change.old_weight * 100).toFixed(0)}%</span>
                                <ArrowRight className="w-3 h-3 text-gray-700" />
                                <span className={`font-bold ${change.delta > 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                                    {(change.new_weight * 100).toFixed(0)}%
                                </span>
                            </div>
                        </div>

                        {/* Reason */}
                        <div className="mb-2">
                            <div className="text-[8px] text-gray-700 uppercase tracking-wider mb-1">Reason:</div>
                            <div className="text-[9px] text-gray-400 leading-relaxed">{change.reason}</div>
                        </div>

                        {/* Recovery Condition */}
                        <div>
                            <div className="text-[8px] text-gray-700 uppercase tracking-wider mb-1">Recovery:</div>
                            <div className="text-[9px] text-gray-400 leading-relaxed">{change.recovery_condition}</div>
                        </div>
                    </div>
                ))}
            </div>

            {/* Overall Guidance */}
            <div className="mt-3 pt-3 border-t border-white/5">
                {defensiveCount > 0 ? (
                    <div className="text-[10px] text-yellow-400 flex items-center gap-2">
                        <div className="w-2 h-2 rounded-full bg-yellow-400"></div>
                        ⚠️ DEFENSIVE: Capital protection mode active. System is scaling down to preserve gains.
                    </div>
                ) : (
                    <div className="text-[10px] text-gray-600 flex items-center gap-2">
                        <div className="w-2 h-2 rounded-full bg-emerald-500"></div>
                        No action needed. System is protecting capital.
                    </div>
                )}
            </div>
        </div>
    );
}
