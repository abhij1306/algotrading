'use client'

import { useState, useEffect } from 'react'
import { GlassCard } from "@/components/ui/GlassCard"
import { ArrowUpRight, ArrowDownRight, Minus } from "lucide-react"

interface Stock {
    symbol: string
    close: number
    ema20: number
    ema50: number
    atr_pct: number
    rsi: number
    vol_percentile: number
    // Advanced Indicators
    macd: number
    macd_signal: number
    adx: number
    stoch_k: number
    stoch_d: number
    bb_upper: number
    bb_middle: number
    bb_lower: number
    // Scores
    intraday_score?: number
    swing_score?: number
    is_20d_breakout: boolean
    trend_7d?: number
    // Financials
    market_cap?: number
    revenue?: number
    net_income?: number
    eps?: number
    pe_ratio?: number
    roe?: number
    debt_to_equity?: number
}

interface Props {
    data: Stock[]
    type: 'intraday' | 'swing' | 'combined'
    viewMode?: 'technical' | 'financial'
}

import { useWebSocket } from '@/hooks/useWebSocket';

export default function ScreenerTable({ data, type, viewMode = 'technical' }: Props) {
    const [sortKey, setSortKey] = useState<keyof Stock>('symbol')
    const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc')
    const [selectedIndex, setSelectedIndex] = useState(0)
    const [hoveredIndex, setHoveredIndex] = useState<number | null>(null)

    // WebSocket Integration
    const { isConnected, lastMessage } = useWebSocket();

    // Live Data State
    const [livePrices, setLivePrices] = useState<Record<string, { ltp: number, change: number }>>({});

    // Subscribe to visible symbols when data changes or connection opens
    useEffect(() => {
        if (isConnected && data.length > 0) {
            const symbols = data.slice(0, 50).map(s => s.symbol);
            const fyersSymbols = symbols.map(s => s.includes(':') ? s : `NSE:${s}-EQ`);

            fetch('/api/websocket/subscribe', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ symbols: fyersSymbols })
            }).catch(console.error);
        }
    }, [isConnected, data]);

    // Update live prices on tick
    useEffect(() => {
        if (lastMessage && lastMessage.symbol) {
            const rawSym = lastMessage.symbol.replace('NSE:', '').replace('-EQ', '');
            setLivePrices(prev => ({
                ...prev,
                [rawSym]: {
                    ltp: lastMessage.ltp,
                    change: lastMessage.ltp && lastMessage.open_price
                        ? ((lastMessage.ltp - lastMessage.open_price) / lastMessage.open_price * 100)
                        : (lastMessage.chp || 0)
                }
            }));
        }
    }, [lastMessage]);

    const handleSort = (key: keyof Stock) => {
        if (sortKey === key) {
            setSortDir(sortDir === 'asc' ? 'desc' : 'asc')
        } else {
            setSortKey(key)
            setSortDir('desc')
        }
    }

    const sortedData = [...data].sort((a, b) => {
        const aVal = a[sortKey]
        const bVal = b[sortKey]

        if (typeof aVal === 'number' && typeof bVal === 'number') {
            return sortDir === 'asc' ? aVal - bVal : bVal - aVal
        }

        const aStr = String(aVal)
        const bStr = String(bVal)
        return sortDir === 'asc' ? aStr.localeCompare(bStr) : bStr.localeCompare(aStr)
    })

    // Keyboard navigation
    useEffect(() => {
        const handleKeyDown = (e: KeyboardEvent) => {
            if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return

            switch (e.key) {
                case 'ArrowUp':
                    e.preventDefault()
                    setSelectedIndex((prev) => Math.max(0, prev - 1))
                    break
                case 'ArrowDown':
                    e.preventDefault()
                    setSelectedIndex((prev) => Math.min(sortedData.length - 1, prev + 1))
                    break
            }
        }

        document.addEventListener('keydown', handleKeyDown)
        return () => document.removeEventListener('keydown', handleKeyDown)
    }, [sortedData])

    const getScoreColor = (score: number) => {
        if (score >= 80) return 'text-emerald-400 text-shadow-neon-emerald'
        if (score >= 60) return 'text-cyan-400'
        return 'text-gray-500'
    }

    const getChangePercent = (close: number, ema20: number) => {
        return ((close - ema20) / ema20) * 100
    }

    if (data.length === 0) {
        return (
            <div className="flex items-center justify-center p-20 border border-dashed border-white/10 rounded-xl text-gray-500 font-mono text-xs uppercase tracking-widest">
                No stocks match the current filters
            </div>
        )
    }

    return (
        <GlassCard className="h-full overflow-hidden flex flex-col">
            {/* HEADER */}
            <div className="sticky top-0 z-10 bg-[#0A0A0A]/90 backdrop-blur-md border-b border-white/5">
                <div className="grid grid-cols-12 gap-2 px-6 py-3 text-[10px] font-bold text-gray-500 uppercase tracking-widest">
                    <div onClick={() => handleSort('symbol')} className="col-span-2 cursor-pointer hover:text-cyan-400 transition-colors flex items-center gap-1">
                        Sym {sortKey === 'symbol' && <span className="text-cyan-400">{sortDir === 'asc' ? '↑' : '↓'}</span>}
                        {isConnected && <div className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse ml-2" title="Live Feed Active"></div>}
                    </div>
                    <div onClick={() => handleSort('close')} className="col-span-1 text-right cursor-pointer hover:text-cyan-400 transition-colors">
                        Price {sortKey === 'close' && <span className="text-cyan-400">{sortDir === 'asc' ? '↑' : '↓'}</span>}
                    </div>

                    {viewMode === 'technical' ? (
                        <>
                            <div onClick={() => handleSort('atr_pct')} className="col-span-1 text-right cursor-pointer hover:text-cyan-400">ATR%</div>
                            <div onClick={() => handleSort('rsi')} className="col-span-1 text-right cursor-pointer hover:text-cyan-400">RSI</div>
                            <div onClick={() => handleSort('macd')} className="col-span-1 text-right cursor-pointer hover:text-cyan-400">MACD</div>
                            <div onClick={() => handleSort('adx')} className="col-span-1 text-right cursor-pointer hover:text-cyan-400">ADX</div>
                            <div onClick={() => handleSort('stoch_k')} className="col-span-1 text-right cursor-pointer hover:text-cyan-400">Stoch</div>
                            <div onClick={() => handleSort('bb_upper')} className="col-span-2 text-right cursor-pointer hover:text-cyan-400">BBands</div>
                            <div className="col-span-1 text-right">Trend 7D</div>
                            <div onClick={() => handleSort(type === 'intraday' ? 'intraday_score' : 'swing_score')} className="col-span-1 text-right cursor-pointer hover:text-cyan-400">Score</div>
                        </>
                    ) : (
                        <>
                            <div onClick={() => handleSort('market_cap')} className="col-span-2 text-right">Mkt Cap</div>
                            <div onClick={() => handleSort('pe_ratio')} className="col-span-1 text-right">P/E</div>
                            <div onClick={() => handleSort('roe')} className="col-span-1 text-right">ROE</div>
                            <div onClick={() => handleSort('eps')} className="col-span-1 text-right">EPS</div>
                            <div onClick={() => handleSort('revenue')} className="col-span-2 text-right">Rev</div>
                            <div onClick={() => handleSort('debt_to_equity')} className="col-span-1 text-right">D/E</div>
                        </>
                    )}
                </div>
            </div>

            {/* BODY */}
            <div className="overflow-y-auto flex-1 custom-scrollbar">
                {sortedData.map((stock, i) => {
                    const score = type === 'intraday' ? stock.intraday_score : stock.swing_score
                    // Use live data if available
                    const live = livePrices[stock.symbol]
                    const displayPrice = live ? live.ltp : stock.close

                    let changePercent = 0
                    if (live) {
                        changePercent = live.change
                    } else {
                        changePercent = getChangePercent(stock.close, stock.ema20)
                    }

                    const isSelected = i === selectedIndex

                    return (
                        <div
                            key={stock.symbol}
                            className={`grid grid-cols-12 gap-2 px-6 py-3 border-b border-white/[0.03] items-center cursor-pointer transition-all duration-200
                                ${isSelected ? 'bg-white/[0.03] border-l-2 border-l-cyan-400' : 'border-l-2 border-l-transparent hover:bg-white/[0.02]'}
                            `}
                            onClick={() => setSelectedIndex(i)}
                        >
                            {/* Symbol */}
                            <div className="col-span-2 flex items-center gap-2">
                                <span className={`font-medium text-sm ${isSelected ? 'text-cyan-400' : 'text-white'}`}>
                                    {stock.symbol}
                                </span>
                                {stock.is_20d_breakout && (
                                    <span className="text-[9px] bg-emerald-500/10 text-emerald-400 px-1.5 py-0.5 rounded font-bold">BO</span>
                                )}
                            </div>

                            {/* Price */}
                            <div className="col-span-1 text-right">
                                <div className={`font-mono text-sm ${live ? 'text-white font-bold' : 'text-gray-200'}`}>
                                    ₹{displayPrice.toFixed(2)}
                                </div>
                                <div className={`text-[10px] font-mono ${changePercent > 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                                    {changePercent > 0 ? '+' : ''}{changePercent.toFixed(1)}%
                                </div>
                            </div>

                            {viewMode === 'technical' ? (
                                <>
                                    <div className="col-span-1 text-right font-mono text-sm text-gray-400">{stock.atr_pct?.toFixed(1)}%</div>
                                    <div className={`col-span-1 text-right font-mono text-sm ${stock.rsi > 70 ? 'text-red-400' : stock.rsi < 30 ? 'text-emerald-400' : 'text-gray-400'}`}>
                                        {stock.rsi?.toFixed(0)}
                                    </div>
                                    {/* MACD */}
                                    <div className="col-span-1 text-right font-mono text-xs">
                                        <span className={stock.macd > stock.macd_signal ? 'text-emerald-400' : 'text-red-400'}>
                                            {stock.macd?.toFixed(1)}
                                        </span>
                                    </div>
                                    {/* ADX */}
                                    <div className="col-span-1 text-right font-mono text-xs text-gray-400">
                                        {stock.adx?.toFixed(0)}
                                    </div>
                                    {/* Stoch */}
                                    <div className="col-span-1 text-right font-mono text-xs text-gray-400">
                                        {stock.stoch_k?.toFixed(0)}
                                    </div>
                                    {/* BBands */}
                                    <div className="col-span-2 text-right font-mono text-[10px] text-gray-500">
                                        {stock.bb_upper?.toFixed(0)}/{stock.bb_lower?.toFixed(0)}
                                    </div>

                                    <div className="col-span-1 text-right">
                                        <div className={`font-mono text-xs ${(stock.trend_7d || 0) > 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                                            {(stock.trend_7d || 0).toFixed(1)}%
                                        </div>
                                    </div>

                                    <div className="col-span-1 text-right">
                                        <span className={`font-mono font-bold text-base ${getScoreColor(score || 0)}`}>
                                            {(score || 0).toFixed(0)}
                                        </span>
                                    </div>
                                </>
                            ) : (
                                <>
                                    <div className="col-span-2 text-right font-mono text-xs text-gray-400">{stock.market_cap ? (stock.market_cap / 100).toFixed(0) : '-'}</div>
                                    <div className="col-span-1 text-right font-mono text-xs text-gray-400">{stock.pe_ratio?.toFixed(1) || '-'}</div>
                                    <div className={`col-span-1 text-right font-mono text-xs ${stock.roe && stock.roe > 15 ? 'text-emerald-400' : 'text-gray-400'}`}>{stock.roe?.toFixed(1)}%</div>
                                    <div className="col-span-1 text-right font-mono text-xs text-gray-400">{stock.eps?.toFixed(1)}</div>
                                    <div className="col-span-2 text-right font-mono text-xs text-gray-400">{stock.revenue ? (stock.revenue / 10000000).toFixed(0) : '-'}</div>
                                    <div className={`col-span-1 text-right font-mono text-xs ${stock.debt_to_equity && stock.debt_to_equity > 1 ? 'text-red-400' : 'text-gray-400'}`}>{stock.debt_to_equity?.toFixed(2)}</div>
                                </>
                            )}
                        </div>
                    )
                })}
            </div>
        </GlassCard>
    )
}
