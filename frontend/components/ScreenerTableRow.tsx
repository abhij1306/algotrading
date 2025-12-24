'use client'

import { memo } from 'react'
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

interface LiveData {
    ltp: number
    change: number
}

interface ScreenerTableRowProps {
    stock: Stock
    index: number
    isSelected: boolean
    viewMode: 'technical' | 'financial'
    type: 'intraday' | 'swing' | 'combined'
    liveData?: LiveData
    onSelect: (index: number) => void
}

function ScreenerTableRow({ stock, index, isSelected, viewMode, type, liveData, onSelect }: ScreenerTableRowProps) {
    const score = type === 'intraday' ? stock.intraday_score : stock.swing_score
    const displayPrice = liveData ? liveData.ltp : stock.close

    const getChangePercent = (close: number, ema20: number) => {
        return ((close - ema20) / ema20) * 100
    }

    let changePercent = 0
    if (liveData) {
        changePercent = liveData.change
    } else {
        changePercent = getChangePercent(stock.close, stock.ema20)
    }

    const getScoreColor = (score: number) => {
        if (score >= 80) return 'text-emerald-400 text-shadow-neon-emerald'
        if (score >= 60) return 'text-cyan-400'
        return 'text-gray-500'
    }

    return (
        <div
            className={`grid grid-cols-12 gap-2 px-6 py-3 border-b border-white/[0.03] items-center cursor-pointer transition-all duration-200
                ${isSelected ? 'bg-white/[0.03] border-l-2 border-l-cyan-400' : 'border-l-2 border-l-transparent hover:bg-white/[0.02]'}
            `}
            onClick={() => onSelect(index)}
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
                <div className={`font-mono text-sm ${liveData ? 'text-white font-bold' : 'text-gray-200'}`}>
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
                    {/* FINANCIAL VIEW - Adjusted Col Spans to sum to 12.
                        Sym (2) + Price (1) + Mkt Cap (2) + P/E (1) + ROE (1) + EPS (1) + Rev (3) + D/E (1) = 12
                        Previously: Rev (2), total 11.
                    */}
                    <div className="col-span-2 text-right font-mono text-xs text-gray-400">{stock.market_cap ? (stock.market_cap / 100).toFixed(0) : '-'}</div>
                    <div className="col-span-1 text-right font-mono text-xs text-gray-400">{stock.pe_ratio?.toFixed(1) || '-'}</div>
                    <div className={`col-span-1 text-right font-mono text-xs ${stock.roe && stock.roe > 15 ? 'text-emerald-400' : 'text-gray-400'}`}>{stock.roe?.toFixed(1)}%</div>
                    <div className="col-span-1 text-right font-mono text-xs text-gray-400">{stock.eps?.toFixed(1)}</div>
                    <div className="col-span-3 text-right font-mono text-xs text-gray-400">{stock.revenue ? (stock.revenue / 10000000).toFixed(0) : '-'}</div>
                    <div className={`col-span-1 text-right font-mono text-xs ${stock.debt_to_equity && stock.debt_to_equity > 1 ? 'text-red-400' : 'text-gray-400'}`}>{stock.debt_to_equity?.toFixed(2)}</div>
                </>
            )}
        </div>
    )
}

export default memo(ScreenerTableRow)
