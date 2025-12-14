"use client";

import { useState } from 'react';

interface StrategyConfigurationProps {
    onRunBacktest: (config: any) => void;
    isRunning: boolean;
}

export default function StrategyConfiguration({ onRunBacktest, isRunning }: StrategyConfigurationProps) {
    const [config, setConfig] = useState({
        strategy: 'ORB',
        symbol: 'NIFTY50-INDEX',
        timeframe: '5min',
        segment: 'options',
        startDate: '2023-10-01',
        endDate: '2023-10-31',
        initialCapital: 100000,
        // ORB specific params
        openingRangeMinutes: 5,
        stopLoss: 0.5,
        takeProfit: 1.5,
        maxPositions: 1,
        trailingSL: false
    });

    const handleSubmit = (e: React.FormEvent) => {
        e.preventDefault();
        onRunBacktest(config);
    };

    return (
        <div className="p-6">
            <h2 className="text-lg font-semibold text-white mb-6">Configuration</h2>

            <form onSubmit={handleSubmit} className="space-y-6">
                {/* Strategy Selection */}
                <div>
                    <label className="block text-sm font-medium text-slate-300 mb-2">
                        STRATEGY
                    </label>
                    <select
                        value={config.strategy}
                        onChange={(e) => setConfig({ ...config, strategy: e.target.value })}
                        className="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-white focus:outline-none focus:ring-2 focus:ring-green-500"
                        disabled={isRunning}
                    >
                        <option value="ORB">Opening Range Breakout (ORB)</option>
                    </select>
                </div>

                {/* Symbol Input */}
                <div>
                    <label className="block text-sm font-medium text-slate-300 mb-2">
                        SYMBOL
                    </label>
                    <input
                        type="text"
                        value={config.symbol}
                        onChange={(e) => setConfig({ ...config, symbol: e.target.value.toUpperCase() })}
                        placeholder="e.g., RELIANCE"
                        className="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-white placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-green-500"
                        disabled={isRunning}
                    />
                </div>

                {/* Timeframe */}
                <div>
                    <label className="block text-sm font-medium text-slate-300 mb-2">
                        TIMEFRAME
                    </label>
                    <select
                        value={config.timeframe}
                        onChange={(e) => setConfig({ ...config, timeframe: e.target.value })}
                        className="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-white focus:outline-none focus:ring-2 focus:ring-green-500"
                        disabled={isRunning}
                    >
                        <option value="1min">1 Min</option>
                        <option value="5min">5 Min</option>
                        <option value="15min">15 Min</option>
                        <option value="30min">30 Min</option>
                        <option value="1D">1 Day</option>
                    </select>
                </div>

                {/* Segment */}
                <div>
                    <label className="block text-sm font-medium text-slate-300 mb-2">
                        SEGMENT
                    </label>
                    <select
                        value={config.segment}
                        onChange={(e) => setConfig({ ...config, segment: e.target.value })}
                        className="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-white focus:outline-none focus:ring-2 focus:ring-green-500"
                        disabled={isRunning}
                    >
                        <option value="options">Options (CE/PE)</option>
                        <option value="equity">Equity</option>
                    </select>
                </div>

                {/* Date Range */}
                <div>
                    <label className="block text-sm font-medium text-slate-300 mb-2">
                        DATE RANGE
                    </label>
                    <div className="space-y-2">
                        <input
                            type="date"
                            value={config.startDate}
                            onChange={(e) => setConfig({ ...config, startDate: e.target.value })}
                            className="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-white focus:outline-none focus:ring-2 focus:ring-green-500"
                            disabled={isRunning}
                        />
                        <input
                            type="date"
                            value={config.endDate}
                            onChange={(e) => setConfig({ ...config, endDate: e.target.value })}
                            className="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-white focus:outline-none focus:ring-2 focus:ring-green-500"
                            disabled={isRunning}
                        />
                    </div>
                </div>

                {/* Risk Management */}
                <div className="pt-4 border-t border-slate-700">
                    <h3 className="text-sm font-semibold text-slate-400 mb-4">RISK MANAGEMENT</h3>

                    <div className="space-y-4">
                        <div>
                            <label className="block text-xs text-slate-400 mb-1">
                                Stop Loss (%)
                            </label>
                            <input
                                type="number"
                                step="0.1"
                                value={config.stopLoss}
                                onChange={(e) => setConfig({ ...config, stopLoss: parseFloat(e.target.value) })}
                                className="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-white focus:outline-none focus:ring-2 focus:ring-green-500"
                                disabled={isRunning}
                            />
                        </div>

                        <div>
                            <label className="block text-xs text-slate-400 mb-1">
                                Take Profit (%)
                            </label>
                            <input
                                type="number"
                                step="0.1"
                                value={config.takeProfit}
                                onChange={(e) => setConfig({ ...config, takeProfit: parseFloat(e.target.value) })}
                                className="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-white focus:outline-none focus:ring-2 focus:ring-green-500"
                                disabled={isRunning}
                            />
                        </div>

                        <div>
                            <label className="flex items-center">
                                <input
                                    type="checkbox"
                                    checked={config.trailingSL}
                                    onChange={(e) => setConfig({ ...config, trailingSL: e.target.checked })}
                                    className="mr-2 "
                                    disabled={isRunning}
                                />
                                <span className="text-sm text-slate-300">Trailing Stop Loss</span>
                            </label>
                        </div>
                    </div>
                </div>

                {/* Run Button */}
                <button
                    type="submit"
                    disabled={isRunning}
                    className={`w-full py-3 rounded-lg font-semibold transition-all ${isRunning
                        ? 'bg-slate-700 text-slate-400 cursor-not-allowed'
                        : 'bg-green-600 hover:bg-green-700 text-white shadow-lg shadow-green-600/30'
                        }`}
                >
                    {isRunning ? (
                        <span className="flex items-center justify-center">
                            <svg className="animate-spin -ml-1 mr-3 h-5 w-5" fill="none" viewBox="0 0 24 24">
                                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
                                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                            </svg>
                            RUNNING...
                        </span>
                    ) : (
                        '▶ RUN BACKTEST'
                    )}
                </button>
            </form>
        </div>
    );
}
