'use client'

import { useState, useEffect } from 'react'
import SkeletonTable from '@/components/SkeletonTable'
import { useSearchParams } from 'next/navigation'
import ScreenerTable from '@/components/ScreenerTable'
import ZeroStateScreener from '@/components/ZeroStateScreener'
import { Search, Filter, Layers, RefreshCw, ChevronLeft, ChevronRight, AlertTriangle } from 'lucide-react'

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
    // Advanced Indicators
    macd: number
    macd_signal: number
    adx: number
    stoch_k: number
    stoch_d: number
    bb_upper: number
    bb_middle: number
    bb_lower: number
    net_income?: number
    eps?: number
    roe?: number
    debt_to_equity?: number
    // Financials
    market_cap?: number
    pe_ratio?: number
    revenue?: number
}

export default function ScreenerPage() {
    const [stocks, setStocks] = useState<Stock[]>([])
    const [loading, setLoading] = useState(false)
    const [error, setError] = useState<string | null>(null) // New Error State

    // Filters
    const [selectedIndex, setSelectedIndex] = useState('NIFTY50')
    const [selectedSymbol, setSelectedSymbol] = useState('')
    const [debouncedSymbol, setDebouncedSymbol] = useState('') // Debounced value for API calls
    const [selectedSector, setSelectedSector] = useState('all')
    const [scannerFilter, setScannerFilter] = useState('ALL')

    // Debounce Effect
    useEffect(() => {
        const handler = setTimeout(() => {
            setDebouncedSymbol(selectedSymbol)
        }, 500)
        return () => clearTimeout(handler)
    }, [selectedSymbol])

    // Autocomplete State
    const [searchResults, setSearchResults] = useState<{ symbol: string, name: string, sector: string }[]>([])
    const [showDropdown, setShowDropdown] = useState(false)
    const [isSearching, setIsSearching] = useState(false)

    // Autocomplete Effect
    useEffect(() => {
        const fetchSearch = async () => {
            if (!selectedSymbol || selectedSymbol.length < 2) {
                setSearchResults([]);
                setShowDropdown(false);
                return;
            }

            setIsSearching(true);
            try {
                const res = await fetch(`http://localhost:8000/api/market/search?query=${selectedSymbol}`)
                if (res.ok) {
                    const text = await res.text()
                    try {
                        const data = JSON.parse(text)
                        if (Array.isArray(data)) {
                            setSearchResults(data)
                            setShowDropdown(true)
                        }
                    } catch {
                        console.warn("Search returned invalid JSON", text.substring(0, 50))
                    }
                }
            } catch (e) {
                console.error("Search failed", e)
            } finally {
                setIsSearching(false)
            }
        }
        const timeout = setTimeout(fetchSearch, 300)
        return () => clearTimeout(timeout)
    }, [selectedSymbol])

    const [availableIndices, setAvailableIndices] = useState<any[]>([
        { id: 'NIFTY50', name: 'NIFTY 50' },
        { id: 'NIFTY100', name: 'NIFTY 100' },
        { id: 'NIFTY200', name: 'NIFTY 200' },
        { id: 'NIFTY500', name: 'NIFTY 500' },
        { id: 'BANKNIFTY', name: 'BANK NIFTY' },
        { id: 'NIFTYMIDCAP', name: 'NIFTY MIDCAP' },
        { id: 'NIFTYSMALLCAP', name: 'NIFTY SMALLCAP' },
        { id: 'ALL', name: 'All Stocks (Slow)' }
    ])
    const [availableSectors, setAvailableSectors] = useState<string[]>([])
    const [viewMode, setViewMode] = useState<'technical' | 'financial'>('technical')

    // Pagination
    const [page, setPage] = useState(1)
    const [limit] = useState(50)
    const [totalRecords, setTotalRecords] = useState(0)

    // Fetch indices (With Fallback)
    useEffect(() => {
        fetch('http://localhost:8000/api/screener/indices')
            .then(res => {
                if (!res.ok) throw new Error("Indices API failed");
                return res.text();
            })
            .then(text => {
                try {
                    const data = JSON.parse(text);
                    if (data.indices) {
                        setAvailableIndices(data.indices)
                        setSelectedIndex(data.default || 'NIFTY50')
                    }
                } catch {
                    console.warn("Invalid Indices JSON, using defaults");
                }
            })
            .catch(err => console.error('Failed to fetch indices, using defaults:', err))
    }, [])

    // Fetch sectors (With Fallback)
    useEffect(() => {
        fetch('http://localhost:8000/api/market/sectors')
            .then(res => res.ok ? res.json() : [])
            .then(data => {
                if (data.sectors) setAvailableSectors(data.sectors)
            })
            .catch(err => console.error('Failed to fetch sectors:', err))
    }, [])

    // Fetch data
    const fetchData = async (silent = false) => {
        if (!silent) {
            setLoading(true)
            setError(null)
        }

        try {
            let endpoint = '/api/screener/'
            let url = `http://localhost:8000${endpoint}?page=${page}&limit=${limit}&index=${selectedIndex || 'ALL'}`

            if (scannerFilter !== 'ALL') url += `&filter_type=${scannerFilter}`
            if (debouncedSymbol) url += `&symbol=${debouncedSymbol}`
            if (selectedSector && selectedSector !== 'all') url += `&sector=${encodeURIComponent(selectedSector)}`
            if (viewMode === 'financial') url += `&view=financial`

            // Skip cache for live updates
            if (!silent) {
                const cacheKey = `screener_cache_${endpoint}_${page}_${limit}_${debouncedSymbol}_${selectedSector}_${scannerFilter}_${viewMode}`
                const cached = localStorage.getItem(cacheKey)
                if (cached) {
                    try {
                        const parsed = JSON.parse(cached)
                        setStocks(parsed.data || parsed) // Handle both list and object format
                        if (parsed.meta) setTotalRecords(parsed.meta.total || 0)
                        setLoading(false)
                    } catch (e) {
                        // Cache error ignored
                    }
                }
            }

            const res = await fetch(url)
            if (!res.ok) {
                // Determine if it was a 404 (Screener router missing) or 500
                const errText = await res.text();
                // If it's HTML, throw a specialized error
                if (errText.trim().startsWith('<')) {
                    throw new Error(`Backend API Error (${res.status}): Router unavailable or server crashed.`)
                }
                throw new Error(`API Error: ${res.status} ${res.statusText}`)
            }

            const text = await res.text()
            let json;
            try {
                json = JSON.parse(text)
            } catch {
                throw new Error("Invalid structure returned from backend")
            }

            let cacheKey = `screener_cache_${endpoint}_${page}_${limit}_${debouncedSymbol}_${selectedSector}_${scannerFilter}_${viewMode}`
            if (json.data) {
                setStocks(json.data)
                if (json.meta) setTotalRecords(json.meta.total || 0)
                localStorage.setItem(cacheKey, JSON.stringify(json))
            } else if (Array.isArray(json)) {
                setStocks(json)
                localStorage.setItem(cacheKey, JSON.stringify(json))
            } else {
                setStocks([])
            }

        } catch (e: any) {
            console.error(e)
            if (!silent) {
                setError(e.message || "Failed to load screener data")
                setStocks([])
            }
        } finally {
            if (!silent) setLoading(false)
        }
    }

    useEffect(() => {
        fetchData()
        const interval = setInterval(() => fetchData(true), 5000) // 5s poll
        return () => clearInterval(interval)
    }, [page, selectedIndex, debouncedSymbol, selectedSector, scannerFilter, viewMode])


    return (
        <div className="h-full flex flex-col gap-4 p-6">
            {/* Control Bar */}
            <div className="glass-subtle rounded-xl border border-white/5 flex items-center px-4 py-2 gap-3 bg-[#080808]/90 backdrop-blur-md shrink-0 shadow-lg z-50">

                {/* Search Bar - FIXED CSS */}
                <div className="relative group w-64 z-[60]">
                    <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400 group-focus-within:text-cyan-400 transition-colors pointer-events-none" />
                    <input
                        type="text"
                        placeholder="SEARCH SYMBOL..."
                        value={selectedSymbol}
                        onChange={(e) => {
                            setSelectedSymbol(e.target.value.toUpperCase());
                            setShowDropdown(true);
                        }}
                        onFocus={() => setShowDropdown(true)}
                        onBlur={() => setTimeout(() => setShowDropdown(false), 200)}
                        className="w-full h-9 pl-10 pr-3 rounded-lg bg-[#151515] border border-white/10 text-sm text-white focus:outline-none focus:border-cyan-500/50 focus:ring-1 focus:ring-cyan-500/20 transition-all placeholder:text-gray-600 font-medium tracking-wide uppercase shadow-inner"
                    />

                    {/* Autocomplete Dropdown */}
                    {showDropdown && (
                        <div className="absolute top-full left-0 w-full mt-1 bg-[#1A1A1A] border border-white/10 rounded-lg shadow-2xl z-[100] overflow-hidden max-h-60 overflow-y-auto ring-1 ring-white/5">
                            {isSearching ? (
                                <div className="px-3 py-2 text-xs text-gray-500 italic">Searching...</div>
                            ) : searchResults.length > 0 ? (
                                searchResults.map((result) => (
                                    <div
                                        key={result.symbol}
                                        className="px-3 py-2.5 hover:bg-cyan-500/10 cursor-pointer flex justify-between items-center group border-b border-white/5 last:border-0 transition-colors"
                                        onClick={() => {
                                            setSelectedSymbol(result.symbol);
                                            setShowDropdown(false);
                                        }}
                                    >
                                        <div className="flex flex-col overflow-hidden">
                                            <span className="text-sm font-bold text-gray-200 group-hover:text-cyan-400 transition-colors">{result.symbol}</span>
                                            <span className="text-[10px] text-gray-500 truncate max-w-[140px] uppercase">{result.name}</span>
                                        </div>
                                        <span className="text-[9px] font-mono text-gray-600 group-hover:text-cyan-500/50 uppercase border border-white/5 px-1 rounded">{result.sector?.substring(0, 10)}</span>
                                    </div>
                                ))
                            ) : selectedSymbol.length > 1 ? (
                                <div className="px-3 py-2 text-xs text-gray-500">No results found</div>
                            ) : null}
                        </div>
                    )}
                </div>

                <div className="h-6 w-[1px] bg-white/10"></div>

                {/* Filters */}
                <div className="flex items-center gap-2 flex-1">
                    {/* Indices Filter */}
                    <div className="relative">
                        <Layers className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-cyan-500" />
                        <select
                            value={selectedIndex}
                            onChange={(e) => setSelectedIndex(e.target.value)}
                            className="h-8 pl-8 pr-8 rounded-lg bg-[#151515] border border-cyan-500/30 text-xs text-cyan-400 font-medium focus:outline-none focus:border-cyan-500/50 appearance-none hover:bg-cyan-500/5 cursor-pointer min-w-[140px]"
                        >
                            {availableIndices.map((index) => (
                                <option key={index.id} value={index.id} className="bg-[#151515] text-gray-300">
                                    {index.name}
                                </option>
                            ))}
                        </select>
                    </div>

                    <div className="relative">
                        <Filter className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-gray-500" />
                        <select
                            value={selectedSector}
                            onChange={(e) => setSelectedSector(e.target.value)}
                            className="h-8 pl-8 pr-8 rounded-lg bg-[#151515] border border-white/10 text-xs text-gray-300 focus:outline-none focus:border-cyan-500/50 appearance-none hover:bg-white/5 cursor-pointer min-w-[140px]"
                        >
                            <option value="all" className="bg-[#151515] text-gray-300">All Sectors</option>
                            {availableSectors.map((sector) => (
                                <option key={sector} value={sector} className="bg-[#151515] text-gray-300">{sector}</option>
                            ))}
                        </select>
                    </div>

                    <div className="relative">
                        <Layers className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-gray-500" />
                        <select
                            value={scannerFilter}
                            onChange={(e) => setScannerFilter(e.target.value)}
                            className="h-8 pl-8 pr-8 rounded-lg bg-[#151515] border border-white/10 text-xs text-gray-300 focus:outline-none focus:border-cyan-500/50 appearance-none hover:bg-white/5 cursor-pointer min-w-[160px]"
                        >
                            <option value="ALL" className="bg-[#151515] text-gray-300">All Stocks</option>
                            <option value="VOLUME_SHOCKER" className="bg-[#151515] text-gray-300">🔥 Volume Shockers</option>
                            <option value="PRICE_SHOCKER" className="bg-[#151515] text-gray-300">🚀 Price Shockers</option>
                            <option value="52W_HIGH" className="bg-[#151515] text-gray-300">📈 52 Week High</option>
                            <option value="52W_LOW" className="bg-[#151515] text-gray-300">📉 52 Week Low</option>
                        </select>
                    </div>

                    <button
                        onClick={() => { setSelectedSymbol(''); setSelectedSector('all'); setScannerFilter('ALL'); fetchData(); }}
                        className="h-8 w-8 flex items-center justify-center rounded-lg bg-[#151515] border border-white/10 text-gray-500 hover:text-white hover:border-white/30 transition-all hover:bg-white/5 hover:scale-105 active:scale-95"
                        title="Reset Filters"
                    >
                        <RefreshCw className="w-3.5 h-3.5" />
                    </button>
                </div>

                <div className="h-6 w-[1px] bg-white/10"></div>

                {/* View Toggle */}
                <div className="flex bg-[#151515] rounded-lg p-0.5 border border-white/10">
                    <button
                        onClick={() => setViewMode('technical')}
                        className={`px-3 py-1 text-[10px] font-bold rounded-md transition-all ${viewMode === 'technical' ? 'bg-white/10 text-white shadow-sm' : 'text-gray-500 hover:text-gray-300'}`}
                    >
                        TECHNICAL
                    </button>
                    <button
                        onClick={() => setViewMode('financial')}
                        className={`px-3 py-1 text-[10px] font-bold rounded-md transition-all ${viewMode === 'financial' ? 'bg-cyan-500/20 text-cyan-400 shadow-sm' : 'text-gray-500 hover:text-gray-300'}`}
                    >
                        FINANCIAL
                    </button>
                </div>
            </div>

            {/* Table Container */}
            <div className="flex-1 glass rounded-xl overflow-hidden relative border border-white/5 bg-[#050505]/50 backdrop-blur-sm shadow-2xl flex flex-col">
                <div className="flex-1 min-h-0 relative">
                    {loading && stocks.length === 0 ? (
                        <div className="h-full w-full p-4">
                            <SkeletonTable rows={15} />
                        </div>
                    ) : error ? (
                        <div className="absolute inset-0 flex flex-col items-center justify-center text-red-400 gap-2">
                            <AlertTriangle className="w-8 h-8 opacity-50" />
                            <span className="text-sm font-medium bg-red-500/10 px-4 py-2 rounded-lg border border-red-500/20">{error}</span>
                            <button onClick={() => fetchData()} className="text-xs text-gray-500 hover:text-white underline mt-2">Retry</button>
                        </div>
                    ) : stocks.length > 0 ? (
                        <ScreenerTable data={stocks} type="intraday" viewMode={viewMode} />
                    ) : (
                        <ZeroStateScreener />
                    )}
                </div>

                {/* Bottom Bar */}
                <div className="h-12 border-t border-white/5 flex items-center justify-between px-4 bg-white/2">
                    <div className="text-xs text-gray-500 font-mono">
                        Showing {stocks.length} results
                    </div>

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
    )
}
