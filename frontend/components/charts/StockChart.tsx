'use client'

import React, { useEffect, useState } from 'react'
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from 'recharts'

interface StockChartProps {
    symbol: string
}

interface PriceData {
    date: string
    close: number
    open: number
    high: number
    low: number
    volume: number
}

export default function StockChart({ symbol }: StockChartProps) {
    const [data, setData] = useState<PriceData[]>([])
    const [loading, setLoading] = useState(true)
    const [error, setError] = useState<string | null>(null)

    useEffect(() => {
        if (!symbol) return

        setLoading(true)
        setError(null)

        // Fetch last 90 days of data
        const endDate = new Date()
        const startDate = new Date()
        startDate.setDate(startDate.getDate() - 90)

        fetch(`/api/market/historical?symbol=${symbol}&start_date=${startDate.toISOString().split('T')[0]}&end_date=${endDate.toISOString().split('T')[0]}`)
            .then(res => {
                if (res.status === 404 || res.status === 204) {
                    throw new Error('No historical data available for this symbol')
                }
                return res.json()
            })
            .then(priceData => {
                if (!priceData || priceData.length === 0) {
                    throw new Error('No data available')
                }
                setData(priceData)
                setLoading(false)
            })
            .catch(err => {
                console.error('[StockChart] Error fetching data:', err)
                setError(err.message)
                setLoading(false)
            })
    }, [symbol])

    if (loading) {
        return (
            <div className="h-full w-full flex items-center justify-center bg-[#050505]">
                <div className="text-gray-400">Loading chart data...</div>
            </div>
        )
    }

    if (error || data.length === 0) {
        return (
            <div className="h-full w-full flex flex-col items-center justify-center bg-[#050505] p-8">
                <div className="text-gray-400 mb-2">📊 No Chart Data Available</div>
                <div className="text-xs text-gray-600">{error || 'Historical data not found for ' + symbol}</div>
                <div className="text-xs text-gray-600 mt-2">Check if symbol exists in database</div>
            </div>
        )
    }

    const minPrice = Math.min(...data.map(d => d.low))
    const maxPrice = Math.max(...data.map(d => d.high))
    const currentPrice = data[data.length - 1]?.close || 0
    const firstPrice = data[0]?.close || 0
    const priceChange = currentPrice - firstPrice
    const priceChangePercent = ((priceChange / firstPrice) * 100).toFixed(2)
    const isPositive = priceChange >= 0

    return (
        <div className="h-full w-full bg-[#050505] flex flex-col p-4">
            {/* Header */}
            <div className="mb-4">
                <div className="flex items-baseline gap-3">
                    <div className="text-2xl font-bold text-white">{symbol}</div>
                    <div className="text-xl font-semibold text-white">₹{currentPrice.toFixed(2)}</div>
                    <div className={`text-sm ${isPositive ? 'text-green-500' : 'text-red-500'}`}>
                        {isPositive ? '+' : ''}{priceChange.toFixed(2)} ({isPositive ? '+' : ''}{priceChangePercent}%)
                    </div>
                </div>
                <div className="text-xs text-gray-500 mt-1">Last 90 days • Daily</div>
            </div>

            {/* Chart */}
            <div className="flex-1">
                <ResponsiveContainer width="100%" height="100%">
                    <LineChart data={data} margin={{ top: 5, right: 20, bottom: 5, left: 0 }}>
                        <CartesianGrid strokeDasharray="3 3" stroke="#1a1a1a" />
                        <XAxis
                            dataKey="date"
                            stroke="#666"
                            tick={{ fill: '#888', fontSize: 12 }}
                            tickFormatter={(date) => {
                                const d = new Date(date)
                                return `${d.getDate()}/${d.getMonth() + 1}`
                            }}
                        />
                        <YAxis
                            stroke="#666"
                            tick={{ fill: '#888', fontSize: 12 }}
                            domain={[minPrice * 0.98, maxPrice * 1.02]}
                            tickFormatter={(value) => `₹${value.toFixed(0)}`}
                        />
                        <Tooltip
                            contentStyle={{
                                backgroundColor: '#1a1a1a',
                                border: '1px solid #333',
                                borderRadius: '8px',
                                padding: '12px'
                            }}
                            labelStyle={{ color: '#fff', marginBottom: '8px' }}
                            itemStyle={{ color: '#0ea5e9' }}
                            formatter={(value: number) => [`₹${value.toFixed(2)}`, 'Close']}
                            labelFormatter={(label) => {
                                const d = new Date(label)
                                return d.toLocaleDateString('en-IN', { year: 'numeric', month: 'short', day: 'numeric' })
                            }}
                        />
                        <Line
                            type="monotone"
                            dataKey="close"
                            stroke={isPositive ? '#10b981' : '#ef4444'}
                            strokeWidth={2}
                            dot={false}
                            animationDuration={300}
                        />
                    </LineChart>
                </ResponsiveContainer>
            </div>

            {/* Footer Stats */}
            <div className="mt-4 grid grid-cols-4 gap-4 text-xs">
                <div>
                    <div className="text-gray-500">High</div>
                    <div className="text-white font-semibold">₹{maxPrice.toFixed(2)}</div>
                </div>
                <div>
                    <div className="text-gray-500">Low</div>
                    <div className="text-white font-semibold">₹{minPrice.toFixed(2)}</div>
                </div>
                <div>
                    <div className="text-gray-500">Volume</div>
                    <div className="text-white font-semibold">{(data[data.length - 1]?.volume / 1000000).toFixed(2)}M</div>
                </div>
                <div>
                    <div className="text-gray-500">Data Points</div>
                    <div className="text-white font-semibold">{data.length}</div>
                </div>
            </div>
        </div>
    )
}
