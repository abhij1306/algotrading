"use client";

import React from "react";
import { TrendingUp, Loader, BarChart3, Activity, Target } from "lucide-react";

interface PortfolioHealthProps {
    equity?: number;
    drawdown?: number;
    volatilityRegime?: string;
    riskState?: "NORMAL" | "CAUTIOUS" | "DEFENSIVE";
}

export default function PortfolioHealth({
    equity = 1124500,
    drawdown = -0.032,
    volatilityRegime = "MODERATE",
    riskState = "NORMAL"
}: PortfolioHealthProps) {

    const getRiskStateColor = (state: string) => {
        switch (state) {
            case "NORMAL":
                return "text-emerald-400 bg-emerald-600/20 border-emerald-500/30";
            case "CAUTIOUS":
                return "text-yellow-400 bg-yellow-600/20 border-yellow-500/30";
            case "DEFENSIVE":
                return "text-rose-400 bg-rose-600/20 border-rose-500/30";
            default:
                return "text-gray-400 bg-gray-600/20 border-gray-500/30";
        }
    };

    const returnPct = ((equity - 1000000) / 1000000) * 100;

    return (
        <div className="bg-[#0A0A0A] border border-white/5 rounded p-4">
            <h3 className="text-[10px] font-bold text-gray-400 uppercase tracking-wider mb-4">
                Portfolio Health
            </h3>

            <div className="grid grid-cols-4 gap-4">
                {/* Equity */}
                <div>
                    <div className="flex items-center gap-1.5 mb-1">
                        <TrendingUp className="w-3.5 h-3.5 text-cyan-600" />
                        <span className="text-[9px] text-gray-600 uppercase tracking-wider font-semibold">Equity</span>
                    </div>
                    <div className="text-xl font-bold font-mono text-cyan-400">₹{(equity / 1000).toFixed(0)}k</div>
                    <div className={`text-[10px] font-mono font-semibold ${returnPct >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                        {returnPct >= 0 ? '+' : ''}{returnPct.toFixed(2)}%
                    </div>
                </div>

                {/* Drawdown */}
                <div>
                    <div className="flex items-center gap-1.5 mb-1">
                        <BarChart3 className="w-3.5 h-3.5 text-rose-600" />
                        <span className="text-[9px] text-gray-600 uppercase tracking-wider font-semibold">Drawdown</span>
                    </div>
                    <div className="text-xl font-bold font-mono text-rose-400">{(drawdown * 100).toFixed(1)}%</div>
                    <div className="text-[9px] text-gray-600 font-mono">Current</div>
                </div>

                {/* Volatility Regime */}
                <div>
                    <div className="flex items-center gap-1.5 mb-1">
                        <Activity className="w-3.5 h-3.5 text-purple-600" />
                        <span className="text-[9px] text-gray-600 uppercase tracking-wider font-semibold">Volatility</span>
                    </div>
                    <div className="text-lg font-bold font-mono text-purple-400">{volatilityRegime}</div>
                    <div className="text-[9px] text-gray-600">Regime</div>
                </div>

                {/* Risk State */}
                <div>
                    <div className="flex items-center gap-1.5 mb-1">
                        <Target className="w-3.5 h-3.5 text-gray-600" />
                        <span className="text-[9px] text-gray-600 uppercase tracking-wider font-semibold">Risk State</span>
                    </div>
                    <div className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded border text-sm font-bold font-mono ${getRiskStateColor(riskState)}`}>
                        <div className="w-2 h-2 rounded-full bg-current"></div>
                        {riskState}
                    </div>
                </div>
            </div>
        </div>
    );
}
