'use client'

import { useState, useEffect, useRef } from 'react'
import { Search, Layers, RefreshCw, ChevronLeft, ChevronRight, AlertTriangle, Loader2 } from 'lucide-react'
import ScreenerTable from '@/components/ScreenerTable'
import ZeroStateScreener from '@/components/ZeroStateScreener'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { GlassSelect } from '@/components/ui/GlassSelect'
import { apiClient } from '@/lib/api-client'

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

  // For debouncing
  const blurTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null)
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
      const result = await apiClient.get<any[]>(`/api/market/search?query=${encodeURIComponent(selectedSymbol)}`)
      if (result.data) {
        setSearchResults(result.data)
        setShowDropdown(true)
      }
      setIsSearching(false)
    }
    const timeout = setTimeout(fetchSearch, 300)
    return () => clearTimeout(timeout)
  }, [selectedSymbol])

  const availableIndices = [
    { value: 'NIFTY50', label: 'NIFTY 50' },
    { value: 'NIFTY100', label: 'NIFTY 100' },
    { value: 'NIFTY500', label: 'NIFTY 500' },
    { value: 'BANKNIFTY', label: 'BANK NIFTY' },
    { value: 'NIFTYMIDCAP', label: 'NIFTY MIDCAP' },
  ]

  const scannerFilters = [
    { value: 'ALL', label: 'All Stocks' },
    { value: 'VOLUME_SHOCKER', label: 'Volume Shockers' },
    { value: 'PRICE_SHOCKER', label: 'Price Shockers' },
    { value: '52W_HIGH', label: '52W High' },
  ]

  const fetchData = async (silent = false) => {
    if (!silent) {
      setLoading(true)
      setError(null)
    }
    try {
      let endpoint = `/api/screener/?page=${page}&limit=${limit}&index=${selectedIndex}`
      if (scannerFilter !== 'ALL') endpoint += `&filter_type=${scannerFilter}`
      if (debouncedSymbol) endpoint += `&symbol=${debouncedSymbol}`
      if (selectedSector !== 'all') endpoint += `&sector=${encodeURIComponent(selectedSector)}`

      const result = await apiClient.get<any>(endpoint)
      
      if (result.data) {
        if (result.data.data) {
          setStocks(result.data.data)
          if (result.data.meta) setTotalRecords(result.data.meta.total || 0)
        } else if (Array.isArray(result.data)) {
          setStocks(result.data)
        }
      } else if (result.error) {
        throw new Error(result.error.message)
      }
    } catch (e: unknown) {
      if (!silent) {
        setError(e instanceof Error ? e.message : 'Failed to load data')
        setStocks([])
      }
    } finally {
      if (!silent) setLoading(false)
    }
  }

  useEffect(() => {
    fetchData()
    const interval = setInterval(() => fetchData(true), 5000)
    return () => {
      clearInterval(interval)
      if (blurTimeoutRef.current) clearTimeout(blurTimeoutRef.current)
    }
  }, [page, selectedIndex, debouncedSymbol, selectedSector, scannerFilter, limit])

  return (
    <div className="h-full flex flex-col gap-4 p-6">
      {/* Control Bar */}
      <Card variant="glass" className="flex items-center px-4 py-2 gap-4">
        {/* Search */}
        <div className="relative w-64">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-[var(--text-muted)] z-10" />
          <Input
            placeholder="Search symbol..."
            value={selectedSymbol}
            onChange={(e) => {
              setSelectedSymbol(e.target.value.toUpperCase())
              setShowDropdown(true)
            }}
            onFocus={() => setShowDropdown(true)}
            onBlur={() => {
              blurTimeoutRef.current = setTimeout(() => setShowDropdown(false), 200)
            }}
            className="pl-10"
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
        <div className="flex items-center gap-3 flex-1">
          <GlassSelect
            options={availableIndices}
            value={selectedIndex}
            onChange={(val) => setSelectedIndex(val as string)}
            className="min-w-[140px]"
          />

          <GlassSelect
            options={scannerFilters}
            value={scannerFilter}
            onChange={(val) => setScannerFilter(val as string)}
            className="min-w-[150px]"
          />

          <Button variant="ghost" size="sm" onClick={() => { setSelectedSymbol(''); setScannerFilter('ALL'); }}>
            <RefreshCw className="w-3.5 h-3.5" />
          </Button>
        </div>

        {/* View Toggle */}
        <div className="flex bg-[var(--color-surface)] rounded-lg p-1 border border-[var(--border-default)]">
          <Button
            variant={viewMode === 'technical' ? 'primary' : 'ghost'}
            size="sm"
            onClick={() => setViewMode('technical')}
            className="h-7 px-3 text-xs"
          >
            Technical
          </Button>
          <Button
            variant={viewMode === 'financial' ? 'primary' : 'ghost'}
            size="sm"
            onClick={() => setViewMode('financial')}
            className="h-7 px-3 text-xs"
          >
            Financial
          </Button>
        </div>
      </Card>

      {/* Table Container */}
      <Card variant="glass" className="flex-1 overflow-hidden flex flex-col min-h-0 p-0">
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
        <div className="h-12 flex items-center justify-between px-6 border-t border-[var(--border-subtle)] bg-[var(--color-surface)]">
          <div className="flex items-center gap-4">
            <span className="text-xs text-[var(--text-tertiary)] font-medium">
              {totalRecords === 0 ? '0 of 0' : `${(page - 1) * limit + 1}-${Math.min(page * limit, totalRecords)} of ${totalRecords}`}
            </span>
            <GlassSelect
              options={[
                { value: '10', label: '10 / page' },
                { value: '25', label: '25 / page' },
                { value: '50', label: '50 / page' },
                { value: '100', label: '100 / page' },
              ]}
              value={limit.toString()}
              onChange={(val) => { setLimit(Number(val)); setPage(1); }}
              className="w-32"
            />
          </div>
          <div className="flex items-center gap-2">
            <Button variant="ghost" size="sm" onClick={() => setPage(p => Math.max(1, p - 1))} disabled={page === 1}>
              <ChevronLeft className="w-4 h-4" />
            </Button>
            <span className="text-xs font-mono font-medium text-[var(--text-secondary)] px-2">
              PAGE {page} <span className="text-[var(--text-muted)]">/</span> {totalPages}
            </span>
            <Button variant="ghost" size="sm" onClick={() => setPage(p => p + 1)} disabled={page >= totalPages}>
              <ChevronRight className="w-4 h-4" />
            </Button>
          </div>
        </div>
      </Card>
    </div>
  )
}
