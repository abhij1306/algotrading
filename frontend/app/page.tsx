'use client'

import { useEffect, useState } from 'react'
import UnifiedPortfolioAnalyzer from '../components/UnifiedPortfolioAnalyzer';
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts';

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
        <tbody className="divide-y divide-gray-200/10 dark:divide-gray-800/30">
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
              <tr key={stock.symbol} className="group transition-colors hover:bg-white/40 dark:hover:bg-white/5">
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
        <thead className="sticky top-0 z-20 bg-white/50 dark:bg-black/50 backdrop-blur-md border-b border-gray-200/20 dark:border-gray-700/30">
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
        <tbody className="divide-y divide-gray-200/10 dark:divide-gray-800/30">
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
              <tr key={stock.symbol} className="group transition-colors hover:bg-white/40 dark:hover:bg-white/5">
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
    <div className="min-h-screen bg-gray-50 dark:bg-gray-900 transition-colors duration-300">
      {/* Background Ambience */}
      <div className="fixed inset-0 pointer-events-none z-0">
        <div className="absolute top-[-20%] right-[-10%] w-[600px] h-[600px] rounded-full bg-blue-500/20 blur-[120px]" />
        <div className="absolute bottom-[-20%] left-[-10%] w-[500px] h-[500px] rounded-full bg-purple-500/15 blur-[100px]" />
      </div>

      <div className="relative z-10 max-w-[1400px] mx-auto px-6 pt-3">

        {/* Header */}
        <header className="flex items-center justify-between px-4 py-2 mb-3 border-b border-gray-200/10">
          <h1 className="text-base font-bold bg-gradient-to-r from-blue-500 to-purple-500 bg-clip-text text-transparent">
            NSE Trading Screener
          </h1>

          <div className="flex items-center gap-2 bg-gray-200 dark:bg-gray-800 rounded-full p-1">
            <button
              onClick={() => setMainTab('screener')}
              className={`px-6 py-1.5 rounded-full text-xs font-medium transition-all duration-300 ${mainTab === 'screener' ? 'bg-white text-gray-900 shadow-sm dark:bg-gray-700 dark:text-white' : 'text-gray-600 dark:text-gray-400 opacity-60 hover:opacity-100'}`}
            >
              Screener
            </button>
            <button
              onClick={() => setMainTab('portfolio')}
              className={`px-6 py-1.5 rounded-full text-xs font-medium transition-all duration-300 ${mainTab === 'portfolio' ? 'bg-white text-gray-900 shadow-sm dark:bg-gray-700 dark:text-white' : 'text-gray-600 dark:text-gray-400 opacity-60 hover:opacity-100'}`}
            >
              Portfolio Risk
            </button>
            <button
              onClick={() => setMainTab('strategies')}
              className={`px-6 py-1.5 rounded-full text-xs font-medium transition-all duration-300 ${mainTab === 'strategies' ? 'bg-white text-gray-900 shadow-sm dark:bg-gray-700 dark:text-white' : 'text-gray-600 dark:text-gray-400 opacity-60 hover:opacity-100'}`}
            >
              Strategies
            </button>
          </div>

          <div className="flex items-center gap-3">
            <span className="text-sm font-sans tabular-nums tracking-tight opacity-50 mr-2 hidden sm:block">
              {currentTime}
            </span>
            <button
              onClick={toggleTheme}
              className="glass-button p-2 rounded-lg"
            >
              {isDark ? '☀️' : '🌙'}
            </button>
          </div>
        </header>

        {/* Content */}
        {mainTab === 'screener' ? (
          <div className="animate-in fade-in slide-in-from-bottom-4">
            {/* Screener Sub-Tabs & Actions */}
            <div className="flex items-center justify-between mb-6 border-b border-gray-200/10 pb-1">
              <div className="flex items-center gap-4">
                <button
                  onClick={() => { setScreenerTab('technicals'); setPage(1); }}
                  className={`pb-2 px-2 text-sm font-medium transition-colors relative ${screenerTab === 'technicals' ? 'text-blue-500' : 'opacity-60 hover:opacity-100'}`}
                >
                  Technicals
                  {screenerTab === 'technicals' && <div className="absolute bottom-[-1px] left-0 right-0 h-0.5 bg-blue-500 rounded-full" />}
                </button>
                <button
                  onClick={() => { setScreenerTab('financials'); setPage(1); }}
                  className={`pb-2 px-2 text-sm font-medium transition-colors relative ${screenerTab === 'financials' ? 'text-blue-500' : 'opacity-60 hover:opacity-100'}`}
                >
                  Financials
                  {screenerTab === 'financials' && <div className="absolute bottom-[-1px] left-0 right-0 h-0.5 bg-blue-500 rounded-full" />}
                </button>
              </div>

              {/* Upload Button (Visible only on Financials tab) */}
              {screenerTab === 'financials' && (
                <div className="pb-2">
                  <label className={`
                      flex items-center gap-2 px-4 py-1.5 rounded-lg text-xs font-medium cursor-pointer transition-all
                      ${uploading ? 'bg-gray-100 dark:bg-gray-800 opacity-50 cursor-wait' : 'bg-blue-500/10 hover:bg-blue-500/20 text-blue-500'}
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
                </div>
              )}
            </div>

            {/* Symbol Search & Sector Filter */}
            <div className="mb-4 relative">
              <div className="flex items-center gap-3">
                {/* Symbol Search */}
                <div className="relative flex-1 max-w-md">
                  <input
                    type="text"
                    value={searchQuery}
                    onChange={handleSearchChange}
                    onFocus={() => searchQuery.length > 0 && setShowAutocomplete(true)}
                    onBlur={() => setTimeout(() => setShowAutocomplete(false), 200)}
                    placeholder="Search symbol..."
                    className="w-full px-4 py-2 pl-10 rounded-lg bg-white/5 border border-gray-200/10 text-sm focus:outline-none focus:border-blue-500/50 transition-colors"
                  />
                  <svg className="absolute left-3 top-2.5 w-4 h-4 opacity-50" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
                  </svg>

                  {/* Autocomplete Dropdown */}
                  {showAutocomplete && searchResults.length > 0 && (
                    <div className="absolute z-50 w-full mt-1 bg-white dark:bg-gray-800 rounded-lg shadow-lg border border-gray-200/10 max-h-60 overflow-y-auto">
                      {searchResults.map((result) => (
                        <button
                          key={result.symbol}
                          onClick={() => selectSymbol(result.symbol)}
                          className="w-full px-4 py-2 text-left hover:bg-blue-500/10 transition-colors text-sm"
                        >
                          <div className="font-medium">{result.symbol}</div>
                          {result.name && <div className="text-xs opacity-60">{result.name}</div>}
                        </button>
                      ))}
                    </div>
                  )}
                </div>

                {/* Sector Filter */}
                <select
                  value={selectedSector}
                  onChange={(e) => {
                    setSelectedSector(e.target.value);
                    setPage(1);
                  }}
                  className="px-4 py-2 rounded-lg bg-gray-100 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 text-gray-900 dark:text-gray-100 text-sm focus:outline-none focus:border-blue-500 transition-colors min-w-[180px]"
                >
                  <option value="all">All Sectors</option>
                  {availableSectors.map((sector: string) => (
                    <option key={sector} value={sector}>{sector}</option>
                  ))}
                </select>

                {selectedSymbol && (
                  <button
                    onClick={clearSearch}
                    className="px-3 py-2 rounded-lg bg-red-500/10 hover:bg-red-500/20 text-red-500 text-xs font-medium transition-all"
                  >
                    Clear
                  </button>
                )}

                <div className="text-xs opacity-60 ml-auto">
                  {totalRecords} stocks {selectedSector !== 'all' && `in ${selectedSector}`}
                </div>
              </div>
            </div>

            {/* Table Card */}
            <div className="bg-card-dark rounded-xl border border-border-dark overflow-hidden flex flex-col card-glow" style={{ maxHeight: 'calc(100vh - 200px)' }}>
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
                    className="px-4 py-1.5 rounded-lg text-sm bg-gray-200/50 dark:bg-gray-800/50 hover:bg-gray-300 dark:hover:bg-gray-700 disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
                  >
                    Previous
                  </button>
                  <span className="text-sm font-medium px-2">
                    Page {page} of {totalPages}
                  </span>
                  <button
                    onClick={() => handlePageChange(page + 1)}
                    disabled={page >= totalPages}
                    className="px-4 py-1.5 rounded-lg text-sm bg-gray-200/50 dark:bg-gray-800/50 hover:bg-gray-300 dark:hover:bg-gray-700 disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
                  >
                    Next
                  </button>
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
        )}

      </div>
    </div>
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
  const [config, setConfig] = useState({
    strategy: 'ORB',
    symbol: 'RELIANCE',
    timeframe: '5min',
    segment: 'options',
    startDate: '2025-11-14',
    endDate: '2025-12-12',
    initialCapital: 1000000,
    openingRangeMinutes: 5,
    stopLoss: 20.0,
    takeProfit: 200.0,
    maxPositions: 1,
    riskPerTrade: 2.0,
  });

  const handleRunBacktest = async () => {
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
            trade_type: config.segment
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
    <div className="grid grid-cols-12 gap-6">
      {/* Left Sidebar - Configuration */}
      <div className="col-span-3 bg-card-dark rounded-xl border border-border-dark p-6">
        <h3 className="text-lg font-semibold mb-6">Strategy Configuration</h3>

        <div className="space-y-2">
          {/* Strategy Selector */}
          <div>
            <label className="block text-xs font-medium opacity-60 mb-1">Strategy</label>
            <select
              value={config.strategy}
              onChange={(e) => setConfig({ ...config, strategy: e.target.value })}
              className="w-full px-3 py-2 rounded-lg bg-gray-100 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 text-sm focus:outline-none focus:border-blue-500"
              disabled={isRunning}
            >
              <option value="ORB">Opening Range Breakout (ORB)</option>
            </select>
          </div>

          {/* Symbol */}
          <div>
            <label className="block text-sm font-medium opacity-60 mb-2">Symbol</label>
            <input
              type="text"
              value={config.symbol}
              onChange={(e) => setConfig({ ...config, symbol: e.target.value.toUpperCase() })}
              className="w-full px-3 py-2 rounded-lg bg-gray-100 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 text-sm focus:outline-none focus:border-blue-500"
              disabled={isRunning}
            />
          </div>

          {/* Timeframe */}
          <div>
            <label className="block text-sm font-medium opacity-60 mb-2">Timeframe</label>
            <select
              value={config.timeframe}
              onChange={(e) => setConfig({ ...config, timeframe: e.target.value })}
              className="w-full px-3 py-2 rounded-lg bg-gray-100 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 text-sm focus:outline-none focus:border-blue-500"
              disabled={isRunning}
            >
              <option value="5min">5 Min</option>
              <option value="15min">15 Min</option>
              <option value="1D">1 Day</option>
            </select>
          </div>

          {/* Date Range */}
          <div>
            <label className="block text-sm font-medium opacity-60 mb-2">Start Date</label>
            <input
              type="date"
              value={config.startDate}
              onChange={(e) => setConfig({ ...config, startDate: e.target.value })}
              className="w-full px-3 py-2 rounded-lg bg-gray-100 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 text-sm focus:outline-none focus:border-blue-500"
              disabled={isRunning}
            />
          </div>

          <div>
            <label className="block text-sm font-medium opacity-60 mb-2">End Date</label>
            <input
              type="date"
              value={config.endDate}
              onChange={(e) => setConfig({ ...config, endDate: e.target.value })}
              className="w-full px-3 py-2 rounded-lg bg-gray-100 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 text-sm focus:outline-none focus:border-blue-500"
              disabled={isRunning}
            />
          </div>

          {/* Capital Settings */}
          <div className="pt-2 border-t border-gray-200/10">
            <label className="block text-xs font-medium opacity-60 mb-1">Initial Capital (₹)</label>
            <input
              type="number"
              step="10000"
              value={config.initialCapital}
              onChange={(e) => setConfig({ ...config, initialCapital: parseFloat(e.target.value) })}
              className="w-full px-2 py-1.5 rounded-lg bg-gray-100 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 text-xs focus:outline-none focus:border-blue-500"
              disabled={isRunning}
            />
          </div>

          <div>
            <label className="block text-xs font-medium opacity-60 mb-1">Risk Per Trade (%)</label>
            <input
              type="number"
              step="0.5"
              value={config.riskPerTrade}
              onChange={(e) => setConfig({ ...config, riskPerTrade: parseFloat(e.target.value) })}
              className="w-full px-2 py-1.5 rounded-lg bg-gray-100 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 text-xs focus:outline-none focus:border-blue-500"
              disabled={isRunning}
            />
          </div>

          {/* Risk Parameters */}
          <div className="pt-4 border-t border-gray-200/10">
            <label className="block text-sm font-medium opacity-60 mb-2">Stop Loss (%)</label>
            <input
              type="number"
              step="0.1"
              value={config.stopLoss}
              onChange={(e) => setConfig({ ...config, stopLoss: parseFloat(e.target.value) })}
              className="w-full px-3 py-2 rounded-lg bg-gray-100 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 text-sm focus:outline-none focus:border-blue-500"
              disabled={isRunning}
            />
          </div>

          <div>
            <label className="block text-sm font-medium opacity-60 mb-2">Take Profit (%)</label>
            <input
              type="number"
              step="0.1"
              value={config.takeProfit}
              onChange={(e) => setConfig({ ...config, takeProfit: parseFloat(e.target.value) })}
              className="w-full px-3 py-2 rounded-lg bg-gray-100 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 text-sm focus:outline-none focus:border-blue-500"
              disabled={isRunning}
            />
          </div>

          {/* Run Button */}
          <button
            onClick={handleRunBacktest}
            disabled={isRunning}
            className={`w-full py-3 rounded-lg font-semibold transition-all ${isRunning
              ? 'bg-gray-300 dark:bg-gray-700 cursor-not-allowed opacity-50'
              : 'bg-gradient-to-r from-blue-500 to-purple-500 hover:from-blue-600 hover:to-purple-600 text-white shadow-lg'
              }`}
          >
            {isRunning ? 'Running...' : '▶ Run Backtest'}
          </button>
        </div>
      </div>

      {/* Right Content - Results */}
      <div className="col-span-9 space-y-3">
        {error && (
          <div className="bg-red-500/10 border border-red-500/50 rounded-lg p-4 text-red-400 text-sm">
            {error}
          </div>
        )}

        {isRunning && (
          <div className="bg-card-dark rounded-xl border border-border-dark p-12 text-center">
            <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-blue-500 mx-auto mb-4"></div>
            <p className="opacity-60">Running backtest...</p>
          </div>
        )}

        {!isRunning && !backtestResults && !error && (
          <div className="bg-card-dark rounded-xl border border-border-dark p-12 text-center opacity-60">
            <p>Configure strategy and run backtest to see results</p>
          </div>
        )}

        {backtestResults && !isRunning && (
          <>
            {/* Results Header */}
            <div className="bg-card-dark rounded-xl border border-border-dark p-3">
              <div className="flex items-center justify-between">
                <div>
                  <h3 className="text-sm font-bold">Opening Range Breakout (ORB)</h3>
                  <p className="text-xs opacity-60 mt-0.5">
                    {backtestResults.symbol} • {backtestResults.period?.start} to {backtestResults.period?.end}
                  </p>
                </div>
                <div className={`text-xl font-bold ${backtestResults.total_return >= 0 ? 'text-green-400' : 'text-red-400'
                  }`}>
                  {backtestResults.total_return >= 0 ? '+' : ''}
                  {backtestResults.total_return?.toFixed(2)}%
                </div>
              </div>
            </div>

            {/* Metrics Grid */}
            <div className="grid grid-cols-3 gap-3">
              {/* Performance */}
              <div className="bg-card-dark rounded-xl border border-border-dark p-3">
                <h4 className="text-xs font-semibold opacity-60 mb-2">PERFORMANCE</h4>
                <div className="space-y-1.5">
                  <div>
                    <div className="text-xs opacity-60">Win Rate</div>
                    <div className="text-lg font-bold text-green-400">
                      {backtestResults.metrics?.trade_analysis?.win_rate_pct?.toFixed(0) || 0}%
                    </div>
                  </div>
                  <div className="pt-1.5 border-t border-gray-200/10 text-xs space-y-1">
                    <div className="flex justify-between">
                      <span className="opacity-60">CAGR</span>
                      <span className="font-medium">{backtestResults.metrics?.performance?.cagr_pct?.toFixed(1)}%</span>
                    </div>
                    <div className="flex justify-between">
                      <span className="opacity-60">Sharpe Ratio</span>
                      <span className="font-medium">{backtestResults.metrics?.performance?.sharpe_ratio?.toFixed(2)}</span>
                    </div>
                  </div>
                </div>
              </div>

              {/* Risk */}
              <div className="bg-card-dark rounded-xl border border-border-dark p-3">
                <h4 className="text-xs font-semibold opacity-60 mb-2">RISK</h4>
                <div className="space-y-1.5">
                  <div>
                    <div className="text-xs opacity-60">Max Drawdown</div>
                    <div className="text-lg font-bold text-red-400">
                      {backtestResults.metrics?.risk?.max_drawdown_pct?.toFixed(1)}%
                    </div>
                  </div>
                  <div className="pt-1.5 border-t border-gray-200/10 text-xs space-y-1">
                    <div className="flex justify-between">
                      <span className="opacity-60">Max Cons. Losses</span>
                      <span className="font-medium">{backtestResults.metrics?.risk?.max_consecutive_losses || 0}</span>
                    </div>
                    <div className="flex justify-between">
                      <span className="opacity-60">Volatility</span>
                      <span className="font-medium">{backtestResults.metrics?.risk?.volatility_pct?.toFixed(1)}%</span>
                    </div>
                  </div>
                </div>
              </div>

              {/* Trades */}
              <div className="bg-card-dark rounded-xl border border-border-dark p-3">
                <h4 className="text-xs font-semibold opacity-60 mb-2">TRADES</h4>
                <div className="space-y-1.5">
                  <div>
                    <div className="text-xs opacity-60">Total Trades</div>
                    <div className="text-lg font-bold">{backtestResults.summary?.total_trades || 0}</div>
                  </div>
                  <div className="pt-1.5 border-t border-gray-200/10 text-xs space-y-1">
                    <div className="flex justify-between">
                      <span className="opacity-60">Wins / Losses</span>
                      <span className="font-medium">
                        {backtestResults.summary?.winning_trades} / {backtestResults.summary?.losing_trades}
                      </span>
                    </div>
                    <div className="flex justify-between">
                      <span className="opacity-60">Profit Factor</span>
                      <span className="font-medium">{backtestResults.metrics?.performance?.profit_factor?.toFixed(2)}</span>
                    </div>
                  </div>
                </div>
              </div>
            </div>

            {/* Equity Curve Chart */}
            {backtestResults.equity_curve && backtestResults.equity_curve.length > 0 && (
              <div className="bg-card-dark rounded-xl border border-border-dark p-3">
                <h4 className="text-xs font-semibold opacity-60 mb-2">EQUITY CURVE</h4>
                <div style={{ width: '100%', height: 150 }}>
                  <ResponsiveContainer>
                    <LineChart data={backtestResults.equity_curve.map((point: any) => ({
                      timestamp: new Date(point.timestamp).toLocaleDateString('en-IN', { month: 'short', day: 'numeric' }),
                      equity: point.equity
                    }))}>
                      <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
                      <XAxis
                        dataKey="timestamp"
                        stroke="#94a3b8"
                        style={{ fontSize: '10px' }}
                      />
                      <YAxis
                        stroke="#94a3b8"
                        style={{ fontSize: '10px' }}
                        tickFormatter={(value) => `₹${(value / 1000).toFixed(0)}k`}
                      />
                      <Tooltip
                        contentStyle={{
                          backgroundColor: '#1e293b',
                          border: '1px solid #334155',
                          borderRadius: '8px',
                          color: '#fff'
                        }}
                        formatter={(value: any) => [`₹${Number(value).toLocaleString()}`, 'Equity']}
                      />
                      <Line
                        type="monotone"
                        dataKey="equity"
                        stroke="#10b981"
                        strokeWidth={2}
                        dot={false}
                      />
                    </LineChart>
                  </ResponsiveContainer>
                </div>
              </div>
            )}

            {/* Trades Table */}
            {backtestResults.trades && backtestResults.trades.length > 0 && (
              <div className="bg-card-dark rounded-xl border border-border-dark p-3">
                <h4 className="text-xs font-semibold opacity-60 mb-2">RECENT TRADES</h4>
                <div className="overflow-x-auto">
                  <table className="w-full text-xs">
                    <thead className="border-b border-gray-200/10">
                      <tr className="text-xs opacity-60">
                        <th className="text-left pb-1.5">Date & Time</th>
                        <th className="text-left pb-1.5">Instrument</th>
                        <th className="text-right pb-1.5">Entry</th>
                        <th className="text-right pb-1.5">Exit</th>
                        <th className="text-right pb-1.5">P&L</th>
                      </tr>
                    </thead>
                    <tbody>
                      {backtestResults.trades.slice(0, 10).map((trade: any, index: number) => (
                        <tr key={index} className="border-b border-gray-200/5">
                          <td className="py-1.5">
                            {new Date(trade.entry_time).toLocaleString('en-IN', {
                              month: 'short',
                              day: '2-digit',
                              hour: '2-digit',
                              minute: '2-digit'
                            })}
                          </td>
                          <td className="py-2 font-medium">{trade.instrument}</td>
                          <td className="py-2 text-right opacity-80">₹{trade.entry_price.toFixed(2)}</td>
                          <td className="py-2 text-right opacity-80">₹{trade.exit_price.toFixed(2)}</td>
                          <td className={`py-2 text-right font-medium ${trade.pnl >= 0 ? 'text-green-400' : 'text-red-400'
                            }`}>
                            {trade.pnl >= 0 ? '+' : ''}₹{trade.pnl.toFixed(2)}
                            <span className="text-xs ml-1">({trade.pnl_pct.toFixed(1)}%)</span>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
