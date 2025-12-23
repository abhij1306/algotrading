"use client";

import React from 'react';
import { Search, FlaskConical, Archive, FileText, ChevronRight, Activity } from "lucide-react";
import { Badge } from "@/components/ui/badge";

interface PortfolioSidebarProps {
    portfolios: any[];
    selectedId: string | number | null;
    onSelect: (id: string | number) => void;
    onCreateNew: () => void;
}

export default function PortfolioSidebar({ portfolios, selectedId, onSelect, onCreateNew }: PortfolioSidebarProps) {

    // Group portfolios (mock logic for now, in real app check status)
    const active = portfolios.filter(p => p.status === 'LIVE' || p.status === 'RESEARCH');
    const archived = portfolios.filter(p => p.status === 'ARCHIVED');

    return (
        <div className="w-[300px] border-r border-[#27272a] bg-[#09090b] flex flex-col h-full">
            {/* Header */}
            <div className="p-4 border-b border-[#27272a]">
                <div className="flex items-center gap-2 mb-4">
                    {/* <Activity className="w-5 h-5 text-purple-500" /> */}
                    <h2 className="font-bold text-gray-200 tracking-tight">Portfolios & Universes</h2>
                </div>

                <div className="relative">
                    <Search className="absolute left-2.5 top-2.5 h-3.5 w-3.5 text-gray-500" />
                    <input
                        className="w-full bg-[#18181b] border border-[#27272a] rounded-md pl-8 pr-3 py-2 text-xs text-gray-300 focus:outline-none focus:border-gray-600 transition-colors"
                        placeholder="Filter portfolios..."
                    />
                </div>
            </div>

            {/* List */}
            <div className="flex-1 overflow-y-auto custom-scrollbar p-2 space-y-6">

                {/* Market Indices (Default) */}
                <div className="mb-6">
                    <h3 className="px-2 text-[10px] font-bold text-gray-500 uppercase tracking-widest mb-2">Market Indices</h3>
                    <div className="space-y-0.5">
                        {['NIFTY 50', 'BANKNIFTY', 'NIFTY 100', 'NIFTY IT', 'NIFTY PHARMA', 'NIFTY AUTO', 'NIFTY FMCG', 'MIDCAP NIFTY', 'SMALLCAP NIFTY'].map(idx => (
                            <button
                                key={idx}
                                onClick={() => onSelect(`INDEX:${idx}`)}
                                className={`w-full text-left px-3 py-2 rounded-md flex items-center gap-3 transition-colors
                                    ${selectedId === `INDEX:${idx}`
                                        ? 'bg-[#27272a] text-white'
                                        : 'text-gray-400 hover:bg-[#27272a]/50 hover:text-gray-200'
                                    }
                                `}
                            >
                                <div className={`w-1.5 h-1.5 rounded-full ${selectedId === `INDEX:${idx}` ? 'bg-cyan-500' : 'bg-gray-700'}`} />
                                <span className="text-xs font-semibold">{idx}</span>
                            </button>
                        ))}
                    </div>
                </div>

                {/* Active Section */}
                <div>
                    <h3 className="px-2 text-[10px] font-bold text-gray-500 uppercase tracking-widest mb-2 flex items-center justify-between">
                        Active
                        <Badge variant="outline" className="text-[9px] h-4 border-gray-800 text-gray-600">{active.length}</Badge>
                    </h3>
                    <div className="space-y-0.5">
                        {active.map(p => (
                            <button
                                key={p.id}
                                onClick={() => onSelect(p.id)}
                                className={`w-full text-left px-3 py-2.5 rounded-md flex items-center justify-between group transition-all duration-200
                                    ${selectedId === p.id
                                        ? 'bg-[#27272a] text-white'
                                        : 'text-gray-400 hover:bg-[#27272a]/50 hover:text-gray-200'
                                    }
                                `}
                            >
                                <div className="flex items-center gap-3 overflow-hidden">
                                    <div className={`w-1.5 h-1.5 rounded-full ${selectedId === p.id ? 'bg-purple-500' : 'bg-gray-700'}`} />
                                    <div className="truncate">
                                        <p className="text-xs font-semibold truncate">{p.name}</p>
                                        <p className="text-[9px] text-gray-600 truncate">{p.benchmark || "NIFTY 50"}</p>
                                    </div>
                                </div>
                                {p.status === 'LIVE' && <span className="text-[9px] text-green-500 font-bold px-1.5 py-0.5 bg-green-500/10 rounded">LIVE</span>}
                            </button>
                        ))}
                    </div>
                </div>

                {/* Create New Button (Using existing UI style) */}
                <div className="px-2">
                    <button
                        onClick={onCreateNew}
                        className="w-full py-2 border border-dashed border-[#3f3f46] rounded-md text-xs text-gray-500 hover:text-gray-300 hover:border-gray-500 transition-colors flex items-center justify-center gap-2"
                    >
                        <FlaskConical className="w-3.5 h-3.5" />
                        <span>New Experiment</span>
                    </button>
                </div>

                {/* Archived Section */}
                {archived.length > 0 && (
                    <div>
                        <h3 className="px-2 text-[10px] font-bold text-gray-500 uppercase tracking-widest mb-2 flex items-center justify-between">
                            Archived
                            <Badge variant="outline" className="text-[9px] h-4 border-gray-800 text-gray-600">{archived.length}</Badge>
                        </h3>
                        <div className="space-y-0.5">
                            {archived.map(p => (
                                <button
                                    key={p.id}
                                    onClick={() => onSelect(p.id)}
                                    className={`w-full text-left px-3 py-2 rounded-md flex items-center gap-3 text-gray-500 hover:text-gray-300 hover:bg-[#27272a]/50`}
                                >
                                    <Archive className="w-3.5 h-3.5" />
                                    <span className="text-xs truncate">{p.name}</span>
                                </button>
                            ))}
                        </div>
                    </div>
                )}

            </div>

            {/* Footer */}
            <div className="p-3 border-t border-[#27272a] text-[10px] text-gray-600 flex justify-between">
                <span>v2.4.0</span>
                <span>Quant_Team_Alpha</span>
            </div>
        </div>
    );
}
