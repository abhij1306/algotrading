'use client'

import { useState, useEffect } from 'react'
import { Search, Layers, RefreshCw, ChevronLeft, ChevronRight, AlertTriangle, Loader2, ChevronDown } from 'lucide-react'
import ScreenerTable from '@/components/ScreenerTable'
import ZeroStateScreener from '@/components/ZeroStateScreener'
import { Button } from '@/components/ui/button'
import { GlassCard } from '@/components/ui/GlassCard'

// Types
interface Stock {
  symbol: string
  close: number
  volume: number
  ema20: number
  ema50: number
  atr_pct: number
  rsi: number
  vol_percentile: number
  change_pct?: number
  intraday_score?: number
  swing_score?: number
  is_20d_breakout: boolean
  trend_7d?: number
  trend_30d?: number
  macd: number
  macd_signal: number
  adx: number
  stoch_k: number
  stoch_d: number
  bb_upper: number
  bb_middle: number
  bb_lower: number
  market_cap?: number
  pe_ratio?: number
  eps?: number
  roe?: number
  debt_to_equity?: number
  revenue?: number
}

export default function ScreenerPage() {
  const [stocks, setStocks] = useState<Stock[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Filters
  const [selectedIndex, setSelectedIndex] = useState('NIFTY50')
  const [selectedSymbol, setSelectedSymbol] = useState('')
  const [debouncedSymbol, setDebouncedSymbol] = useState('')
  const [selectedSector, setSelectedSector] = useState('all')
  const [scannerFilter, setScannerFilter] = useState('ALL')
  const [viewMode, setViewMode] = useState<'technical' | 'financial'>('technical')

  // Pagination
  const [page, setPage] = useState(1)
  const [limit, setLimit] = useState(10)
  const [totalRecords, setTotalRecords] = useState(0)
  const totalPages = Math.max(1, Math.ceil(totalRecords / limit))

  // Autocomplete
  const [searchResults, setSearchResults] = useState<{ symbol: string, name: string, sector: string }[]>([])
  const [showDropdown, setShowDropdown] = useState(false)
  const [isSearching, setIsSearching] = useState(false)

  // Debounce symbol
  useEffect(() => {
    const handler = setTimeout(() => setDebouncedSymbol(selectedSymbol), 500)
    return () => clearTimeout(handler)
  }, [selectedSymbol])

  // Autocomplete
  useEffect(() => {
    const fetchSearch = async () => {
      if (!selectedSymbol || selectedSymbol.length < 2) {
        setSearchResults([])
        setShowDropdown(false)
        return
      }
      setIsSearching(true)
      try {
        const res = await fetch(`http://localhost:8000/api/market/search?query=${selectedSymbol}`)
        if (res.ok) {
          const data = await res.json()
          if (Array.isArray(data)) {
            setSearchResults(data)
            setShowDropdown(true)
          }
        }
      } finally {
        setIsSearching(false)
      }
    }
    const timeout = setTimeout(fetchSearch, 300)
    return () => clearTimeout(timeout)
  }, [selectedSymbol])

  const availableIndices = [
    { id: 'NIFTY50', name: 'NIFTY 50' },
    { id: 'NIFTY100', name: 'NIFTY 100' },
    { id: 'NIFTY500', name: 'NIFTY 500' },
    { id: 'BANKNIFTY', name: 'BANK NIFTY' },
    { id: 'NIFTYMIDCAP', name: 'NIFTY MIDCAP' },
  ]

  const fetchData = async (silent = false) => {
    if (!silent) {
      setLoading(true)
      setError(null)
    }
    try {
      let url = `http://localhost:8000/api/screener/?page=${page}&limit=${limit}&index=${selectedIndex}`
      if (scannerFilter !== 'ALL') url += `&filter_type=${scannerFilter}`
      if (debouncedSymbol) url += `&symbol=${debouncedSymbol}`
      if (selectedSector !== 'all') url += `&sector=${encodeURIComponent(selectedSector)}`

      const res = await fetch(url)
      if (!res.ok) throw new Error(`API Error: ${res.status}`)
      
      const text = await res.text()
      const json = JSON.parse(text)
      
      if (json.data) {
        setStocks(json.data)
        if (json.meta) setTotalRecords(json.meta.total || 0)
      } else if (Array.isArray(json)) {
        setStocks(json)
      }
    } catch (e: any) {
      if (!silent) {
        setError(e.message || 'Failed to load data')
        setStocks([])
      }
    } finally {
      if (!silent) setLoading(false)
    }
  }

  useEffect(() => {
    fetchData()
    const interval = setInterval(() => fetchData(true), 5000)
    return () => clearInterval(interval)
  }, [page, selectedIndex, debouncedSymbol, selectedSector, scannerFilter, limit])

  return (
    <div className="h-full flex flex-col gap-4 p-6">
      {/* Control Bar */}
      <GlassCard className="flex items-center px-4 py-2 gap-3">
        {/* Search */}
        <div className="relative w-64">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-[var(--text-muted)]" />
          <input
            type="text"
            placeholder="Search symbol..."
            value={selectedSymbol}
            onChange={(e) => {
              setSelectedSymbol(e.target.value.toUpperCase())
              setShowDropdown(true)
            }}
            onFocus={() => setShowDropdown(true)}
            onBlur={() => setTimeout(() => setShowDropdown(false), 200)}
            className="w-full pl-10 pr-3 py-2 bg-[var(--color-surface)] border border-[var(--border-default)] rounded-lg text-sm text-[var(--text-primary)] placeholder-[var(--text-muted)] focus:outline-none focus:border-[var(--color-primary)]"
          />
          {showDropdown && searchResults.length > 0 && (
            <div className="absolute top-full left-0 w-full mt-1 bg-[var(--color-surface)] border border-[var(--border-default)] rounded-lg shadow-lg z-50 overflow-hidden">
              {searchResults.map(result => (
                <div
                  key={result.symbol}
                  onClick={() => {
                    setSelectedSymbol(result.symbol)
                    setShowDropdown(false)
                  }}
                  className="px-3 py-2 hover:bg-[var(--glass-highlight)] cursor-pointer"
                >
                  <div className="text-sm font-medium text-[var(--text-primary)]">{result.symbol}</div>
                  <div className="text-xs text-[var(--text-muted)]">{result.name}</div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Filters */}
        <div className="flex items-center gap-2 flex-1">
          <div className="relative">
            <Layers className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-[var(--color-primary)]" />
            <select
              value={selectedIndex}
              onChange={(e) => setSelectedIndex(e.target.value)}
              className="pl-8 pr-8 py-2 bg-[var(--color-surface)] border border-[var(--border-default)] rounded-lg text-sm text-[var(--text-primary)] focus:outline-none"
            >
              {availableIndices.map(idx => (
                <option key={idx.id} value={idx.id}>{idx.name}</option>
              ))}
            </select>
          </div>

          <select
            value={scannerFilter}
            onChange={(e) => setScannerFilter(e.target.value)}
            className="px-3 py-2 bg-[var(--color-surface)] border border-[var(--border-default)] rounded-lg text-sm text-[var(--text-primary)] focus:outline-none"
          >
            <option value="ALL">All Stocks</option>
            <option value="VOLUME_SHOCKER">Volume Shockers</option>
            <option value="PRICE_SHOCKER">Price Shockers</option>
            <option value="52W_HIGH">52W High</option>
          </select>

          <Button variant="ghost" size="sm" onClick={() => { setSelectedSymbol(''); setScannerFilter('ALL'); }}>
            <RefreshCw className="w-3.5 h-3.5" />
          </Button>
        </div>

        {/* View Toggle */}
        <div className="flex bg-[var(--color-surface)] rounded-lg p-0.5 border border-[var(--border-default)]">
          <button
            onClick={() => setViewMode('technical')}
            className={`px-3 py-1 text-xs font-medium rounded-md transition ${viewMode === 'technical' ? 'bg-[var(--color-primary)] text-white' : 'text-[var(--text-secondary)] hover:text-[var(--text-primary)]'}`}
          >
            Technical
          </button>
          <button
            onClick={() => setViewMode('financial')}
            className={`px-3 py-1 text-xs font-medium rounded-md transition ${viewMode === 'financial' ? 'bg-[var(--color-primary)] text-white' : 'text-[var(--text-secondary)] hover:text-[var(--text-primary)]'}`}
          >
            Financial
          </button>
        </div>
      </GlassCard>

      {/* Table Container */}
      <GlassCard className="flex-1 overflow-hidden flex flex-col min-h-0">
        <div className="flex-1 overflow-auto">
          {loading && stocks.length === 0 ? (
            <div className="h-full flex items-center justify-center">
              <Loader2 className="w-6 h-6 animate-spin text-[var(--color-primary)]" />
            </div>
          ) : error ? (
            <div className="h-full flex flex-col items-center justify-center text-[var(--color-loss)] gap-2">
              <AlertTriangle className="w-8 h-8" />
              <span className="text-sm">{error}</span>
              <Button variant="ghost" size="sm" onClick={() => fetchData()}>Retry</Button>
            </div>
          ) : stocks.length > 0 ? (
            <ScreenerTable data={stocks} viewMode={viewMode} />
          ) : (
            <ZeroStateScreener />
          )}
        </div>

        {/* Footer */}
        <div className="h-10 flex items-center justify-between px-4 border-t border-[var(--border-subtle)] bg-[var(--color-surface)]">
          <div className="flex items-center gap-3">
            <span className="text-xs text-[var(--text-tertiary)]">
              {(page - 1) * limit + 1}-{Math.min(page * limit, totalRecords)} of {totalRecords}
            </span>
            <div className="relative">
              <select
                value={limit}
                onChange={(e) => { setLimit(Number(e.target.value)); setPage(1); }}
                className="pl-2 pr-6 py-1 bg-[var(--color-base)] border border-[var(--border-default)] rounded text-xs text-[var(--text-secondary)] appearance-none cursor-pointer"
              >
                <option value="10">10</option>
                <option value="25">25</option>
                <option value="50">50</option>
                <option value="100">100</option>
              </select>
              <ChevronDown className="absolute right-1 top-1/2 -translate-y-1/2 w-3 h-3 text-[var(--text-muted)] pointer-events-none" />
            </div>
          </div>
          <div className="flex items-center gap-2">
            <Button variant="ghost" size="sm" onClick={() => setPage(p => Math.max(1, p - 1))} disabled={page === 1}>
              <ChevronLeft className="w-4 h-4" />
            </Button>
            <span className="text-xs text-[var(--text-secondary)] px-2">{page} / {totalPages}</span>
            <Button variant="ghost" size="sm" onClick={() => setPage(p => p + 1)} disabled={page >= totalPages}>
              <ChevronRight className="w-4 h-4" />
            </Button>
          </div>
        </div>
      </GlassCard>
    </div>
  )
}
