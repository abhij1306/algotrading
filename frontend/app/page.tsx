'use client'

import { useState, useEffect } from 'react'
import SkeletonTable from '@/components/SkeletonTable'
import { useSearchParams } from 'next/navigation'
import { useKeyboardShortcuts } from '@/hooks/useKeyboardShortcuts'
import ScreenerTable from '@/components/ScreenerTable'
import BacktestHUD from '@/components/BacktestHUD'
import PortfolioTab from '@/components/PortfolioTab'
import AIAssistant from '@/components/AIAssistant'
import Terminal from '@/components/Terminal'
import ZeroStateScreener from '@/components/ZeroStateScreener'
import { LayoutGrid, PieChart, PlayCircle, Zap, Search, Filter, Layers, RefreshCw, ChevronLeft, ChevronRight } from 'lucide-react'

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
}

type MainTab = 'screener' | 'pf_analysis' | 'backtest' | 'execution'

export default function RaycastPage() {
  // Global keyboard shortcuts
  useKeyboardShortcuts()

  // State
  const [mainTab, setMainTab] = useState<MainTab>('screener')

  // URL Routing
  const searchParams = useSearchParams()
  const view = searchParams.get('view')

  useEffect(() => {
    if (view === 'screener') setMainTab('screener')
    if (view === 'analyst') setMainTab('pf_analysis')
    if (view === 'tester') setMainTab('backtest')
    if (view === 'trader') setMainTab('execution')
  }, [view])
  const [stocks, setStocks] = useState<Stock[]>([])
  const [loading, setLoading] = useState(false)

  // Filters
  const [selectedSymbol, setSelectedSymbol] = useState('')
  const [selectedSector, setSelectedSector] = useState('all')
  const [scannerFilter, setScannerFilter] = useState('ALL')
  const [availableSectors, setAvailableSectors] = useState<string[]>([])

  // Pagination
  const [page, setPage] = useState(1)
  const [limit] = useState(50)
  const [totalRecords, setTotalRecords] = useState(0)

  // Fetch sectors
  useEffect(() => {
    fetch('http://localhost:8000/api/sectors')
      .then(res => res.json())
      .then(data => setAvailableSectors(data.sectors || []))
      .catch(err => console.error('Failed to fetch sectors:', err))
  }, [])

  // Fetch data
  const fetchData = async () => {
    setLoading(true)
    try {
      let endpoint = '/api/screener'
      if (scannerFilter !== 'ALL') {
        endpoint = '/api/screener/trending'
      }

      let url = `http://localhost:8000${endpoint}?page=${page}&limit=${limit}`

      if (scannerFilter !== 'ALL') {
        url += `&filter_type=${scannerFilter}`
      }
      if (selectedSymbol) {
        url += `&symbol=${selectedSymbol}`
      }
      if (selectedSector && selectedSector !== 'all') {
        url += `&sector=${encodeURIComponent(selectedSector)}`
      }

      // 1. CACHE CHECK (Stale-while-revalidate)
      const cacheKey = `screener_cache_${endpoint}_${page}_${limit}_${selectedSymbol}_${selectedSector}_${scannerFilter}`
      const cached = localStorage.getItem(cacheKey)
      if (cached) {
        try {
          const parsed = JSON.parse(cached)
          setStocks(parsed.data || parsed) // Handle {data: [], meta: ...} vs []
          if (parsed.meta) {
            setTotalRecords(parsed.meta.total || 0)
          }
          setLoading(false) // Show cached immediately
        } catch (e) {
          console.error("Cache parse error", e)
        }
      }

      // 2. NETWORK FETCH
      const res = await fetch(url)
      const json = await res.json()

      if (json.data) {
        setStocks(json.data)
        if (json.meta) {
          setTotalRecords(json.meta.total || 0)
        }
        // Update cache
        localStorage.setItem(cacheKey, JSON.stringify(json))
      } else if (Array.isArray(json)) {
        // Fallback if API returns array directly
        setStocks(json)
        localStorage.setItem(cacheKey, JSON.stringify(json))
      }

    } catch (e) {
      console.error(e)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    if (mainTab === 'screener') {
      fetchData()
    }
  }, [mainTab, page, selectedSymbol, selectedSector, scannerFilter])

  const tabs = [
    { id: 'screener', label: 'SCREENER', icon: LayoutGrid, role: 'Data Foundation (Deterministic)' },
    { id: 'pf_analysis', label: 'ANALYST', icon: PieChart, role: 'Insight & Risk (Multi-Agent)' },
    { id: 'backtest', label: 'TESTER', icon: PlayCircle, role: 'Optimization Lab (Simulation)' },
    { id: 'execution', label: 'TRADER', icon: Zap, role: 'Self-Learning Execution' },
  ]

  const activeAgent = tabs.find(t => t.id === mainTab)

  return (
    <div className="h-screen bg-[#050505] text-gray-200 flex flex-col overflow-hidden font-sans selection:bg-cyan-500/30 selection:text-cyan-200">

      {/* ============================================================ */}
      {/* ULTRA-COMPACT HEADER (32px height equivalent)                */}
      {/* ============================================================ */}
      <nav className="h-14 border-b border-white/5 bg-[#080808]/80 backdrop-blur-md flex items-center px-4 justify-between shrink-0 z-50">

        {/* Creative Logo */}
        <div className="flex items-center gap-3 group cursor-default">
          <div className="relative w-9 h-9 flex items-center justify-center transition-transform group-hover:scale-105 duration-500">
            <div className="absolute inset-0 bg-gradient-to-tr from-cyan-600/30 to-purple-600/30 rounded-xl blur-md group-hover:blur-lg transition-all"></div>
            <div className="absolute inset-0 border border-white/10 rounded-xl bg-[#0A0A0A]/80 backdrop-blur-xl"></div>

            {/* Animated Gradient Border */}
            <div className="absolute inset-0 rounded-xl bg-gradient-to-r from-cyan-500/20 to-purple-500/20 opacity-0 group-hover:opacity-100 transition-opacity duration-700"></div>

            <div className="relative font-black tracking-tighter text-transparent bg-clip-text bg-gradient-to-tr from-cyan-400 to-purple-400 text-sm">
              ST
            </div>

            {/* Tech Accents */}
            <div className="absolute top-0 right-0 w-1.5 h-1.5 bg-cyan-400 rounded-full shadow-[0_0_8px_rgba(34,211,238,0.8)]"></div>
            <div className="absolute bottom-0 left-0 w-1 h-1 bg-purple-500 rounded-full opacity-50"></div>
          </div>

          <div className="h-8 w-[1px] bg-white/5 mx-1"></div>

          <div className="flex flex-col justify-center">
            <span className="font-bold text-sm tracking-widest text-white leading-none">
              SMAR<span className="text-cyan-400">TRADER</span>
            </span>
            <span className="text-[9px] text-gray-500 font-mono tracking-[0.2em] uppercase mt-0.5 flex items-center gap-1.5">
              AI NATIVE <div className="w-1 h-1 rounded-full bg-green-500 shadow-[0_0_5px_rgba(34,197,94,0.6)] animate-pulse"></div>
            </span>
          </div>
        </div>

        {/* Floating Tab Bar */}
        <div className="flex items-center bg-white/5 rounded-full p-1 border border-white/5">
          {tabs.map((tab) => (
            <button
              key={tab.id}
              onClick={() => setMainTab(tab.id as MainTab)}
              className={`relative px-4 py-1.5 rounded-full text-xs font-semibold transition-all duration-300 flex items-center gap-2 ${mainTab === tab.id
                ? 'text-white shadow-[0_0_20px_rgba(34,211,238,0.15)] bg-white/5'
                : 'text-gray-500 hover:text-gray-300 hover:bg-white/5'
                }`}
            >
              {mainTab === tab.id && (
                <div className="absolute inset-0 rounded-full bg-gradient-to-r from-cyan-500/10 to-purple-500/10 border border-white/5"></div>
              )}
              <tab.icon className={`w-3.5 h-3.5 ${mainTab === tab.id ? 'text-cyan-400' : ''}`} />
              <span className="relative z-10">{tab.label}</span>
            </button>
          ))}
        </div>

        {/* Agent Identity & Status */}
        <div className="flex items-center gap-4">
          <div className="flex flex-col items-end mr-2 w-64">
            <span className="text-[10px] text-gray-500 font-bold uppercase tracking-wider">Active Agent</span>
            <span className="text-xs text-cyan-400 font-mono truncate">{activeAgent?.role}</span>
          </div>

          <div className="h-8 w-[1px] bg-white/10"></div>

          <div className="w-8 h-8 rounded-full bg-gradient-to-tr from-gray-800 to-gray-900 border border-white/10 flex items-center justify-center text-xs font-bold text-gray-400 hover:text-white hover:border-white/30 transition-all cursor-pointer">
            AB
          </div>
        </div>
      </nav>

      {/* ============================================================ */}
      {/* MAIN CONTENT AREA */}
      {/* ============================================================ */}
      <main className="flex-1 overflow-hidden relative flex flex-col p-6">

        {/* TAB 1: SCREENER (Restored Raycast Style) */}
        {mainTab === 'screener' && (
          <div className="h-full flex flex-col gap-4">

            {/* Control Bar (Single Row, Floating) */}
            <div className="glass-subtle rounded-xl border border-white/5 flex items-center px-4 py-2 gap-3 bg-[#080808]/80 backdrop-blur-md shrink-0 shadow-lg">

              {/* Search (Autocomplete) */}
              <div className="relative group w-64 z-20">
                <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-500 group-focus-within:text-cyan-400 transition-colors pointer-events-none" />
                <input
                  type="text"
                  placeholder="Search symbols..."
                  value={selectedSymbol}
                  onChange={(e) => setSelectedSymbol(e.target.value.toUpperCase())}
                  onFocus={() => { /* Open dropdown logic if needed, currently simplistic */ }}
                  className="w-full h-8 pl-9 pr-3 rounded-lg bg-[#0A0A0A] border border-white/10 text-xs text-white focus:outline-none focus:border-cyan-500/50 focus:ring-1 focus:ring-cyan-500/20 transition-all placeholder:text-gray-600 uppercase"
                />
              </div>

              <div className="h-4 w-[1px] bg-white/10"></div>

              {/* Filters in one row */}
              <div className="flex items-center gap-2 flex-1">
                <div className="relative">
                  <Filter className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-gray-500" />
                  <select
                    value={selectedSector}
                    onChange={(e) => setSelectedSector(e.target.value)}
                    className="h-8 pl-8 pr-8 rounded-lg bg-[#0A0A0A] border border-white/10 text-xs text-gray-300 focus:outline-none focus:border-cyan-500/50 appearance-none hover:bg-white/5 cursor-pointer min-w-[140px]"
                  >
                    <option value="all" className="bg-[#0A0A0A] text-gray-300">All Sectors</option>
                    {availableSectors.map((sector) => (
                      <option key={sector} value={sector} className="bg-[#0A0A0A] text-gray-300">{sector}</option>
                    ))}
                  </select>
                </div>

                <div className="relative">
                  <Layers className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-gray-500" />
                  <select
                    value={scannerFilter}
                    onChange={(e) => setScannerFilter(e.target.value)}
                    className="h-8 pl-8 pr-8 rounded-lg bg-[#0A0A0A] border border-white/10 text-xs text-gray-300 focus:outline-none focus:border-cyan-500/50 appearance-none hover:bg-white/5 cursor-pointer min-w-[160px]"
                  >
                    <option value="ALL" className="bg-[#0A0A0A] text-gray-300">All Stocks</option>
                    <option value="VOLUME_SHOCKER" className="bg-[#0A0A0A] text-gray-300">🔥 Volume Shockers</option>
                    <option value="PRICE_SHOCKER" className="bg-[#0A0A0A] text-gray-300">🚀 Price Shockers</option>
                    <option value="52W_HIGH" className="bg-[#0A0A0A] text-gray-300">📈 52 Week High</option>
                    <option value="52W_LOW" className="bg-[#0A0A0A] text-gray-300">📉 52 Week Low</option>
                  </select>
                </div>

                <button
                  onClick={() => { setSelectedSymbol(''); setSelectedSector('all'); setScannerFilter('ALL'); }}
                  className="h-8 w-8 flex items-center justify-center rounded-lg bg-[#0A0A0A] border border-white/10 text-gray-500 hover:text-white hover:border-white/30 transition-all"
                  title="Reset Filters"
                >
                  <RefreshCw className="w-3.5 h-3.5" />
                </button>
              </div>


            </div>

            {/* Content Area - Raycast Style Glass Container */}
            <div className="flex-1 glass rounded-xl overflow-hidden relative border border-white/5 bg-[#050505]/50 backdrop-blur-sm shadow-2xl flex flex-col">
              {/* SCREENER TABLE or SKELETON */}
              <div className="flex-1 min-h-0 relative">
                {loading && stocks.length === 0 ? (
                  <SkeletonTable rows={15} />
                ) : stocks.length > 0 ? (
                  <ScreenerTable data={stocks} type="intraday" />
                ) : (
                  <ZeroStateScreener />
                )}
              </div>

              {/* Bottom Bar: Pagination & Stats */}
              <div className="h-12 border-t border-white/5 flex items-center justify-between px-4 bg-white/2">
                <div className="text-xs text-gray-500 font-mono">
                  Showing {stocks.length} results
                </div>

                {/* Pagination Controls moved here */}
                <div className="flex items-center gap-2">
                  <button
                    onClick={() => setPage(p => Math.max(1, p - 1))}
                    disabled={page === 1}
                    className="h-8 w-8 flex items-center justify-center rounded-lg bg-[#0A0A0A] border border-white/10 text-gray-400 hover:text-white disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                  >
                    <ChevronLeft className="w-4 h-4" />
                  </button>
                  <span className="text-xs font-mono text-gray-400 min-w-[60px] text-center bg-[#0A0A0A] py-1.5 rounded-md border border-white/5">
                    PAGE {page}
                  </span>
                  <button
                    onClick={() => setPage(p => p + 1)}
                    disabled={stocks.length < limit}
                    className="h-8 w-8 flex items-center justify-center rounded-lg bg-[#0A0A0A] border border-white/10 text-gray-400 hover:text-white disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                  >
                    <ChevronRight className="w-4 h-4" />
                  </button>
                </div>
              </div>
            </div>
          </div>
        )}

        {/* TABS 2, 3, 4: Keep mounted, hide with CSS */}
        <div className={`h-full w-full glass rounded-xl overflow-hidden border border-white/5 bg-[#050505]/50 backdrop-blur-sm shadow-2xl p-6 ${mainTab === 'pf_analysis' ? '' : 'hidden'}`}>
          <PortfolioTab />
        </div>

        <div className={`h-full w-full glass rounded-xl overflow-hidden border border-white/5 bg-[#050505]/50 backdrop-blur-sm shadow-2xl p-6 ${mainTab === 'backtest' ? '' : 'hidden'}`}>
          <BacktestHUD />
        </div>

        <div className={`h-full w-full glass rounded-xl overflow-hidden border border-white/5 bg-[#050505]/50 backdrop-blur-sm shadow-2xl ${mainTab === 'execution' ? '' : 'hidden'}`}>
          <Terminal />
        </div>

      </main>

      {/* FIXED AI ASSISTANT OVERLAY */}
      <AIAssistant />
    </div>
  )
}
