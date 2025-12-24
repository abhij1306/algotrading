'use client'

import { useEffect, useState } from 'react'
import { TrendingUp, TrendingDown, Search, RefreshCw, Activity, Info } from 'lucide-react'

// --- Types ---

interface MarketData {
    indices: {
        name: string
        symbol: string
        price: number
        change: number
        change_pct: number
        status: 'POSITIVE' | 'NEGATIVE'
    }[]
    sentiment: {
        us_fear_greed: { score: number; status: string }
        india_sentiment: { score: number; status: string; vix?: number; source?: string }
    }
    condition: {
        status: string
        adx: number
    }
    timestamp: string
}

type FilterType = 'all' | 'indices' | 'commodities' | 'crypto'

// --- Components ---

const SentimentGauge = ({ title, score, status, subText }: any) => {
    // Simple original design
    let color = 'text-gray-400'
    let progressColor = 'bg-gray-500'

    if (score >= 75) { color = 'text-green-500'; progressColor = 'bg-green-500'; }
    else if (score >= 55) { color = 'text-green-400'; progressColor = 'bg-green-400'; }
    else if (score <= 25) { color = 'text-red-500'; progressColor = 'bg-red-500'; }
    else if (score <= 45) { color = 'text-orange-500'; progressColor = 'bg-orange-500'; }

    const rotation = (score / 100) * 180 - 90

    return (
        <div className="bg-[#0A0A0A] border border-white/5 rounded-xl p-6 flex flex-col items-center justify-center relative overflow-hidden">
            <h3 className="text-sm text-gray-400 mb-4 font-medium">{title}</h3>

            <div className="relative w-48 h-24 mb-2">
                {/* Simple arc */}
                <div className="absolute w-full h-full rounded-t-full border-[12px] border-white/5 border-b-0" />

                {/* Needle */}
                <div
                    className="absolute bottom-0 left-1/2 w-1 h-full origin-bottom transition-transform duration-1000 ease-out"
                    style={{ transform: `translateX(-50%) rotate(${rotation}deg)` }}
                >
                    <div className={`w-full h-full ${progressColor} rounded-t-full shadow-[0_0_10px_currentColor]`} />
                    <div className="absolute bottom-0 left-1/2 -translate-x-1/2 translate-y-1/2 w-4 h-4 bg-white rounded-full shadow-lg" />
                </div>
            </div>

            <div className={`text-3xl font-bold mt-2 ${color}`}>{score}</div>
            <div className={`text-sm font-medium uppercase tracking-wide opacity-80 ${color}`}>{status}</div>
            {subText && <div className="text-xs text-gray-600 mt-2">{subText}</div>}
        </div>
    )
}

export default function DashboardPage() {
    const [data, setData] = useState<MarketData | null>(null)
    const [loading, setLoading] = useState(true)
    const [error, setError] = useState('')
    const [filter, setFilter] = useState<FilterType>('all')
    const [searchQuery, setSearchQuery] = useState('')

    const fetchData = async () => {
        setLoading(true)
        try {
            const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'
            const res = await fetch(`${API_BASE}/api/market/overview`)
            if (!res.ok) throw new Error('Failed to fetch data')
            const json = await res.json()
            setData(json)
        } catch (err) {
            setError('Unable to load market data')
            console.error(err)
        } finally {
            setLoading(false)
        }
    }

    useEffect(() => {
        fetchData()

        // Simple 30-second auto-refresh (reliable, no WebSocket errors)
        const interval = setInterval(() => {
            fetchData()
        }, 30000)

        return () => clearInterval(interval)
    }, [])

    // Filter logic
    const getFilteredIndices = () => {
        if (!data) return []

        let filtered = data.indices

        if (filter === 'indices') {
            filtered = filtered.filter(i => ['Nifty 50', 'Bank Nifty', 'S&P 500', 'Nasdaq', 'Dow Jones'].includes(i.name))
        } else if (filter === 'commodities') {
            filtered = filtered.filter(i => ['Gold (Global)', 'Silver (Global)'].includes(i.name))
        } else if (filter === 'crypto') {
            filtered = []
        }

        if (searchQuery) {
            filtered = filtered.filter(i =>
                i.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
                i.symbol.toLowerCase().includes(searchQuery.toLowerCase())
            )
        }

        return filtered
    }

    const getCategory = (name: string) => {
        if (['Nifty 50', 'Bank Nifty', 'VIX (India)'].includes(name)) return { label: 'INDIA', color: 'border-orange-500' }
        if (['S&P 500', 'Nasdaq', 'Dow Jones', 'VIX (US)'].includes(name)) return { label: 'USA', color: 'border-blue-500' }
        if (['Gold (Global)', 'Silver (Global)'].includes(name)) return { label: 'COMMODITY', color: 'border-yellow-500' }
        if (name.includes('VIX')) return { label: 'VOLATILITY', color: 'border-purple-500' }
        return { label: 'OTHER', color: 'border-gray-500' }
    }

    const filteredIndices = getFilteredIndices()

    return (
        <div className="h-full overflow-y-auto bg-[#050505] p-8">
            <div className="max-w-7xl mx-auto space-y-6">

                {/* Sentiment Gauges */}
                <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                    <SentimentGauge
                        title="US Fear & Greed"
                        score={loading ? 0 : data?.sentiment.us_fear_greed.score}
                        status={loading ? 'Loading...' : data?.sentiment.us_fear_greed.status}
                        subText="Source: CNN Money"
                    />
                    <SentimentGauge
                        title="India Sentiment"
                        score={loading ? 0 : data?.sentiment.india_sentiment.score}
                        status={loading ? 'Loading...' : data?.sentiment.india_sentiment.status}
                        subText="Tickertape MMI"
                    />

                    {/* Market Condition */}
                    <div className="bg-[#0A0A0A] border border-white/5 rounded-xl p-6">
                        <div className="flex items-center gap-2 mb-4">
                            <Activity className="w-4 h-4 text-gray-500" />
                            <h3 className="text-sm text-gray-400 font-medium">Market Condition</h3>
                        </div>

                        <div className="text-2xl font-bold text-purple-400 mb-2">
                            {loading ? 'Analyzing...' : data?.condition.status || 'Unavailable'}
                        </div>

                        <div className="mb-4">
                            <div className="flex justify-between text-xs text-gray-500 mb-1">
                                <span>ADX STRENGTH (14)</span>
                                <span className="text-white font-mono">{loading ? 0 : data?.condition.adx}</span>
                            </div>
                            <div className="h-1.5 w-full bg-white/5 rounded-full overflow-hidden">
                                <div
                                    className={`h-full rounded-full transition-all duration-1000 ${(data?.condition.adx || 0) > 25 ? 'bg-cyan-500' : 'bg-purple-500'}`}
                                    style={{ width: `${Math.min(data?.condition.adx || 0, 100)}%` }}
                                />
                            </div>
                            <div className="flex justify-between mt-1 text-[9px] text-gray-600">
                                <span>Weak</span>
                                <span>Strong</span>
                            </div>
                        </div>

                        <div className="p-2 bg-white/5 rounded-lg border border-white/5 flex gap-2 items-start">
                            <Info className="w-3 h-3 text-gray-500 mt-0.5 flex-shrink-0" />
                            <p className="text-[10px] text-gray-500 leading-relaxed">
                                {(data?.condition.adx || 0) > 25 ? "Strong momentum" : "Non-directional"}
                            </p>
                        </div>
                    </div>
                </div>

                {/* Table */}
                <div className="bg-[#0A0A0A] border border-white/5 rounded-xl overflow-hidden">
                    <div className="px-6 py-4 border-b border-white/5">
                        <div className="flex items-center justify-between">
                            <div className="flex items-center gap-3">
                                <div className="w-1 h-1 rounded-full bg-cyan-400"></div>
                                <h2 className="text-sm font-bold text-white uppercase tracking-wider">Global Market Overview</h2>
                            </div>
                            <button onClick={fetchData} disabled={loading}
                                className="flex items-center gap-2 px-3 py-1.5 bg-white/5 hover:bg-white/10 border border-white/10 rounded-lg text-xs text-gray-400">
                                <RefreshCw className={`w-3 h-3 ${loading ? 'animate-spin' : ''}`} />
                                Refresh
                            </button>
                        </div>

                        <div className="flex items-center justify-between mt-4">
                            <div className="flex gap-2">
                                {[
                                    { key: 'all', label: 'All Assets' },
                                    { key: 'indices', label: 'Indices' },
                                    { key: 'commodities', label: 'Commodities' },
                                    { key: 'crypto', label: 'Crypto' }
                                ].map(({ key, label }) => (
                                    <button key={key} onClick={() => setFilter(key as FilterType)}
                                        className={`px-3 py-1.5 text-xs rounded-lg transition ${filter === key
                                            ? 'bg-cyan-500/20 text-cyan-400 border border-cyan-500/30'
                                            : 'bg-white/5 text-gray-500 hover:bg-white/10'
                                            }`}>
                                        {label}
                                    </button>
                                ))}
                            </div>

                            <div className="relative">
                                <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-gray-500" />
                                <input type="text" placeholder="Search..." value={searchQuery}
                                    onChange={(e) => setSearchQuery(e.target.value)}
                                    className="pl-9 pr-3 py-1.5 bg-white/5 border border-white/10 rounded-lg text-xs text-white placeholder-gray-600 focus:outline-none focus:border-cyan-500/50 w-48"
                                />
                            </div>
                        </div>
                    </div>

                    <div className="overflow-x-auto">
                        <table className="w-full">
                            <thead>
                                <tr className="border-b border-white/5">
                                    <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">Instrument</th>
                                    <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">Region</th>
                                    <th className="px-6 py-3 text-right text-xs font-medium text-gray-500 uppercase">Price</th>
                                    <th className="px-6 py-3 text-right text-xs font-medium text-gray-500 uppercase">Change</th>
                                    <th className="px-6 py-3 text-right text-xs font-medium text-gray-500 uppercase">% Change</th>
                                    <th className="px-6 py-3 text-center text-xs font-medium text-gray-500 uppercase">Trend</th>
                                </tr>
                            </thead>
                            <tbody className="divide-y divide-white/5">
                                {loading ? (
                                    Array(8).fill(0).map((_, i) => (
                                        <tr key={i} className="animate-pulse">
                                            <td className="px-6 py-4"><div className="h-4 w-24 bg-white/5 rounded"></div></td>
                                            <td className="px-6 py-4"><div className="h-4 w-16 bg-white/5 rounded"></div></td>
                                            <td className="px-6 py-4"><div className="h-4 w-20 bg-white/5 rounded ml-auto"></div></td>
                                            <td className="px-6 py-4"><div className="h-4 w-16 bg-white/5 rounded ml-auto"></div></td>
                                            <td className="px-6 py-4"><div className="h-4 w-12 bg-white/5 rounded ml-auto"></div></td>
                                            <td className="px-6 py-4"><div className="h-4 w-8 bg-white/5 rounded mx-auto"></div></td>
                                        </tr>
                                    ))
                                ) : filteredIndices.length === 0 ? (
                                    <tr><td colSpan={6} className="px-6 py-12 text-center text-sm text-gray-500">No instruments found</td></tr>
                                ) : (
                                    filteredIndices.map((idx) => {
                                        const category = getCategory(idx.name)
                                        const isPositive = idx.status === 'POSITIVE'

                                        return (
                                            <tr key={idx.symbol} className="hover:bg-white/5 transition">
                                                <td className="px-6 py-4">
                                                    <div className="flex items-center gap-3">
                                                        <div className={`w-1 h-8 rounded ${category.color}`}></div>
                                                        <span className="text-sm font-medium text-white">{idx.name}</span>
                                                    </div>
                                                </td>
                                                <td className="px-6 py-4">
                                                    <span className="text-xs px-2 py-1 rounded bg-white/5 text-gray-400 font-mono">
                                                        {category.label}
                                                    </span>
                                                </td>
                                                <td className="px-6 py-4 text-right">
                                                    <span className="text-sm font-mono text-white">
                                                        {idx.price.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                                                    </span>
                                                </td>
                                                <td className="px-6 py-4 text-right">
                                                    <span className={`text-sm font-mono ${isPositive ? 'text-green-400' : 'text-red-400'}`}>
                                                        {isPositive ? '+' : ''}{idx.change.toFixed(2)}
                                                    </span>
                                                </td>
                                                <td className="px-6 py-4 text-right">
                                                    <span className={`text-sm font-mono ${isPositive ? 'text-green-400' : 'text-red-400'}`}>
                                                        {isPositive ? '+' : ''}{idx.change_pct.toFixed(2)}%
                                                    </span>
                                                </td>
                                                <td className="px-6 py-4">
                                                    <div className="flex justify-center">
                                                        {isPositive ? (
                                                            <TrendingUp className="w-4 h-4 text-green-400" />
                                                        ) : (
                                                            <TrendingDown className="w-4 h-4 text-red-400" />
                                                        )}
                                                    </div>
                                                </td>
                                            </tr>
                                        )
                                    })
                                )}
                            </tbody>
                        </table>
                    </div>
                </div>

            </div>
        </div>
    )
}
