"use client";

import React, { useState, useEffect } from "react";
import {
    Clock,
    Check,
    ChevronDown,
    ChevronUp,
    AlertTriangle,
    Info,
    Search,
    Shield,
    XCircle,
    Activity
} from "lucide-react";

interface StrategyConfig {
    id: string;
    params: Record<string, number>;
    enabled: boolean;
}

interface StrategyCardsProps {
    selection: StrategyConfig[];
    onChange: (selection: StrategyConfig[]) => void;
    compatibleStrategies: string[];
}

interface Contract {
    strategy_id: string;
    allowed_universes: string[];
    timeframe: string;
    holding_period: string;
    regime: string;
    description: string;
    when_loses: string;
    parameters?: Record<string, any>;
}

export default function StrategyCards({
    selection,
    onChange,
    compatibleStrategies
}: StrategyCardsProps) {
    const [contracts, setContracts] = useState<Contract[]>([]);
    const [expandedRow, setExpandedRow] = useState<string | null>(null);
    const [searchTerm, setSearchTerm] = useState("");

    useEffect(() => {
        fetch("http://localhost:9000/api/portfolio/strategy-contracts")
            .then((res) => res.json())
            .then((data) => setContracts(data.contracts || []))
            .catch((err) => console.error("Failed to fetch contracts", err));
    }, []);

    const toggleStrategy = (id: string) => {
        const exists = selection.find((s) => s.id === id);
        if (exists) {
            onChange(selection.map(s => s.id === id ? { ...s, enabled: !s.enabled } : s));
        } else {
            const contract = contracts.find(c => c.strategy_id === id);
            const defaultParams = contract?.parameters || {};
            onChange([...selection, { id, params: defaultParams, enabled: true }]);
        }
    };

    const toggleRow = (id: string, e: React.MouseEvent) => {
        e.stopPropagation();
        setExpandedRow(expandedRow === id ? null : id);
    };

    const filteredContracts = contracts.filter(c =>
        c.strategy_id.toLowerCase().includes(searchTerm.toLowerCase()) ||
        c.regime.toLowerCase().includes(searchTerm.toLowerCase())
    );

    return (
        <div className="bg-[#050505] border border-white/5 rounded-2xl overflow-hidden flex flex-col shadow-[0_0_50px_-20px_rgba(0,0,0,0.5)]">
            {/* Toolbar */}
            <div className="px-6 py-4 border-b border-white/5 flex items-center justify-between bg-black/20">
                <div className="relative group">
                    <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-gray-600 group-focus-within:text-cyan-500 transition-colors" />
                    <input
                        type="text"
                        placeholder="Search strategies..."
                        className="bg-white/[0.03] border border-white/5 rounded-full pl-9 pr-4 py-2 text-xs text-white focus:outline-none focus:border-cyan-500/30 focus:bg-white/[0.05] w-64 transition-all"
                        value={searchTerm}
                        onChange={(e) => setSearchTerm(e.target.value)}
                    />
                </div>

                <div className="flex gap-4 text-[10px] font-mono uppercase tracking-widest text-gray-600 border-l border-white/5 pl-6">
                    <div className="flex items-center gap-2">
                        <div className="w-2 h-2 rounded-full bg-cyan-500"></div>
                        <span>Trend</span>
                    </div>
                    <div className="flex items-center gap-2">
                        <div className="w-2 h-2 rounded-full bg-purple-500"></div>
                        <span>Mean Rev</span>
                    </div>
                    <div className="flex items-center gap-2">
                        <div className="w-2 h-2 rounded-full bg-amber-500"></div>
                        <span>Gap</span>
                    </div>
                </div>
            </div>

            {/* List Header */}
            <div className="grid grid-cols-12 gap-8 px-8 py-3 bg-white/[0.01] border-b border-white/5 text-[10px] uppercase tracking-widest text-gray-600 font-bold select-none">
                <div className="col-span-4 pl-12">Alpha Source</div>
                <div className="col-span-2">Regime</div>
                <div className="col-span-2">Timeframe</div>
                <div className="col-span-3">Risk Profile</div>
                <div className="col-span-1 text-right">Details</div>
            </div>

            {/* List Body */}
            <div className="divide-y divide-white/[0.03]">
                {filteredContracts.map((contract) => {
                    const isCompatible = compatibleStrategies.includes(contract.strategy_id);
                    const config = selection.find(s => s.id === contract.strategy_id);
                    const isSelected = config?.enabled || false;
                    const isRowExpanded = expandedRow === contract.strategy_id;

                    const opacityClass = isCompatible ? "opacity-100" : "opacity-30 grayscale pointer-events-none";

                    return (
                        <div key={contract.strategy_id} className={`group transition-all duration-300 ${opacityClass}`}>
                            {/* Main Row */}
                            <div
                                className={`
                                    grid grid-cols-12 gap-8 px-8 py-5 items-center cursor-pointer transition-all duration-200
                                    ${isSelected ? "bg-cyan-950/10" : "hover:bg-white/[0.02]"}
                                `}
                                onClick={() => isCompatible && toggleStrategy(contract.strategy_id)}
                            >
                                {/* Name & Checkbox */}
                                <div className="col-span-4 flex items-center gap-5">
                                    <div className={`
                                        w-5 h-5 rounded flex items-center justify-center transition-all duration-300 flex-shrink-0
                                        ${isSelected
                                            ? "bg-cyan-500 text-black shadow-[0_0_15px_rgba(6,182,212,0.5)] scale-110"
                                            : "border border-white/10 group-hover:border-white/30 bg-transparent"
                                        }
                                    `}>
                                        {isSelected && <Check className="w-3.5 h-3.5" strokeWidth={3} />}
                                    </div>
                                    <div className="flex flex-col">
                                        <div className={`text-sm font-bold tracking-tight transition-colors ${isSelected ? "text-cyan-200" : "text-gray-300 group-hover:text-white"}`}>
                                            {contract.strategy_id.replace(/_/g, ' ')}
                                        </div>
                                        <div className="text-[10px] text-gray-600 font-mono mt-0.5 group-hover:text-gray-500 transition-colors">
                                            {contract.strategy_id}
                                        </div>
                                    </div>
                                </div>

                                {/* Regime */}
                                <div className="col-span-2">
                                    <span className={`
                                        px-2.5 py-1 rounded-md text-[10px] uppercase font-bold tracking-wider border
                                        ${isSelected
                                            ? "bg-cyan-900/30 border-cyan-500/20 text-cyan-400"
                                            : "bg-white/5 border-white/5 text-gray-500 group-hover:border-white/10"
                                        }
                                    `}>
                                        {contract.regime || "GENERAL"}
                                    </span>
                                </div>

                                {/* Timeframe */}
                                <div className="col-span-2 flex items-center gap-2 text-xs font-mono text-gray-500">
                                    <Clock className="w-3.5 h-3.5 text-gray-700 group-hover:text-gray-500 transition-colors" />
                                    {contract.timeframe}
                                </div>

                                {/* Risk Profile */}
                                <div className="col-span-3 flex items-center gap-2 pr-4">
                                    <Activity className="w-3.5 h-3.5 text-amber-500/50 flex-shrink-0 mb-px" />
                                    <span className="text-[11px] text-gray-500 truncate group-hover:text-gray-400 transition-colors" title={contract.when_loses}>
                                        {contract.when_loses}
                                    </span>
                                </div>

                                {/* Expand Button */}
                                <div className="col-span-1 flex justify-end">
                                    <button
                                        onClick={(e) => toggleRow(contract.strategy_id, e)}
                                        className={`
                                            p-1.5 rounded-lg transition-all
                                            ${isRowExpanded ? "bg-white/10 text-white" : "text-gray-700 hover:bg-white/5 hover:text-gray-400"}
                                        `}
                                    >
                                        {isRowExpanded ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
                                    </button>
                                </div>
                            </div>

                            {/* Expanded Details Panel */}
                            {isRowExpanded && (
                                <div className="bg-[#020202] border-y border-white/5 px-8 py-6 grid grid-cols-2 gap-12 shadow-inner">

                                    {/* Description Column */}
                                    <div className="space-y-4">
                                        <div>
                                            <h4 className="text-[10px] uppercase tracking-widest text-gray-600 font-bold mb-2 flex items-center gap-2">
                                                <Info className="w-3 h-3" /> Strategy Thesis
                                            </h4>
                                            <p className="text-sm text-gray-400 leading-relaxed font-light border-l-2 border-white/10 pl-4 py-1">
                                                {contract.description}
                                            </p>
                                        </div>

                                        <div className="pt-4">
                                            <h4 className="text-[10px] uppercase tracking-widest text-gray-600 font-bold mb-2 flex items-center gap-2">
                                                <Shield className="w-3 h-3" /> Risk Management
                                            </h4>
                                            <div className="flex items-start gap-3 p-3 rounded-lg bg-amber-950/10 border border-amber-900/20 text-amber-500/80 text-xs">
                                                <AlertTriangle className="w-4 h-4 mt-0.5 flex-shrink-0" />
                                                <p>{contract.when_loses}. <span className="opacity-70">Monitor capital utilization during high volatility events in this regime.</span></p>
                                            </div>
                                        </div>
                                    </div>

                                    {/* Parameters Column */}
                                    <div>
                                        <h4 className="text-[10px] uppercase tracking-widest text-gray-600 font-bold mb-3 flex items-center gap-2">
                                            <Activity className="w-3 h-3" /> Configuration
                                        </h4>
                                        {isSelected ? (
                                            <div className="bg-black border border-white/10 rounded-lg p-5">
                                                <div className="grid grid-cols-2 gap-4">
                                                    {Object.entries(config?.params || {}).map(([key, value]) => (
                                                        <div key={key}>
                                                            <div className="text-[10px] text-gray-500 uppercase font-mono mb-1">{key}</div>
                                                            <div className="text-sm text-cyan-400 font-mono">{value}</div>
                                                        </div>
                                                    ))}
                                                    {Object.keys(config?.params || {}).length === 0 && (
                                                        <div className="col-span-2 text-xs text-gray-600 italic">No configurable parameters.</div>
                                                    )}
                                                </div>
                                            </div>
                                        ) : (
                                            <div className="h-full flex flex-col items-center justify-center border border-dashed border-white/10 rounded-lg p-6 text-gray-600 gap-2">
                                                <XCircle className="w-6 h-6 opacity-20" />
                                                <p className="text-xs">Enable this strategy to configure parameters.</p>
                                            </div>
                                        )}
                                    </div>
                                </div>
                            )}
                        </div>
                    );
                })}
            </div>

            {/* Footer Summary */}
            <div className="px-8 py-4 bg-white/[0.02] border-t border-white/5 text-[10px] uppercase tracking-widest text-gray-500 flex justify-between items-center">
                <span>{filteredContracts.length} Strategies Available</span>
                <div>
                    {selection.filter(s => s.enabled).length > 0 && (
                        <span className="text-cyan-500">{selection.filter(s => s.enabled).length} Selected for Execution</span>
                    )}
                </div>
            </div>
        </div>
    );
}
