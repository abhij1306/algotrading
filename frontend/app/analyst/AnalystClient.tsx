'use client'

import { useState, useEffect, useRef } from 'react'
import { Plus, Trash2, Loader2, Search, BarChart3, PieChart, Briefcase } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { GlassSelect } from '@/components/ui/GlassSelect'
import Portal from '@/components/ui/Portal'
import { useErrorToast } from '@/components/Toast'

interface DraftPosition {
  symbol: string
  quantity: number
  avg_buy_price: number
  invested_value: number
}

interface SearchResult {
  symbol: string
  name?: string
}

export default function AnalystClient() {
  const [activeTab, setActiveTab] = useState<'PORTFOLIO' | 'BACKTEST'>('PORTFOLIO')
  const [portfolios, setPortfolios] = useState<Array<{id: number; portfolio_name: string; positions?: Array<{symbol: string}>}>>([])
  const [selectedPortfolioId, setSelectedPortfolioId] = useState<number | null>(null)
  const [loading, setLoading] = useState(true)

  // Draft portfolio
  const [portfolioName, setPortfolioName] = useState('')
  const [description, setDescription] = useState('')
  const [draftPositions, setDraftPositions] = useState<DraftPosition[]>([])

  // Search
  const [searchQuery, setSearchQuery] = useState('')
  const [searchResults, setSearchResults] = useState<SearchResult[]>([])
  const [currentQty, setCurrentQty] = useState('100')
  const [currentPrice, setCurrentPrice] = useState('')
  const [creating, setCreating] = useState(false)

  const searchInputRef = useRef<HTMLInputElement>(null)
  const skipSearch = useRef(false)

  const toast = useErrorToast()

  useEffect(() => {
    fetchPortfolios()
  }, [])

  const fetchPortfolios = async () => {
    try {
      const res = await fetch('/api/portfolio/stocks/')
      if (res.ok) {
        const data = await res.json()
        setPortfolios(data.portfolios || [])
      }
    } catch {
      console.error('Failed to fetch portfolios')
    } finally {
      setLoading(false)
    }
  }

  const handleSearch = async (query: string) => {
    if (query.length < 1) {
      setSearchResults([])
      return
    }
    try {
      const res = await fetch(`/api/market/search?query=${encodeURIComponent(query)}&exclude_indices=true`)
      if (res.ok) {
        const data = await res.json()
        setSearchResults(Array.isArray(data) ? data : [])
      }
    } catch {
      setSearchResults([])
    }
  }

  useEffect(() => {
    const timer = setTimeout(() => {
      if (skipSearch.current) {
        skipSearch.current = false
        return
      }
      if (searchQuery) handleSearch(searchQuery)
    }, 300)
    return () => clearTimeout(timer)
  }, [searchQuery])

  const addDraftPosition = (symbol: string, price?: number) => {
    const qty = parseFloat(currentQty) || 0
    const avgPrice = price || parseFloat(currentPrice) || 0
    if (!symbol || qty <= 0 || avgPrice <= 0) return
    const newPos: DraftPosition = {
      symbol: symbol.toUpperCase(),
      quantity: qty,
      avg_buy_price: avgPrice,
      invested_value: qty * avgPrice
    }
    setDraftPositions([newPos, ...draftPositions])
    setSearchQuery('')
    setSearchResults([])
    setCurrentPrice('')
  }

  const removeDraftPosition = (index: number) => {
    setDraftPositions(draftPositions.filter((_, i) => i !== index))
  }

  const handleCreateAndAnalyze = async () => {
    if (!portfolioName.trim() || draftPositions.length === 0) return
    setCreating(true)
    try {
      const res = await fetch('/api/portfolio/stocks/', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          portfolio_name: portfolioName,
          description,
          positions: draftPositions.map(p => ({
            symbol: p.symbol,
            invested_value: p.invested_value,
            quantity: p.quantity,
            avg_buy_price: p.avg_buy_price
          }))
        })
      })
      if (res.ok) {
        const data = await res.json()
        await fetchPortfolios()
        setSelectedPortfolioId(data.id)
        setPortfolioName('')
        setDescription('')
        setDraftPositions([])
      } else {
        const errorText = await res.text()
        toast(errorText || 'Failed to create portfolio')
        return
      }
    } catch (error) {
      toast(error instanceof Error ? error.message : 'Network error occurred')
      return
    } finally {
      setCreating(false)
    }
  }

  const totalInvested = draftPositions.reduce((sum, p) => sum + p.invested_value, 0)

  if (loading) {
    return (
      <div className="h-full w-full flex items-center justify-center">
        <div className="flex flex-col items-center gap-4">
          <Loader2 className="w-10 h-10 text-[var(--color-primary)] animate-spin" />
          <span className="text-sm text-[var(--text-secondary)]">Loading Analyst Hub...</span>
        </div>
      </div>
    )
  }

  return (
    <div className="h-full w-full flex flex-col text-[var(--text-primary)] overflow-hidden bg-[var(--color-base)]">
      {/* Toolbar */}
      <div className="h-14 shrink-0 border-b border-[var(--border-subtle)] bg-[var(--color-surface)] flex items-center px-6">
        <div className="flex items-center bg-[var(--color-elevated)] rounded-lg p-1">
          <button
            onClick={() => setActiveTab('PORTFOLIO')}
            className={`flex items-center gap-2 px-4 py-1.5 rounded-md text-xs font-medium transition ${
              activeTab === 'PORTFOLIO' 
                ? 'bg-[var(--color-primary-bg)] text-[var(--color-primary)]' 
                : 'text-[var(--text-secondary)] hover:text-[var(--text-primary)]'
            }`}
          >
            <PieChart className="w-3.5 h-3.5" />
            Risk Analysis
          </button>
          <button
            onClick={() => setActiveTab('BACKTEST')}
            className={`flex items-center gap-2 px-4 py-1.5 rounded-md text-xs font-medium transition ${
              activeTab === 'BACKTEST' 
                ? 'bg-[var(--color-primary-bg)] text-[var(--color-primary)]' 
                : 'text-[var(--text-secondary)] hover:text-[var(--text-primary)]'
            }`}
          >
            <BarChart3 className="w-3.5 h-3.5" />
            Backtest
          </button>
        </div>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-auto">
        {activeTab === 'BACKTEST' ? (
          <div className="p-6 text-center text-[var(--text-muted)]">Backtest interface coming soon...</div>
        ) : selectedPortfolioId ? (
          <div className="p-6">Portfolio dashboard for ID: {selectedPortfolioId}</div>
        ) : (
          <div className="h-full flex items-center justify-center p-8">
            <div className="w-full max-w-2xl card p-8">
              {/* Header */}
              <div className="flex items-center gap-4 mb-6">
                <div className="w-10 h-10 rounded-lg bg-[var(--color-primary-bg)] flex items-center justify-center">
                  <Briefcase className="w-5 h-5 text-[var(--color-primary)]" />
                </div>
                <div>
                  <h2 className="text-lg font-semibold text-[var(--text-primary)]">New Portfolio</h2>
                  <p className="text-xs text-[var(--text-muted)]">Create a portfolio to analyze risk and performance</p>
                </div>
              </div>

              {/* Form */}
              <div className="space-y-4">
                <div>
                  <label className="block text-xs font-medium text-[var(--text-secondary)] mb-1">Portfolio Name</label>
                  <input
                    type="text"
                    value={portfolioName}
                    onChange={(e) => setPortfolioName(e.target.value)}
                    placeholder="e.g., Tech Growth Q4"
                    className="w-full bg-[var(--color-surface)] border border-[var(--border-default)] rounded-lg px-3 py-2 text-sm text-[var(--text-primary)] placeholder-[var(--text-muted)] focus:outline-none focus:border-[var(--color-primary)]"
                  />
                </div>

                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <label className="block text-xs font-medium text-[var(--text-secondary)] mb-1">Quick Add Position</label>
                    <div className="relative">
                      <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-[var(--text-muted)]" />
                      <input
                        ref={searchInputRef}
                        type="text"
                        value={searchQuery}
                        onChange={(e) => setSearchQuery(e.target.value)}
                        placeholder="Symbol (e.g., RELIANCE)"
                        className="w-full pl-10 pr-3 py-2 bg-[var(--color-surface)] border border-[var(--border-default)] rounded-lg text-sm text-[var(--text-primary)] uppercase placeholder-[var(--text-muted)] focus:outline-none focus:border-[var(--color-primary)]"
                      />
                      {searchResults.length > 0 && (
                        <Portal>
                          <div 
                            className="fixed z-50 bg-[var(--color-surface)] border border-[var(--border-default)] rounded-lg shadow-lg overflow-hidden"
                            style={{
                              top: (searchInputRef.current?.getBoundingClientRect().bottom || 0) + 4,
                              left: searchInputRef.current?.getBoundingClientRect().left || 0,
                              width: searchInputRef.current?.getBoundingClientRect().width || 0,
                            }}
                          >
                            {searchResults.map((s, i) => (
                              <button
                                key={i}
                                onClick={async () => {
                                  skipSearch.current = true
                                  setSearchQuery(s.symbol)
                                  setSearchResults([])
                                  try {
                                    const res = await fetch(`/api/market/quote/${s.symbol}`)
                                    if (res.ok) {
                                      const quote = await res.json()
                                      setCurrentPrice(quote.ltp?.toFixed(2) || '')
                                    }
                                  } catch {}
                                }}
                                className="w-full px-4 py-2 text-left hover:bg-[var(--glass-highlight)] text-sm text-[var(--text-primary)]"
                              >
                                <span className="font-medium">{s.symbol}</span>
                              </button>
                            ))}
                          </div>
                        </Portal>
                      )}
                    </div>
                  </div>
                  <div className="flex gap-2">
                    <div className="flex-1">
                      <label className="block text-xs font-medium text-[var(--text-secondary)] mb-1">Quantity</label>
                      <input
                        type="number"
                        value={currentQty}
                        onChange={(e) => setCurrentQty(e.target.value)}
                        className="w-full bg-[var(--color-surface)] border border-[var(--border-default)] rounded-lg px-3 py-2 text-sm text-[var(--text-primary)] focus:outline-none focus:border-[var(--color-primary)]"
                      />
                    </div>
                    <div className="flex-1">
                      <label className="block text-xs font-medium text-[var(--text-secondary)] mb-1">Price</label>
                      <input
                        type="number"
                        value={currentPrice}
                        onChange={(e) => setCurrentPrice(e.target.value)}
                        className="w-full bg-[var(--color-surface)] border border-[var(--border-default)] rounded-lg px-3 py-2 text-sm text-[var(--text-primary)] focus:outline-none focus:border-[var(--color-primary)]"
                      />
                    </div>
                    <Button variant="primary" onClick={() => addDraftPosition(searchQuery)} className="mt-6">
                      <Plus className="w-4 h-4" />
                    </Button>
                  </div>
                </div>

                {/* Draft Positions */}
                {draftPositions.length > 0 && (
                  <div className="border border-[var(--border-subtle)] rounded-lg overflow-hidden">
                    <div className="px-4 py-2 bg-[var(--color-surface)] border-b border-[var(--border-subtle)] flex justify-between items-center">
                      <span className="text-xs font-medium text-[var(--text-secondary)]">Positions</span>
                      <span className="text-sm font-mono text-[var(--color-primary)]">₹{totalInvested.toLocaleString()}</span>
                    </div>
                    <div className="max-h-40 overflow-auto">
                      {draftPositions.map((p, i) => (
                        <div key={i} className="px-4 py-2 border-b border-[var(--border-subtle)] last:border-0 flex justify-between items-center hover:bg-[var(--glass-highlight)]">
                          <div>
                            <span className="text-sm font-medium text-[var(--text-primary)]">{p.symbol}</span>
                            <span className="text-xs text-[var(--text-muted)] ml-2">{p.quantity} × ₹{p.avg_buy_price}</span>
                          </div>
                          <div className="flex items-center gap-4">
                            <span className="text-sm font-mono text-[var(--text-primary)]">₹{p.invested_value.toLocaleString()}</span>
                            <button onClick={() => removeDraftPosition(i)} className="text-[var(--text-muted)] hover:text-[var(--color-loss)]">
                              <Trash2 className="w-4 h-4" />
                            </button>
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>

              {/* Actions */}
              <div className="flex justify-end gap-3 mt-6">
                <Button variant="ghost" onClick={() => { setPortfolioName(''); setDraftPositions([]); }}>
                  Clear
                </Button>
                <Button 
                  variant="primary" 
                  onClick={handleCreateAndAnalyze} 
                  disabled={creating || !portfolioName.trim() || draftPositions.length === 0}
                >
                  {creating ? <Loader2 className="w-4 h-4 animate-spin" /> : <BarChart3 className="w-4 h-4" />}
                  Run Analysis
                </Button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
