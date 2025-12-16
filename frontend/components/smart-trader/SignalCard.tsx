'use client'

import { useState } from 'react';

interface SignalCardProps {
    signal: any;
    onTakeTrade: (signalId: string) => void;
}

// Helper function to format timestamp as relative time
function formatTimestamp(timestamp: string): string {
    try {
        const signalTime = new Date(timestamp);
        const now = new Date();
        const diffMs = now.getTime() - signalTime.getTime();
        const diffMins = Math.floor(diffMs / 60000);

        if (diffMins < 1) return 'Just now';
        if (diffMins < 60) return `${diffMins}m ago`;
        const diffHours = Math.floor(diffMins / 60);
        if (diffHours < 24) return `${diffHours}h ago`;
        const diffDays = Math.floor(diffHours / 24);
        return `${diffDays}d ago`;
    } catch {
        return '';
    }
}

// Get confidence color
function getConfidenceColor(level: string): string {
    switch (level) {
        case 'HIGH': return 'text-green-400 bg-green-500/10';
        case 'MEDIUM': return 'text-yellow-400 bg-yellow-500/10';
        case 'LOW': return 'text-gray-400 bg-gray-500/10';
        default: return 'text-gray-400 bg-gray-500/10';
    }
}

export default function SignalCard({ signal, onTakeTrade }: SignalCardProps) {
    const [isExpanded, setIsExpanded] = useState(false);

    const handleTrade = () => {
        onTakeTrade(signal.id);
    };

    return (
        <div className="bg-card-dark rounded-xl border border-border-dark hover:border-primary transition-all shadow-sm overflow-hidden group">
            {/* Header Row */}
            <div className="p-3 flex items-center justify-between gap-3 bg-gradient-to-r from-background-dark to-card-dark">
                <div className="flex items-center gap-3">
                    <div className={`w-1 h-8 rounded-full ${signal.direction === 'LONG' ? 'bg-profit' : 'bg-loss'}`}></div>
                    <div>
                        <div className="flex items-center gap-2">
                            <span className="font-bold text-white text-base">{signal.symbol}</span>
                            <span className={`px-1.5 py-0.5 rounded text-[10px] font-bold ${signal.direction === 'LONG' ? 'bg-profit/10 text-profit' : 'bg-loss/10 text-loss'
                                }`}>
                                {signal.direction}
                            </span>
                            <span className={`px-1.5 py-0.5 rounded text-[10px] font-bold ${getConfidenceColor(signal.confidence_level)}`}>
                                {signal.confidence_level}
                            </span>
                        </div>
                        <div className="flex items-center gap-2 text-[10px] opacity-60">
                            <span>{signal.timeframe}</span>
                            <span>•</span>
                            <span>Confluence: {signal.confluence_count}</span>
                            {signal.timestamp && (
                                <>
                                    <span>•</span>
                                    <span>{formatTimestamp(signal.timestamp)}</span>
                                </>
                            )}
                        </div>
                    </div>
                </div>

                <div className="text-right">
                    <div className="text-sm font-bold text-white">{(signal.final_confidence * 100).toFixed(0)}%</div>
                    <div className="text-[10px] opacity-60">Confidence</div>
                </div>
            </div>

            {/* Signal Families */}
            <div className="px-3 py-2 bg-[#0f1115] border-y border-border-dark/50">
                <div className="flex flex-wrap gap-1">
                    {signal.signal_families.map((family: string, idx: number) => (
                        <span key={idx} className="px-2 py-0.5 rounded-full text-[9px] font-medium bg-primary/10 text-primary">
                            {family}
                        </span>
                    ))}
                </div>
            </div>

            {/* Metrics Row */}
            <div className="px-3 py-2 bg-[#0f1115] flex justify-between items-center text-xs border-b border-border-dark/50">
                <div className="flex gap-4">
                    <div className="flex flex-col">
                        <span className="opacity-40 text-[9px] uppercase">Strength</span>
                        <span className="text-white font-medium">{(signal.aggregate_strength * 100).toFixed(0)}%</span>
                    </div>
                    <div className="flex flex-col">
                        <span className="opacity-40 text-[9px] uppercase">Signals</span>
                        <span className="text-text-secondary font-medium">{signal.signal_names.length}</span>
                    </div>
                </div>

                <button
                    onClick={() => setIsExpanded(!isExpanded)}
                    className="text-text-secondary hover:text-white transition-colors"
                >
                    <span className={`material-symbols-outlined text-lg transform transition-transform ${isExpanded ? 'rotate-180' : ''}`}>expand_more</span>
                </button>
            </div>

            {/* Action Area */}
            <div className="p-3 bg-card-dark">
                <button
                    onClick={handleTrade}
                    className={`w-full h-9 rounded-lg text-sm font-bold text-white shadow-lg transition-all ${signal.direction === 'LONG'
                            ? 'bg-primary hover:bg-blue-600 shadow-primary/20'
                            : 'bg-loss hover:bg-red-600 shadow-orange-500/20'
                        }`}
                >
                    Execute Trade
                </button>
            </div>

            {/* Collapsible Details */}
            {isExpanded && (
                <div className="px-3 pb-3 bg-card-dark border-t border-border-dark/50 animate-in slide-in-from-top-2">
                    {/* Deterministic Reasons */}
                    <div className="mt-3">
                        <div className="text-[10px] font-bold opacity-40 uppercase mb-1.5">Deterministic Signals</div>
                        <ul className="space-y-1">
                            {signal.reasons.map((reason: string, idx: number) => (
                                <li key={idx} className="text-[10px] text-text-secondary flex items-start gap-1.5 leading-tight">
                                    <span className="w-1 h-1 rounded-full bg-primary/50 mt-1 shrink-0"></span>
                                    {reason}
                                </li>
                            ))}
                        </ul>
                    </div>

                    {/* LLM Narrative */}
                    {signal.llm_narrative && (
                        <div className="mt-3">
                            <div className="text-[10px] font-bold opacity-40 uppercase mb-1.5">🤖 LLM Analysis</div>
                            <p className="text-[10px] text-text-secondary leading-relaxed italic">
                                {signal.llm_narrative}
                            </p>
                        </div>
                    )}

                    {/* Risk Flags */}
                    {signal.risk_flags && signal.risk_flags.length > 0 && (
                        <div className="mt-3">
                            <div className="text-[10px] font-bold opacity-40 uppercase mb-1.5">⚠️ Risk Flags</div>
                            <ul className="space-y-1">
                                {signal.risk_flags.map((flag: string, idx: number) => (
                                    <li key={idx} className="text-[10px] text-yellow-400 flex items-start gap-1.5 leading-tight">
                                        <span className="w-1 h-1 rounded-full bg-yellow-400 mt-1 shrink-0"></span>
                                        {flag}
                                    </li>
                                ))}
                            </ul>
                        </div>
                    )}

                    {/* Signal Names */}
                    <div className="mt-3">
                        <div className="text-[10px] font-bold opacity-40 uppercase mb-1.5">Signal Types</div>
                        <div className="flex flex-wrap gap-1">
                            {signal.signal_names.map((name: string, idx: number) => (
                                <span key={idx} className="px-2 py-0.5 rounded text-[9px] bg-white/5 text-gray-400">
                                    {name}
                                </span>
                            ))}
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
}
