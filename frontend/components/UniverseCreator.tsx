'use client'

import { useState } from 'react'
import { Database, Filter, X } from 'lucide-react'

interface UniverseCreatorProps {
    onClose: () => void
    onSave: (universe: any) => void
}

export default function UniverseCreator({ onClose, onSave }: UniverseCreatorProps) {
    const [selectionMode, setSelectionMode] = useState<'manual' | 'smart'>('manual')
    const [universeId, setUniverseId] = useState('')
    const [description, setDescription] = useState('')
    const [symbols, setSymbols] = useState('')
    const [sector, setSector] = useState('')
    const [marketCap, setMarketCap] = useState('')
    const [index, setIndex] = useState('')
    const [rebalance, setRebalance] = useState('MONTHLY')
    const [stockCount, setStockCount] = useState(0)

    const handleSave = () => {
        const universe = {
            id: universeId,
            description,
            selection_method: selectionMode,
            ...(selectionMode === 'manual' ? {
                symbols: symbols.split(',').map(s => s.trim()).filter(Boolean)
            } : {
                filters: { sector, market_cap: marketCap, index }
            }),
            rebalance_frequency: rebalance
        }
        onSave(universe)
    }

    return (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" onClick={onClose}>
            <div className="bg-[#0A0A0A] border border-white/10 rounded-2xl p-6 max-w-2xl w-full m-4 max-h-[85vh] overflow-y-auto" onClick={(e) => e.stopPropagation()}>
                {/* Header */}
                <div className="flex items-center justify-between mb-2">
                    <h2 className="text-xl font-bold text-white">Create Universe</h2>
                    <button onClick={onClose} className="text-gray-500 hover:text-white">
                        <X className="w-5 h-5" />
                    </button>
                </div>
                <p className="text-sm text-gray-500 mb-6">Define stock selection criteria for backtesting</p>

                <div className="space-y-6">
                    {/* Universe ID */}
                    <div>
                        <label className="block text-xs font-bold text-gray-400 mb-2">Universe ID</label>
                        <input
                            type="text"
                            value={universeId}
                            onChange={(e) => setUniverseId(e.target.value)}
                            placeholder="e.g., CUSTOM_UNIVERSE"
                            className="w-full bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-cyan-500/50"
                        />
                    </div>

                    {/* Selection Method Toggle */}
                    <div>
                        <label className="block text-xs font-bold text-gray-400 mb-3">Selection Method</label>
                        <div className="grid grid-cols-2 gap-3">
                            <button
                                onClick={() => setSelectionMode('manual')}
                                className={`px-4 py-3 text-white text-xs font-bold rounded-lg transition-all text-left ${selectionMode === 'manual' ? 'bg-cyan-600' : 'bg-white/5 hover:bg-white/10'
                                    }`}
                            >
                                <div className="flex items-center gap-2 mb-1">
                                    <Database className="w-4 h-4" />
                                    Manual Selection
                                </div>
                                <div className={`text-[10px] ${selectionMode === 'manual' ? 'text-cyan-200' : 'text-gray-500'}`}>
                                    Pick individual stocks
                                </div>
                            </button>
                            <button
                                onClick={() => setSelectionMode('smart')}
                                className={`px-4 py-3 text-white text-xs font-bold rounded-lg transition-all text-left ${selectionMode === 'smart' ? 'bg-cyan-600' : 'bg-white/5 hover:bg-white/10'
                                    }`}
                            >
                                <div className="flex items-center gap-2 mb-1">
                                    <Filter className="w-4 h-4" />
                                    Smart Filters
                                </div>
                                <div className={`text-[10px] ${selectionMode === 'smart' ? 'text-cyan-200' : 'text-gray-500'}`}>
                                    Sector, Cap, Index
                                </div>
                            </button>
                        </div>
                    </div>

                    {/* Manual Selection UI */}
                    {selectionMode === 'manual' && (
                        <div className="glass-subtle p-4 rounded-xl border border-white/5">
                            <label className="block text-xs font-bold text-gray-400 mb-2">Stock Symbols</label>
                            <textarea
                                value={symbols}
                                onChange={(e) => setSymbols(e.target.value)}
                                placeholder="Enter symbols separated by commas&#10;e.g., RELIANCE, TCS, INFY, HDFCBANK"
                                className="w-full bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-cyan-500/50 h-24 font-mono"
                            />
                            <div className="mt-2 text-[10px] text-gray-600">
                                💡 Tip: Use smart filters for sector or market cap based selection
                            </div>
                        </div>
                    )}

                    {/* Smart Filters UI */}
                    {selectionMode === 'smart' && (
                        <div className="glass-subtle p-4 rounded-xl border border-white/5 space-y-4">
                            <div>
                                <label className="block text-xs font-bold text-gray-400 mb-2">Sector</label>
                                <select
                                    value={sector}
                                    onChange={(e) => setSector(e.target.value)}
                                    className="w-full bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-cyan-500/50"
                                >
                                    <option value="">All Sectors</option>
                                    <option value="Banking">Banking</option>
                                    <option value="Information Technology">Information Technology</option>
                                    <option value="Pharmaceuticals">Pharmaceuticals</option>
                                    <option value="Automobiles">Automobiles</option>
                                    <option value="Energy">Energy</option>
                                    <option value="FMCG">FMCG</option>
                                    <option value="Metals">Metals & Mining</option>
                                    <option value="Realty">Real Estate</option>
                                </select>
                            </div>

                            <div>
                                <label className="block text-xs font-bold text-gray-400 mb-2">Market Cap</label>
                                <select
                                    value={marketCap}
                                    onChange={(e) => setMarketCap(e.target.value)}
                                    className="w-full bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-cyan-500/50"
                                >
                                    <option value="">All</option>
                                    <option value="LARGE">Large Cap (&gt; ₹20k cr)</option>
                                    <option value="MID">Mid Cap (₹5k - ₹20k cr)</option>
                                    <option value="SMALL">Small Cap (&lt; ₹5k cr)</option>
                                </select>
                            </div>

                            <div>
                                <label className="block text-xs font-bold text-gray-400 mb-2">Index Membership</label>
                                <select
                                    value={index}
                                    onChange={(e) => setIndex(e.target.value)}
                                    className="w-full bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-cyan-500/50"
                                >
                                    <option value="">Any</option>
                                    <option value="NIFTY50">NIFTY 50</option>
                                    <option value="NIFTY100">NIFTY 100</option>
                                    <option value="NIFTY200">NIFTY 200</option>
                                    <option value="NIFTY500">NIFTY 500</option>
                                    <option value="BANKNIFTY">BANK NIFTY</option>
                                </select>
                            </div>

                            <button className="w-full px-4 py-2 bg-cyan-600/20 hover:bg-cyan-600/30 text-cyan-400 text-xs font-bold rounded-lg transition-all">
                                <Filter className="w-3 h-3 inline mr-2" />
                                Apply Filters → {stockCount} stocks selected
                            </button>
                        </div>
                    )}

                    {/* Description */}
                    <div>
                        <label className="block text-xs font-bold text-gray-400 mb-2">Description</label>
                        <textarea
                            value={description}
                            onChange={(e) => setDescription(e.target.value)}
                            placeholder="Describe the universe selection criteria..."
                            className="w-full bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-cyan-500/50 h-20"
                        />
                    </div>

                    {/* Rebalance Frequency */}
                    <div>
                        <label className="block text-xs font-bold text-gray-400 mb-2">Rebalance Frequency</label>
                        <select
                            value={rebalance}
                            onChange={(e) => setRebalance(e.target.value)}
                            className="w-full bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-cyan-500/50"
                        >
                            <option>MONTHLY</option>
                            <option>QUARTERLY</option>
                            <option>ANNUAL</option>
                            <option>NONE (Fixed)</option>
                        </select>
                    </div>
                </div>

                {/* Actions */}
                <div className="flex gap-3 mt-6">
                    <button onClick={onClose} className="flex-1 px-4 py-2 bg-white/5 hover:bg-white/10 text-white text-sm font-bold rounded-lg transition-all">
                        Cancel
                    </button>
                    <button
                        onClick={handleSave}
                        disabled={!universeId}
                        className="flex-1 px-4 py-2 bg-cyan-600 hover:bg-cyan-500 disabled:bg-gray-700 disabled:cursor-not-allowed text-white text-sm font-bold rounded-lg transition-all"
                    >
                        Create Universe
                    </button>
                </div>
            </div>
        </div>
    )
}
