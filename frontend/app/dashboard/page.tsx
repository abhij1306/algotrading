'use client'

import { useEffect, useState } from 'react'
import { TrendingUp, TrendingDown, Search, RefreshCw, Activity } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { GlassCard } from '@/components/ui/GlassCard'

// Types
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
    us_fear_greek: { score: number; status: string }
    india_sentiment: { score: number; status: string; vix?: number }
  }
  condition: {
    status: string
    adx: number
  }
  timestamp: string
}

type FilterType = 'all' | 'indices' | 'commodities'

// Sentiment Gauge Component
function SentimentGauge({ title, score, status }: { title: string; score: number; status: string }) {
  const rotation = (score / 100) * 180 - 90
  const color = score >= 75 ? 'text-[var(--color-profit)]' : score <= 25 ? 'text-[var(--color-loss)]' : 'text-[var(--color-warning)]'
  
  return (
    <GlassCard className="flex flex-col items-center justify-center p-6">
      <h3 className="text-sm text-[var(--text-secondary)] mb-4 font-medium">{title}</h3>
      <div className="relative w-48 h-24 mb-2">
        <div className="absolute w-full h-full rounded-t-full border-[12px] border-[var(--border-subtle)]" />
        <div 
          className="absolute bottom-0 left-1/2 w-1 h-full origin-bottom transition-transform duration-1000"
          style={{ transform: `translateX(-50%) rotate(${rotation}deg)` }}
        >
          <div className={`w-full h-full ${color} rounded-t-full`} />
          <div className="absolute bottom-0 left-1/2 -translate-x-1/2 translate-y-1/2 w-3 h-3 bg-white rounded-full" />
        </div>
      </div>
      <div className={`text-3xl font-bold ${color}`}>{score}</div>
      <div className={`text-sm font-medium uppercase tracking-wide opacity-80 ${color}`}>{status}</div>
    </GlassCard>
  )
}

export default function DashboardPage() {
  const [data, setData] = useState<MarketData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [filter, setFilter] = useState<FilterType>('all')
  const [searchQuery, setSearchQuery] = useState('')

  const fetchData = async () => {
    setLoading(true)
    setError(null)
    try {
      const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'
      const res = await fetch(`${API_BASE}/api/market/overview`)
      if (res.ok) {
        const json = await res.json()
        setData(json)
      } else {
        setError('Failed to fetch market data')
        setData(null)
      }
    } catch (err) {
      console.error('Error fetching market data:', err)
      setError(err instanceof Error ? err.message : 'Network error occurred')
      setData(null)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchData()
    const interval = setInterval(fetchData, 30000)
    return () => clearInterval(interval)
  }, [])

  const getFilteredIndices = () => {
    if (!data?.indices) return []
    let filtered = data.indices
    if (filter === 'indices') {
      filtered = filtered.filter(i => ['Nifty 50', 'Bank Nifty', 'S&P 500', 'Nasdaq'].includes(i.name))
    } else if (filter === 'commodities') {
      filtered = filtered.filter(i => ['Gold (Global)', 'Silver (Global)'].includes(i.name))
    }
    if (searchQuery) {
      filtered = filtered.filter(i => i.name.toLowerCase().includes(searchQuery.toLowerCase()))
    }
    return filtered
  }

  const getRegionColor = (name: string) => {
    if (['Nifty 50', 'Bank Nifty'].includes(name)) return 'bg-[var(--color-warning)]'
    if (['S&P 500', 'Nasdaq'].includes(name)) return 'bg-[var(--color-primary)]'
    return 'bg-[var(--color-profit)]'
  }

  const filteredIndices = getFilteredIndices()

  return (
    <div className="h-full overflow-y-auto bg-[var(--color-base)] p-6">
      <div className="max-w-6xl mx-auto space-y-6">
        {/* Sentiment Gauges */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <SentimentGauge title="US Fear & Greed" score={data?.sentiment?.us_fear_greek?.score ?? 0} status={data?.sentiment?.us_fear_greek?.status ?? 'Loading'} />
          <SentimentGauge title="India Sentiment" score={data?.sentiment?.india_sentiment?.score ?? 0} status={data?.sentiment?.india_sentiment?.status ?? 'Loading'} />
          
          {/* Market Condition */}
          <GlassCard className="p-6">
            <div className="flex items-center gap-2 mb-4">
              <Activity className="w-4 h-4 text-[var(--text-tertiary)]" />
              <h3 className="text-sm text-[var(--text-secondary)] font-medium">Market Condition</h3>
            </div>
            <div className="text-xl font-bold text-[var(--color-secondary)] mb-3">
              {loading ? 'Analyzing...' : data?.condition?.status || 'Unavailable'}
            </div>
            <div className="mb-3">
              <div className="flex justify-between text-xs text-[var(--text-muted)] mb-1">
                <span>ADX (14)</span>
                <span className="font-mono">{data?.condition?.adx || 0}</span>
              </div>
              <div className="h-1.5 w-full bg-[var(--border-subtle)] rounded-full overflow-hidden">
                <div 
                  className={`h-full rounded-full transition-all ${(data?.condition?.adx || 0) > 25 ? 'bg-[var(--color-primary)]' : 'bg-[var(--color-secondary)]'}`}
                  style={{ width: `${Math.min(data?.condition?.adx || 0, 100)}%` }}
                />
              </div>
            </div>
          </GlassCard>
        </div>

        {/* Table */}
        <GlassCard className="overflow-hidden">
          <div className="px-6 py-4 border-b border-[var(--border-subtle)]">
            <div className="flex items-center justify-between">
              <h2 className="text-sm font-bold text-[var(--text-primary)] uppercase tracking-wider">Market Overview</h2>
              <Button variant="ghost" size="sm" onClick={fetchData} disabled={loading}>
                <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
                Refresh
              </Button>
            </div>
            <div className="flex items-center justify-between mt-4">
              <div className="flex gap-2">
                {(['all', 'indices', 'commodities'] as FilterType[]).map(key => (
                  <button
                    key={key}
                    onClick={() => setFilter(key)}
                    className={`px-3 py-1.5 text-xs rounded-lg transition ${
                      filter === key
                        ? 'bg-[var(--color-primary-bg)] text-[var(--color-primary)]'
                        : 'bg-[var(--glass-highlight)] text-[var(--text-secondary)] hover:text-[var(--text-primary)]'
                    }`}
                  >
                    {key === 'all' ? 'All' : key.charAt(0).toUpperCase() + key.slice(1)}
                  </button>
                ))}
              </div>
              <div className="relative">
                <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-[var(--text-muted)]" />
                <input
                  type="text"
                  placeholder="Search..."
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  className="pl-9 pr-3 py-1.5 bg-[var(--color-surface)] border border-[var(--border-default)] rounded-lg text-xs text-[var(--text-primary)] placeholder-[var(--text-muted)] focus:outline-none focus:border-[var(--color-primary)] w-48"
                />
              </div>
            </div>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="border-b border-[var(--border-subtle)]">
                  <th className="px-6 py-3 text-left text-xs font-medium text-[var(--text-tertiary)] uppercase">Instrument</th>
                  <th className="px-6 py-3 text-right text-xs font-medium text-[var(--text-tertiary)] uppercase">Price</th>
                  <th className="px-6 py-3 text-right text-xs font-medium text-[var(--text-tertiary)] uppercase">Change</th>
                  <th className="px-6 py-3 text-center text-xs font-medium text-[var(--text-tertiary)] uppercase">Trend</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[var(--border-subtle)]">
                {loading ? (
                  Array(6).fill(0).map((_, i) => (
                    <tr key={i} className="animate-pulse">
                      <td className="px-6 py-4"><div className="h-4 w-24 bg-[var(--glass-highlight)] rounded" /></td>
                      <td className="px-6 py-4"><div className="h-4 w-20 bg-[var(--glass-highlight)] rounded ml-auto" /></td>
                      <td className="px-6 py-4"><div className="h-4 w-16 bg-[var(--glass-highlight)] rounded ml-auto" /></td>
                      <td className="px-6 py-4"><div className="h-4 w-8 bg-[var(--glass-highlight)] rounded mx-auto" /></td>
                    </tr>
                  ))
                ) : filteredIndices.length === 0 ? (
                  <tr><td colSpan={4} className="px-6 py-12 text-center text-sm text-[var(--text-muted)]">No data found</td></tr>
                ) : (
                  filteredIndices.map(idx => {
                    const isPositive = idx.status === 'POSITIVE'
                    return (
                      <tr key={idx.symbol} className="hover:bg-[var(--glass-highlight)] transition">
                        <td className="px-6 py-4">
                          <div className="flex items-center gap-3">
                            <div className={`w-1 h-8 rounded ${getRegionColor(idx.name)}`} />
                            <span className="text-sm font-medium text-[var(--text-primary)]">{idx.name}</span>
                          </div>
                        </td>
                        <td className="px-6 py-4 text-right">
                          <span className="text-sm font-mono text-[var(--text-primary)]">
                            {idx.price.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                          </span>
                        </td>
                        <td className="px-6 py-4 text-right">
                          <span className={`text-sm font-mono ${isPositive ? 'text-[var(--color-profit)]' : 'text-[var(--color-loss)]'}`}>
                            {isPositive ? '+' : ''}{idx.change.toFixed(2)} ({isPositive ? '+' : ''}{idx.change_pct.toFixed(2)}%)
                          </span>
                        </td>
                        <td className="px-6 py-4 text-center">
                          {isPositive ? <TrendingUp className="w-4 h-4 text-[var(--color-profit)] mx-auto" /> : <TrendingDown className="w-4 h-4 text-[var(--color-loss)] mx-auto" />}
                        </td>
                      </tr>
                    )
                  })
                )}
              </tbody>
            </table>
          </div>
        </GlassCard>
      </div>
    </div>
  )
}
