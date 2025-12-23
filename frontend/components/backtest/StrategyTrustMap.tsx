"use client";

import React from "react";
import { TrendingUp, TrendingDown, Activity, AlertTriangle, CheckCircle } from "lucide-react";

interface StrategyTrustRow {
    strategy: string;
    weight: number;
    return_30d: number;
    drawdown: number;
    status: "normal" | "cautious" | "suspended";
    reason: string;
}

interface StrategyTrustMapProps {
    data?: StrategyTrustRow[];
}

const DEFAULT_DATA: StrategyTrustRow[] = [
    {
        strategy: "Momentum",
        weight: 0.42,
        return_30d: 0.082,
        drawdown: -0.051,
        status: "normal",
        reason: "Normal operations"
    },
    {
        strategy: "Mean Rev",
        weight: 0.38,
        return_30d: 0.065,
        drawdown: -0.032,
        status: "normal",
        reason: "Normal operations"
    },
    {
        strategy: "Gap Rev",
        weight: 0.15,
        return_30d: 0.021,
        drawdown: -0.083,
        status: "cautious",
        reason: "High DD"
    },
    {
        strategy: "Index MR",
        weight: 0.05,
        return_30d: -0.012,
        drawdown: -0.021,
        status: "cautious",
        reason: "Low activity"
    }
];

export default function StrategyTrustMap({ data = DEFAULT_DATA }: StrategyTrustMapProps) {
    const getStatusIcon = (status: string) => {
        switch (status) {
            case "normal":
                return <CheckCircle className="w-3.5 h-3.5 text-emerald-500" />;
            case "cautious":
                return <AlertTriangle className="w-3.5 h-3.5 text-yellow-500" />;
            case "suspended":
                return <Activity className="w-3.5 h-3.5 text-rose-500" />;
            default:
                return null;
        }
    };

    return (
        <div className="bg-[#0A0A0A] border border-white/5 rounded p-4">
            <h3 className="text-[10px] font-bold text-gray-400 uppercase tracking-wider mb-3">
                Strategy Trust Map
            </h3>

            <div className="overflow-x-auto">
                <table className="w-full text-[10px]">
                    <thead>
                        <tr className="border-b border-white/5">
                            <th className="text-left pb-2 text-[9px] text-gray-600 uppercase tracking-wider font-semibold">Strategy</th>
                            <th className="text-center pb-2 text-[9px] text-gray-600 uppercase tracking-wider font-semibold">Weight</th>
                            <th className="text-center pb-2 text-[9px] text-gray-600 uppercase tracking-wider font-semibold">30D Ret</th>
                            <th className="text-center pb-2 text-[9px] text-gray-600 uppercase tracking-wider font-semibold">Drawdown</th>
                            <th className="text-center pb-2 text-[9px] text-gray-600 uppercase tracking-wider font-semibold">Status</th>
                            <th className="text-left pb-2 text-[9px] text-gray-600 uppercase tracking-wider font-semibold">Reason</th>
                        </tr>
                    </thead>
                    <tbody>
                        {data.map((row, idx) => (
                            <tr key={idx} className="border-b border-white/5 hover:bg-white/5 transition-colors">
                                <td className="py-2 font-mono font-semibold text-gray-300">{row.strategy}</td>
                                <td className="py-2 text-center font-mono font-bold text-cyan-400">{(row.weight * 100).toFixed(0)}%</td>
                                <td className={`py-2 text-center font-mono font-bold ${row.return_30d >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                                    {row.return_30d >= 0 ? '+' : ''}{(row.return_30d * 100).toFixed(1)}%
                                </td>
                                <td className="py-2 text-center font-mono font-bold text-rose-400">{(row.drawdown * 100).toFixed(1)}%</td>
                                <td className="py-2 text-center flex items-center justify-center">
                                    {getStatusIcon(row.status)}
                                </td>
                                <td className="py-2 text-gray-500 text-[9px]">{row.reason}</td>
                            </tr>
                        ))}
                    </tbody>
                </table>
            </div>

            {/* Status Legend */}
            <div className="mt-3 pt-3 border-t border-white/5 flex gap-4 justify-center">
                <div className="flex items-center gap-1.5">
                    <CheckCircle className="w-3 h-3 text-emerald-500" />
                    <span className="text-[8px] text-gray-600 uppercase">Normal</span>
                </div>
                <div className="flex items-center gap-1.5">
                    <AlertTriangle className="w-3 h-3 text-yellow-500" />
                    <span className="text-[8px] text-gray-600 uppercase">Cautious</span>
                </div>
                <div className="flex items-center gap-1.5">
                    <Activity className="w-3 h-3 text-rose-500" />
                    <span className="text-[8px] text-gray-600 uppercase">Suspended</span>
                </div>
            </div>
        </div>
    );
}
