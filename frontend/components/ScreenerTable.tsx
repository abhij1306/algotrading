'use client'

import { useState } from 'react'

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
}

interface Props {
    data: Stock[]
    type: 'intraday' | 'swing' | 'combined'
}

export default function ScreenerTable({ data, type }: Props) {
    const [sortKey, setSortKey] = useState<keyof Stock>('symbol')
    const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc')

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

    const getScoreColor = (score: number) => {
        if (score >= 80) return 'text-green-400'
        if (score >= 60) return 'text-yellow-400'
        return 'text-gray-400'
    }

    if (data.length === 0) {
        return (
            <div className="bg-slate-800/50 rounded-lg p-12 text-center border border-purple-500/30">
                <p className="text-gray-400 text-lg">No stocks match the current filters</p>
            </div>
        )
    }

    return (
        <div className="bg-slate-800/50 rounded-lg overflow-hidden border border-purple-500/30">
            <div className="overflow-x-auto">
                <table className="w-full">
                    <thead className="bg-slate-900/50">
                        <tr>
                            {[
                                { key: 'symbol', label: 'Symbol' },
                                { key: 'close', label: 'Close' },
                                { key: 'ema20', label: 'EMA20' },
                                { key: 'ema50', label: 'EMA50' },
                                { key: 'atr_pct', label: 'ATR%' },
                                { key: 'rsi', label: 'RSI' },
                                { key: 'vol_percentile', label: 'Vol %ile' },
                                { key: type === 'intraday' ? 'intraday_score' : 'swing_score', label: 'Score' },
                            ].map(({ key, label }) => (
                                <th
                                    key={key}
                                    onClick={() => handleSort(key as keyof Stock)}
                                    className="px-4 py-3 text-left text-xs font-semibold text-purple-300 uppercase tracking-wider cursor-pointer hover:bg-slate-800/50 transition"
                                >
                                    <div className="flex items-center gap-2">
                                        {label}
                                        {sortKey === key && (
                                            <span className="text-purple-400">
                                                {sortDir === 'asc' ? '↑' : '↓'}
                                            </span>
                                        )}
                                    </div>
                                </th>
                            ))}
                        </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-700/50">
                        {sortedData.map((stock, idx) => {
                            const score = type === 'intraday' ? stock.intraday_score : stock.swing_score
                            return (
                                <tr
                                    key={stock.symbol}
                                    className="hover:bg-slate-700/30 transition"
                                >
                                    <td className="px-4 py-3">
                                        <div className="flex items-center gap-2">
                                            <span className="font-bold text-white">{stock.symbol}</span>
                                            {stock.is_20d_breakout && (
                                                <span className="text-xs bg-green-500/20 text-green-400 px-2 py-0.5 rounded">
                                                    BO
                                                </span>
                                            )}
                                        </div>
                                    </td>
                                    <td className="px-4 py-3 text-white font-mono">₹{stock.close.toFixed(2)}</td>
                                    <td className="px-4 py-3 text-gray-300 font-mono">{stock.ema20.toFixed(2)}</td>
                                    <td className="px-4 py-3 text-gray-300 font-mono">{stock.ema50.toFixed(2)}</td>
                                    <td className="px-4 py-3 text-gray-300 font-mono">{stock.atr_pct.toFixed(2)}%</td>
                                    <td className="px-4 py-3 text-gray-300 font-mono">{stock.rsi.toFixed(1)}</td>
                                    <td className="px-4 py-3 text-gray-300 font-mono">{Math.round(stock.vol_percentile)}%</td>
                                    <td className="px-4 py-3">
                                        <span className={`font-bold text-lg ${getScoreColor(score || 0)}`}>
                                            {(score || 0).toFixed(0)}
                                        </span>
                                    </td>
                                </tr>
                            )
                        })}
                    </tbody>
                </table>
            </div>
        </div>
    )
}
