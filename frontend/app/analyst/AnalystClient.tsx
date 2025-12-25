'use client'

import { useState, useEffect, useRef } from 'react'
import { Plus, Trash2, Loader2, Search, TrendingUp, BarChart3, PieChart, FlaskConical, Briefcase } from 'lucide-react'
import PortfolioRiskDashboard from '@/components/PortfolioRiskDashboard'
import BacktestInterface from '@/components/BacktestInterface'
import Portal from '@/components/ui/Portal'
import EmptyState from '@/components/ui/EmptyState'
import { GlassCard } from "@/components/ui/GlassCard"
import { GlassSelect } from "@/components/ui/GlassSelect"
import { Button, Input, Label, Badge, Heading, Text, Data } from '@/components/design-system/atoms'

interface DraftPosition {
    symbol: string;
    quantity: number;
    avg_buy_price: number;
    invested_value: number;
}

export default function AnalystClient() {
    // Top Tabs
    const [activeTab, setActiveTab] = useState<'PORTFOLIO' | 'BACKTEST'>('PORTFOLIO')

    // Portfolios List
    const [portfolios, setPortfolios] = useState<{ id: number; portfolio_name: string; positions?: any[] }[]>([])
    const [selectedPortfolioId, setSelectedPortfolioId] = useState<number | null>(null)
    const [loading, setLoading] = useState(true)

    // Draft Portfolio State
    const [portfolioName, setPortfolioName] = useState('')
    const [description, setDescription] = useState('')
    const [draftPositions, setDraftPositions] = useState<DraftPosition[]>([])

    // Search & Add State
    const [searchQuery, setSearchQuery] = useState('')
    const [searchResults, setSearchResults] = useState<{ symbol: string; name?: string }[]>([])
    const [currentQty, setCurrentQty] = useState('100')
    const [currentPrice, setCurrentPrice] = useState('')
    const [isSearching, setIsSearching] = useState(false)
    const [creating, setCreating] = useState(false)

    const searchInputRef = useRef<HTMLInputElement>(null)
    const skipSearch = useRef(false)

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
        } catch (error) {
            console.error('Failed to fetch portfolios:', error)
        } finally {
            setLoading(false)
        }
    }

    const handleDeletePortfolio = async (id: number) => {
        if (!confirm("Are you sure you want to delete this portfolio? This cannot be undone.")) return;

        try {
            const res = await fetch(`/api/portfolio/stocks/${id}`, {
                method: 'DELETE'
            });

            if (res.ok) {
                setPortfolios(portfolios.filter(p => p.id !== id));
                if (selectedPortfolioId === id) {
                    setSelectedPortfolioId(null);
                }
            }
        } catch (err) {
            console.error(err);
        }
    }

    // Debounced Search
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

    const handleSearch = async (query: string) => {
        if (query.length < 1) {
            setSearchResults([])
            return
        }
        setIsSearching(true)
        try {
            const res = await fetch(`/api/market/search?query=${query}&exclude_indices=true`)
            if (res.ok) {
                const data = await res.json()
                setSearchResults(data || [])
            }
        } catch (error) {
            console.error('Search error:', error)
        } finally {
            setIsSearching(false)
        }
    }

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
                    description: description,
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
                // Reset local draft
                setPortfolioName('')
                setDescription('')
                setDraftPositions([])
            }
        } catch (error) {
            console.error('Failed to create portfolio:', error)
        } finally {
            setCreating(false)
        }
    }

    const totalInvested = draftPositions.reduce((sum, p) => sum + p.invested_value, 0)

    if (loading) {
        return (
            <div className="h-full w-full bg-deep-space flex items-center justify-center">
                <div className="flex flex-col items-center gap-4">
                    <Loader2 className="w-10 h-10 text-cyan-500 animate-spin" />
                    <Text variant="small" className="tracking-widest uppercase">Initializing Analyst Hub</Text>
                </div>
            </div>
        )
    }

    return (
        <div className="h-full w-full bg-deep-space flex flex-col text-gray-300 font-sans selection:bg-cyan-500/30 selection:text-cyan-100 overflow-hidden">

            {/* ANALYST TOP TOOLBAR */}
            <div className="h-16 shrink-0 border-b border-white/5 bg-[#0A0A0A]/30 flex items-center justify-between px-8 backdrop-blur-sm z-30">

                {/* Left: Mode Switcher */}
                <div className="flex bg-white/5 p-1 rounded-lg border border-white/5 gap-1">
                    <Button
                        onClick={() => setActiveTab('PORTFOLIO')}
                        variant={activeTab === 'PORTFOLIO' ? 'secondary' : 'ghost'}
                        size="sm"
                        icon={<PieChart className="w-3.5 h-3.5" />}
                    >
                        RISK ANALYSIS
                    </Button>
                    <Button
                        onClick={() => setActiveTab('BACKTEST')}
                        variant={activeTab === 'BACKTEST' ? 'secondary' : 'ghost'}
                        size="sm"
                        icon={<FlaskConical className="w-3.5 h-3.5" />}
                    >
                        STRATEGY LAB
                    </Button>
                </div>

                {/* Center/Right: Portfolio Context (Only in Risk Mode) */}
                {activeTab === 'PORTFOLIO' && (
                    <div className="flex items-center gap-4">
                        <div className="relative group flex items-center gap-3">
                            {/* Portfolio Selector (Glass Select) */}
                            <Label size="sm" className="hidden sm:block uppercase tracking-widest text-[#666]">Active Portfolio:</Label>
                            <div className="min-w-[240px]">
                                <GlassSelect
                                    options={portfolios.map(p => ({
                                        value: p.id,
                                        label: `${p.portfolio_name} (${p.positions?.length || 0} Assets)`
                                    }))}
                                    value={selectedPortfolioId}
                                    onChange={(val) => setSelectedPortfolioId(val)}
                                    placeholder="-- Select Portfolio --"
                                    className="w-full"
                                />
                            </div>
                        </div>

                        {(selectedPortfolioId || portfolios.length > 0) && (
                            <Button
                                onClick={() => setSelectedPortfolioId(null)}
                                variant="primary"
                                size="sm"
                                icon={<Plus className="w-3 h-3" />}
                                className="ml-2"
                            >
                                NEW
                            </Button>
                        )}
                    </div>
                )}
            </div>

            {/* MAIN CONTENT AREA */}
            <div className="flex-1 overflow-auto relative custom-scrollbar">
                {activeTab === 'BACKTEST' ? (
                    <BacktestInterface />
                ) : (
                    selectedPortfolioId ? (
                        <div className="h-full animate-in fade-in duration-300">
                            <PortfolioRiskDashboard
                                portfolioId={selectedPortfolioId}
                                portfolios={portfolios}
                                onSelectPortfolio={(id) => setSelectedPortfolioId(id)}
                            />
                        </div>
                    ) : (
                        // ================= CREATE PORTFOLIO FORM =================
                        <div className="min-h-full flex items-center justify-center p-8 pb-32 bg-[radial-gradient(ellipse_at_center,_var(--tw-gradient-stops))] from-cyan-900/5 via-[#050505] to-[#050505]">
                            <GlassCard className="w-full max-w-3xl p-0 overflow-hidden shadow-2xl shadow-black/80 border border-white/10 ring-1 ring-white/5">
                                {/* Header */}
                                <div className="px-8 py-6 border-b border-white/5 flex items-center justify-between bg-white/[0.02]">
                                    <div className="space-y-1">
                                        <Heading level="h3" className="flex items-center gap-3 text-white">
                                            <div className="w-8 h-8 rounded-lg bg-cyan-500/10 flex items-center justify-center border border-cyan-500/20 shadow-[0_0_10px_rgba(6,182,212,0.2)]">
                                                <Briefcase className="w-4 h-4 text-cyan-400" />
                                            </div>
                                            New Portfolio Analysis
                                        </Heading>
                                        <Text variant="small" className="pl-11">Create a new portfolio to analyze risk, exposure, and performance.</Text>
                                    </div>
                                </div>

                                <div className="p-8 space-y-8 bg-[#0A0A0A]/50 backdrop-blur-xl">
                                    {/* Name & Desc */}
                                    <div className="space-y-6">
                                        <div className="space-y-2">
                                            <Label>Portfolio Name</Label>
                                            <Input
                                                value={portfolioName}
                                                onChange={(e) => setPortfolioName(e.target.value)}
                                                placeholder="e.g., 'Tech Growth Q4'"
                                                className="text-xl font-light py-6"
                                                autoFocus
                                            />
                                        </div>

                                        <div className="grid grid-cols-1 md:grid-cols-2 gap-8 pt-4">
                                            <div className="space-y-3">
                                                <Label size="sm" className="uppercase tracking-wider ml-1">Strategy Description</Label>
                                                <textarea
                                                    value={description}
                                                    onChange={(e) => setDescription(e.target.value)}
                                                    placeholder="Define the core thesis..."
                                                    rows={5}
                                                    className="w-full bg-white/[0.03] border border-white/10 rounded-xl p-4 text-sm text-gray-300 focus:border-cyan-500/30 focus:bg-white/[0.05] focus:outline-none resize-none placeholder:text-gray-700 transition-all shadow-inner font-ui"
                                                />
                                            </div>

                                            {/* Quick Add Asset */}
                                            <div className="space-y-3 relative">
                                                <Label size="sm" className="uppercase tracking-wider ml-1 flex items-center gap-2">
                                                    <Search className="w-3 h-3" /> Quick Add Position
                                                </Label>
                                                <div className="bg-white/[0.03] border border-white/10 rounded-xl p-4 space-y-4 hover:border-white/20 transition-colors">
                                                    <div className="relative group z-20">
                                                        <Input
                                                            ref={searchInputRef}
                                                            value={searchQuery}
                                                            onChange={(e) => setSearchQuery(e.target.value)}
                                                            onKeyDown={(e) => {
                                                                if (e.key === 'Enter') {
                                                                    addDraftPosition(searchQuery)
                                                                }
                                                            }}
                                                            placeholder="SEARCH SYMBOL (e.g. RELIANCE)"
                                                            icon={<Search className="w-4 h-4 text-gray-500" />}
                                                        />
                                                        {/* Search Results Portal */}
                                                        {searchResults.length > 0 && searchQuery && (
                                                            <Portal>
                                                                <div
                                                                    className="fixed z-[9999] bg-[#151515] border border-white/10 rounded-lg shadow-2xl overflow-hidden max-h-60 overflow-y-auto animate-in fade-in zoom-in-95 duration-150"
                                                                    style={{
                                                                        top: (searchInputRef.current?.getBoundingClientRect().bottom || 0) + 6,
                                                                        left: (searchInputRef.current?.getBoundingClientRect().left || 0),
                                                                        width: (searchInputRef.current?.getBoundingClientRect().width || 0),
                                                                    }}
                                                                >
                                                                    {searchResults.map((s, i) => (
                                                                        <button
                                                                            key={i}
                                                                            onClick={async () => {
                                                                                skipSearch.current = true;
                                                                                setSearchQuery(s.symbol);
                                                                                setSearchResults([]);

                                                                                // Fetch live price
                                                                                try {
                                                                                    const res = await fetch(`/api/market/quote/${s.symbol}`);
                                                                                    if (res.ok) {
                                                                                        const quote = await res.json();
                                                                                        setCurrentPrice(quote.ltp?.toFixed(2) || '');
                                                                                    }
                                                                                } catch (err) {
                                                                                    console.error('Failed to fetch quote:', err);
                                                                                }
                                                                            }}
                                                                            className="w-full px-4 py-3 text-left hover:bg-cyan-500/10 flex items-center justify-between group border-b border-white/5 last:border-0 transition-colors"
                                                                        >
                                                                            <span className="text-xs font-bold text-gray-200 group-hover:text-cyan-400 font-inter">{s.symbol}</span>
                                                                            <span className="text-[10px] text-gray-500 font-mono">{s.name ? s.name.substring(0, 15) : ''}...</span>
                                                                        </button>
                                                                    ))}
                                                                </div>
                                                            </Portal>
                                                        )}
                                                    </div>

                                                    <div className="flex gap-2 items-center">
                                                        <div className="relative flex-1">
                                                            <div className="absolute left-3 top-2.5 text-gray-600 text-[10px] font-bold">QTY</div>
                                                            <input
                                                                type="number"
                                                                value={currentQty}
                                                                onChange={e => setCurrentQty(e.target.value)}
                                                                className="w-full bg-black/40 border border-white/10 rounded-lg pl-10 pr-3 py-2 text-xs text-white text-right font-data focus:border-cyan-500/40 focus:outline-none transition-colors"
                                                                placeholder="0"
                                                            />
                                                        </div>
                                                        <div className="relative flex-1">
                                                            <div className="absolute left-3 top-2.5 text-gray-600 text-[10px] font-bold">₹</div>
                                                            <input
                                                                type="number"
                                                                value={currentPrice}
                                                                onChange={e => setCurrentPrice(e.target.value)}
                                                                className="w-full bg-black/40 border border-white/10 rounded-lg pl-8 pr-3 py-2 text-xs text-white text-right font-data focus:border-cyan-500/40 focus:outline-none transition-colors"
                                                                placeholder="0.00"
                                                            />
                                                        </div>
                                                        <button
                                                            onClick={() => addDraftPosition(searchQuery)}
                                                            className="p-2 bg-gradient-to-br from-cyan-600 to-cyan-700 hover:from-cyan-500 hover:to-cyan-600 text-white rounded-lg transition-all shadow-lg shadow-cyan-500/20 active:scale-95"
                                                        >
                                                            <Plus className="w-4 h-4" />
                                                        </button>
                                                    </div>
                                                </div>
                                            </div>
                                        </div>
                                    </div>

                                    {/* Draft Positions Table */}
                                    <div className="border border-white/10 rounded-xl overflow-hidden bg-[#050505]/30">
                                        <div className="bg-white/[0.02] px-6 py-3 border-b border-white/5 flex justify-between items-center">
                                            <span className="text-[10px] font-bold text-gray-500 uppercase tracking-widest flex items-center gap-2">
                                                <TrendingUp className="w-3 h-3" /> Composition
                                            </span>
                                            <div className="text-sm font-data text-cyan-400 font-bold tracking-tight bg-cyan-500/10 px-3 py-1 rounded-md border border-cyan-500/20">
                                                <Data value={totalInvested.toLocaleString()} prefix="₹" />
                                            </div>
                                        </div>
                                        <div className="max-h-48 overflow-y-auto min-h-[120px] custom-scrollbar bg-black/20">
                                            {draftPositions.length === 0 ? (
                                                <div className="h-full flex flex-col items-center justify-center text-gray-700 py-10 gap-2">
                                                    <div className="w-10 h-10 rounded-full bg-white/5 flex items-center justify-center">
                                                        <BarChart3 className="w-4 h-4 text-gray-600" />
                                                    </div>
                                                    <Text variant="tiny" className="uppercase tracking-widest font-bold">No Assets Added</Text>
                                                </div>
                                            ) : (
                                                <table className="w-full text-left border-collapse">
                                                    <thead className="bg-white/[0.02] text-[9px] uppercase font-bold text-gray-500 sticky top-0 z-10 backdrop-blur-sm">
                                                        <tr>
                                                            <th className="px-6 py-2.5">Symbol</th>
                                                            <th className="px-4 py-2.5 text-right">Qty</th>
                                                            <th className="px-4 py-2.5 text-right">Price</th>
                                                            <th className="px-4 py-2.5 text-right">Value</th>
                                                            <th className="px-4 py-2.5 w-8"></th>
                                                        </tr>
                                                    </thead>
                                                    <tbody className="text-xs">
                                                        {draftPositions.map((p, idx) => (
                                                            <tr key={idx} className="border-b border-white/5 last:border-0 group hover:bg-white/5 transition-colors">
                                                                <td className="px-6 py-2.5">
                                                                    <Text className="font-bold text-white" as="span">{p.symbol}</Text>
                                                                </td>
                                                                <td className="px-4 py-2.5 text-right text-gray-400">
                                                                    <Data value={p.quantity} />
                                                                </td>
                                                                <td className="px-4 py-2.5 text-right text-gray-400">
                                                                    <Data value={p.avg_buy_price} prefix="₹" />
                                                                </td>
                                                                <td className="px-4 py-2.5 text-cyan-100 text-right font-bold">
                                                                    <Data value={p.invested_value.toLocaleString()} prefix="₹" />
                                                                </td>
                                                                <td className="px-4 py-2.5 text-right">
                                                                    <button onClick={() => removeDraftPosition(idx)} className="text-gray-600 hover:text-red-400 transition-colors">
                                                                        <Trash2 className="w-3.5 h-3.5" />
                                                                    </button>
                                                                </td>
                                                            </tr>
                                                        ))}
                                                    </tbody>
                                                </table>
                                            )}
                                        </div>
                                    </div>
                                </div>

                                {/* Footer Actions */}
                                <div className="p-6 bg-[#111]/90 border-t border-white/10 flex justify-end gap-3 z-10 relative">
                                    <Button
                                        onClick={() => {
                                            setPortfolioName('');
                                            setDescription('');
                                            setDraftPositions([]);
                                        }}
                                        variant="ghost"
                                        size="md"
                                    >
                                        CLEAR FORM
                                    </Button>
                                    <Button
                                        onClick={handleCreateAndAnalyze}
                                        disabled={creating || !portfolioName.trim() || draftPositions.length === 0}
                                        variant="primary"
                                        size="md"
                                        loading={creating}
                                        icon={!creating && <BarChart3 className="w-3.5 h-3.5" />}
                                        className="shadow-[0_0_20px_rgba(6,182,212,0.3)]"
                                    >
                                        RUN ANALYSIS
                                    </Button>
                                </div>
                            </GlassCard>
                        </div>
                    )
                )}
            </div>
        </div>
    )
}
