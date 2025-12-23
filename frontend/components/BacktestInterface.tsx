'use client'

import React, { useState, useEffect, useRef } from 'react'
import {
    LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, AreaChart, Area
} from 'recharts'
import { Play, Calendar, Settings, Activity, TrendingUp, TrendingDown, AlertTriangle, Loader2, ArrowRight, Search, ChevronRight, BarChart3 } from 'lucide-react'
import Portal from '@/components/ui/Portal'
import EmptyState from '@/components/ui/EmptyState'

export default function BacktestInterface() {
    const [strategies, setStrategies] = useState<any[]>([])
    const [selectedStrategy, setSelectedStrategy] = useState('ORB')

    // Search State
    const [symbol, setSymbol] = useState('NIFTY')
    const [searchResults, setSearchResults] = useState<any[]>([])
    const [isSearching, setIsSearching] = useState(false)
    const searchInputRef = useRef<HTMLInputElement>(null)
    const dropdownRef = useRef<HTMLDivElement>(null)
    const skipSearch = useRef(true)

    const [startDate, setStartDate] = useState('2024-01-01')
    const [endDate, setEndDate] = useState(new Date().toISOString().split('T')[0])

    const [initialCapital, setInitialCapital] = useState('100000')

    // Debounced Search
    useEffect(() => {
        const timer = setTimeout(() => {
            if (skipSearch.current) {
                skipSearch.current = false
                return
            }
            if (symbol && symbol.length >= 2) handleSearch(symbol)
        }, 300)
        return () => clearTimeout(timer)
    }, [symbol])

    const handleSearch = async (query: string) => {
        if (query.length < 1) {
            setSearchResults([])
            return
        }
        setIsSearching(true)
        try {
            const res = await fetch(`/api/market/search?query=${query}`)
            if (res.ok) {
                const data = await res.json()
                setSearchResults(data || [])
            }
        } catch (error) {
            console.error('Search error:', error)
        } finally {
            setIsSearching(false)
        }
    }

    // Close dropdown on outside click
    useEffect(() => {
        const handleClickOutside = (e: MouseEvent) => {
            if (
                searchInputRef.current &&
                !searchInputRef.current.contains(e.target as Node) &&
                dropdownRef.current &&
                !dropdownRef.current.contains(e.target as Node)
            ) {
                setSearchResults([])
            }
        }
        document.addEventListener('mousedown', handleClickOutside)
        return () => document.removeEventListener('mousedown', handleClickOutside)
    }, [])

    // Params
    const [atrStopLoss, setAtrStopLoss] = useState('1.5')
    const [atrTakeProfit, setAtrTakeProfit] = useState('3.0')
    const [riskPerTrade, setRiskPerTrade] = useState('1.0')
    const [timeframe, setTimeframe] = useState('5min')

    // State
    const [loading, setLoading] = useState(false)
    const [results, setResults] = useState<any>(null)
    const [error, setError] = useState('')

    useEffect(() => {
        // Fetch strategies from backend
        const fetchStrategies = async () => {
            try {
                const res = await fetch('/api/backtest/list')
                if (res.ok) {
                    const data = await res.json()
                    setStrategies(data.strategies || [])
                }
            } catch (err) {
                console.error("Failed to fetch strategies", err)
            }
        }
        fetchStrategies()
    }, [])

    const runBacktest = async () => {
        if (!symbol) return
        setLoading(true)
        setError('')
        setResults(null)

        try {
            const res = await fetch('/api/backtest/run', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    strategy_name: selectedStrategy,
                    symbol: symbol.toUpperCase(),
                    start_date: startDate,
                    end_date: endDate,
                    timeframe: timeframe,
                    initial_capital: parseFloat(initialCapital) || 100000,
                    params: {
                        stopLoss: parseFloat(atrStopLoss),
                        takeProfit: parseFloat(atrTakeProfit),
                        riskPerTrade: parseFloat(riskPerTrade) || 1.0
                    }
                })
            })

            if (!res.ok) {
                const err = await res.json()
                throw new Error(err.detail || 'Backtest Failed')
            }

            const data = await res.json()
            setResults(data)

        } catch (err: any) {
            setError(err.message)
        } finally {
            setLoading(false)
        }
    }

    return (
        <div className="h-full flex flex-col bg-[#050505] text-gray-200 overflow-hidden font-sans">
            {/* Toolbar */}
            <div className="border-b border-white/5 bg-[#0A0A0A]/50 backdrop-blur-md p-4 flex flex-wrap gap-4 items-end z-20">

                {/* Strategy Selector */}
                <div className="space-y-1.5 min-w-[180px]">
                    <label className="text-[9px] font-bold text-gray-500 uppercase tracking-widest pl-1">Strategy Model</label>
                    <div className="relative">
                        <select
                            value={selectedStrategy}
                            onChange={(e) => setSelectedStrategy(e.target.value)}
                            className="w-full bg-[#050505] border border-white/10 rounded-lg px-3 py-2 text-xs font-bold text-white focus:border-cyan-500/50 focus:outline-none appearance-none hover:bg-white/5 transition-colors cursor-pointer"
                        >
                            {strategies.map(s => (
                                <option key={s.name} value={s.name}>{s.display_name}</option>
                            ))}
                        </select>
                        <ChevronRight className="absolute right-3 top-2.5 w-3 h-3 text-gray-500 pointer-events-none rotate-90" />
                    </div>
                </div>

                {/* Symbol Search */}
                <div className="space-y-1.5 w-[180px]">
                    <label className="text-[9px] font-bold text-gray-500 uppercase tracking-widest pl-1">Asset</label>
                    <div className="relative group">
                        <Search className="absolute left-3 top-2.5 h-3.5 w-3.5 text-gray-600 group-focus-within:text-cyan-500 transition-colors pointer-events-none" />
                        <input
                            ref={searchInputRef}
                            type="text"
                            value={symbol}
                            onChange={(e) => setSymbol(e.target.value.toUpperCase())}
                            placeholder="SEARCH SYMBOL..."
                            autoComplete="off"
                            className="w-full bg-[#050505] border border-white/10 rounded-lg pl-9 pr-3 py-2 text-xs font-bold text-white uppercase focus:border-cyan-500/50 focus:outline-none placeholder-gray-700 transition-all font-mono"
                        />
                        {searchResults.length > 0 && symbol && (
                            <Portal>
                                <div
                                    ref={dropdownRef}
                                    className="fixed z-[9999] bg-[#0A0A0A] border border-white/10 rounded-xl shadow-2xl backdrop-blur-xl max-h-64 overflow-y-auto"
                                    style={{
                                        top: (searchInputRef.current?.getBoundingClientRect().bottom || 0) + 4,
                                        left: (searchInputRef.current?.getBoundingClientRect().left || 0),
                                        width: (searchInputRef.current?.getBoundingClientRect().width || 0),
                                    }}
                                >
                                    {searchResults.map((s, i) => (
                                        <button
                                            key={i}
                                            onClick={() => {
                                                skipSearch.current = true
                                                setSymbol(s.symbol)
                                                setSearchResults([])
                                            }}
                                            className="w-full p-3 text-left hover:bg-white/5 flex items-center justify-between group transition-colors border-b border-white/5 last:border-0"
                                        >
                                            <div className="flex items-center gap-3">
                                                <div className="w-6 h-6 rounded bg-white/5 flex items-center justify-center font-bold text-[10px] text-gray-400 group-hover:text-cyan-400 transition-colors">
                                                    {s.symbol[0]}
                                                </div>
                                                <div>
                                                    <p className="text-xs font-bold text-white leading-none">{s.symbol}</p>
                                                    <p className="text-[9px] text-gray-600 mt-0.5 uppercase truncate max-w-[120px]">{s.name}</p>
                                                </div>
                                            </div>
                                        </button>
                                    ))}
                                </div>
                            </Portal>
                        )}
                    </div>
                </div>

                {/* Date Values */}
                <div className="space-y-1.5 hidden md:block">
                    <label className="text-[9px] font-bold text-gray-500 uppercase tracking-widest flex items-center gap-1 pl-1">
                        <Calendar className="w-3 h-3" /> Period
                    </label>
                    <div className="flex gap-2 bg-[#050505] border border-white/10 rounded-lg p-1">
                        <input
                            type="date"
                            value={startDate}
                            onChange={(e) => setStartDate(e.target.value)}
                            className="bg-transparent text-xs text-gray-400 focus:text-white focus:outline-none px-2 py-1 w-24"
                        />
                        <span className="text-gray-700 self-center font-mono">/</span>
                        <input
                            type="date"
                            value={endDate}
                            onChange={(e) => setEndDate(e.target.value)}
                            className="bg-transparent text-xs text-gray-400 focus:text-white focus:outline-none px-2 py-1 w-24"
                        />
                    </div>
                </div>

                {/* ATR Configuration */}
                <div className="flex gap-2 border-l border-white/5 pl-4 ml-2">
                    <div className="space-y-1.5 w-[70px]">
                        <label className="text-[9px] font-bold text-gray-600 uppercase tracking-widest pl-1">SL (ATR)</label>
                        <input
                            type="number"
                            step="0.1"
                            value={atrStopLoss}
                            onChange={(e) => setAtrStopLoss(e.target.value)}
                            className="w-full bg-[#050505] border border-white/10 rounded-lg px-2 py-2 text-xs text-right font-mono text-cyan-500 focus:border-cyan-500/50 focus:outline-none"
                        />
                    </div>
                    <div className="space-y-1.5 w-[70px]">
                        <label className="text-[9px] font-bold text-gray-600 uppercase tracking-widest pl-1">TP (ATR)</label>
                        <input
                            type="number"
                            step="0.1"
                            value={atrTakeProfit}
                            onChange={(e) => setAtrTakeProfit(e.target.value)}
                            className="w-full bg-[#050505] border border-white/10 rounded-lg px-2 py-2 text-xs text-right font-mono text-green-500 focus:border-green-500/50 focus:outline-none"
                        />
                    </div>
                </div>

                {/* Capital & Risk */}
                <div className="flex gap-2 border-l border-white/5 pl-4 ml-2">
                    <div className="space-y-1.5 w-[80px]">
                        <label className="text-[9px] font-bold text-gray-600 uppercase tracking-widest pl-1">Capital (₹)</label>
                        <input
                            type="number"
                            value={initialCapital}
                            onChange={(e) => setInitialCapital(e.target.value)}
                            className="w-full bg-[#050505] border border-white/10 rounded-lg px-2 py-2 text-xs text-right font-mono text-white focus:border-cyan-500/50 focus:outline-none"
                        />
                    </div>
                    <div className="space-y-1.5 w-[60px]">
                        <label className="text-[9px] font-bold text-gray-600 uppercase tracking-widest pl-1">Risk %</label>
                        <input
                            type="number"
                            step="0.1"
                            value={riskPerTrade}
                            onChange={(e) => setRiskPerTrade(e.target.value)}
                            className="w-full bg-[#050505] border border-white/10 rounded-lg px-2 py-2 text-xs text-right font-mono text-white focus:border-cyan-500/50 focus:outline-none"
                        />
                    </div>
                </div>

                {/* Action Button */}
                <div className="flex-1 flex justify-end">
                    <button
                        onClick={runBacktest}
                        disabled={loading || !symbol}
                        className="bg-gradient-to-r from-cyan-600 to-cyan-500 hover:from-cyan-500 hover:to-cyan-400 text-white px-6 py-2.5 rounded-lg flex items-center gap-2 font-bold text-xs tracking-wider transition-all shadow-lg shadow-cyan-500/20 disabled:opacity-50 disabled:cursor-not-allowed disabled:shadow-none"
                    >
                        {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Play className="w-4 h-4 fill-current" />}
                        RUN BACKTEST
                    </button>
                </div>
            </div>

            {/* Content Area */}
            <div className="flex-1 overflow-y-auto p-6 space-y-6 scrollbar-thin scrollbar-thumb-white/10">

                {error && (
                    <div className="p-4 bg-red-500/5 border border-red-500/10 rounded-xl flex items-center gap-3 text-red-400 animate-in slide-in-from-top-2">
                        <AlertTriangle className="w-5 h-5 flex-shrink-0" />
                        <p className="text-xs font-mono">{error}</p>
                    </div>
                )}

                {!loading && !results && (
                    <div className="h-[400px] flex items-center justify-center border border-dashed border-white/10 rounded-2xl bg-white/[0.02]">
                        <EmptyState
                            icon={BarChart3}
                            title="Ready to Simulate"
                            description="Configure parameters above and click Run Backtest to start the simulation."
                        />
                    </div>
                )}

                {loading && (
                    <div className="h-[400px] flex flex-col items-center justify-center">
                        <Loader2 className="w-10 h-10 text-cyan-500 animate-spin mb-4" />
                        <p className="text-cyan-500 font-mono text-[10px] uppercase tracking-widest animate-pulse">Running Simulation on {symbol}...</p>
                    </div>
                )}

                {results && (
                    <div className="space-y-6 animate-in fade-in slide-in-from-bottom-4 duration-500">
                        {/* 1. KPI Cards */}
                        <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-3">
                            <MetricCard
                                label="Total Return"
                                value={`${results.metrics.total_return_pct}%`}
                                trend={results.metrics.total_return_pct >= 0 ? 'up' : 'down'}
                            />
                            <MetricCard
                                label="Sharpe Ratio"
                                value={results.metrics.sharpe_ratio}
                                color={results.metrics.sharpe_ratio > 1 ? 'text-green-400' : 'text-orange-400'}
                            />
                            <MetricCard
                                label="Max Drawdown"
                                value={`${results.metrics.max_drawdown_pct}%`}
                                color="text-red-400"
                            />
                            <MetricCard
                                label="Win Rate"
                                value={`${results.metrics.win_rate_pct}%`}
                                subValue={`${results.metrics.winning_trades}/${results.metrics.total_trades} Trades`}
                            />
                            <MetricCard
                                label="CAGR"
                                value={`${results.metrics.cagr_pct}%`}
                                trend={results.metrics.cagr_pct > 0 ? 'up' : 'down'}
                            />
                            <MetricCard
                                label="Profit Factor"
                                value={results.metrics.profit_factor}
                            />
                        </div>

                        {/* 2. Equity Curve */}
                        <div className="h-[400px] bg-[#0A0A0A] border border-white/10 rounded-xl p-6 relative group overflow-hidden">
                            <div className="absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-cyan-500/20 to-transparent"></div>
                            <div className="absolute top-6 left-6 z-10">
                                <h3 className="text-[10px] font-bold text-gray-500 uppercase tracking-widest mb-0.5">Equity Curve</h3>
                                <p className="text-xl font-bold text-white font-mono">₹{(results.metrics.final_equity || 0).toLocaleString()}</p>
                            </div>
                            <ResponsiveContainer width="100%" height="100%">
                                <AreaChart data={results.equity_curve} margin={{ top: 40, right: 0, left: 0, bottom: 0 }}>
                                    <defs>
                                        <linearGradient id="colorEquity" x1="0" y1="0" x2="0" y2="1">
                                            <stop offset="5%" stopColor="#06b6d4" stopOpacity={0.2} />
                                            <stop offset="95%" stopColor="#06b6d4" stopOpacity={0} />
                                        </linearGradient>
                                    </defs>
                                    <CartesianGrid strokeDasharray="3 3" stroke="#ffffff05" vertical={false} />
                                    <XAxis
                                        dataKey="timestamp"
                                        tick={{ fill: '#525252', fontSize: 9, fontFamily: 'monospace' }}
                                        axisLine={false}
                                        tickLine={false}
                                        minTickGap={60}
                                        dy={10}
                                    />
                                    <YAxis
                                        domain={['auto', 'auto']}
                                        tick={{ fill: '#525252', fontSize: 9, fontFamily: 'monospace' }}
                                        axisLine={false}
                                        tickLine={false}
                                        tickFormatter={(val) => `₹${(val / 1000).toFixed(0)}k`}
                                        dx={-10}
                                    />
                                    <Tooltip
                                        contentStyle={{ backgroundColor: '#09090b', borderColor: '#27272a', borderRadius: '8px', fontSize: '12px' }}
                                        itemStyle={{ color: '#fff' }}
                                        labelStyle={{ color: '#a1a1aa' }}
                                    />
                                    <Area
                                        type="monotone"
                                        dataKey="equity"
                                        stroke="#06b6d4"
                                        strokeWidth={2}
                                        fillOpacity={1}
                                        fill="url(#colorEquity)"
                                    />
                                </AreaChart>
                            </ResponsiveContainer>
                        </div>

                        {/* 3. Trade Log Table */}
                        <div className="border border-white/10 rounded-xl overflow-hidden bg-[#0A0A0A]">
                            <div className="px-4 py-3 border-b border-white/5 flex justify-between items-center bg-white/[0.02]">
                                <h3 className="text-[10px] font-bold text-gray-500 uppercase tracking-widest">Trade Log</h3>
                                <span className="text-[10px] text-gray-600 font-mono">{results.trades.length} EXECUTION EVENTS</span>
                            </div>
                            <div className="overflow-x-auto max-h-[400px]">
                                <table className="w-full text-left border-collapse">
                                    <thead className="sticky top-0 bg-[#0A0A0A] z-10 shadow-sm">
                                        <tr className="border-b border-white/5 text-[9px] font-bold text-gray-600 uppercase tracking-wider">
                                            <th className="p-3">Time</th>
                                            <th className="p-3">Symbol</th>
                                            <th className="p-3">Type</th>
                                            <th className="p-3 text-right">Entry</th>
                                            <th className="p-3 text-right">Exit</th>
                                            <th className="p-3 text-right">Qty</th>
                                            <th className="p-3 text-right">P&L</th>
                                            <th className="p-3 text-right">%</th>
                                        </tr>
                                    </thead>
                                    <tbody className="text-[11px] font-mono">
                                        {results.trades.map((t: any, i: number) => (
                                            <tr key={i} className="border-b border-white/5 hover:bg-white/5 transition-colors group">
                                                <td className="p-3 text-gray-500">{t.entry_time ? new Date(t.entry_time).toLocaleString() : '-'}</td>
                                                <td className="p-3 text-white font-bold">{t.symbol}</td>
                                                <td className={`p-3 font-bold ${t.direction === 'LONG' ? 'text-green-500' : 'text-red-500'}`}>
                                                    <span className={`px-1.5 py-0.5 rounded ${t.direction === 'LONG' ? 'bg-green-500/10' : 'bg-red-500/10'}`}>
                                                        {t.direction}
                                                    </span>
                                                </td>
                                                <td className="p-3 text-right text-gray-300">₹{(t.entry_price || 0).toFixed(2)}</td>
                                                <td className="p-3 text-right text-gray-300">
                                                    {(t.exit_price && t.exit_price > 0) ? `₹${t.exit_price.toFixed(2)}` : <span className="text-cyan-500/50 text-[10px]">OPEN</span>}
                                                </td>
                                                <td className="p-3 text-right text-gray-500">{t.quantity}</td>
                                                <td className={`p-3 text-right font-bold ${t.pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                                                    ₹{(t.pnl || 0).toLocaleString()}
                                                </td>
                                                <td className={`p-3 text-right font-bold ${t.pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                                                    {t.pnl_pct}%
                                                </td>
                                            </tr>
                                        ))}
                                    </tbody>
                                </table>
                            </div>
                        </div>

                    </div>
                )}
            </div>
        </div>
    )
}

function MetricCard({ label, value, subValue, trend, color }: any) {
    return (
        <div className="p-4 rounded-xl bg-[#0A0A0A] border border-white/10 hover:border-white/20 transition-all group relative overflow-hidden">
            <div className="absolute inset-0 bg-gradient-to-br from-white/5 to-transparent opacity-0 group-hover:opacity-100 transition-opacity"></div>
            <p className="text-[9px] font-bold text-gray-600 uppercase tracking-widest mb-1.5">{label}</p>
            <div className="flex items-end gap-2">
                <span className={`text-lg font-bold font-mono tracking-tight ${color || 'text-white'}`}>
                    {value}
                </span>
                {trend && (
                    trend === 'up'
                        ? <TrendingUp className="w-3.5 h-3.5 text-green-500 mb-1" />
                        : <TrendingDown className="w-3.5 h-3.5 text-red-500 mb-1" />
                )}
            </div>
            {subValue && (
                <p className="text-[9px] text-gray-600 mt-1 font-mono">{subValue}</p>
            )}
        </div>
    )
}
