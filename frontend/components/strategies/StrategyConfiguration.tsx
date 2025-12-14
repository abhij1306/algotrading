"use client";

import { useState, useEffect } from 'react';
import { Play } from 'lucide-react';

interface StrategyConfigurationProps {
    onRunBacktest: (config: any) => void;
    isRunning: boolean;
}

export default function StrategyConfiguration({ onRunBacktest, isRunning }: StrategyConfigurationProps) {
    // Dynamic default dates (Last 30 days)
    const [config, setConfig] = useState({
        strategy: 'ORB',
        symbol: 'NIFTY50-INDEX',
        timeframe: '5min',
        segment: 'options',
        startDate: '', // Set in useEffect
        endDate: '',   // Set in useEffect
        initialCapital: 100000,
        // Params
        riskPerTrade: 2.0,
        stopLoss: 0.5,
        takeProfit: 1.5,
        maxPositions: 1,
        trailingSL: false,
        // Display helpers
        openingRangeMinutes: 5,
    });

    // Symbol autocomplete state
    const [symbolSuggestions, setSymbolSuggestions] = useState<any[]>([]);
    const [showSuggestions, setShowSuggestions] = useState(false);

    useEffect(() => {
        const end = new Date();
        const start = new Date();
        start.setDate(end.getDate() - 30);

        setConfig(prev => ({
            ...prev,
            startDate: start.toISOString().split('T')[0],
            endDate: end.toISOString().split('T')[0]
        }));
    }, []);

    const handleSubmit = (e: React.FormEvent) => {
        e.preventDefault();
        onRunBacktest(config);
    };

    // Helper for slider color
    const getSliderBackground = (value: number, min: number, max: number) => {
        const percentage = ((value - min) / (max - min)) * 100;
        return `linear-gradient(to right, #3b82f6 ${percentage}%, #1e293b ${percentage}%)`;
    };

    return (
        <div className="p-4 font-sans">
            <h3 className="text-sm font-bold opacity-60 mb-4 px-1 uppercase">CONFIGURATION</h3>

            <form onSubmit={handleSubmit} className="space-y-4">

                {/* Strategy Logic */}
                <div>
                    <label className="block text-xs font-medium opacity-60 mb-2">
                        Strategy Logic
                    </label>
                    <div className="relative">
                        <select
                            value={config.strategy}
                            onChange={(e) => setConfig({ ...config, strategy: e.target.value })}
                            className="w-full px-3 py-2 rounded-lg bg-background-dark border border-border-dark text-sm focus:outline-none focus:border-primary transition-colors appearance-none"
                            disabled={isRunning}
                        >
                            <option value="ORB">Opening Range Breakout (ORB)</option>
                        </select>
                        <div className="absolute right-3 top-2.5 pointer-events-none opacity-60">
                            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" /></svg>
                        </div>
                    </div>
                </div>

                {/* Symbol & Timeframe */}
                <div className="grid grid-cols-2 gap-3">
                    <div>
                        <label className="block text-xs font-medium opacity-60 mb-2">
                            Symbol
                        </label>
                        <div className="relative">
                            <input
                                type="text"
                                value={config.symbol}
                                onChange={async (e) => {
                                    const value = e.target.value.toUpperCase();
                                    setConfig({ ...config, symbol: value });

                                    // Fetch suggestions if input length > 0
                                    if (value.length > 0) {
                                        try {
                                            const res = await fetch(`http://localhost:8000/api/symbols/search?q=${value}`);
                                            const data = await res.json();
                                            setSymbolSuggestions(data.symbols || []);
                                        } catch (err) {
                                            setSymbolSuggestions([]);
                                        }
                                    } else {
                                        setSymbolSuggestions([]);
                                    }
                                }}
                                onFocus={() => setShowSuggestions(true)}
                                onBlur={() => setTimeout(() => setShowSuggestions(false), 200)}
                                className="w-full px-3 py-2 rounded-lg bg-background-dark border border-border-dark text-sm focus:outline-none focus:border-primary transition-colors uppercase"
                                placeholder="NIFTY50-INDEX"
                                disabled={isRunning}
                            />
                            {showSuggestions && symbolSuggestions.length > 0 && (
                                <div className="absolute z-10 w-full mt-1 bg-background-dark border border-border-dark rounded-lg shadow-lg max-h-48 overflow-y-auto">
                                    {symbolSuggestions.map((sym: any, idx: number) => (
                                        <div
                                            key={idx}
                                            className="px-3 py-2 hover:bg-card-dark cursor-pointer text-sm"
                                            onMouseDown={() => {
                                                setConfig({ ...config, symbol: sym.symbol });
                                                setShowSuggestions(false);
                                            }}
                                        >
                                            <div className="font-medium">{sym.symbol}</div>
                                            <div className="text-xs text-text-secondary opacity-60">{sym.name}</div>
                                        </div>
                                    ))}
                                </div>
                            )}
                        </div>
                    </div>
                    <div>
                        <label className="block text-xs font-medium opacity-60 mb-2">
                            Timeframe
                        </label>
                        <div className="relative">
                            <select
                                value={config.timeframe}
                                onChange={(e) => setConfig({ ...config, timeframe: e.target.value })}
                                className="w-full px-3 py-2 rounded-lg bg-background-dark border border-border-dark text-sm focus:outline-none focus:border-primary transition-colors appearance-none"
                                disabled={isRunning}
                            >
                                <option value="1min">1m</option>
                                <option value="3min">3m</option>
                                <option value="5min">5m</option>
                                <option value="15min">15m</option>
                                <option value="1h">1h</option>
                                <option value="1D">1D</option>
                            </select>
                            <div className="absolute right-3 top-2.5 pointer-events-none opacity-60">
                                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" /></svg>
                            </div>
                        </div>
                    </div>
                </div>

                {/* Initial Capital */}
                <div>
                    <label className="block text-xs font-medium opacity-60 mb-2">Initial Capital (₹)</label>
                    <input
                        type="number"
                        value={config.initialCapital}
                        onChange={(e) => setConfig({ ...config, initialCapital: parseFloat(e.target.value) })}
                        className="w-full px-3 py-2 rounded-lg bg-background-dark border border-border-dark text-sm focus:outline-none focus:border-primary transition-colors"
                        disabled={isRunning}
                    />
                </div>

                {/* Backtest Period */}
                <div>
                    <label className="block text-xs font-medium opacity-60 mb-2">Backtest Period</label>
                    <div className="grid grid-cols-2 gap-2">
                        <input
                            type="date"
                            value={config.startDate}
                            onChange={(e) => setConfig({ ...config, startDate: e.target.value })}
                            className="w-full px-3 py-2 rounded-lg bg-background-dark border border-border-dark text-sm focus:outline-none focus:border-primary transition-colors [color-scheme:dark]"
                        />
                        <input
                            type="date"
                            value={config.endDate}
                            onChange={(e) => setConfig({ ...config, endDate: e.target.value })}
                            className="w-full px-3 py-2 rounded-lg bg-background-dark border border-border-dark text-sm focus:outline-none focus:border-primary transition-colors [color-scheme:dark]"
                        />
                    </div>
                </div>

                <div className="pt-4 border-t border-border-dark mt-4">
                    <h3 className="text-sm font-bold opacity-60 mb-4 px-1 uppercase">RISK MANAGEMENT</h3>

                    <div className="space-y-6">
                        {/* RIsk Per Trade */}
                        <div>
                            <div className="flex justify-between items-center mb-2">
                                <label className="text-xs font-medium opacity-60">Risk Per Trade %</label>
                                <span className="text-xs font-bold text-primary">{config.riskPerTrade}%</span>
                            </div>
                            <input
                                type="range"
                                min="0.1"
                                max="5.0"
                                step="0.1"
                                value={config.riskPerTrade}
                                onChange={(e) => setConfig({ ...config, riskPerTrade: parseFloat(e.target.value) })}
                                className="w-full h-1.5 rounded-full appearance-none cursor-pointer"
                                style={{ background: getSliderBackground(config.riskPerTrade, 0.1, 5.0) }}
                            />
                            <style jsx>{`
                            input[type=range]::-webkit-slider-thumb {
                                -webkit-appearance: none;
                                height: 16px;
                                width: 16px;
                                border-radius: 50%;
                                background: #3b82f6;
                                cursor: pointer;
                                margin-top: -6px; /* You need to specify a margin in Chrome, but in Firefox and IE it is automatic */
                                border: 2px solid #0f172a;
                                box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.3);
                            }
                            input[type=range]::-moz-range-thumb {
                                height: 16px;
                                width: 16px;
                                border-radius: 50%;
                                background: #3b82f6;
                                cursor: pointer;
                                border: 2px solid #0f172a;
                            }
                            input[type=range]::-webkit-slider-runnable-track {
                                width: 100%;
                                height: 4px;
                                cursor: pointer;
                                border-radius: 2px;
                            }
                        `}</style>
                        </div>

                        {/* Stop Loss (ATR) - Mapped to stopLoss % for now */}
                        <div>
                            <div className="flex justify-between items-center mb-2">
                                <label className="text-xs font-medium opacity-60">Stop Loss (ATR)</label>
                                <span className="text-xs font-bold text-primary">{config.stopLoss}x</span>
                            </div>
                            <input
                                type="range"
                                min="0.1"
                                max="5.0"
                                step="0.1"
                                value={config.stopLoss}
                                onChange={(e) => setConfig({ ...config, stopLoss: parseFloat(e.target.value) })}
                                className="w-full h-1.5 rounded-full appearance-none cursor-pointer"
                                style={{ background: getSliderBackground(config.stopLoss, 0.1, 5.0) }}
                            />
                        </div>

                        {/* Take Profit (ATR) */}
                        <div>
                            <div className="flex justify-between items-center mb-2">
                                <label className="text-xs font-medium opacity-60">Take Profit (ATR)</label>
                                <span className="text-xs font-bold text-primary">{config.takeProfit}x</span>
                            </div>
                            <input
                                type="range"
                                min="0.5"
                                max="10.0"
                                step="0.5"
                                value={config.takeProfit}
                                onChange={(e) => setConfig({ ...config, takeProfit: parseFloat(e.target.value) })}
                                className="w-full h-1.5 rounded-full appearance-none cursor-pointer"
                                style={{ background: getSliderBackground(config.takeProfit, 0.5, 10.0) }}
                            />
                        </div>
                    </div>
                </div>

                <div className="pt-4">
                    <button
                        type="submit"
                        disabled={isRunning}
                        className={`w-full py-3.5 rounded-lg font-bold text-sm transition-all flex items-center justify-center gap-2 ${isRunning
                            ? 'bg-primary/20 text-primary cursor-not-allowed'
                            : 'bg-primary hover:bg-blue-600 text-white shadow-lg shadow-blue-500/20 hover:shadow-blue-500/40'
                            }`}
                    >
                        {isRunning ? (
                            <>
                                <div className="animate-spin h-4 w-4 border-2 border-current border-t-transparent rounded-full"></div>
                                Running Backtest...
                            </>
                        ) : (
                            <>
                                <Play className="w-4 h-4 fill-current" /> Run Backtest
                            </>
                        )}
                    </button>
                </div>
            </form>
        </div>
    );
}
