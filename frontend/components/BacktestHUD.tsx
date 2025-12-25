'use client'

import { useState, useEffect } from 'react'

export default function BacktestHUD() {
    const [backtestResults, setBacktestResults] = useState<any>(null)
    const [isRunning, setIsRunning] = useState(false)
    const [error, setError] = useState<string | null>(null)
    const [searchResults, setSearchResults] = useState<any[]>([])
    const [showDropdown, setShowDropdown] = useState(false)

    // Close dropdowns on outside click
    useEffect(() => {
        const handleClickOutside = (event: MouseEvent) => {
            const target = event.target as HTMLElement;
            if (!target.closest('.search-container') && !target.closest('.params-container')) {
                setShowDropdown(false);
                // Also close params details if open
                const details = document.querySelector('details.params-container');
                if (details && details.hasAttribute('open') && !target.closest('.params-container')) {
                    details.removeAttribute('open');
                }
            }
        }
        document.addEventListener('mousedown', handleClickOutside);
        return () => document.removeEventListener('mousedown', handleClickOutside);
    }, []);
    // Configuration state
    const [config, setConfig] = useState({
        strategy: 'ORB',
        symbol: '',
        startDate: '2024-01-01',
        endDate: '2024-12-20',
        timeframe: '5min',
        initialCapital: 100000,
        openingRangeMinutes: 15,
        stopLoss: 1.0,
        takeProfit: 2.0,
        maxPositions: 3,
        segment: 'INTRADAY',
    })

    // Debounced Search for Symbol
    useEffect(() => {
        const timer = setTimeout(() => {
            if (config.symbol && config.symbol.length > 0) {
                fetch(`http://localhost:9000/api/market/search/?query=${config.symbol}`)
                    .then(res => res.json())
                    .then(data => {
                        const symbols = Array.isArray(data) ? data : (data.symbols || []);
                        setSearchResults(symbols);
                        setShowDropdown(symbols.length > 0);
                    })
                    .catch(() => {
                        setSearchResults([])
                        setShowDropdown(false)
                    })
            } else {
                setSearchResults([])
                setShowDropdown(false)
            }
        }, 300)
        return () => clearTimeout(timer)
    }, [config.symbol])

    const handleRunBacktest = async () => {
        setIsRunning(true)
        setError(null)

        try {
            const response = await fetch('http://localhost:9000/api/strategies/backtest', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    strategy_name: config.strategy,
                    symbol: config.symbol,
                    start_date: config.startDate,
                    end_date: config.endDate,
                    timeframe: config.timeframe,
                    initial_capital: config.initialCapital,
                    params: {
                        opening_range_minutes: config.openingRangeMinutes,
                        stop_loss_pct: config.stopLoss,
                        take_profit_pct: config.takeProfit,
                        max_positions_per_day: config.maxPositions,
                        trade_type: config.segment,
                    },
                }),
            })

            if (!response.ok) {
                const errorData = await response.json()
                console.error("Backtest Failed Details:", errorData) // Added logging
                const errorMessage = typeof errorData.detail === 'string'
                    ? errorData.detail
                    : JSON.stringify(errorData.detail) || 'Backtest failed'
                throw new Error(errorMessage)
            }

            const data = await response.json()
            setBacktestResults(data)
        } catch (err: any) {
            setError(err.message)
            console.error('Backtest error:', err)
        } finally {
            setIsRunning(false)
        }
    }

    return (
        <div>
            {/* Glass Command Strip for Backtest Config */}
            <div className="glass-subtle rounded-xl border border-white/5 flex items-center px-4 py-2 gap-3 bg-[#080808]/80 backdrop-blur-md shrink-0 shadow-lg mb-6 relative z-[100]">

                {/* Strategy Selector */}
                <div className="relative min-w-[180px]">
                    <span className="absolute left-3 top-1/2 -translate-y-1/2 text-[10px] text-gray-500 font-bold uppercase">Strategy</span>
                    <select
                        value={config.strategy}
                        onChange={(e) => setConfig({ ...config, strategy: e.target.value })}
                        className="w-full h-9 pl-20 pr-3 rounded-lg bg-[#0A0A0A] border border-white/10 text-xs text-white focus:outline-none focus:border-cyan-500/50 appearance-none font-medium"
                    >
                        <option value="ORB">ORB Strategy</option>
                        <option value="MEAN_REVERSION">Mean Reversion</option>
                    </select>
                </div>

                <div className="h-5 w-[1px] bg-white/10"></div>

                {/* Symbol Input with Autocomplete */}
                <div className="relative w-32 z-[200] search-container">
                    <input
                        type="text"
                        value={config.symbol}
                        onChange={(e) => setConfig({ ...config, symbol: e.target.value.toUpperCase() })}
                        onBlur={() => setTimeout(() => setShowDropdown(false), 200)}
                        onFocus={() => {
                            if (config.symbol.length > 0 && searchResults.length > 0) {
                                setShowDropdown(true);
                            }
                        }}
                        placeholder="SYMBOL"
                        className="w-full h-9 px-3 rounded-lg bg-[#0A0A0A] border border-white/10 text-xs text-white focus:outline-none focus:border-cyan-500/50 font-bold text-center placeholder:font-normal uppercase"
                    />

                    {/* Dropdown */}
                    {showDropdown && searchResults.length > 0 && (
                        <div className="absolute top-full left-0 w-[240px] mt-1 bg-[#0a0a0a] border border-cyan-500/30 rounded-lg shadow-2xl max-h-60 overflow-y-auto z-[9999] backdrop-blur-xl">
                            {searchResults.map((s: any) => (
                                <div
                                    key={s.symbol}
                                    onClick={() => {
                                        setConfig({ ...config, symbol: s.symbol });
                                        setShowDropdown(false);
                                    }}
                                    className="px-3 py-2 text-xs text-gray-300 hover:bg-white/10 cursor-pointer flex flex-col border-b border-white/5 last:border-0 text-left"
                                >
                                    <span className="font-bold text-white">{s.symbol}</span>
                                    <span className="text-[10px] text-gray-500 truncate">{s.name}</span>
                                </div>
                            ))}
                        </div>
                    )}
                </div>

                {/* Capital Input */}
                <div className="relative w-32 group">
                    <span className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-500 text-xs">₹</span>
                    <input
                        type="number"
                        value={config.initialCapital}
                        onChange={(e) => setConfig({ ...config, initialCapital: parseInt(e.target.value) })}
                        className="w-full h-9 pl-6 pr-3 rounded-lg bg-[#0A0A0A] border border-white/10 text-xs text-white focus:outline-none focus:border-cyan-500/50 font-mono text-right"
                    />
                </div>

                <div className="h-5 w-[1px] bg-white/10"></div>

                {/* Date Range (Compact) */}
                <div className="flex items-center gap-2">
                    <input
                        type="date"
                        value={config.startDate}
                        onChange={(e) => setConfig({ ...config, startDate: e.target.value })}
                        className="h-9 px-3 rounded-lg bg-[#0A0A0A] border border-white/10 text-[10px] text-gray-300 focus:outline-none focus:border-cyan-500/50 font-mono uppercase"
                    />
                    <span className="text-gray-600 text-xs">→</span>
                    <input
                        type="date"
                        value={config.endDate}
                        onChange={(e) => setConfig({ ...config, endDate: e.target.value })}
                        className="h-9 px-3 rounded-lg bg-[#0A0A0A] border border-white/10 text-[10px] text-gray-300 focus:outline-none focus:border-cyan-500/50 font-mono uppercase"
                    />
                </div>

                {/* Advanced Toggle & Run */}
                <div className="ml-auto flex items-center gap-3">
                    <details className="relative group z-[101] params-container">
                        <summary className="list-none cursor-pointer flex items-center gap-1.5 px-3 py-1.5 rounded-lg hover:bg-white/5 text-[10px] font-bold text-gray-400 transition-colors uppercase tracking-wider">
                            <span>Parameters</span>
                            <span className="material-symbols-outlined text-[14px]">tune</span>
                        </summary>
                        <div className="absolute right-0 top-full mt-2 w-64 p-4 bg-[#0a0a0a] border border-cyan-500/30 rounded-xl shadow-2xl z-[9999] backdrop-blur-xl grid grid-cols-2 gap-3">
                            <div>
                                <label className="text-[10px] text-gray-500 block mb-1">STOP LOSS %</label>
                                <input type="number" step="0.1" value={config.stopLoss} onChange={e => setConfig({ ...config, stopLoss: parseFloat(e.target.value) })} className="w-full bg-[#050505] border border-white/10 rounded px-2 py-1 text-xs text-white" />
                            </div>
                            <div>
                                <label className="text-[10px] text-gray-500 block mb-1">TAKE PROFIT %</label>
                                <input type="number" step="0.1" value={config.takeProfit} onChange={e => setConfig({ ...config, takeProfit: parseFloat(e.target.value) })} className="w-full bg-[#050505] border border-white/10 rounded px-2 py-1 text-xs text-white" />
                            </div>
                        </div>
                    </details>

                    <button
                        onClick={handleRunBacktest}
                        disabled={isRunning}
                        className="h-9 px-6 bg-cyan-600 hover:bg-cyan-500 text-white rounded-lg text-xs font-bold transition-all shadow-lg shadow-cyan-500/20 disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2"
                    >
                        {isRunning ? (
                            <>
                                <div className="w-3 h-3 border-2 border-white/30 border-t-white rounded-full animate-spin"></div>
                                RUNNING...
                            </>
                        ) : (
                            <>
                                <span className="material-symbols-outlined text-[16px]">play_arrow</span>
                                RUN TEST
                            </>
                        )}
                    </button>
                </div>
            </div>

            {/* Error Display */}
            {error && (
                <div className="glass rounded-xl p-4 mb-6 border-l-4 border-loss">
                    <p className="text-loss text-sm">{error}</p>
                </div>
            )}

            {/* Loading State */}
            {isRunning && (
                <div className="glass rounded-xl p-12 text-center border-white/5">
                    <div className="flex flex-col items-center gap-4">
                        <div className="w-12 h-12 border-2 border-cyan-500 border-t-transparent rounded-full animate-spin shadow-[0_0_20px_rgba(6,182,212,0.4)]"></div>
                        <p className="text-cyan-400 font-medium tracking-widest text-xs animate-pulse">SIMULATING MARKET DATA...</p>
                    </div>
                </div>
            )}

            {/* Results Display */}
            {!isRunning && backtestResults && (
                <div className="space-y-6 animate-fade-in">
                    {/* Performance Metrics Grid */}
                    <div className="grid grid-cols-4 gap-4">
                        <div className="glass rounded-xl p-6 border-white/5 bg-[#0A0A0A]/50 backdrop-blur-md relative overflow-hidden group">
                            <div className="absolute top-0 right-0 w-24 h-24 bg-cyan-500/10 blur-2xl rounded-full -mr-12 -mt-12 pointer-events-none group-hover:bg-cyan-500/20 transition-all"></div>
                            <div className="text-[10px] text-gray-500 mb-2 uppercase tracking-widest font-bold">Net Profit</div>
                            <div className={`text-3xl font-bold font-data tabular-nums relative z-10 ${(backtestResults.summary?.net_profit || 0) >= 0 ? 'text-green-400' : 'text-red-400'
                                }`}>
                                ₹{(backtestResults.summary?.net_profit || 0).toLocaleString('en-IN')}
                            </div>
                            <div className={`text-xs font-data tabular-nums mt-1 font-bold px-1.5 py-0.5 rounded inline-block ${(backtestResults.summary?.roi_pct || 0) >= 0 ? 'text-green-400 bg-green-500/10' : 'text-red-400 bg-red-500/10'
                                }`}>
                                {(backtestResults.summary?.roi_pct || 0) >= 0 ? '+' : ''}
                                {(backtestResults.summary?.roi_pct || 0).toFixed(2)}%
                            </div>
                        </div>

                        <div className="glass rounded-xl p-6 border-white/5 bg-[#0A0A0A]/50 backdrop-blur-md">
                            <div className="text-[10px] text-gray-500 mb-2 uppercase tracking-widest font-bold">Win Rate</div>
                            <div className="text-3xl font-bold font-data tabular-nums text-white">
                                {(backtestResults.summary?.win_rate_pct || 0).toFixed(1)}%
                            </div>
                            <div className="text-xs text-gray-500 mt-1 font-mono">
                                {backtestResults.summary?.winning_trades || 0} / {backtestResults.summary?.total_trades || 0} trades
                            </div>
                        </div>

                        <div className="glass rounded-xl p-6 border-white/5 bg-[#0A0A0A]/50 backdrop-blur-md">
                            <div className="text-[10px] text-gray-500 mb-2 uppercase tracking-widest font-bold">Sharpe Ratio</div>
                            <div className="text-3xl font-bold font-data tabular-nums text-purple-400">
                                {(backtestResults.metrics?.risk_metrics?.sharpe_ratio || 0).toFixed(2)}
                            </div>
                            <div className="text-xs text-gray-500 mt-1">
                                Risk-Adjusted Return
                            </div>
                        </div>

                        <div className="glass rounded-xl p-6 border-white/5 bg-[#0A0A0A]/50 backdrop-blur-md">
                            <div className="text-[10px] text-gray-500 mb-2 uppercase tracking-widest font-bold">Max Drawdown</div>
                            <div className="text-3xl font-bold font-data tabular-nums text-orange-400">
                                {(backtestResults.metrics?.risk_metrics?.max_drawdown_pct || 0).toFixed(2)}%
                            </div>
                            <div className="text-xs text-gray-500 mt-1">
                                Worst decline
                            </div>
                        </div>
                    </div>

                    {/* Equity Curve Placeholder */}
                    <div className="glass rounded-xl p-6 border-white/5 bg-[#0A0A0A]/50 backdrop-blur-md h-64 relative overflow-hidden">
                        <div className="absolute top-0 right-0 w-64 h-64 bg-purple-500/5 blur-3xl rounded-full -mr-32 -mt-32 pointer-events-none"></div>
                        <h3 className="text-sm font-bold text-gray-300 mb-4 uppercase tracking-wider">
                            Equity Curve
                        </h3>
                        <div className="h-48 flex items-center justify-center text-gray-600 border border-dashed border-white/5 rounded-lg">
                            <span className="font-mono text-xs">VISUALIZATION ENGINE PLACEHOLDER</span>
                        </div>
                    </div>
                </div>
            )}

            {/* Zero State */}
            {!isRunning && !backtestResults && !error && (
                <div className="glass rounded-xl p-12 text-center border-white/5 bg-[#0A0A0A]/30">
                    <div className="w-16 h-16 rounded-full bg-white/5 mx-auto mb-4 flex items-center justify-center">
                        <span className="material-symbols-outlined text-3xl text-gray-600">science</span>
                    </div>
                    <h2 className="text-xl font-bold text-white mb-2">Tester Agent: Strategy Lab</h2>
                    <p className="text-gray-500 text-sm mb-6 max-w-md mx-auto">
                        Configure your parameters in the command strip above to simulate trading strategies against historical data.
                    </p>
                </div>
            )}
        </div>
    )
}
