'use client'

import React, { useState, useEffect } from 'react';
import { Download, Upload, Search, Plus, X, TrendingUp, ChevronDown, ChevronUp, BarChart3, LayoutDashboard, List } from 'lucide-react';
import PerformanceChart from './charts/PerformanceChart';
import SectorAllocationChart from './charts/SectorAllocationChart';
import RiskScatterPlot from './charts/RiskScatterPlot';

interface Position {
    symbol: string;
    investedValue: number;
    quantity?: number;
    avgBuyPrice?: number;
    currentPrice?: number;
    currentValue?: number;
}

export default function UnifiedPortfolioAnalyzer() {
    // Portfolio State
    const [portfolioName, setPortfolioName] = useState('');
    const [description, setDescription] = useState('');
    const [positions, setPositions] = useState<Position[]>([]);
    const [totalCapital, setTotalCapital] = useState(1000000); // Added for save/load functionality
    const [benchmarkIndex, setBenchmarkIndex] = useState('NIFTY50'); // Added for save/load functionality

    // Input State
    const [currentSymbol, setCurrentSymbol] = useState('');
    const [currentValue, setCurrentValue] = useState('');
    const [currentQuantity, setCurrentQuantity] = useState('');
    const [currentAvgPrice, setCurrentAvgPrice] = useState('');
    const [searchResults, setSearchResults] = useState<any[]>([]);

    // UI State
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState('');
    const [viewMode, setViewMode] = useState<'edit' | 'analysis'>('edit'); // Replaces showInput
    const [portfolioId, setPortfolioId] = useState<number | null>(null);
    const [riskAnalysis, setRiskAnalysis] = useState<any>(null);
    const [livePrices, setLivePrices] = useState<Record<string, number>>({});

    // Search symbols
    const searchSymbols = async (query: string) => {
        if (query.length < 1) {
            setSearchResults([]);
            return;
        }

        try {
            const response = await fetch(`http://localhost:8000/api/symbols/search?q=${query}`);
            const data = await response.json();
            setSearchResults(data.symbols || []);
        } catch (err) {
            console.error('Search error:', err);
        }
    };

    // Auto-populate invested value when quantity and avg price are entered
    useEffect(() => {
        if (currentQuantity && currentAvgPrice) {
            const qty = parseFloat(currentQuantity);
            const price = parseFloat(currentAvgPrice);
            if (!isNaN(qty) && !isNaN(price) && qty > 0 && price > 0) {
                setCurrentValue((qty * price).toString());
            }
        }
    }, [currentQuantity, currentAvgPrice]);

    // Add position
    const addPosition = () => {
        if (!currentSymbol || !currentValue) {
            setError('Please enter symbol and invested value');
            return;
        }

        const value = parseFloat(currentValue);
        const qty = currentQuantity ? parseFloat(currentQuantity) : undefined;
        const avgPrice = currentAvgPrice ? parseFloat(currentAvgPrice) : undefined;

        if (isNaN(value) || value <= 0) {
            setError('Please enter a valid invested value');
            return;
        }

        // If both qty and avg price provided, validate they match invested value
        if (qty && avgPrice && Math.abs((qty * avgPrice) - value) > 1) {
            setError('Invested value should equal Quantity × Avg Price');
            return;
        }

        setPositions([...positions, {
            symbol: currentSymbol.toUpperCase(),
            investedValue: value,
            quantity: qty,
            avgBuyPrice: avgPrice
        }]);

        setCurrentSymbol('');
        setCurrentValue('');
        setCurrentQuantity('');
        setCurrentAvgPrice('');
        setSearchResults([]);
        setError('');
    };

    // Remove position
    const removePosition = (index: number) => {
        setPositions(positions.filter((_, i) => i !== index));
    };

    // Save portfolio
    const handleSavePortfolio = () => {
        const portfolioData = {
            portfolioName,
            totalCapital,
            benchmarkIndex,
            positions,
            savedAt: new Date().toISOString()
        };

        const blob = new Blob([JSON.stringify(portfolioData, null, 2)], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `portfolio_${portfolioName.replace(/\s+/g, '_')}_${new Date().toISOString().split('T')[0]}.json`;
        a.click();
        URL.revokeObjectURL(url);
    };

    // Load portfolio
    const handleLoadPortfolio = (event: React.ChangeEvent<HTMLInputElement>) => {
        const file = event.target.files?.[0];
        if (!file) return;

        const reader = new FileReader();
        reader.onload = (e) => {
            try {
                const data = JSON.parse(e.target?.result as string);
                setPortfolioName(data.portfolioName || '');
                setTotalCapital(data.totalCapital || 1000000);
                setBenchmarkIndex(data.benchmarkIndex || 'NIFTY50');
                setPositions(data.positions || []);
                setError(''); // Clear any previous errors
            } catch (error) {
                console.error('Error loading portfolio:', error);
                setError('Failed to load portfolio file. Please ensure it is a valid JSON.');
            }
        };
        reader.readAsText(file);
    };

    // Analyze portfolio
    const handleAnalyze = async () => {
        if (!portfolioName) {
            setError('Please enter portfolio name');
            return;
        }

        if (positions.length === 0) {
            setError('Please add at least one position');
            return;
        }

        setLoading(true);
        setError('');

        try {
            // Create portfolio
            const createResponse = await fetch('http://localhost:8000/api/portfolios', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    portfolio_name: portfolioName,
                    description: description,
                    positions: positions.map(p => ({
                        symbol: p.symbol,
                        invested_value: p.investedValue,
                        quantity: p.quantity,
                        avg_buy_price: p.avgBuyPrice
                    }))
                })
            });

            if (!createResponse.ok) {
                const errorData = await createResponse.json();
                throw new Error(errorData.detail || 'Failed to create portfolio');
            }

            const portfolioData = await createResponse.json();
            setPortfolioId(portfolioData.id);

            // Analyze portfolio
            const analyzeResponse = await fetch(`http://localhost:8000/api/portfolios/${portfolioData.id}/analyze`, {
                method: 'POST'
            });

            if (!analyzeResponse.ok) {
                throw new Error('Failed to analyze portfolio');
            }

            const analysisData = await analyzeResponse.json();
            setRiskAnalysis(analysisData);
            setViewMode('analysis'); // Switch to Analysis View

        } catch (err: any) {
            setError(err.message || 'Failed to create portfolio');
        } finally {
            setLoading(false);
        }
    };

    // Fetch live prices for positions
    useEffect(() => {
        if (positions.length === 0) return;

        const fetchLivePrices = async () => {
            try {
                const symbols = positions.map(p => p.symbol).join(',');
                const response = await fetch(`http://localhost:8000/api/quotes/live?symbols=${symbols}`);
                const data = await response.json();

                if (data.quotes) {
                    const prices: Record<string, number> = {};
                    Object.entries(data.quotes).forEach(([symbol, quote]: [string, any]) => {
                        if (quote.ltp) {
                            prices[symbol] = quote.ltp;
                        }
                    });
                    setLivePrices(prices);
                }
            } catch (err) {
                console.error('Failed to fetch live prices:', err);
            }
        };

        fetchLivePrices();
        const interval = setInterval(fetchLivePrices, 10000); // Update every 10 seconds

        return () => clearInterval(interval);
    }, [positions]);

    const totalInvested = positions.reduce((sum, p) => sum + p.investedValue, 0);
    const totalCurrentValue = positions.reduce((sum, p) => {
        const currentPrice = livePrices[p.symbol];
        if (currentPrice && p.quantity) {
            return sum + (currentPrice * p.quantity);
        }
        return sum + p.investedValue; // Fallback to invested value if no live price
    }, 0);
    const totalPnL = totalCurrentValue - totalInvested;
    const totalReturn = totalInvested > 0 ? (totalPnL / totalInvested * 100) : 0;

    return (
        <div className="min-h-screen bg-background-dark text-white font-sans">
            {/* Main Grid Layout */}
            <div className="grid grid-cols-12 gap-6 animate-in fade-in slide-in-from-bottom-4">

                {/* Left Sidebar: Inputs & Configuration */}
                <div className="col-span-3 bg-card-dark rounded-xl border border-border-dark p-4 h-[calc(100vh-140px)] sticky top-24 overflow-y-auto flex flex-col">
                    <div className="flex justify-between items-center mb-4">
                        <h3 className="text-sm font-bold opacity-60 px-1">PORTFOLIO DETAILS</h3>
                        <div className="flex gap-1">
                            <input
                                type="file"
                                id="portfolio-upload"
                                className="hidden"
                                accept=".json"
                                onChange={handleLoadPortfolio}
                            />
                            <button
                                onClick={() => document.getElementById('portfolio-upload')?.click()}
                                className="p-1.5 hover:bg-white/10 rounded-lg text-text-secondary hover:text-white transition-colors"
                                title="Load Portfolio"
                            >
                                <Upload className="w-4 h-4" />
                            </button>
                            <button
                                onClick={handleSavePortfolio}
                                className="p-1.5 hover:bg-white/10 rounded-lg text-text-secondary hover:text-white transition-colors"
                                title="Save Portfolio"
                            >
                                <Download className="w-4 h-4" />
                            </button>
                        </div>
                    </div>

                    <div className="space-y-4 flex-grow">
                        {/* Portfolio Name */}
                        <div>
                            <label className="block text-xs font-medium opacity-60 mb-2">Name</label>
                            <input
                                type="text"
                                value={portfolioName}
                                onChange={(e) => setPortfolioName(e.target.value)}
                                placeholder="My Growth Portfolio"
                                className="w-full px-3 py-2 rounded-lg bg-background-dark border border-border-dark text-sm focus:outline-none focus:border-primary transition-colors"
                            />
                        </div>

                        {/* Description */}
                        <div>
                            <label className="block text-xs font-medium opacity-60 mb-2">Description</label>
                            <textarea
                                value={description}
                                onChange={(e) => setDescription(e.target.value)}
                                placeholder="Portfolio strategy..."
                                rows={2}
                                className="w-full px-3 py-2 rounded-lg bg-background-dark border border-border-dark text-sm focus:outline-none focus:border-primary transition-colors resize-none"
                            />
                        </div>

                        <div className="h-px bg-border-dark my-2"></div>

                        <h3 className="text-sm font-bold opacity-60 px-1">ADD POSITIONS</h3>

                        {/* Symbol Search */}
                        <div className="relative">
                            <label className="block text-xs font-medium opacity-60 mb-2">Symbol</label>
                            <div className="relative">
                                <Search className="absolute left-3 top-2.5 w-4 h-4 opacity-40" />
                                <input
                                    type="text"
                                    value={currentSymbol}
                                    onChange={(e) => {
                                        setCurrentSymbol(e.target.value);
                                        searchSymbols(e.target.value);
                                    }}
                                    placeholder="Search..."
                                    className="w-full pl-9 pr-3 py-2 rounded-lg bg-background-dark border border-border-dark text-sm focus:outline-none focus:border-primary transition-colors"
                                />
                            </div>
                            {searchResults.length > 0 && (
                                <div className="absolute z-50 w-full mt-1 bg-card-dark rounded-lg shadow-xl border border-border-dark max-h-48 overflow-y-auto">
                                    {searchResults.map((result, idx) => (
                                        <button
                                            key={idx}
                                            onClick={() => {
                                                setCurrentSymbol(result.symbol);
                                                setSearchResults([]);
                                            }}
                                            className="w-full px-4 py-2 text-left hover:bg-primary/10 transition-colors text-sm border-b border-border-dark last:border-0"
                                        >
                                            <div className="font-medium text-white">{result.symbol}</div>
                                            <div className="text-xs opacity-60">{result.name}</div>
                                        </button>
                                    ))}
                                </div>
                            )}
                        </div>

                        <div className="grid grid-cols-2 gap-3">
                            <div>
                                <label className="block text-xs font-medium opacity-60 mb-2">Qty</label>
                                <input
                                    type="number"
                                    value={currentQuantity}
                                    onChange={(e) => setCurrentQuantity(e.target.value)}
                                    placeholder="0"
                                    className="w-full px-3 py-2 rounded-lg bg-background-dark border border-border-dark text-sm focus:outline-none focus:border-primary transition-colors"
                                />
                            </div>
                            <div>
                                <label className="block text-xs font-medium opacity-60 mb-2">Avg Price</label>
                                <input
                                    type="number"
                                    value={currentAvgPrice}
                                    onChange={(e) => setCurrentAvgPrice(e.target.value)}
                                    placeholder="₹0.00"
                                    className="w-full px-3 py-2 rounded-lg bg-background-dark border border-border-dark text-sm focus:outline-none focus:border-primary transition-colors"
                                />
                            </div>
                        </div>

                        <div>
                            <label className="block text-xs font-medium opacity-60 mb-2">Invested Value (₹)</label>
                            <input
                                type="number"
                                value={currentValue}
                                onChange={(e) => setCurrentValue(e.target.value)}
                                placeholder="100000"
                                className="w-full px-3 py-2 rounded-lg bg-background-dark border border-border-dark text-sm focus:outline-none focus:border-primary transition-colors"
                            />
                            {currentQuantity && currentAvgPrice && (
                                <p className="text-[10px] opacity-60 mt-1 text-right">
                                    Auto: ₹{(parseFloat(currentQuantity) * parseFloat(currentAvgPrice)).toLocaleString()}
                                </p>
                            )}
                        </div>

                        <button
                            onClick={addPosition}
                            className="w-full py-2 bg-primary/10 text-primary border border-primary/20 hover:bg-primary/20 rounded-lg text-sm font-medium transition-all flex items-center justify-center gap-2"
                        >
                            <Plus className="w-4 h4" /> Add to Portfolio
                        </button>

                        {error && (
                            <div className="p-3 bg-red-500/10 border border-red-500/20 text-red-500 rounded-lg text-xs">
                                {error}
                            </div>
                        )}
                    </div>

                    <div className="pt-4 border-t border-border-dark mt-4">
                        <div className="flex justify-between items-center mb-3">
                            <span className="text-sm opacity-60">Total Positions</span>
                            <span className="font-bold">{positions.length}</span>
                        </div>
                        <div className="flex justify-between items-center mb-2">
                            <span className="text-sm opacity-60">Total Invested</span>
                            <span className="font-bold text-primary">₹{totalInvested.toLocaleString()}</span>
                        </div>
                        <div className="flex justify-between items-center mb-2">
                            <span className="text-sm opacity-60">Current Value</span>
                            <span className="font-bold text-white">₹{totalCurrentValue.toLocaleString()}</span>
                        </div>
                        <div className="flex justify-between items-center mb-4">
                            <span className="text-sm opacity-60">Total P&L</span>
                            <span className={`font-bold ${totalPnL >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                                {totalPnL >= 0 ? '+' : ''}₹{totalPnL.toLocaleString()} ({totalReturn >= 0 ? '+' : ''}{totalReturn.toFixed(2)}%)
                            </span>
                        </div>

                        <button
                            onClick={handleAnalyze}
                            disabled={loading || positions.length === 0}
                            className="w-full py-3 bg-primary hover:bg-blue-600 text-white rounded-lg font-bold shadow-lg shadow-blue-500/20 disabled:opacity-50 disabled:cursor-not-allowed transition-all"
                        >
                            {loading ? 'Analyzing...' : 'Analyze Risk'}
                        </button>
                    </div>
                </div>

                {/* Right Main Content */}
                <div className="col-span-9 space-y-4">
                    {/* Top Bar Switcher */}
                    <div className="flex items-center justify-between bg-card-dark rounded-xl border border-border-dark p-2">
                        <div className="flex items-center gap-1">
                            <button
                                onClick={() => setViewMode('edit')}
                                className={`flex items-center gap-2 px-4 py-1.5 rounded-lg text-sm font-medium transition-all ${viewMode === 'edit' ? 'bg-background-dark text-white shadow-sm border border-border-dark' : 'text-text-secondary hover:text-white'}`}
                            >
                                <List className="w-4 h-4" /> Positions List
                            </button>
                            <button
                                onClick={() => viewMode !== 'analysis' && riskAnalysis && setViewMode('analysis')}
                                disabled={!riskAnalysis}
                                className={`flex items-center gap-2 px-4 py-1.5 rounded-lg text-sm font-medium transition-all ${viewMode === 'analysis' ? 'bg-background-dark text-white shadow-sm border border-border-dark' : 'text-text-secondary hover:text-white disabled:opacity-30'}`}
                            >
                                <LayoutDashboard className="w-4 h-4" /> Risk Dashboard
                            </button>
                        </div>
                        {viewMode === 'analysis' && (
                            <span className="text-xs opacity-40 px-3">Last Analyzed: just now</span>
                        )}
                    </div>

                    {/* CONTENT AREA */}
                    {viewMode === 'edit' ? (
                        <div className="bg-card-dark rounded-xl border border-border-dark overflow-hidden flex flex-col h-[calc(100vh-200px)]">
                            {positions.length === 0 ? (
                                <div className="flex-1 flex flex-col items-center justify-center opacity-40">
                                    <BarChart3 className="w-16 h-16 mb-4" />
                                    <p>Start adding stocks from the sidebar</p>
                                </div>
                            ) : (
                                <div className="flex-grow overflow-auto">
                                    <table className="w-full">
                                        <thead className="sticky top-0 bg-background-dark/95 backdrop-blur-md border-b border-border-dark z-10">
                                            <tr className="text-xs text-left opacity-60">
                                                <th className="py-3 px-6">SYMBOL</th>
                                                <th className="py-3 px-6 text-right">QTY</th>
                                                <th className="py-3 px-6 text-right">AVG PRICE</th>
                                                <th className="py-3 px-6 text-right">CMP</th>
                                                <th className="py-3 px-6 text-right">INVESTED</th>
                                                <th className="py-3 px-6 text-right">CURRENT</th>
                                                <th className="py-3 px-6 text-right">P&L</th>
                                                <th className="py-3 px-6 text-right">ALLOCATION</th>
                                                <th className="py-3 px-6"></th>
                                            </tr>
                                        </thead>
                                        <tbody className="divide-y divide-border-dark">
                                            {positions.map((pos, idx) => {
                                                const allocation = totalInvested > 0 ? (pos.investedValue / totalInvested * 100) : 0;
                                                const currentPrice = livePrices[pos.symbol];
                                                const currentValue = currentPrice && pos.quantity ? currentPrice * pos.quantity : pos.investedValue;
                                                const pnl = currentValue - pos.investedValue;
                                                const pnlPct = pos.investedValue > 0 ? (pnl / pos.investedValue * 100) : 0;

                                                return (
                                                    <tr key={idx} className="group hover:bg-primary/5 transition-colors">
                                                        <td className="py-3 px-6 font-medium">{pos.symbol}</td>
                                                        <td className="py-3 px-6 text-right font-mono text-sm opacity-80">{pos.quantity || '-'}</td>
                                                        <td className="py-3 px-6 text-right font-mono text-sm opacity-80">{pos.avgBuyPrice ? `₹${pos.avgBuyPrice.toFixed(2)}` : '-'}</td>
                                                        <td className="py-3 px-6 text-right font-mono text-sm">
                                                            {currentPrice ? (
                                                                <span className={pnl >= 0 ? 'text-green-400' : 'text-red-400'}>
                                                                    ₹{currentPrice.toFixed(2)}
                                                                </span>
                                                            ) : '-'}
                                                        </td>
                                                        <td className="py-3 px-6 text-right font-mono text-sm opacity-80">₹{pos.investedValue.toLocaleString()}</td>
                                                        <td className="py-3 px-6 text-right font-mono text-sm font-semibold">₹{currentValue.toLocaleString()}</td>
                                                        <td className="py-3 px-6 text-right font-mono text-sm">
                                                            <div className={`${pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                                                                {pnl >= 0 ? '+' : ''}₹{pnl.toLocaleString()}
                                                                <div className="text-xs opacity-80">({pnlPct >= 0 ? '+' : ''}{pnlPct.toFixed(2)}%)</div>
                                                            </div>
                                                        </td>
                                                        <td className="py-3 px-6 text-right">
                                                            <div className="flex items-center justify-end gap-2">
                                                                <span className="font-mono text-sm">{allocation.toFixed(1)}%</span>
                                                                <div className="w-16 h-1.5 bg-background-dark rounded-full overflow-hidden">
                                                                    <div className="h-full bg-primary" style={{ width: `${allocation}%` }}></div>
                                                                </div>
                                                            </div>
                                                        </td>
                                                        <td className="py-3 px-6 text-right">
                                                            <button
                                                                onClick={() => removePosition(idx)}
                                                                className="text-text-secondary hover:text-red-500 transition-colors opacity-0 group-hover:opacity-100"
                                                            >
                                                                <X className="w-4 h-4" />
                                                            </button>
                                                        </td>
                                                    </tr>
                                                )
                                            })}
                                        </tbody>
                                        <tfoot className="bg-background-dark border-t border-border-dark">
                                            <tr>
                                                <td className="py-3 px-6 font-bold text-sm">TOTAL</td>
                                                <td colSpan={3}></td>
                                                <td className="py-3 px-6 text-right font-bold text-primary">₹{totalInvested.toLocaleString()}</td>
                                                <td className="py-3 px-6 text-right font-bold text-white">₹{totalCurrentValue.toLocaleString()}</td>
                                                <td className="py-3 px-6 text-right font-bold">
                                                    <div className={`${totalPnL >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                                                        {totalPnL >= 0 ? '+' : ''}₹{totalPnL.toLocaleString()}
                                                        <div className="text-xs opacity-80">({totalReturn >= 0 ? '+' : ''}{totalReturn.toFixed(2)}%)</div>
                                                    </div>
                                                </td>
                                                <td colSpan={2}></td>
                                            </tr>
                                        </tfoot>
                                    </table>
                                </div>
                            )}
                        </div>
                    ) : (
                        /* ANALYSIS DASHBOARD */
                        <div className="space-y-6 h-[calc(100vh-200px)] overflow-y-auto pr-2">
                            {/* Summary Cards */}
                            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
                                <SummaryCard
                                    title="Total Portfolio Value"
                                    value={`₹${totalInvested.toLocaleString()}`}
                                    change={0}
                                    icon="💼"
                                />
                                <SummaryCard
                                    title="Portfolio Volatility"
                                    value={`${((riskAnalysis?.market_risk?.portfolio_volatility || 0) * 100).toFixed(1)}%`}
                                    subtitle="Annualized"
                                    icon="📊"
                                />
                                <SummaryCard
                                    title="Sharpe Ratio"
                                    value={(riskAnalysis?.market_risk?.sharpe_ratio || 0).toFixed(2)}
                                    subtitle="Risk-Adjusted Return"
                                    icon="⚖️"
                                />
                                <SummaryCard
                                    title="Portfolio Beta"
                                    value={(riskAnalysis?.market_risk?.beta || 0).toFixed(2)}
                                    subtitle="vs Market"
                                    icon="📈"
                                />
                            </div>

                            {/* Charts */}
                            {riskAnalysis?.charts && (
                                <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
                                    <div className="lg:col-span-2 bg-card-dark rounded-xl border border-border-dark p-4">
                                        <div className="flex items-center justify-between mb-3">
                                            <h2 className="text-sm font-semibold opacity-80">Portfolio Performance vs Nifty 50</h2>
                                        </div>
                                        <div className="h-64">
                                            <PerformanceChart data={riskAnalysis.charts.performance} />
                                        </div>
                                    </div>

                                    <div className="bg-card-dark rounded-xl border border-border-dark p-4">
                                        <h2 className="text-sm font-semibold opacity-80 mb-3">Sector Allocation</h2>
                                        <div className="h-64">
                                            <SectorAllocationChart data={riskAnalysis.charts.sectors} />
                                        </div>
                                    </div>

                                    {/* Risk Matrix */}
                                    <div className="lg:col-span-3 bg-card-dark rounded-xl border border-border-dark p-4">
                                        <h2 className="text-sm font-semibold opacity-80 mb-3">Risk Analysis Matrix</h2>
                                        <div className="h-64 relative">
                                            <RiskScatterPlot data={riskAnalysis.charts.risk_scatter} />
                                        </div>
                                    </div>
                                </div>
                            )}

                            {/* detailed metrics... (Keeping specific metrics in a clean grid) */}
                            <div className="bg-card-dark rounded-xl border border-border-dark p-6">
                                <h2 className="text-sm font-bold opacity-60 mb-4 uppercase tracking-wide">Risk Metrics Deep Dive</h2>
                                <div className="grid grid-cols-1 md:grid-cols-4 gap-6">
                                    {/* Market Risk */}
                                    <div className="space-y-3">
                                        <h3 className="text-sm font-semibold text-primary">Market Risk</h3>
                                        <MetricRow label="VaR (95%)" value={`${((riskAnalysis?.market_risk?.var_95 || 0) * 100).toFixed(2)}%`} />
                                        <MetricRow label="VaR (99%)" value={`${((riskAnalysis?.market_risk?.var_99 || 0) * 100).toFixed(2)}%`} />
                                        <MetricRow label="Max Drawdown" value={`${((riskAnalysis?.market_risk?.max_drawdown?.max_drawdown || 0) * 100).toFixed(2)}%`} />
                                    </div>

                                    {/* Tail Risk */}
                                    <div className="space-y-3">
                                        <h3 className="text-sm font-semibold text-primary">Tail Risk</h3>
                                        <MetricRow label="Skewness" value={(riskAnalysis?.market_risk?.skewness || 0).toFixed(3)} />
                                        <MetricRow label="Kurtosis" value={(riskAnalysis?.market_risk?.kurtosis || 0).toFixed(3)} />
                                        <MetricRow label="Excess Kurtosis" value={(riskAnalysis?.market_risk?.excess_kurtosis || 0).toFixed(3)} />
                                    </div>

                                    {/* Concentration */}
                                    <div className="space-y-3">
                                        <h3 className="text-sm font-semibold text-primary">Concentration</h3>
                                        <MetricRow label="Top 3 Holdings" value={`${((riskAnalysis?.portfolio_risk?.top_3_concentration || 0) * 100).toFixed(1)}%`} />
                                        <MetricRow label="Largest Position" value={`${((riskAnalysis?.portfolio_risk?.max_position || 0) * 100).toFixed(1)}%`} />
                                        <MetricRow label="HHI Index" value={(riskAnalysis?.portfolio_risk?.hhi || 0).toFixed(3)} />
                                    </div>

                                    {/* Correlation */}
                                    <div className="space-y-3">
                                        <h3 className="text-sm font-semibold text-primary">Correlation</h3>
                                        <MetricRow label="Avg Correlation" value={(riskAnalysis?.portfolio_risk?.avg_correlation || 0).toFixed(3)} />
                                        <MetricRow label="Fundamental Score" value={`${(riskAnalysis?.fundamental_risk?.avg_fragility || 0).toFixed(1)}`} />
                                    </div>
                                </div>
                            </div>

                        </div>
                    )}
                </div>
            </div>
        </div>
    );
}

// Sub-components
function SummaryCard({ title, value, change, subtitle, icon }: any) {
    return (
        <div className="bg-card-dark rounded-xl border border-border-dark p-4 hover:border-primary/30 transition-all shadow-sm">
            <div className="flex items-start justify-between mb-2 opacity-80">
                <div className="text-xl grayscale">{icon}</div>
                {/* Change badge could go here */}
            </div>
            <div className="text-xs text-text-secondary font-medium uppercase tracking-wider mb-1">{title}</div>
            <div className="text-2xl font-bold text-white mb-0.5">{value}</div>
            {subtitle && <div className="text-[10px] opacity-50">{subtitle}</div>}
        </div>
    );
}

function MetricRow({ label, value }: { label: string; value: string }) {
    return (
        <div className="flex justify-between items-center text-sm border-b border-border-dark/50 pb-1 last:border-0 last:pb-0">
            <span className="text-text-secondary">{label}</span>
            <span className="font-mono font-medium">{value}</span>
        </div>
    );
}
