'use client'

import { useEffect, useState } from 'react'
import UnifiedPortfolioAnalyzer from '../components/UnifiedPortfolioAnalyzer';
import StrategyConfiguration from '../components/strategies/StrategyConfiguration';
import PerformanceMetrics from '../components/strategies/PerformanceMetrics';
import EquityCurve from '../components/strategies/EquityCurve';
import TradesTable from '../components/strategies/TradesTable';
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts';
import Navbar from '../components/Navbar';

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
  // Removed score, is_20d_breakout might strictly not be needed if not in API, but let's keep it safe
}

interface FinancialRecord {
  symbol: string
  market_cap: number
  revenue: number
  net_income: number
  eps: number
  roe: number
  debt_to_equity: number
  pe_ratio: number
}

type MainTab = 'screener' | 'portfolio' | 'strategies'
type ScreenerTab = 'technicals' | 'financials'

export default function MacOSPage() {
  // State
  const [mainTab, setMainTab] = useState<MainTab>('screener')
  const [screenerTab, setScreenerTab] = useState<ScreenerTab>('technicals')

  const [isDark, setIsDark] = useState(true)
  const [currentTime, setCurrentTime] = useState('')

  // Data State
  const [stocks, setStocks] = useState<Stock[]>([])
  const [financials, setFinancials] = useState<FinancialRecord[]>([])
  const [loading, setLoading] = useState(false)

  // Sector Filter State
  const [selectedSector, setSelectedSector] = useState('all')
  const [availableSectors, setAvailableSectors] = useState<string[]>([])

  // Pagination State
  const [page, setPage] = useState(1)
  const [limit] = useState(100)
  const [totalRecords, setTotalRecords] = useState(0)
  const [totalPages, setTotalPages] = useState(0)

  // Sorting & Upload State
  const [sortBy, setSortBy] = useState('symbol')
  const [sortOrder, setSortOrder] = useState<'asc' | 'desc'>('asc')
  const [uploading, setUploading] = useState(false)
  const [searchQuery, setSearchQuery] = useState('')
  const [searchResults, setSearchResults] = useState<{ symbol: string, name: string }[]>([])
  const [showAutocomplete, setShowAutocomplete] = useState(false)
  const [selectedSymbol, setSelectedSymbol] = useState('')

  // Theme effect
  useEffect(() => {
    if (typeof window !== 'undefined') {
      const isDarkQuery = window.matchMedia('(prefers-color-scheme: dark)')
      setIsDark(isDarkQuery.matches)
      document.body.classList.toggle('dark', isDarkQuery.matches)
      const listener = (e: MediaQueryListEvent) => {
        setIsDark(e.matches)
        document.body.classList.toggle('dark', e.matches)
      }
      isDarkQuery.addEventListener('change', listener)
      return () => isDarkQuery.removeEventListener('change', listener)
    }
  }, [])

  const toggleTheme = () => {
    const newMode = !isDark
    setIsDark(newMode)
    document.body.classList.toggle('dark', newMode)
  }

  // Clock effect
  useEffect(() => {
    const timer = setInterval(() => {
      setCurrentTime(new Date().toLocaleTimeString('en-US', {
        hour: '2-digit',
        minute: '2-digit',
        hour12: false
      }))
    }, 1000)
    return () => clearInterval(timer)
  }, [])

  // Fetch sectors for filter
  useEffect(() => {
    fetch('http://localhost:8000/api/sectors/list')
      .then(res => res.json())
      .then(data => setAvailableSectors(data.sectors || []))
      .catch(err => console.error('Failed to fetch sectors:', err))
  }, [])

  // Symbol Search
  const searchSymbols = async (query: string) => {
    if (query.length < 1) {
      setSearchResults([])
      setShowAutocomplete(false)
      return
    }

    try {
      const res = await fetch(`http://localhost:8000/api/symbols/search?q=${encodeURIComponent(query)}`)
      if (res.ok) {
        const data = await res.json()
        setSearchResults(data.symbols || [])
        setShowAutocomplete(data.symbols.length > 0)
      }
    } catch (e) {
      console.error('Symbol search failed:', e)
    }
  }

  const handleSearchChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const value = e.target.value.toUpperCase()
    setSearchQuery(value)
    searchSymbols(value)
  }

  const selectSymbol = (symbol: string) => {
    setSelectedSymbol(symbol)
    setSearchQuery(symbol)
    setShowAutocomplete(false)
    setPage(1)
  }

  const clearSearch = () => {
    setSearchQuery('')
    setSelectedSymbol('')
    setSearchResults([])
    setShowAutocomplete(false)
    setPage(1)
  }

  // Data fetching
  const fetchData = async () => {
    setLoading(true)
    try {
      if (mainTab === 'screener') {
        const endpoint = screenerTab === 'technicals' ? '/api/screener' : '/api/screener/financials'
        let url = `http://localhost:8000${endpoint}?page=${page}&limit=${limit}&sort_by=${sortBy}&sort_order=${sortOrder}`

        // Add symbol filter if selected
        if (selectedSymbol) {
          url += `&symbol=${selectedSymbol}`
        }

        // Add sector filter if selected
        if (selectedSector && selectedSector !== 'all') {
          url += `&sector=${encodeURIComponent(selectedSector)}`
        }

        const res = await fetch(url)
        const json = await res.json()

        if (json.data) {
          if (screenerTab === 'technicals') {
            setStocks(json.data)
          } else {
            setFinancials(json.data)
          }
          // Backend returns 'meta' not 'metadata'
          if (json.meta) {
            setTotalRecords(json.meta.total || 0)
            setTotalPages(json.meta.total_pages || 0)
          }
        }
      }
    } catch (e) {
      console.error(e)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchData()
  }, [mainTab, screenerTab, page, sortBy, sortOrder, selectedSymbol, selectedSector])

  // Pagination Handler
  const handlePageChange = (newPage: number) => {
    if (newPage >= 1 && newPage <= totalPages) {
      setPage(newPage)
    }
  }

  // Sorting Handler
  const handleSort = (field: string) => {
    if (sortBy === field) {
      setSortOrder(sortOrder === 'asc' ? 'desc' : 'asc')
    } else {
      setSortBy(field)
      setSortOrder('desc')
    }
    setPage(1)
  }

  // File Upload Handler
  const handleFileUpload = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const files = event.target.files
    if (!files || files.length === 0) return

    setUploading(true)
    const formData = new FormData()

    // Append all selected files with the key 'files' (must match backend argument name)
    Array.from(files).forEach((file) => {
      formData.append('files', file)
      console.log(`Adding file: ${file.name}, size: ${file.size}`)
    })

    try {
      console.log('Uploading to:', 'http://localhost:8000/api/upload/bulk-financials')
      const res = await fetch('http://localhost:8000/api/upload/bulk-financials', {
        method: 'POST',
        body: formData
      })

      console.log('Response status:', res.status)

      if (res.ok) {
        const data = await res.json()
        console.log('Upload response:', data)

        // Show detailed message
        let message = data.message
        if (data.summary) {
          if (data.summary.symbols_updated && data.summary.symbols_updated.length > 0) {
            message += `\n\nUpdated: ${data.summary.symbols_updated.join(', ')}`
          }
          if (data.summary.symbols_not_found && data.summary.symbols_not_found.length > 0) {
            message += `\n\nNot in database: ${data.summary.symbols_not_found.join(', ')}`
          }
        }

        alert(message)

        if (data.errors && data.errors.length > 0) {
          console.warn('Upload warnings:', data.errors)
        }

        fetchData() // Refresh data
      } else {
        const errorText = await res.text()
        console.error('Upload failed:', errorText)
        try {
          const err = JSON.parse(errorText)
          alert('Upload failed: ' + (err.detail || 'Unknown error'))
        } catch {
          alert('Upload failed: ' + errorText)
        }
      }
    } catch (e) {
      console.error('Upload exception:', e)
      alert('Upload failed: ' + (e instanceof Error ? e.message : 'Server error'))
    } finally {
      setUploading(false)
      event.target.value = ''
    }
  }

  // Helper for Header Sort Icon
  const SortIcon = ({ field }: { field: string }) => {
    if (sortBy !== field) return <span className="ml-1 opacity-20">⇅</span>
    return <span className="ml-1 opacity-100">{sortOrder === 'asc' ? '↑' : '↓'}</span>
  }

  const Th = ({ field, label, align = 'right' }: { field: string, label: string, align?: string }) => (
    <th
      className={`px-6 py-4 text-xs font-semibold opacity-60 tracking-wider cursor-pointer hover:bg-black/5 dark:hover:bg-white/5 transition-colors select-none text-${align}`}
      onClick={() => handleSort(field)}
    >
      <div className={`flex items-center ${align === 'right' ? 'justify-end' : 'justify-start'}`}>
        {label}
        <SortIcon field={field} />
      </div>
    </th>
  )

  // Render Technicals Table
  const renderTechnicals = () => (
    <div className="overflow-x-auto">
      <table className="w-full">
        <thead className="sticky top-0 z-20 bg-white/50 dark:bg-black/50 backdrop-blur-md border-b border-gray-200/20 dark:border-gray-700/30">
          <tr>
            <Th field="symbol" label="SYMBOL" align="left" />
            <Th field="close" label="LTP" />
            <Th field="volume" label="VOLUME" />
            <Th field="ema20" label="EMA 20" />
            <Th field="ema50" label="EMA 50" />
            <Th field="atr_pct" label="ATR%" />
            <Th field="rsi" label="RSI" />
            <Th field="vol_percentile" label="VOL %" />
          </tr>
        </thead>
        <tbody className="divide-y divide-border-dark">
          {loading ? (
            Array.from({ length: 10 }).map((_, i) => (
              <tr key={i} className="animate-pulse">
                <td className="px-6 py-4"><div className="h-4 w-24 bg-gray-200 dark:bg-gray-800 rounded"></div></td>
                <td className="px-6 py-4."><div className="h-4 w-16 bg-gray-200 dark:bg-gray-800 rounded ml-auto"></div></td>
                <td className="px-6 py-4"><div className="h-4 w-16 bg-gray-200 dark:bg-gray-800 rounded ml-auto"></div></td>
                <td className="px-6 py-4"><div className="h-4 w-16 bg-gray-200 dark:bg-gray-800 rounded ml-auto"></div></td>
                <td className="px-6 py-4"><div className="h-4 w-16 bg-gray-200 dark:bg-gray-800 rounded ml-auto"></div></td>
                <td className="px-6 py-4"><div className="h-4 w-12 bg-gray-200 dark:bg-gray-800 rounded ml-auto"></div></td>
                <td className="px-6 py-4"><div className="h-4 w-12 bg-gray-200 dark:bg-gray-800 rounded ml-auto"></div></td>
                <td className="px-6 py-4"><div className="h-4 w-12 bg-gray-200 dark:bg-gray-800 rounded ml-auto"></div></td>
              </tr>
            ))
          ) : (
            stocks.map((stock) => (
              <tr key={stock.symbol} className="group transition-colors hover:bg-primary/5 border-b border-border-dark last:border-0 text-gray-300">
                <td className="px-6 py-4 font-semibold text-[15px]">{stock.symbol}</td>
                <td className="px-6 py-4 text-right font-sans tabular-nums tracking-tight text-sm opacity-90">
                  {stock.close.toLocaleString('en-IN', { style: 'currency', currency: 'INR' })}
                </td>
                <td className="px-6 py-4 text-right font-sans tabular-nums tracking-tight text-sm opacity-80">
                  {stock.volume ? (stock.volume / 100000).toFixed(2) + 'L' : '-'}
                </td>
                <td className="px-6 py-4 text-right text-sm opacity-60 font-sans tabular-nums tracking-tight">{stock.ema20.toFixed(2)}</td>
                <td className="px-6 py-4 text-right text-sm opacity-60 font-sans tabular-nums tracking-tight">{stock.ema50.toFixed(2)}</td>
                <td className="px-6 py-4 text-right text-sm font-sans tabular-nums tracking-tight">
                  <span className={`${stock.atr_pct > 2.5 ? 'text-red-500' : 'opacity-80'}`}>
                    {stock.atr_pct.toFixed(2)}%
                  </span>
                </td>
                <td className="px-6 py-4 text-right text-sm font-sans tabular-nums tracking-tight">
                  <span className={`${stock.rsi > 70 ? 'text-purple-500 font-bold' : stock.rsi < 30 ? 'text-blue-500 font-bold' : 'opacity-80'}`}>
                    {stock.rsi.toFixed(1)}
                  </span>
                </td>
                <td className="px-6 py-4 text-right text-sm opacity-80 font-sans tabular-nums tracking-tight">{Math.round(stock.vol_percentile)}%</td>
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  )

  // Render Financials Table
  const renderFinancials = () => (
    <div className="overflow-x-auto">
      <table className="w-full">
        <thead className="sticky top-0 z-20 bg-background-dark/95 backdrop-blur-md border-b border-border-dark">
          <tr>
            <Th field="symbol" label="SYMBOL" align="left" />
            <Th field="market_cap" label="MARKET CAP (Cr)" align="right" />
            <Th field="revenue" label="REVENUE (Cr)" align="right" />
            <Th field="net_income" label="NET INCOME (Cr)" align="right" />
            <Th field="eps" label="EPS" align="right" />
            <Th field="roe" label="ROE %" align="right" />
            <Th field="ev_ebitda" label="EV/EBITDA" align="right" />
            <Th field="pe_ratio" label="P/E" align="right" />
          </tr>
        </thead>
        <tbody className="divide-y divide-border-dark">
          {loading ? (
            Array.from({ length: 10 }).map((_, i) => (
              <tr key={i} className="animate-pulse">
                <td className="px-6 py-4"><div className="h-4 w-24 bg-gray-200 dark:bg-gray-800 rounded"></div></td>
                <td className="px-6 py-4"><div className="h-4 w-16 bg-gray-200 dark:bg-gray-800 rounded ml-auto"></div></td>
                <td className="px-6 py-4"><div className="h-4 w-16 bg-gray-200 dark:bg-gray-800 rounded ml-auto"></div></td>
                <td className="px-6 py-4"><div className="h-4 w-12 bg-gray-200 dark:bg-gray-800 rounded ml-auto"></div></td>
                <td className="px-6 py-4"><div className="h-4 w-12 bg-gray-200 dark:bg-gray-800 rounded ml-auto"></div></td>
                <td className="px-6 py-4"><div className="h-4 w-12 bg-gray-200 dark:bg-gray-800 rounded ml-auto"></div></td>
                <td className="px-6 py-4"><div className="h-4 w-12 bg-gray-200 dark:bg-gray-800 rounded ml-auto"></div></td>
                <td className="px-6 py-4"><div className="h-4 w-16 bg-gray-200 dark:bg-gray-800 rounded ml-auto"></div></td>
              </tr>
            ))
          ) : (
            financials.map((stock) => (
              <tr key={stock.symbol} className="group transition-colors hover:bg-primary/5 border-b border-border-dark last:border-0 text-gray-300">
                <td className="px-6 py-4 font-semibold text-[15px]">{stock.symbol}</td>
                <td className="px-6 py-4 text-right font-sans tabular-nums tracking-tight text-sm opacity-90">
                  {stock.market_cap ? (stock.market_cap / 1000).toFixed(2) : '-'}
                </td>
                <td className="px-6 py-4 text-right font-sans tabular-nums tracking-tight text-sm opacity-90">
                  {stock.revenue ? (stock.revenue).toFixed(2) : '-'}
                </td>
                <td className="px-6 py-4 text-right font-sans tabular-nums tracking-tight text-sm opacity-90">
                  {stock.net_income ? (stock.net_income).toFixed(2) : '-'}
                </td>
                <td className="px-6 py-4 text-right font-sans tabular-nums tracking-tight text-sm opacity-90">
                  {stock.eps ? stock.eps.toFixed(2) : '-'}
                </td>
                <td className="px-6 py-4 text-right font-sans tabular-nums tracking-tight text-sm opacity-90">
                  {stock.roe ? stock.roe.toFixed(2) : '-'}
                </td>
                <td className="px-6 py-4 text-right font-sans tabular-nums tracking-tight text-sm opacity-90">
                  {(() => {
                    // EV/EBITDA = (Market Cap + Debt) / EBITDA
                    // Using Net Income as EBITDA proxy
                    const ev = (stock.market_cap || 0) + ((stock.debt_to_equity || 0) * (stock.market_cap || 0) / 100);
                    const ebitda = stock.net_income || 0;
                    const evEbitda = ebitda > 0 ? ev / ebitda : 0;
                    return evEbitda > 0 ? evEbitda.toFixed(2) : '-';
                  })()}
                </td>
                <td className="px-6 py-4 text-right font-sans tabular-nums tracking-tight text-sm opacity-90">
                  {stock.pe_ratio ? stock.pe_ratio.toFixed(2) : '-'}
                </td>
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  )

  return (
    <div className="min-h-screen bg-background-dark text-white font-sans transition-colors duration-300">
      <Navbar activeTab={mainTab} onTabChange={setMainTab} />

      <div className="max-w-[1920px] mx-auto px-6 py-6">




        {/* Content */}
        {
          mainTab === 'screener' ? (
            <div className="grid grid-cols-12 gap-6 animate-in fade-in slide-in-from-bottom-4">
              {/* Sidebar Filters */}
              <div className="col-span-3 bg-card-dark rounded-xl border border-border-dark p-4 h-[calc(100vh-140px)] sticky top-24 overflow-y-auto">
                <div className="flex items-center justify-between mb-4">
                  <h3 className="text-sm font-bold opacity-60">FILTERS</h3>
                  <button
                    onClick={() => { setSelectedSector('all'); setSelectedSymbol(''); setSearchQuery(''); }}
                    className="text-xs text-primary hover:text-white transition-colors"
                  >
                    Reset
                  </button>
                </div>

                <div className="space-y-6">
                  {/* Symbol Search */}
                  <div>
                    <label className="block text-xs font-medium opacity-60 mb-2">Symbol</label>
                    <div className="relative">
                      <input
                        type="text"
                        value={searchQuery}
                        onChange={handleSearchChange}
                        onFocus={() => searchQuery.length > 0 && setShowAutocomplete(true)}
                        onBlur={() => setTimeout(() => setShowAutocomplete(false), 200)}
                        placeholder="Search ticker..."
                        className="w-full px-3 py-2 rounded-lg bg-background-dark border border-border-dark text-sm focus:outline-none focus:border-primary transition-colors"
                      />
                      <svg className="absolute right-3 top-2.5 w-4 h-4 opacity-40" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
                      </svg>

                      {/* Autocomplete Dropdown */}
                      {showAutocomplete && searchResults.length > 0 && (
                        <div className="absolute z-50 w-full mt-1 bg-card-dark rounded-lg shadow-xl border border-border-dark max-h-60 overflow-y-auto">
                          {searchResults.map((result) => (
                            <button
                              key={result.symbol}
                              onClick={() => selectSymbol(result.symbol)}
                              className="w-full px-4 py-2 text-left hover:bg-primary/10 transition-colors text-sm border-b border-border-dark last:border-0"
                            >
                              <div className="font-medium text-white">{result.symbol}</div>
                              {result.name && <div className="text-xs opacity-60">{result.name}</div>}
                            </button>
                          ))}
                        </div>
                      )}
                    </div>
                    {selectedSymbol && (
                      <div className="mt-2 flex items-center justify-between bg-primary/10 px-2 py-1 rounded border border-primary/20">
                        <span className="text-xs font-bold text-primary">{selectedSymbol}</span>
                        <button onClick={clearSearch} className="text-primary hover:text-white">×</button>
                      </div>
                    )}
                  </div>

                  {/* Sector Filter */}
                  <div>
                    <label className="block text-xs font-medium opacity-60 mb-2">Sector</label>
                    <select
                      value={selectedSector}
                      onChange={(e) => {
                        setSelectedSector(e.target.value);
                        setPage(1);
                      }}
                      className="w-full px-3 py-2 rounded-lg bg-background-dark border border-border-dark text-sm focus:outline-none focus:border-primary transition-colors"
                    >
                      <option value="all">All Sectors</option>
                      {availableSectors.map((sector: string) => (
                        <option key={sector} value={sector}>{sector}</option>
                      ))}
                    </select>
                  </div>

                  {/* Mock Price Range Filter */}
                  <div>
                    <label className="block text-xs font-medium opacity-60 mb-2">Price Range</label>
                    <div className="flex items-center gap-2">
                      <input type="number" placeholder="Min" className="w-1/2 px-3 py-2 rounded-lg bg-background-dark border border-border-dark text-xs" />
                      <span className="opacity-40">-</span>
                      <input type="number" placeholder="Max" className="w-1/2 px-3 py-2 rounded-lg bg-background-dark border border-border-dark text-xs" />
                    </div>
                  </div>
                </div>
              </div>

              {/* Main Content */}
              <div className="col-span-9 space-y-4">
                {/* Sub-Tabs & Actions */}
                <div className="flex items-center justify-between bg-card-dark rounded-xl border border-border-dark p-2">
                  <div className="flex items-center gap-1">
                    <button
                      onClick={() => { setScreenerTab('technicals'); setPage(1); }}
                      className={`px-4 py-1.5 rounded-lg text-sm font-medium transition-all ${screenerTab === 'technicals' ? 'bg-background-dark text-white shadow-sm border border-border-dark' : 'text-text-secondary hover:text-white'}`}
                    >
                      Technicals
                    </button>
                    <button
                      onClick={() => { setScreenerTab('financials'); setPage(1); }}
                      className={`px-4 py-1.5 rounded-lg text-sm font-medium transition-all ${screenerTab === 'financials' ? 'bg-background-dark text-white shadow-sm border border-border-dark' : 'text-text-secondary hover:text-white'}`}
                    >
                      Financials
                    </button>
                  </div>

                  {screenerTab === 'financials' && (
                    <label className={`
                       flex items-center gap-2 px-3 py-1.5 rounded-lg text-xs font-medium cursor-pointer transition-all
                       ${uploading ? 'bg-background-dark opacity-50 cursor-wait' : 'bg-primary/10 hover:bg-primary/20 text-primary border border-primary/20'}
                    `}>
                      {uploading ? 'Uploading...' : 'Upload Excel(s)'}
                      <input
                        type="file"
                        accept=".xlsx, .xls, .csv"
                        multiple
                        className="hidden"
                        onChange={handleFileUpload}
                        disabled={uploading}
                      />
                    </label>
                  )}
                </div>

                {/* Active Filters Row */}
                <div className="flex items-center gap-2 text-xs">
                  <span className="opacity-60">Results:</span>
                  <span className="text-white font-medium">{totalRecords} stocks</span>
                  {selectedSector !== 'all' && <span className="px-2 py-0.5 rounded bg-primary/20 text-primary border border-primary/20">Sector: {selectedSector}</span>}
                </div>

                {/* DATA TABLE CONTAINER */}
                <div className="bg-card-dark rounded-xl border border-border-dark overflow-hidden flex flex-col shadow-lg shadow-black/20" style={{ minHeight: '600px' }}>
                  <div className="flex-grow overflow-auto">
                    {screenerTab === 'technicals' ? renderTechnicals() : renderFinancials()}
                  </div>
                  {/* Pagination Controls */}
                  <div className="flex items-center justify-between p-4 border-t border-border-dark bg-card-dark">
                    <span className="text-sm opacity-60">
                      Showing {(page - 1) * limit + 1} to {Math.min(page * limit, totalRecords)} of {totalRecords} entries
                    </span>
                    <div className="flex items-center gap-2">
                      <button
                        onClick={() => handlePageChange(page - 1)}
                        disabled={page <= 1}
                        className="px-3 py-1.5 rounded-lg text-sm bg-background-dark border border-border-dark hover:bg-white/5 disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
                      >
                        Previous
                      </button>
                      <span className="text-sm font-medium px-2">
                        Page {page} of {totalPages}
                      </span>
                      <button
                        onClick={() => handlePageChange(page + 1)}
                        disabled={page >= totalPages}
                        className="px-3 py-1.5 rounded-lg text-sm bg-background-dark border border-border-dark hover:bg-white/5 disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
                      >
                        Next
                      </button>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          ) : mainTab === 'portfolio' ? (
            <div className="animate-in fade-in slide-in-from-bottom-4">
              {/* Portfolio Risk Tab Content */}
              <PortfolioRiskTab />
            </div>
          ) : (
            <div className="animate-in fade-in slide-in-from-bottom-4">
              {/* Strategies Tab Content */}
              <StrategiesTab />
            </div>
          )
        }

      </div >
    </div >
  )
}

// Portfolio Risk Tab Component - Now Using Unified Interface
function PortfolioRiskTab() {
  return <UnifiedPortfolioAnalyzer />;
}

// Strategies Tab Component
function StrategiesTab() {
  const [backtestResults, setBacktestResults] = useState<any>(null);
  const [isRunning, setIsRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleRunBacktest = async (config: any) => {
    setIsRunning(true);
    setError(null);

    try {
      const response = await fetch('http://localhost:8000/api/strategies/backtest', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          strategy_name: config.strategy,
          symbol: config.symbol,
          start_date: config.startDate,
          end_date: config.endDate,
          timeframe: config.timeframe,
          initial_capital: config.initialCapital,
          params: {
            opening_range_minutes: config.openingRangeMinutes,
            stop_loss_pct: config.stopLoss,
            take_profit_pct: config.takeProfit,
            max_positions_per_day: config.maxPositions,
            trade_type: config.segment,
            trailing_sl: config.trailingSL
          }
        }),
      });

      if (!response.ok) {
        const errorData = await response.json();
        throw new Error(errorData.detail || 'Backtest failed');
      }

      const data = await response.json();
      setBacktestResults(data);
    } catch (err: any) {
      setError(err.message);
      console.error('Backtest error:', err);
    } finally {
      setIsRunning(false);
    }
  };

  return (
    <div className="grid grid-cols-12 gap-6 animate-in fade-in slide-in-from-bottom-4">
      {/* Left Sidebar - Configuration */}
      <div className="col-span-3 bg-card-dark rounded-xl border border-border-dark py-4 h-[calc(100vh-140px)] sticky top-24 overflow-y-auto">
        <StrategyConfiguration onRunBacktest={handleRunBacktest} isRunning={isRunning} />
      </div>

      {/* Main Content - Results */}
      <div className="col-span-9 space-y-6 h-[calc(100vh-140px)] overflow-y-auto pr-2">
        {error && (
          <div className="bg-red-500/10 border border-red-500/50 rounded-lg p-4 text-red-400 text-sm">
            {error}
          </div>
        )}

        {isRunning && (
          <div className="h-full flex items-center justify-center">
            <div className="bg-card-dark rounded-xl border border-border-dark p-12 text-center">
              <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-primary mx-auto mb-4"></div>
              <p className="opacity-60">Running strategy backtest...</p>
            </div>
          </div>
        )}

        {!isRunning && !backtestResults && !error && (
          <div className="h-full flex items-center justify-center opacity-40">
            <div className="text-center">
              <div className="text-6xl mb-4">🚀</div>
              <p>Select settings and run backtest</p>
            </div>
          </div>
        )}

        {backtestResults && !isRunning && (
          <>
            {/* Performance KPIs */}
            <PerformanceMetrics results={backtestResults} />

            {/* Equity Curve */}
            {backtestResults.equity_curve && (
              <EquityCurve equityCurve={backtestResults.equity_curve} initialCapital={backtestResults.initial_capital} />
            )}

            {/* Trades Table */}
            {backtestResults.trades && (
              <TradesTable trades={backtestResults.trades} />
            )}
          </>
        )}
      </div>
    </div>
  );
}
