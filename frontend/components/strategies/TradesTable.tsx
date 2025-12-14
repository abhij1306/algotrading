"use client";

import { useState } from 'react';

interface TradesTableProps {
    trades: any[];
}

export default function TradesTable({ trades }: TradesTableProps) {
    const [showAll, setShowAll] = useState(false);

    if (!trades || trades.length === 0) {
        return (
            <div className="bg-slate-800/50 rounded-lg p-6 border border-slate-700">
                <h3 className="text-sm font-semibold text-slate-400 mb-4">RECENT TRADES</h3>
                <div className="text-center text-slate-500 py-8">No trades executed</div>
            </div>
        );
    }

    const displayTrades = showAll ? trades : trades.slice(0, 10);

    return (
        <div className="bg-slate-800/50 rounded-lg p-6 border border-slate-700">
            <div className="flex items-center justify-between mb-4">
                <h3 className="text-sm font-semibold text-slate-400 flex items-center">
                    <svg className="w-4 h-4 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2" />
                    </svg>
                    RECENT TRADES
                </h3>
                {trades.length > 10 && (
                    <button
                        onClick={() => setShowAll(!showAll)}
                        className="text-xs text-green-400 hover:text-green-300 transition-colors"
                    >
                        {showAll ? 'Show Less' : `View All ${trades.length} Trades`}
                    </button>
                )}
            </div>

            <div className="overflow-x-auto">
                <table className="w-full text-sm">
                    <thead>
                        <tr className="text-xs text-slate-400 border-b border-slate-700">
                            <th className="text-left pb-3 font-medium">DATE & TIME</th>
                            <th className="text-left pb-3 font-medium">INSTRUMENT</th>
                            <th className="text-left pb-3 font-medium">TYPE</th>
                            <th className="text-right pb-3 font-medium">ENTRY</th>
                            <th className="text-right pb-3 font-medium">EXIT</th>
                            <th className="text-right pb-3 font-medium">P&L</th>
                        </tr>
                    </thead>
                    <tbody>
                        {displayTrades.map((trade, index) => (
                            <tr key={index} className="border-b border-slate-700/50 hover:bg-slate-700/30 transition-colors">
                                <td className="py-3 text-slate-300">
                                    {new Date(trade.entry_time.endsWith('Z') ? trade.entry_time : trade.entry_time + 'Z').toLocaleString('en-IN', {
                                        month: 'short',
                                        day: '2-digit',
                                        hour: '2-digit',
                                        minute: '2-digit',
                                        timeZone: 'Asia/Kolkata' // Explicitly use IST
                                    })}
                                </td>
                                <td className="py-3 text-white font-medium">
                                    {trade.instrument}
                                </td>
                                <td className="py-3">
                                    <span className={`inline-flex items-center px-2 py-1 rounded text-xs font-medium ${trade.position_type === 'LONG'
                                        ? 'bg-green-500/20 text-green-400'
                                        : 'bg-red-500/20 text-red-400'
                                        }`}>
                                        {trade.position_type}
                                    </span>
                                </td>
                                <td className="py-3 text-right text-slate-300">
                                    ₹{trade.entry_price.toFixed(2)}
                                </td>
                                <td className="py-3 text-right text-slate-300">
                                    ₹{trade.exit_price.toFixed(2)}
                                </td>
                                <td className="py-3 text-right font-medium">
                                    <div className={trade.pnl >= 0 ? 'text-green-400' : 'text-red-400'}>
                                        {trade.pnl >= 0 ? '+' : ''}₹{trade.pnl.toFixed(2)}
                                    </div>
                                    <div className={`text-xs ${trade.pnl_pct >= 0 ? 'text-green-400/70' : 'text-red-400/70'}`}>
                                        {trade.pnl_pct >= 0 ? '+' : ''}{trade.pnl_pct.toFixed(2)}%
                                    </div>
                                </td>
                            </tr>
                        ))}
                    </tbody>
                </table>
            </div>

            {!showAll && trades.length > 10 && (
                <button
                    onClick={() => setShowAll(true)}
                    className="w-full mt-4 py-2 text-sm text-slate-400 hover:text-white transition-colors"
                >
                    Load More ({trades.length - 10} more)
                </button>
            )}
        </div>
    );
}
