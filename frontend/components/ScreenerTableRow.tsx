'use client'

import { memo } from 'react'
import { ArrowUpRight, ArrowDownRight, Minus } from "lucide-react"
import { Text, Data } from "@/components/design-system/atoms"

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
                <Text variant="small" weight="medium" className={isSelected ? 'text-cyan-400' : 'text-white'}>
                    {stock.symbol}
                </Text>
                {stock.is_20d_breakout && (
                    <span className="text-[9px] bg-emerald-500/10 text-emerald-400 px-1.5 py-0.5 rounded font-bold">BO</span>
                )}
            </div>

            {/* Price */}
            <div className="col-span-1 text-right">
                <Data
                    value={displayPrice.toFixed(2)}
                    prefix="₹"
                    size="sm"
                    className={liveData ? 'text-white font-bold' : 'text-gray-200'}
                />
                <div className={`text-[10px] font-mono ${changePercent > 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                    {changePercent > 0 ? '+' : ''}{changePercent.toFixed(1)}%
                </div>
            </div>

            {viewMode === 'technical' ? (
                <>
                    <div className="col-span-1 text-right"><Data value={stock.atr_pct?.toFixed(1) + '%'} size="sm" variant="muted" /></div>
                    <div className="col-span-1 text-right">
                        <Data
                            value={stock.rsi?.toFixed(0)}
                            size="sm"
                            className={stock.rsi > 70 ? 'text-red-400' : stock.rsi < 30 ? 'text-emerald-400' : 'text-gray-400'}
                        />
                    </div>
                    {/* MACD */}
                    <div className="col-span-1 text-right">
                        <Data
                            value={stock.macd?.toFixed(1)}
                            size="sm"
                            className={stock.macd > stock.macd_signal ? 'text-emerald-400' : 'text-red-400'}
                        />
                    </div>
                    {/* ADX */}
                    <div className="col-span-1 text-right"><Data value={stock.adx?.toFixed(0)} size="sm" variant="muted" /></div>
                    {/* Stoch */}
                    <div className="col-span-1 text-right"><Data value={stock.stoch_k?.toFixed(0)} size="sm" variant="muted" /></div>
                    {/* BBands */}
                    <div className="col-span-2 text-right">
                        <Data value={`${stock.bb_upper?.toFixed(0)}/${stock.bb_lower?.toFixed(0)}`} size="sm" variant="muted" className="text-[10px]" />
                    </div>

                    <div className="col-span-1 text-right">
                        <Data
                            value={(stock.trend_7d || 0).toFixed(1) + '%'}
                            size="sm"
                            className={(stock.trend_7d || 0) > 0 ? 'text-emerald-400' : 'text-red-400'}
                        />
                    </div>

                    <div className="col-span-1 text-right">
                        <Data
                            value={(score || 0).toFixed(0)}
                            size="md"
                            className={`font-bold ${getScoreColor(score || 0)}`}
                        />
                    </div>
                </>
            ) : (
                <>
                    <div className="col-span-2 text-right"><Data value={stock.market_cap ? (stock.market_cap / 100).toFixed(0) : '-'} size="sm" variant="muted" /></div>
                    <div className="col-span-1 text-right"><Data value={stock.pe_ratio?.toFixed(1) || '-'} size="sm" variant="muted" /></div>
                    <div className="col-span-1 text-right">
                        <Data
                            value={stock.roe?.toFixed(1) + '%' || '-'}
                            size="sm"
                            className={stock.roe && stock.roe > 15 ? 'text-emerald-400' : 'text-gray-400'}
                        />
                    </div>
                    <div className="col-span-1 text-right"><Data value={stock.eps?.toFixed(1) || '-'} size="sm" variant="muted" /></div>
                    <div className="col-span-3 text-right"><Data value={stock.revenue ? (stock.revenue / 10000000).toFixed(0) : '-'} size="sm" variant="muted" /></div>
                    <div className="col-span-1 text-right">
                        <Data
                            value={stock.debt_to_equity?.toFixed(2) || '-'}
                            size="sm"
                            className={stock.debt_to_equity && stock.debt_to_equity > 1 ? 'text-red-400' : 'text-gray-400'}
                        />
                    </div>
                </>
            )}
        </div>
    )
}

export default memo(ScreenerTableRow)
