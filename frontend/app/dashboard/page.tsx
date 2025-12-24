'use client'

import { useEffect, useState } from 'react'
import { ArrowUp, ArrowDown, TrendingUp, TrendingDown, Minus, RefreshCw, AlertTriangle, Info, Activity } from 'lucide-react'

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
        india_sentiment: { score: number; status: string; vix: number }
    }
    condition: {
        status: string
        adx: number
        trend_strength: string
        technical_summary: string
    }
    timestamp: string
}

// --- Components ---

const StatCard = ({ label, value, subValue, trend, loading }: any) => {
    if (loading) {
        return (
            <div className="bg-[#0A0A0A] border border-white/5 rounded-xl p-4 animate-pulse">
                <div className="h-4 w-24 bg-white/10 rounded mb-2" />
                <div className="h-8 w-32 bg-white/10 rounded mb-1" />
                <div className="h-4 w-16 bg-white/10 rounded" />
            </div>
        )
    }

    const isPositive = trend === 'POSITIVE'
    const colorClass = isPositive ? 'text-green-400' : 'text-red-400'
    const bgClass = isPositive ? 'bg-green-400/10' : 'bg-red-400/10'
    const Icon = isPositive ? ArrowUp : ArrowDown

    return (
        <div className="bg-[#0A0A0A] border border-white/5 rounded-xl p-4 hover:border-white/10 transition-all">
            <div className="flex justify-between items-start mb-2">
                <span className="text-xs text-gray-500 font-medium uppercase tracking-wider">{label}</span>
                <div className={`p-1 rounded-full ${bgClass}`}>
                    <Icon className={`w-3 h-3 ${colorClass}`} />
                </div>
            </div>
            <div className="text-2xl font-bold text-white mb-1">
                {typeof value === 'number' ? value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : value}
            </div>
            <div className={`text-xs font-mono flex items-center gap-1 ${colorClass}`}>
                <span>{subValue > 0 ? '+' : ''}{subValue.toFixed(2)}%</span>
            </div>
        </div>
    )
}

const SentimentGauge = ({ title, score, status, subText }: any) => {
    // Score 0-100.
    // Map 0 (Extreme Fear) -> Red, 100 (Extreme Greed) -> Green
    // Color stops: 0-25 Red, 25-45 Orange, 45-55 Gray, 55-75 Light Green, 75-100 Green

    let color = 'text-gray-400'
    let progressColor = 'bg-gray-500'

    if (score >= 75) { color = 'text-green-500'; progressColor = 'bg-green-500'; }
    else if (score >= 55) { color = 'text-green-400'; progressColor = 'bg-green-400'; }
    else if (score <= 25) { color = 'text-red-500'; progressColor = 'bg-red-500'; }
    else if (score <= 45) { color = 'text-orange-500'; progressColor = 'bg-orange-500'; }

    // Rotation for gauge needle (semi-circle)
    // 0 -> -90deg, 100 -> 90deg
    const rotation = (score / 100) * 180 - 90

    return (
        <div className="bg-[#0A0A0A] border border-white/5 rounded-xl p-6 flex flex-col items-center justify-center relative overflow-hidden">
            <h3 className="text-sm text-gray-400 mb-4 font-medium">{title}</h3>

            <div className="relative w-48 h-24 mb-2">
                {/* Background Arc */}
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

const TechnicalWidget = ({ condition, adx }: any) => {
    const isTrending = condition?.includes('Trend')
    const isRange = condition?.includes('Range') || condition?.includes('Sideways')

    return (
        <div className="bg-[#0A0A0A] border border-white/5 rounded-xl p-6 relative overflow-hidden">
             {/* Background glow */}
            <div className={`absolute top-0 right-0 w-32 h-32 blur-[60px] opacity-20 rounded-full
                ${isTrending ? 'bg-cyan-500' : 'bg-purple-500'}`} />

            <div className="flex items-center gap-2 mb-6">
                <Activity className="w-5 h-5 text-gray-400" />
                <h3 className="text-sm text-gray-300 font-medium">Market Condition</h3>
            </div>

            <div className="space-y-6">
                <div>
                    <div className="text-xs text-gray-500 mb-1">Primary State</div>
                    <div className={`text-2xl font-bold flex items-center gap-2
                        ${isTrending ? 'text-cyan-400' : 'text-purple-400'}`}>
                        {isTrending ? <TrendingUp className="w-6 h-6" /> : <Minus className="w-6 h-6" />}
                        {condition}
                    </div>
                </div>

                <div>
                    <div className="flex justify-between items-end mb-1">
                        <span className="text-xs text-gray-500">ADX Strength (14)</span>
                        <span className="text-sm font-mono text-white">{adx}</span>
                    </div>
                    {/* ADX Bar */}
                    <div className="h-2 w-full bg-white/5 rounded-full overflow-hidden">
                        <div
                            className={`h-full rounded-full transition-all duration-1000 ${adx > 25 ? 'bg-cyan-500' : 'bg-purple-500'}`}
                            style={{ width: `${Math.min(adx, 100)}%` }}
                        />
                    </div>
                    <div className="flex justify-between mt-1 text-[10px] text-gray-600">
                        <span>Weak (0)</span>
                        <span>Strong (25+)</span>
                        <span>Extreme (50+)</span>
                    </div>
                </div>

                <div className="p-3 bg-white/5 rounded-lg border border-white/5">
                    <div className="flex gap-2 items-start">
                        <Info className="w-4 h-4 text-gray-400 mt-0.5" />
                        <p className="text-xs text-gray-400 leading-relaxed">
                            {adx > 25
                                ? "Market is showing strong directional momentum. Trend-following strategies are favored."
                                : "Market is non-directional / choppy. Mean reversion strategies are favored."}
                        </p>
                    </div>
                </div>
            </div>
        </div>
    )
}

export default function DashboardPage() {
    const [data, setData] = useState<MarketData | null>(null)
    const [loading, setLoading] = useState(true)
    const [error, setError] = useState('')

    const fetchData = async () => {
        setLoading(true)
        try {
            // Use environment variable for API URL or default to localhost
            const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'
            const res = await fetch(`${API_BASE}/api/market/overview`)
            if (!res.ok) throw new Error('Failed to fetch data')
            const json = await res.json()
            setData(json)
        } catch (err) {
            setError('Unable to load market data. Backend may be offline.')
            console.error(err)
        } finally {
            setLoading(false)
        }
    }

    useEffect(() => {
        fetchData()
    }, [])

    if (error) {
        return (
            <div className="h-full flex items-center justify-center flex-col gap-4 text-gray-500">
                <AlertTriangle className="w-10 h-10 opacity-50" />
                <p>{error}</p>
                <button onClick={fetchData} className="px-4 py-2 bg-white/5 rounded hover:bg-white/10 text-sm text-white">
                    Retry
                </button>
            </div>
        )
    }

    // Categorize indices
    const usIndices = data?.indices.filter(i => ['S&P 500', 'Nasdaq', 'Dow Jones', 'VIX (US)'].includes(i.name)) || []
    const inIndices = data?.indices.filter(i => ['Nifty 50', 'Bank Nifty', 'VIX (India)'].includes(i.name)) || []
    const commodities = data?.indices.filter(i => ['Gold (Global)', 'Silver (Global)'].includes(i.name)) || []

    return (
        <div className="h-full overflow-y-auto bg-[#050505] p-8">
            <div className="max-w-7xl mx-auto space-y-8">

                {/* Header */}
                <div className="flex justify-between items-end border-b border-white/10 pb-6">
                    <div>
                        <h1 className="text-3xl font-bold text-white mb-2">Market Overview</h1>
                        <p className="text-gray-500 text-sm">Global snapshot and technical conditions</p>
                    </div>
                    <button
                        onClick={fetchData}
                        disabled={loading}
                        className="flex items-center gap-2 px-4 py-2 bg-white/5 hover:bg-white/10 border border-white/10 rounded-lg text-sm text-gray-300 transition-colors"
                    >
                        <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
                        Refresh
                    </button>
                </div>

                {/* Sentiment & Condition Section */}
                <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
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
                        subText={`Based on India VIX: ${data?.sentiment.india_sentiment.vix || '--'}`}
                    />
                    <TechnicalWidget
                        condition={loading ? 'Analyzing...' : data?.condition.status}
                        adx={loading ? 0 : data?.condition.adx}
                    />
                </div>

                {/* Indices Grid */}
                <div>
                    <h2 className="text-sm font-bold text-gray-500 uppercase tracking-widest mb-4">Global Markets</h2>
                    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
                        {loading
                            ? Array(4).fill(0).map((_, i) => <StatCard key={i} loading={true} />)
                            : usIndices.map(idx => (
                                <StatCard
                                    key={idx.symbol}
                                    label={idx.name}
                                    value={idx.price}
                                    subValue={idx.change_pct}
                                    trend={idx.status}
                                />
                            ))
                        }
                    </div>
                </div>

                <div>
                    <h2 className="text-sm font-bold text-gray-500 uppercase tracking-widest mb-4">Indian Markets</h2>
                    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
                        {loading
                             ? Array(4).fill(0).map((_, i) => <StatCard key={i} loading={true} />)
                             : inIndices.map(idx => (
                                <StatCard
                                    key={idx.symbol}
                                    label={idx.name}
                                    value={idx.price}
                                    subValue={idx.change_pct}
                                    trend={idx.status}
                                />
                            ))
                        }
                    </div>
                </div>

                 <div>
                    <h2 className="text-sm font-bold text-gray-500 uppercase tracking-widest mb-4">Commodities</h2>
                    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
                        {loading
                             ? Array(2).fill(0).map((_, i) => <StatCard key={i} loading={true} />)
                             : commodities.map(idx => (
                                <StatCard
                                    key={idx.symbol}
                                    label={idx.name}
                                    value={idx.price}
                                    subValue={idx.change_pct}
                                    trend={idx.status}
                                />
                            ))
                        }
                    </div>
                </div>

            </div>
        </div>
    )
}
