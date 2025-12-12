'use client'

import { useEffect, useState } from 'react'

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
  score: number
  is_20d_breakout: boolean
}

interface MarketData {
  cash_market: {
    fii: { daily: Activity; monthly: Activity; yearly: Activity }
    dii: { daily: Activity; monthly: Activity; yearly: Activity }
  }
  derivatives: {
    index_futures: { fii: FnoActivity; pro: FnoActivity }
    index_options: { fii: FnoActivity; pro: FnoActivity }
  }
  last_updated: string
}

interface Activity {
  buy: number
  sell: number
  net: number
}

interface FnoActivity {
  buy: number
  sell: number
  net: number
  oi: string
}

import RiskDashboard from '../components/RiskDashboard'

type TabType = 'intraday' | 'swing' | 'market_data' | 'risk'
type SortField = 'symbol' | 'close' | 'current_price' | 'volume' | 'ema20' | 'ema50' | 'atr_pct' | 'rsi' | 'vol_percentile' | 'score'
type SortOrder = 'asc' | 'desc'

export default function MacOSPage() {
  const [activeTab, setActiveTab] = useState<TabType>('intraday')
  const [isDark, setIsDark] = useState(true)
  const [isDrawerOpen, setIsDrawerOpen] = useState(false)
  const [data, setData] = useState<Stock[]>([])
  const [marketData, setMarketData] = useState<MarketData | null>(null)
  const [loading, setLoading] = useState(true)
  const [currentTime, setCurrentTime] = useState('')

  // Sorting State
  const [sortField, setSortField] = useState<SortField>('score')
  const [sortOrder, setSortOrder] = useState<SortOrder>('desc')

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

  // Data fetching
  const fetchData = async (forceRefresh = false) => {
    setLoading(true)
    try {
      if (forceRefresh) {
        await fetch('http://localhost:8000/api/cache/clear', { method: 'POST' })
      }

      if (activeTab === 'market_data') {
        const res = await fetch('http://localhost:8000/api/market-data')
        const json = await res.json()
        setMarketData(json)
      } else {
        const strategy = activeTab === 'intraday' ? 'intraday' : 'swing_trade'
        const res = await fetch(`http://localhost:8000/api/screener?strategy=${strategy}`)
        const json = await res.json()
        if (json.stocks) {
          setData(json.stocks)
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
  }, [activeTab])

  // Sorting Logic
  const handleSort = (field: SortField) => {
    if (sortField === field) {
      setSortOrder(sortOrder === 'asc' ? 'desc' : 'asc')
    } else {
      setSortField(field)
      setSortOrder('desc') // Default to desc for new field
    }
  }

  const sortedData = [...data].sort((a, b) => {
    let valA = a[fieldToDataKey(sortField)]
    let valB = b[fieldToDataKey(sortField)]

    if (valA === undefined) valA = -Infinity
    if (valB === undefined) valB = -Infinity

    if (typeof valA === 'string' && typeof valB === 'string') {
      return sortOrder === 'asc' ? valA.localeCompare(valB) : valB.localeCompare(valA)
    }

    // Numeric sort
    return sortOrder === 'asc' ? (valA as number) - (valB as number) : (valB as number) - (valA as number)
  })

  function fieldToDataKey(field: SortField): keyof Stock {
    if (field === 'current_price') return 'close'
    return field as keyof Stock
  }

  // Helper to render sort icon
  const SortIcon = ({ field }: { field: SortField }) => {
    if (sortField !== field) return <span className="opacity-20 ml-1">⇅</span>
    return <span className="ml-1 text-blue-500">{sortOrder === 'asc' ? '↑' : '↓'}</span>
  }

  // Scoring Logic Panel (Drawer)
  const renderDrawer = () => (
    <>
      <div
        className={`fixed inset-0 bg-black/20 backdrop-blur-sm z-40 transition-opacity duration-300 ${isDrawerOpen ? 'opacity-100' : 'opacity-0 pointer-events-none'}`}
        onClick={() => setIsDrawerOpen(false)}
      />
      <div className={`fixed top-0 left-0 h-full w-[360px] glass-panel z-50 transform transition-transform duration-300 ease-out ${isDrawerOpen ? 'translate-x-0' : '-translate-x-full'}`}>
        <div className="p-6 h-full overflow-y-auto">
          <div className="flex justify-between items-center mb-8">
            <h2 className="text-xl font-semibold tracking-tight">Scoring Logic</h2>
            <button
              onClick={() => setIsDrawerOpen(false)}
              className="p-2 rounded-full hover:bg-gray-200/50 dark:hover:bg-gray-700/50 transition-colors"
            >
              <svg className="w-5 h-5 opacity-60" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>
          <div className="space-y-8">
            <div>
              <div className="flex items-center gap-2 mb-3">
                <div className="w-8 h-8 rounded-lg bg-blue-500/10 flex items-center justify-center text-blue-500">
                  <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 7h8m0 0v8m0-8l-8 8-4-4-6 6" /></svg>
                </div>
                <h3 className="font-medium text-lg">Trend Alignment</h3>
              </div>
              <p className="text-sm opacity-70 leading-relaxed pl-10">
                Stocks must be trading above both EMA 20 and EMA 50, with EMA 20 &gt; EMA 50 to confirm a strong uptrend.
              </p>
              <div className="ml-10 mt-2 text-xs font-sans tabular-nums tracking-tight opacity-50 bg-gray-100 dark:bg-gray-800/50 p-2 rounded">
                {'Price > EMA20 > EMA50'}
              </div>
            </div>
            <div>
              <div className="flex items-center gap-2 mb-3">
                <div className="w-8 h-8 rounded-lg bg-green-500/10 flex items-center justify-center text-green-500">
                  <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 002 2h2a2 2 0 002-2z" /></svg>
                </div>
                <h3 className="font-medium text-lg">Volume Surge</h3>
              </div>
              <p className="text-sm opacity-70 leading-relaxed pl-10">
                Current volume must be significantly higher than the 10-day average volume to confirm institutional interest.
              </p>
            </div>
            <div>
              <div className="flex items-center gap-2 mb-3">
                <div className="w-8 h-8 rounded-lg bg-purple-500/10 flex items-center justify-center text-purple-500">
                  <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 10V3L4 14h7v7l9-11h-7z" /></svg>
                </div>
                <h3 className="font-medium text-lg">Momentum (RSI)</h3>
              </div>
              <p className="text-sm opacity-70 leading-relaxed pl-10">
                RSI (14) must be in the bullish zone (55-70) indicating strength without being overbought.
              </p>
            </div>
          </div>
        </div>
      </div>
    </>
  )

  const columns: { id: SortField; label: string; align: 'left' | 'right' | 'center' }[] = [
    { id: 'symbol', label: 'SYMBOL', align: 'left' },
    { id: 'close', label: 'LTP', align: 'right' },
    { id: 'current_price', label: 'CURRENT PRICE', align: 'right' },
    { id: 'volume', label: 'VOLUME', align: 'right' },
    { id: 'ema20', label: 'EMA 20', align: 'right' },
    { id: 'ema50', label: 'EMA 50', align: 'right' },
    { id: 'atr_pct', label: 'ATR%', align: 'right' },
    { id: 'rsi', label: 'RSI', align: 'right' },
    { id: 'vol_percentile', label: 'VOL %', align: 'right' },
    { id: 'score', label: 'SCORE', align: 'center' },
  ]

  // Format currency
  const formatCr = (val: number) => {
    return (val / 100).toFixed(2) + 'Cr'
  }

  // Market Data View
  const renderMarketData = () => {
    if (!marketData) return null
    return (
      <div className="animate-in fade-in slide-in-from-bottom-4 space-y-8">
        {/* Cash Market */}
        <div className="glass-card rounded-2xl p-6">
          <div className="flex items-center justify-between mb-6">
            <h3 className="text-lg font-semibold flex items-center gap-2">
              <span className="w-2 h-2 rounded-full bg-blue-500"></span>
              FII/DII Cash Market Activity
            </h3>
            <span className="text-xs opacity-50 font-sans tabular-nums tracking-tight">Updated: {marketData.last_updated}</span>
          </div>

          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-black/5 dark:bg-white/5 border-b border-gray-200/10">
                <tr>
                  <th className="px-4 py-3 text-left opacity-60">Category</th>
                  <th className="px-4 py-3 text-right opacity-60">Daily Net</th>
                  <th className="px-4 py-3 text-right opacity-60">Monthly Net</th>
                  <th className="px-4 py-3 text-right opacity-60">Yearly Net</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-200/10">
                <tr>
                  <td className="px-4 py-4 font-semibold">FII Cash</td>
                  <td className={`px-4 py-4 text-right font-sans tabular-nums tracking-tight ${marketData.cash_market.fii.daily.net > 0 ? 'text-green-500' : 'text-red-500'}`}>
                    {formatCr(marketData.cash_market.fii.daily.net)}
                  </td>
                  <td className={`px-4 py-4 text-right font-sans tabular-nums tracking-tight ${marketData.cash_market.fii.monthly.net > 0 ? 'text-green-500' : 'text-red-500'}`}>
                    {formatCr(marketData.cash_market.fii.monthly.net)}
                  </td>
                  <td className={`px-4 py-4 text-right font-sans tabular-nums tracking-tight ${marketData.cash_market.fii.yearly.net > 0 ? 'text-green-500' : 'text-red-500'}`}>
                    {formatCr(marketData.cash_market.fii.yearly.net)}
                  </td>
                </tr>
                <tr>
                  <td className="px-4 py-4 font-semibold">DII Cash</td>
                  <td className={`px-4 py-4 text-right font-sans tabular-nums tracking-tight ${marketData.cash_market.dii.daily.net > 0 ? 'text-green-500' : 'text-red-500'}`}>
                    {formatCr(marketData.cash_market.dii.daily.net)}
                  </td>
                  <td className={`px-4 py-4 text-right font-sans tabular-nums tracking-tight ${marketData.cash_market.dii.monthly.net > 0 ? 'text-green-500' : 'text-red-500'}`}>
                    {formatCr(marketData.cash_market.dii.monthly.net)}
                  </td>
                  <td className={`px-4 py-4 text-right font-sans tabular-nums tracking-tight ${marketData.cash_market.dii.yearly.net > 0 ? 'text-green-500' : 'text-red-500'}`}>
                    {formatCr(marketData.cash_market.dii.yearly.net)}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        {/* Derivatives */}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
          {/* Index Futures */}
          <div className="glass-card rounded-2xl p-6">
            <h3 className="text-lg font-semibold mb-4 flex items-center gap-2">
              <span className="w-2 h-2 rounded-full bg-purple-500"></span>
              Index Futures
            </h3>
            <table className="w-full text-sm">
              <thead className="bg-black/5 dark:bg-white/5 border-b border-gray-200/10">
                <tr>
                  <th className="px-3 py-2 text-left opacity-60">Client</th>
                  <th className="px-3 py-2 text-right opacity-60">Net (Cr)</th>
                  <th className="px-3 py-2 text-right opacity-60">OI</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-200/10">
                <tr>
                  <td className="px-3 py-3 font-medium">FII</td>
                  <td className={`px-3 py-3 text-right font-sans tabular-nums tracking-tight ${marketData.derivatives.index_futures.fii.net > 0 ? 'text-green-500' : 'text-red-500'}`}>
                    {marketData.derivatives.index_futures.fii.net.toFixed(2)}
                  </td>
                  <td className="px-3 py-3 text-right font-sans tabular-nums tracking-tight opacity-80">{marketData.derivatives.index_futures.fii.oi}</td>
                </tr>
                <tr>
                  <td className="px-3 py-3 font-medium">Pro</td>
                  <td className={`px-3 py-3 text-right font-sans tabular-nums tracking-tight ${marketData.derivatives.index_futures.pro.net > 0 ? 'text-green-500' : 'text-red-500'}`}>
                    {marketData.derivatives.index_futures.pro.net.toFixed(2)}
                  </td>
                  <td className="px-3 py-3 text-right font-sans tabular-nums tracking-tight opacity-80">{marketData.derivatives.index_futures.pro.oi}</td>
                </tr>
              </tbody>
            </table>
          </div>

          {/* Index Options */}
          <div className="glass-card rounded-2xl p-6">
            <h3 className="text-lg font-semibold mb-4 flex items-center gap-2">
              <span className="w-2 h-2 rounded-full bg-orange-500"></span>
              Index Options
            </h3>
            <table className="w-full text-sm">
              <thead className="bg-black/5 dark:bg-white/5 border-b border-gray-200/10">
                <tr>
                  <th className="px-3 py-2 text-left opacity-60">Client</th>
                  <th className="px-3 py-2 text-right opacity-60">Net (Cr)</th>
                  <th className="px-3 py-2 text-right opacity-60">OI</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-200/10">
                <tr>
                  <td className="px-3 py-3 font-medium">FII</td>
                  <td className={`px-3 py-3 text-right font-sans tabular-nums tracking-tight ${marketData.derivatives.index_options.fii.net > 0 ? 'text-green-500' : 'text-red-500'}`}>
                    {marketData.derivatives.index_options.fii.net.toFixed(2)}
                  </td>
                  <td className="px-3 py-3 text-right font-sans tabular-nums tracking-tight opacity-80">{marketData.derivatives.index_options.fii.oi}</td>
                </tr>
                <tr>
                  <td className="px-3 py-3 font-medium">Pro</td>
                  <td className={`px-3 py-3 text-right font-sans tabular-nums tracking-tight ${marketData.derivatives.index_options.pro.net > 0 ? 'text-green-500' : 'text-red-500'}`}>
                    {marketData.derivatives.index_options.pro.net.toFixed(2)}
                  </td>
                  <td className="px-3 py-3 text-right font-sans tabular-nums tracking-tight opacity-80">{marketData.derivatives.index_options.pro.oi}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="min-h-screen transition-colors duration-300">

      {/* Background Ambience */}
      <div className="fixed inset-0 pointer-events-none z-0">
        <div className="absolute top-[-20%] right-[-10%] w-[600px] h-[600px] rounded-full bg-blue-500/20 blur-[120px]" />
        <div className="absolute bottom-[-20%] left-[-10%] w-[500px] h-[500px] rounded-full bg-purple-500/15 blur-[100px]" />
      </div>

      {renderDrawer()}

      {/* Main Content */}
      <div className="relative z-10 max-w-[1400px] mx-auto px-6 pt-6">

        {/* Header */}
        <header className="flex items-center justify-between mb-8 sticky top-6 z-30">
          <div className="flex items-center gap-4">
            <button
              onClick={() => setIsDrawerOpen(true)}
              className="p-2 rounded-full glass-button opacity-70 hover:opacity-100"
            >
              <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
              </svg>
            </button>
            <h1 className="text-2xl font-semibold tracking-tight" style={{ fontFamily: '"Segoe UI", sans-serif' }}>
              NSE Trading Screener
            </h1>
          </div>

          <div className="absolute left-1/2 transform -translate-x-1/2">
            <div className="flex bg-gray-200/50 dark:bg-gray-800/50 p-1 rounded-full backdrop-blur-md">
              <button
                onClick={() => setActiveTab('intraday')}
                className={`flex items-center gap-2 px-6 py-1.5 rounded-full text-sm font-medium transition-all duration-300 ${activeTab === 'intraday' ? 'bg-white dark:bg-gray-700 shadow-sm text-blue-600 dark:text-blue-400' : 'opacity-60 hover:opacity-100'}`}
              >
                <span>⚡</span> Intraday
              </button>
              <button
                onClick={() => setActiveTab('swing')}
                className={`flex items-center gap-2 px-6 py-1.5 rounded-full text-sm font-medium transition-all duration-300 ${activeTab === 'swing' ? 'bg-white dark:bg-gray-700 shadow-sm text-green-600 dark:text-green-400' : 'opacity-60 hover:opacity-100'}`}
              >
                <span>📈</span> Swing
              </button>
              <button
                onClick={() => setActiveTab('market_data')}
                className={`flex items-center gap-2 px-6 py-1.5 rounded-full text-sm font-medium transition-all duration-300 ${activeTab === 'market_data' ? 'bg-white dark:bg-gray-700 shadow-sm text-purple-600 dark:text-purple-400' : 'opacity-60 hover:opacity-100'}`}
              >
                <span>📊</span> Market Data
              </button>
              <button
                onClick={() => setActiveTab('risk')}
                className={`flex items-center gap-2 px-6 py-1.5 rounded-full text-sm font-medium transition-all duration-300 ${activeTab === 'risk' ? 'bg-white dark:bg-gray-700 shadow-sm text-red-600 dark:text-red-400' : 'opacity-60 hover:opacity-100'}`}
              >
                <span>⚠️</span> Risk
              </button>
            </div>
          </div>

          <div className="flex items-center gap-3">
            <span className="text-sm font-sans tabular-nums tracking-tight opacity-50 mr-2 hidden sm:block">
              {currentTime}
            </span>
            <button className="glass-button px-4 py-1.5 rounded-lg text-sm font-medium flex items-center gap-2">
              Export
            </button>
            <button
              onClick={() => fetchData(true)}
              className="glass-button px-4 py-1.5 rounded-lg text-sm font-medium flex items-center gap-2 group"
            >
              <svg className={`w-4 h-4 transition-transform duration-700 ${loading ? 'animate-spin' : 'group-hover:rotate-180'}`} fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" /></svg>
              Refresh
            </button>
            <button
              onClick={toggleTheme}
              className="glass-button p-1.5 rounded-lg"
            >
              {isDark ? '☀️' : '🌙'}
            </button>
          </div>
        </header>

        {/* Dynamic Content */}
        {activeTab === 'market_data' ? (
          renderMarketData()
        ) : activeTab === 'risk' ? (
          <RiskDashboard />
        ) : (
          /* Table Container */
          <div className="glass-card rounded-2xl overflow-hidden mt-8 min-h-[600px] mb-10 transition-all duration-500 animate-in fade-in slide-in-from-bottom-4">
            <div className="overflow-x-auto">
              <table className="w-full">
                <thead className="sticky top-0 z-20 bg-white/50 dark:bg-black/50 backdrop-blur-md border-b border-gray-200/20 dark:border-gray-700/30">
                  <tr>
                    {columns.map((col) => (
                      <th
                        key={col.id}
                        onClick={() => handleSort(col.id)}
                        className={`px-6 py-4 text-xs font-semibold opacity-60 tracking-wider cursor-pointer hover:bg-black/5 dark:hover:bg-white/5 transition-colors text-${col.align === 'right' ? 'right' : col.align === 'center' ? 'center' : 'left'}`}
                      >
                        <div className={`flex items-center gap-1 ${col.align === 'right' ? 'justify-end' : col.align === 'center' ? 'justify-center' : 'justify-start'}`}>
                          {col.label}
                          <SortIcon field={col.id} />
                        </div>
                      </th>
                    ))}
                  </tr>
                </thead>

                <tbody className="divide-y divide-gray-200/10 dark:divide-gray-800/30">
                  {loading ? (
                    Array.from({ length: 12 }).map((_, i) => (
                      <tr key={i} className="animate-pulse">
                        <td className="px-6 py-4"><div className="h-4 w-24 bg-gray-200 dark:bg-gray-800 rounded"></div></td>
                        <td className="px-6 py-4"><div className="h-4 w-16 bg-gray-200 dark:bg-gray-800 rounded ml-auto"></div></td>
                        <td className="px-6 py-4"><div className="h-4 w-16 bg-gray-200 dark:bg-gray-800 rounded ml-auto"></div></td>
                        <td className="px-6 py-4"><div className="h-4 w-16 bg-gray-200 dark:bg-gray-800 rounded ml-auto"></div></td>
                        <td className="px-6 py-4"><div className="h-4 w-16 bg-gray-200 dark:bg-gray-800 rounded ml-auto"></div></td>
                        <td className="px-6 py-4"><div className="h-4 w-12 bg-gray-200 dark:bg-gray-800 rounded ml-auto"></div></td>
                        <td className="px-6 py-4"><div className="h-4 w-12 bg-gray-200 dark:bg-gray-800 rounded ml-auto"></div></td>
                        <td className="px-6 py-4"><div className="h-4 w-12 bg-gray-200 dark:bg-gray-800 rounded ml-auto"></div></td>
                        <td className="px-6 py-4"><div className="h-6 w-10 bg-gray-200 dark:bg-gray-800 rounded mx-auto"></div></td>
                      </tr>
                    ))
                  ) : (
                    sortedData.map((stock) => (
                      <tr
                        key={stock.symbol}
                        className="group transition-colors hover:bg-white/40 dark:hover:bg-white/5"
                      >
                        <td className="px-6 py-4">
                          <div className="flex items-center gap-2">
                            <span className="font-semibold text-[15px]">{stock.symbol}</span>
                            {stock.is_20d_breakout && (
                              <span className="px-1.5 py-0.5 rounded text-[10px] font-bold bg-green-500/20 text-green-600 dark:text-green-400">BO</span>
                            )}
                          </div>
                        </td>
                        <td className="px-6 py-4 text-right font-sans tabular-nums tracking-tight text-sm opacity-90">
                          {stock.close.toLocaleString('en-IN', { style: 'currency', currency: 'INR' })}
                        </td>
                        <td className="px-6 py-4 text-right font-sans tabular-nums tracking-tight text-sm opacity-50">
                          {/* Empty until updated tomorrow */}
                          -
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
                        <td className="px-6 py-4 text-center">
                          <div className={`
                            inline-flex items-center justify-center w-12 h-7 rounded-lg text-sm font-bold
                            ${stock.score >= 80 ? 'bg-green-500 text-white shadow-lg shadow-green-500/30' :
                              stock.score >= 60 ? 'bg-yellow-500 text-white shadow-lg shadow-yellow-500/30' :
                                'bg-gray-200 dark:bg-gray-800 opacity-60'}
                          `}>
                            {stock.score.toFixed(0)}
                          </div>
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
