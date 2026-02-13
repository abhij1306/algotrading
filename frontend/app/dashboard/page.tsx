'use client'

import { useEffect, useState } from 'react'
import { TrendingUp, TrendingDown, Search, RefreshCw, Activity } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card, CardHeader, CardTitle, MetricCard } from '@/components/ui/card'
import {
  Table,
  TableHeader,
  TableBody,
  TableRow,
  TableHead,
  TableCell
} from '@/components/ui/table'
import { Input } from '@/components/ui/input'
import { SkeletonTable } from '@/components/ui/skeleton'
import { apiClient } from '@/lib/api-client'
import { Price, PriceChange } from '@/components/ui/price'

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
  const color = score >= 75 ? 'text-[var(--color-profit)]' : score <= 25 ? 'text-[var(--color-loss)]' : 'text-[var(--color-primary)]'
  const strokeColor = score >= 75 ? 'var(--color-profit)' : score <= 25 ? 'var(--color-loss)' : 'var(--color-primary)'
  
  return (
    <Card variant="glass" className="flex flex-col items-center justify-center p-6">
      <h3 className="text-sm text-[var(--text-secondary)] mb-4 font-medium uppercase tracking-wider">{title}</h3>
      <div className="relative w-48 h-24 mb-2">
        <div className="absolute w-full h-full rounded-t-full border-[12px] border-[var(--border-subtle)]" />
        <div 
          className="absolute bottom-0 left-1/2 w-1 h-full origin-bottom transition-transform duration-1000"
          style={{ transform: `translateX(-50%) rotate(${rotation}deg)` }}
        >
          <div className="w-full h-full rounded-t-full" style={{ backgroundColor: strokeColor }} />
          <div className="absolute bottom-0 left-1/2 -translate-x-1/2 translate-y-1/2 w-3 h-3 bg-white rounded-full" />
        </div>
      </div>
      <div className={`text-3xl font-bold ${color}`}>{score}</div>
      <div className={`text-sm font-medium uppercase tracking-wide opacity-80 ${color}`}>{status}</div>
    </Card>
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
    const result = await apiClient.get<MarketData>('/api/market/overview')
    if (result.data) {
      setData(result.data)
    } else {
      setError(result.error?.message || 'Failed to fetch market data')
      setData(null)
    }
    setLoading(false)
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
          <Card variant="glass" className="p-6">
            <div className="flex items-center gap-2 mb-4">
              <Activity className="w-4 h-4 text-[var(--text-tertiary)]" />
              <h3 className="text-sm text-[var(--text-secondary)] font-medium uppercase tracking-wider">Market Condition</h3>
            </div>
            <div className="text-xl font-bold text-[var(--text-primary)] mb-3">
              {loading ? 'Analyzing...' : data?.condition?.status || 'Unavailable'}
            </div>
            <div className="mb-3">
              <div className="flex justify-between text-xs text-[var(--text-muted)] mb-1">
                <span>ADX (14)</span>
                <span className="font-mono">{data?.condition?.adx || 0}</span>
              </div>
              <div className="h-1.5 w-full bg-[var(--border-subtle)] rounded-full overflow-hidden">
                <div 
                  className={`h-full rounded-full transition-all ${(data?.condition?.adx || 0) > 25 ? 'bg-[var(--color-primary)]' : 'bg-[var(--color-warning)]'}`}
                  style={{ width: `${Math.min(data?.condition?.adx || 0, 100)}%` }}
                />
              </div>
            </div>
          </Card>
        </div>

        {/* Table */}
        <Card variant="glass" className="overflow-hidden">
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
                  <Button
                    key={key}
                    variant={filter === key ? 'primary' : 'ghost'}
                    size="sm"
                    onClick={() => setFilter(key)}
                    className={filter === key ? '' : 'bg-[var(--glass-highlight)]'}
                  >
                    {key === 'all' ? 'All' : key.charAt(0).toUpperCase() + key.slice(1)}
                  </Button>
                ))}
              </div>
              <div className="relative w-64">
                <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-[var(--text-muted)] z-10" />
                <Input
                  placeholder="Search indices..."
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  className="pl-9"
                />
              </div>
            </div>
          </div>

          <Table>
            <TableHeader>
              <TableRow variant="ghost">
                <TableHead>Instrument</TableHead>
                <TableHead numeric>Price</TableHead>
                <TableHead numeric>Change</TableHead>
                <TableHead className="text-center">Trend</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {loading && filteredIndices.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={4} className="p-0">
                    <SkeletonTable rows={5} columns={4} />
                  </TableCell>
                </TableRow>
              ) : error ? (
                <TableRow>
                  <TableCell colSpan={4} className="px-6 py-12 text-center text-sm text-[var(--color-loss)]">
                    {error}
                  </TableCell>
                </TableRow>
              ) : filteredIndices.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={4} className="px-6 py-12 text-center text-sm text-[var(--text-muted)]">
                    No data found
                  </TableCell>
                </TableRow>
              ) : (
                filteredIndices.map(idx => (
                  <TableRow key={idx.symbol}>
                    <TableCell>
                      <div className="flex items-center gap-3">
                        <div className={`w-1 h-6 rounded ${getRegionColor(idx.name)}`} />
                        <span className="font-medium">{idx.name}</span>
                      </div>
                    </TableCell>
                    <TableCell numeric>
                      <Price value={idx.price} />
                    </TableCell>
                    <TableCell numeric>
                      <PriceChange change={idx.change} changePercent={idx.change_pct} />
                    </TableCell>
                    <TableCell>
                      <div className="flex justify-center">
                        {idx.status === 'POSITIVE' ? (
                          <TrendingUp className="w-4 h-4 text-[var(--color-profit)]" />
                        ) : (
                          <TrendingDown className="w-4 h-4 text-[var(--color-loss)]" />
                        )}
                      </div>
                    </TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>
        </Card>
      </div>
    </div>
  )
}
