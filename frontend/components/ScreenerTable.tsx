'use client'

import { useState, useEffect } from 'react'
// import { Sparkline } from './Sparkline' // Temporarily unused

interface Stock {
    symbol: string
    close: number
    ema20: number
    ema50: number
    atr_pct: number
    rsi: number
    vol_percentile: number
    intraday_score?: number
    swing_score?: number
    is_20d_breakout: boolean
    trend_7d?: number
    trend_30d?: number
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

export default function ScreenerTable({ data, type, viewMode = 'technical' }: Props) {
    const [sortKey, setSortKey] = useState<keyof Stock>('symbol')
    const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc')
    const [selectedIndex, setSelectedIndex] = useState(0)
    const [hoveredIndex, setHoveredIndex] = useState<number | null>(null)

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
            // Ignore if user is typing
            if (
                e.target instanceof HTMLInputElement ||
                e.target instanceof HTMLTextAreaElement
            ) {
                return
            }

            switch (e.key) {
                case 'ArrowUp':
                    e.preventDefault()
                    setSelectedIndex((prev) => Math.max(0, prev - 1))
                    break
                case 'ArrowDown':
                    e.preventDefault()
                    setSelectedIndex((prev) => Math.min(sortedData.length - 1, prev + 1))
                    break
                case 'Enter':
                    e.preventDefault()
                    // TODO: Open stock detail
                    console.log('Open detail for:', sortedData[selectedIndex]?.symbol)
                    break
                case ' ':
                    e.preventDefault()
                    // TODO: Open peek preview
                    console.log('Peek preview for:', sortedData[selectedIndex]?.symbol)
                    break
            }
        }

        document.addEventListener('keydown', handleKeyDown)
        return () => document.removeEventListener('keydown', handleKeyDown)
    }, [sortedData, selectedIndex])

    const getScoreColor = (score: number) => {
        if (score >= 80) return 'text-profit glow-profit'
        if (score >= 60) return 'text-electric-amber'
        return 'text-text-tertiary'
    }

    const getChangePercent = (close: number, ema20: number) => {
        return ((close - ema20) / ema20) * 100
    }

    if (data.length === 0) {
        return (
            <div className="glass rounded-lg p-12 text-center">
                <p className="text-text-secondary text-lg">No stocks match the current filters</p>
            </div>
        )
    }

    return (
        <div className="h-full overflow-auto">
            {/* ============================================================ */}
            {/* STICKY GLASS HEADER (Phantom Grid) */}
            {/* ============================================================ */}
            {/* ============================================================ */}
            {/* STICKY GLASS HEADER (Phantom Grid) */}
            {/* ============================================================ */}
            <div className="sticky top-0 z-10 glass-subtle">
                <div className="grid grid-cols-8 gap-4 px-4 py-2 text-xs font-medium text-text-tertiary uppercase tracking-wide">
                    <div
                        onClick={() => handleSort('symbol')}
                        className="cursor-pointer hover:text-electric-blue transition-colors flex items-center gap-1"
                    >
                        Symbol
                        {sortKey === 'symbol' && (
                            <span className="text-electric-blue">{sortDir === 'asc' ? '↑' : '↓'}</span>
                        )}
                    </div>
                    <div
                        onClick={() => handleSort('close')}
                        className="text-right cursor-pointer hover:text-electric-blue transition-colors flex items-center justify-end gap-1"
                    >
                        Price
                        {sortKey === 'close' && (
                            <span className="text-electric-blue">{sortDir === 'asc' ? '↑' : '↓'}</span>
                        )}
                    </div>
                    {/* DYNAMIC MIDDLE COLUMNS */}
                    {viewMode === 'technical' ? (
                        <>
                            <div
                                onClick={() => handleSort('atr_pct')}
                                className="text-right cursor-pointer hover:text-electric-blue transition-colors flex items-center justify-end gap-1"
                            >
                                ATR%
                                {sortKey === 'atr_pct' && (
                                    <span className="text-electric-blue">{sortDir === 'asc' ? '↑' : '↓'}</span>
                                )}
                            </div>
                            <div
                                onClick={() => handleSort('rsi')}
                                className="text-right cursor-pointer hover:text-electric-blue transition-colors flex items-center justify-end gap-1"
                            >
                                RSI
                                {sortKey === 'rsi' && (
                                    <span className="text-electric-blue">{sortDir === 'asc' ? '↑' : '↓'}</span>
                                )}
                            </div>
                            <div
                                onClick={() => handleSort('vol_percentile')}
                                className="text-right cursor-pointer hover:text-electric-blue transition-colors flex items-center justify-end gap-1"
                            >
                                Vol %ile
                                {sortKey === 'vol_percentile' && (
                                    <span className="text-electric-blue">{sortDir === 'asc' ? '↑' : '↓'}</span>
                                )}
                            </div>
                            <div className="text-right">Trend (7D)</div>
                            <div className="text-right">Trend (30D)</div>
                            <div
                                onClick={() => handleSort(type === 'intraday' ? 'intraday_score' : 'swing_score')}
                                className="text-right cursor-pointer hover:text-electric-blue transition-colors flex items-center justify-end gap-1 group relative"
                            >
                                Technical Score
                                {sortKey === (type === 'intraday' ? 'intraday_score' : 'swing_score') && (
                                    <span className="text-electric-blue">{sortDir === 'asc' ? '↑' : '↓'}</span>
                                )}
                                {/* Tooltip */}
                                <div className="absolute top-full right-0 mt-2 w-48 p-2 bg-[#1a1a1a] border border-white/10 rounded-lg shadow-xl opacity-0 group-hover:opacity-100 transition-opacity pointer-events-none z-50">
                                    <p className="text-[10px] text-gray-400 normal-case font-normal leading-relaxed text-left">
                                        Quant-model based on Trend, Momentum & Volatility. Higher is better.
                                    </p>
                                </div>
                            </div>
                        </>
                    ) : (
                        <>
                            <div onClick={() => handleSort('market_cap')} className="text-right cursor-pointer hover:text-electric-blue">Cap (Cr)</div>
                            <div onClick={() => handleSort('pe_ratio')} className="text-right cursor-pointer hover:text-electric-blue">P/E</div>
                            <div onClick={() => handleSort('roe')} className="text-right cursor-pointer hover:text-electric-blue">ROE%</div>
                            <div onClick={() => handleSort('eps')} className="text-right cursor-pointer hover:text-electric-blue">EPS</div>
                            <div onClick={() => handleSort('revenue')} className="text-right cursor-pointer hover:text-electric-blue">Rev (Cr)</div>
                            <div onClick={() => handleSort('debt_to_equity')} className="text-right cursor-pointer hover:text-electric-blue">D/E</div>
                        </>
                    )}

                </div>
            </div>

            {/* ============================================================ */}
            {/* PHANTOM GRID ROWS (32px Dense) */}
            {/* ============================================================ */}
            <div className="divide-y divide-white/5">
                {sortedData.map((stock, i) => {
                    const score = type === 'intraday' ? stock.intraday_score : stock.swing_score
                    const changePercent = getChangePercent(stock.close, stock.ema20)
                    const isSelected = i === selectedIndex
                    const isHovered = i === hoveredIndex

                    return (
                        <div
                            key={stock.symbol}
                            className={`grid grid-cols-8 gap-4 px-4 h-row-dense items-center
                         row-ghost cursor-pointer transition-all
                         ${isSelected ? 'row-active' : ''}
                         ${isHovered ? 'bg-white/3' : ''}
                         group`}
                            onMouseEnter={() => setHoveredIndex(i)}
                            onMouseLeave={() => setHoveredIndex(null)}
                            onClick={() => setSelectedIndex(i)}
                        >
                            {/* Symbol */}
                            <div className="flex items-center gap-2">
                                <span className="font-mono font-semibold text-sm text-text-primary">
                                    {stock.symbol}
                                </span>
                                {stock.is_20d_breakout && (
                                    <span className="text-xs bg-profit/20 text-profit px-1.5 py-0.5 rounded">
                                        BO
                                    </span>
                                )}
                            </div>

                            {/* Price */}
                            <div className="text-right">
                                <div className="font-data text-sm tabular-nums text-text-primary">
                                    ₹{stock.close.toFixed(2)}
                                </div>
                                <div className={`text-xs font-data tabular-nums ${changePercent > 0 ? 'text-profit' : 'text-loss'
                                    }`}>
                                    {changePercent > 0 ? '+' : ''}{changePercent.toFixed(2)}%
                                </div>
                            </div>

                            {/* Dynamic Columns based on View Mode */}
                            {viewMode === 'technical' ? (
                                <>
                                    {/* ATR% */}
                                    <div className="text-right font-data text-sm text-text-secondary tabular-nums">
                                        {stock.atr_pct.toFixed(2)}%
                                    </div>
                                    {/* RSI */}
                                    <div className="text-right font-data text-sm text-text-secondary tabular-nums">
                                        {stock.rsi.toFixed(1)}
                                    </div>
                                    {/* Vol Percentile */}
                                    <div className="text-right font-data text-sm text-text-secondary tabular-nums">
                                        {Math.round(stock.vol_percentile)}%
                                    </div>
                                    {/* Trend 7D */}
                                    <div className="flex justify-end items-center">
                                        <span className={`text-xs font-bold tabular-nums ${(stock.trend_7d || 0) >= 0 ? 'text-profit' : 'text-loss'}`}>
                                            {(stock.trend_7d || 0) > 0 ? '+' : ''}{(stock.trend_7d || 0).toFixed(1)}%
                                        </span>
                                    </div>
                                    {/* Trend 30D */}
                                    <div className="flex justify-end items-center">
                                        <span className={`text-xs font-bold tabular-nums ${(stock.trend_30d || 0) >= 0 ? 'text-profit' : 'text-loss'}`}>
                                            {(stock.trend_30d || 0) > 0 ? '+' : ''}{(stock.trend_30d || 0).toFixed(1)}%
                                        </span>
                                    </div>
                                    {/* Score */}
                                    <div className="text-right">
                                        <span className={`font-data text-base font-semibold tabular-nums ${getScoreColor(score || 0)}`}>
                                            {(score || 0).toFixed(0)}
                                        </span>
                                    </div>
                                </>
                            ) : (
                                <>
                                    {/* Market Cap */}
                                    <div className="text-right font-data text-sm text-text-primary tabular-nums">
                                        {stock.market_cap ? (stock.market_cap / 100).toFixed(0) : '-'}
                                    </div>
                                    {/* P/E */}
                                    <div className="text-right font-data text-sm tabular-nums text-text-secondary">
                                        {stock.pe_ratio ? stock.pe_ratio.toFixed(1) : '-'}
                                    </div>
                                    {/* ROE */}
                                    <div className="text-right font-data text-sm tabular-nums text-text-secondary">
                                        {stock.roe ? stock.roe.toFixed(1) + '%' : '-'}
                                    </div>
                                    {/* EPS */}
                                    <div className="text-right font-data text-sm tabular-nums text-text-secondary">
                                        {stock.eps ? '₹' + stock.eps.toFixed(1) : '-'}
                                    </div>
                                    {/* Revenue */}
                                    <div className="text-right font-data text-sm tabular-nums text-text-secondary">
                                        {stock.revenue ? (stock.revenue / 10000000).toFixed(0) + 'Cr' : '-'}
                                    </div>
                                    {/* Debt to Equity */}
                                    <div className="text-right font-data text-sm tabular-nums text-text-secondary">
                                        {stock.debt_to_equity ? stock.debt_to_equity.toFixed(2) : '-'}
                                    </div>
                                </>
                            )}


                        </div>
                    )
                })}
            </div>

            {/* ============================================================ */}
            {/* KEYBOARD HINT (Bottom) */}
            {/* ============================================================ */}
            <div className="sticky bottom-0 glass-subtle border-t border-white/5 px-4 py-2">
                <div className="flex items-center gap-4 text-xs text-text-tertiary">
                    <span className="flex items-center gap-1">
                        <kbd className="px-1.5 py-0.5 bg-white/10 rounded font-mono">↑↓</kbd>
                        Navigate
                    </span>
                    <span className="flex items-center gap-1">
                        <kbd className="px-1.5 py-0.5 bg-white/10 rounded font-mono">Enter</kbd>
                        Open
                    </span>
                    <span className="flex items-center gap-1">
                        <kbd className="px-1.5 py-0.5 bg-white/10 rounded font-mono">Space</kbd>
                        Preview
                    </span>
                    <span className="ml-auto text-text-tertiary">
                        {sortedData.length} stocks
                    </span>
                </div>
            </div>
        </div >
    )
}
