'use client'

import { useState } from 'react';

interface SignalCardProps {
    signal: any;
    onTakeTrade: (signalId: string, tradeType: string, optionType?: string) => void;
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

export default function SignalCard({ signal, onTakeTrade }: SignalCardProps) {
    const [tradeType, setTradeType] = useState<string>('SPOT');
    const [optionType, setOptionType] = useState<string>('CE');
    const [isExpanded, setIsExpanded] = useState(false);

    const handleTrade = () => {
        onTakeTrade(signal.id, tradeType, optionType);
    };

    return (
        <div className="bg-card-dark rounded-xl border border-border-dark hover:border-primary transition-all shadow-sm overflow-hidden group">
            {/* Compact Header Row */}
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
                        </div>
                        <div className="flex items-center gap-2 text-[10px] opacity-60">
                            <span>{signal.instrument_type}</span>
                            <span>•</span>
                            <span>Score: {signal.momentum_score}</span>
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
                    <div className="text-sm font-bold text-white">₹{signal.entry_price.toFixed(1)}</div>
                    <div className="text-[10px] opacity-60">Entry Price</div>
                </div>
            </div>

            {/* Compact Metrics Row */}
            <div className="px-3 py-2 bg-[#0f1115] flex justify-between items-center text-xs border-y border-border-dark/50">
                <div className="flex gap-4">
                    <div className="flex flex-col">
                        <span className="opacity-40 text-[9px] uppercase">Target</span>
                        <span className="text-profit font-medium">₹{signal.target.toFixed(1)}</span>
                    </div>
                    <div className="flex flex-col">
                        <span className="opacity-40 text-[9px] uppercase">Stop Loss</span>
                        <span className="text-loss font-medium">₹{signal.stop_loss.toFixed(1)}</span>
                    </div>
                    <div className="flex flex-col">
                        <span className="opacity-40 text-[9px] uppercase">R:R</span>
                        <span className="text-text-secondary font-medium">{signal.risk_reward_ratio}:1</span>
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
                {signal.instrument_type === 'STOCK' && (
                    <div className="flex gap-2 mb-2">
                        <select
                            value={tradeType}
                            onChange={(e) => setTradeType(e.target.value)}
                            className="flex-1 h-8 rounded-lg bg-background-dark border border-border-dark text-xs px-2 focus:border-primary focus:outline-none text-white"
                        >
                            <option value="SPOT">Spot</option>
                            <option value="FUTURES">Fut</option>
                            <option value="OPTIONS">Opt</option>
                        </select>

                        {tradeType === 'OPTIONS' && (
                            <select
                                value={optionType}
                                onChange={(e) => setOptionType(e.target.value)}
                                className="w-20 h-8 rounded-lg bg-background-dark border border-border-dark text-xs px-2 focus:border-primary focus:outline-none text-white"
                            >
                                <option value="CE">CE</option>
                                <option value="PE">PE</option>
                            </select>
                        )}

                        <button
                            onClick={handleTrade}
                            className={`h-8 px-4 rounded-lg text-xs font-bold text-white shadow-lg shadow-primary/20 hover:shadow-primary/40 transition-all ${signal.direction === 'LONG' ? 'bg-primary hover:bg-blue-600' : 'bg-loss hover:bg-red-600'
                                }`}
                        >
                            Execute
                        </button>
                    </div>
                )}

                {signal.instrument_type !== 'STOCK' && (
                    <button
                        onClick={handleTrade}
                        className={`w-full h-8 rounded-lg text-xs font-bold text-white shadow-lg transition-all ${signal.direction === 'LONG' ? 'bg-primary hover:bg-blue-600 shadow-primary/20' : 'bg-loss hover:bg-red-600 shadow-orange-500/20'
                            }`}
                    >
                        Execute Trade
                    </button>
                )}
            </div>

            {/* Collapsible Details */}
            {isExpanded && (
                <div className="px-3 pb-3 bg-card-dark border-t border-border-dark/50 animate-in slide-in-from-top-2">
                    <div className="text-[10px] font-bold opacity-40 uppercase mb-1.5 mt-2">Analysis Logic</div>
                    <ul className="space-y-1">
                        {signal.reasons.map((reason: string, idx: number) => (
                            <li key={idx} className="text-[10px] text-text-secondary flex items-start gap-1.5 leading-tight">
                                <span className="w-1 h-1 rounded-full bg-primary/50 mt-1 shrink-0"></span>
                                {reason}
                            </li>
                        ))}
                    </ul>
                </div>
            )}
        </div>
    );
}
