'use client'

import React, { useState, useEffect } from 'react';
import { Search, Plus, X, TrendingUp, ChevronDown, ChevronUp, BarChart3 } from 'lucide-react';
import PerformanceChart from './charts/PerformanceChart';
import SectorAllocationChart from './charts/SectorAllocationChart';
import RiskScatterPlot from './charts/RiskScatterPlot';

interface Position {
    symbol: string;
    investedValue: number;
    quantity?: number;
    avgBuyPrice?: number;
}

export default function UnifiedPortfolioAnalyzer() {
    // Portfolio State
    const [portfolioName, setPortfolioName] = useState('');
    const [description, setDescription] = useState('');
    const [positions, setPositions] = useState<Position[]>([]);

    // Input State
    const [currentSymbol, setCurrentSymbol] = useState('');
    const [currentValue, setCurrentValue] = useState('');
    const [currentQuantity, setCurrentQuantity] = useState('');
    const [currentAvgPrice, setCurrentAvgPrice] = useState('');
    const [searchResults, setSearchResults] = useState<any[]>([]);

    // UI State
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState('');
    const [showInput, setShowInput] = useState(true);
    const [portfolioId, setPortfolioId] = useState<number | null>(null);
    const [riskAnalysis, setRiskAnalysis] = useState<any>(null);
    const [showDetailedReport, setShowDetailedReport] = useState(false);

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

    // Analyze portfolio
    const analyzePortfolio = async () => {
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
            setShowInput(false);

        } catch (err: any) {
            setError(err.message || 'Failed to create portfolio');
        } finally {
            setLoading(false);
        }
    };

    const totalInvested = positions.reduce((sum, p) => sum + p.investedValue, 0);

    return (
        <div className="min-h-screen bg-background-dark p-4">
            <div className="max-w-7xl mx-auto">
                {/* Header */}
                <div className="mb-4">
                    <div className="flex items-center gap-2 mb-1">
                        <div className="p-2 bg-primary/10 rounded-lg">
                            <BarChart3 className="w-5 h-5 text-primary" />
                        </div>
                        <div>
                            <h1 className="text-xl font-bold gradient-text">Portfolio Risk Analyzer</h1>
                            <p className="text-text-secondary text-xs">Create and analyze your portfolio in real-time</p>
                        </div>
                    </div>
                </div>

                {/* Main Content */}
                {showInput ? (
                    /* Input Mode */
                    <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
                        {/* Left: Portfolio Input */}
                        <div className="space-y-4">
                            {/* Portfolio Details */}
                            <div className="bg-card-dark rounded-lg border border-border-dark p-4 card-glow">
                                <h2 className="text-sm font-semibold mb-3 flex items-center gap-2">
                                    <span className="w-1.5 h-1.5 bg-primary rounded-full animate-pulse"></span>
                                    Portfolio Details
                                </h2>
                                <div className="space-y-3">
                                    <div>
                                        <label className="block text-xs font-medium text-text-secondary mb-1">
                                            Portfolio Name *
                                        </label>
                                        <input
                                            type="text"
                                            value={portfolioName}
                                            onChange={(e) => setPortfolioName(e.target.value)}
                                            placeholder="My Growth Portfolio"
                                            className="w-full px-3 py-2 text-sm rounded-lg bg-background-base border border-border-dark text-white placeholder-text-muted focus:border-primary focus:ring-1 focus:ring-primary/20 transition-all"
                                        />
                                    </div>
                                    <div>
                                        <label className="block text-sm font-medium text-text-secondary mb-2">
                                            Description (Optional)
                                        </label>
                                        <textarea
                                            value={description}
                                            onChange={(e) => setDescription(e.target.value)}
                                            placeholder="High growth tech stocks..."
                                            rows={2}
                                            className="w-full px-4 py-3 rounded-lg bg-background-base border border-border-dark text-white placeholder-text-muted focus:border-primary focus:ring-2 focus:ring-primary/20 transition-all resize-none"
                                        />
                                    </div>
                                </div>
                            </div>

                            {/* Add Stocks */}
                            <div className="bg-card-dark rounded-xl border border-border-dark p-6">
                                <h2 className="text-lg font-semibold mb-4">Add Stocks</h2>
                                <div className="space-y-4">
                                    <div className="relative">
                                        <label className="block text-sm font-medium text-text-secondary mb-2">
                                            Stock Symbol *
                                        </label>
                                        <div className="relative">
                                            <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 w-4 h-4 text-text-muted" />
                                            <input
                                                type="text"
                                                value={currentSymbol}
                                                onChange={(e) => {
                                                    setCurrentSymbol(e.target.value);
                                                    searchSymbols(e.target.value);
                                                }}
                                                placeholder="INFY, TCS..."
                                                className="w-full pl-10 pr-4 py-3 rounded-lg bg-background-base border border-border-dark text-white placeholder-text-muted focus:border-primary focus:ring-2 focus:ring-primary/20 transition-all"
                                            />
                                        </div>

                                        {/* Search Results */}
                                        {searchResults.length > 0 && (
                                            <div className="absolute z-10 w-full mt-2 bg-card-dark rounded-lg shadow-2xl border border-border-light max-h-48 overflow-y-auto">
                                                {searchResults.map((result, idx) => (
                                                    <button
                                                        key={idx}
                                                        onClick={() => {
                                                            setCurrentSymbol(result.symbol);
                                                            setSearchResults([]);
                                                        }}
                                                        className="w-full px-4 py-3 text-left hover:bg-card-hover transition-colors"
                                                    >
                                                        <div className="font-medium text-white text-sm">{result.symbol}</div>
                                                        <div className="text-xs text-text-secondary">{result.name}</div>
                                                    </button>
                                                ))}
                                            </div>
                                        )}
                                    </div>

                                    <div className="grid grid-cols-2 gap-4">
                                        <div>
                                            <label className="block text-sm font-medium text-text-secondary mb-2">
                                                Quantity (Optional)
                                            </label>
                                            <input
                                                type="number"
                                                value={currentQuantity}
                                                onChange={(e) => setCurrentQuantity(e.target.value)}
                                                placeholder="50"
                                                className="w-full px-4 py-3 rounded-lg bg-background-base border border-border-dark text-white placeholder-text-muted focus:border-primary focus:ring-2 focus:ring-primary/20 transition-all"
                                            />
                                        </div>
                                        <div>
                                            <label className="block text-sm font-medium text-text-secondary mb-2">
                                                Avg Buy Price (Optional)
                                            </label>
                                            <input
                                                type="number"
                                                value={currentAvgPrice}
                                                onChange={(e) => setCurrentAvgPrice(e.target.value)}
                                                placeholder="1500.00"
                                                className="w-full px-4 py-3 rounded-lg bg-background-base border border-border-dark text-white placeholder-text-muted focus:border-primary focus:ring-2 focus:ring-primary/20 transition-all"
                                            />
                                        </div>
                                    </div>

                                    <div>
                                        <label className="block text-sm font-medium text-text-secondary mb-2">
                                            Invested Value (₹) *
                                        </label>
                                        <input
                                            type="number"
                                            value={currentValue}
                                            onChange={(e) => setCurrentValue(e.target.value)}
                                            placeholder="100000"
                                            className="w-full px-4 py-3 rounded-lg bg-background-base border border-border-dark text-white placeholder-text-muted focus:border-primary focus:ring-2 focus:ring-primary/20 transition-all"
                                        />
                                        <p className="text-xs text-text-muted mt-1">
                                            {currentQuantity && currentAvgPrice
                                                ? `Auto-calculated: ₹${(parseFloat(currentQuantity) * parseFloat(currentAvgPrice)).toLocaleString()}`
                                                : 'Or will be calculated from Qty × Avg Price'
                                            }
                                        </p>
                                    </div>
                                </div>

                                <button
                                    onClick={addPosition}
                                    className="w-full px-4 py-3 bg-primary hover:bg-primary-dark rounded-lg transition-all flex items-center justify-center gap-2 font-medium shadow-lg shadow-primary/20"
                                >
                                    <Plus className="w-5 h-5" />
                                    Add Stock
                                </button>
                            </div>

                            {error && (
                                <div className="bg-loss-bg border border-loss/20 rounded-lg p-4 text-loss text-sm">
                                    {error}
                                </div>
                            )}
                        </div>

                        {/* Right: Positions List */}
                        <div className="bg-card-dark rounded-xl border border-border-dark p-6 flex flex-col">
                            <div className="flex items-center justify-between mb-6">
                                <h2 className="text-lg font-semibold">
                                    Portfolio Positions ({positions.length})
                                </h2>
                            </div>

                            {positions.length === 0 ? (
                                <div className="flex-1 flex items-center justify-center text-center py-12">
                                    <div>
                                        <div className="text-6xl mb-4 opacity-20">📊</div>
                                        <p className="text-text-secondary">No stocks added yet</p>
                                        <p className="text-text-muted text-sm mt-2">Add stocks to build your portfolio</p>
                                    </div>
                                </div>
                            ) : (
                                <>
                                    <div className="flex-1 overflow-y-auto space-y-3 mb-6 pr-2">
                                        {positions.map((pos, idx) => {
                                            const allocation = totalInvested > 0 ? (pos.investedValue / totalInvested * 100) : 0;
                                            return (
                                                <div
                                                    key={idx}
                                                    className="flex items-center justify-between p-4 rounded-lg border border-border-dark hover:border-border-light transition-all glass"
                                                >
                                                    <div className="flex-1">
                                                        <div className="font-semibold text-white">{pos.symbol}</div>
                                                        <div className="text-sm text-text-secondary">
                                                            ₹{pos.investedValue.toLocaleString()} • {allocation.toFixed(1)}%
                                                        </div>
                                                    </div>
                                                    <button
                                                        onClick={() => removePosition(idx)}
                                                        className="p-2 text-text-muted hover:text-loss hover:bg-loss-bg rounded-lg transition-all"
                                                    >
                                                        <X className="w-4 h-4" />
                                                    </button>
                                                </div>
                                            );
                                        })}
                                    </div>

                                    <div className="pt-6 border-t border-border-dark space-y-4">
                                        <div className="flex justify-between text-lg font-semibold">
                                            <span className="text-text-secondary">Total Invested</span>
                                            <span className="text-primary">₹{totalInvested.toLocaleString()}</span>
                                        </div>

                                        <button
                                            onClick={analyzePortfolio}
                                            disabled={loading || positions.length === 0}
                                            className="w-full px-6 py-4 bg-gradient-to-r from-primary to-accent-purple text-white rounded-lg hover:shadow-xl hover:shadow-primary/30 transition-all font-semibold text-lg disabled:opacity-50 disabled:cursor-not-allowed"
                                        >
                                            {loading ? 'Analyzing...' : 'Analyze Portfolio'}
                                        </button>
                                    </div>
                                </>
                            )}
                        </div>
                    </div>
                ) : (
                    /* Analysis Mode - High-Level Summary */
                    <div className="space-y-6">
                        {/* Header with Create New Portfolio button */}
                        <div className="flex items-center justify-between">
                            <h2 className="text-lg font-semibold">Portfolio Analysis</h2>
                            <button
                                onClick={() => {
                                    setShowInput(true);
                                    setRiskAnalysis(null);
                                    setPortfolioId(null);
                                }}
                                className="px-4 py-2 bg-primary hover:bg-primary-dark text-white rounded-lg transition-all font-medium text-sm shadow-lg shadow-primary/20"
                            >
                                + Create New Portfolio
                            </button>
                        </div>

                        {/* Holdings Table - First */}
                        {riskAnalysis?.positions && riskAnalysis.positions.length > 0 && (
                            <div className="bg-card-dark rounded-lg border border-border-dark p-4">
                                <div className="flex items-center justify-between mb-3">
                                    <h2 className="text-sm font-semibold">Holdings</h2>
                                    <div className="text-xs text-text-secondary">
                                        {riskAnalysis.positions.length} Positions
                                    </div>
                                </div>
                                <div className="overflow-x-auto">
                                    <table className="w-full">
                                        <thead className="border-b border-border-dark">
                                            <tr className="text-xs text-text-secondary uppercase">
                                                <th className="text-left py-3 px-2">Instrument</th>
                                                <th className="text-right py-3 px-2">Qty</th>
                                                <th className="text-right py-3 px-2">Avg Price</th>
                                                <th className="text-right py-3 px-2">LTP</th>
                                                <th className="text-right py-3 px-2">Invested</th>
                                                <th className="text-right py-3 px-2">Current Value</th>
                                                <th className="text-right py-3 px-2">P&L</th>
                                                <th className="text-right py-3 px-2">P&L %</th>
                                            </tr>
                                        </thead>
                                        <tbody>
                                            {riskAnalysis.positions.map((pos: any) => (
                                                <tr key={pos.symbol} className="border-b border-border-dark/50 hover:bg-card-hover transition-colors">
                                                    <td className="py-4 px-2">
                                                        <div className="font-medium text-white">{pos.symbol}</div>
                                                        <div className="text-xs text-text-secondary truncate max-w-[150px]">
                                                            {pos.company_name}
                                                        </div>
                                                        <div className="text-xs text-text-muted">{pos.sector}</div>
                                                    </td>
                                                    <td className="text-right py-4 px-2 text-sm">
                                                        {pos.quantity?.toLocaleString() || '--'}
                                                    </td>
                                                    <td className="text-right py-4 px-2 text-sm">
                                                        ₹{pos.avg_buy_price?.toFixed(2) || '--'}
                                                    </td>
                                                    <td className="text-right py-4 px-2">
                                                        <div className="text-sm font-medium">
                                                            {pos.ltp ? `₹${pos.ltp.toFixed(2)}` : '--'}
                                                        </div>
                                                        {pos.last_updated && (
                                                            <div className="text-xs text-text-muted">
                                                                {new Date(pos.last_updated).toLocaleDateString()}
                                                            </div>
                                                        )}
                                                    </td>
                                                    <td className="text-right py-4 px-2 text-sm">
                                                        ₹{pos.invested_value?.toLocaleString() || '--'}
                                                    </td>
                                                    <td className="text-right py-4 px-2 text-sm font-medium">
                                                        ₹{pos.current_value?.toLocaleString() || '--'}
                                                    </td>
                                                    <td className={`text-right py-4 px-2 text-sm font-medium ${pos.pnl === null ? '' : pos.pnl >= 0 ? 'text-profit' : 'text-loss'
                                                        }`}>
                                                        {pos.pnl !== null ? `₹${pos.pnl.toLocaleString()}` : '--'}
                                                    </td>
                                                    <td className={`text-right py-4 px-2 text-sm font-bold ${pos.pnl_pct === null ? '' : pos.pnl_pct >= 0 ? 'text-profit' : 'text-loss'
                                                        }`}>
                                                        {pos.pnl_pct !== null
                                                            ? `${pos.pnl_pct >= 0 ? '+' : ''}${pos.pnl_pct.toFixed(2)}%`
                                                            : '--'
                                                        }
                                                    </td>
                                                </tr>
                                            ))}
                                        </tbody>
                                        <tfoot className="border-t-2 border-border-light">
                                            <tr className="font-semibold">
                                                <td className="py-4 px-2 text-white">Total</td>
                                                <td className="text-right py-4 px-2"></td>
                                                <td className="text-right py-4 px-2"></td>
                                                <td className="text-right py-4 px-2"></td>
                                                <td className="text-right py-4 px-2 text-white">
                                                    ₹{riskAnalysis.positions.reduce((sum: number, p: any) => sum + (p.invested_value || 0), 0).toLocaleString()}
                                                </td>
                                                <td className="text-right py-4 px-2 text-white">
                                                    ₹{riskAnalysis.positions.reduce((sum: number, p: any) => sum + (p.current_value || 0), 0).toLocaleString()}
                                                </td>
                                                <td className={`text-right py-4 px-2 ${riskAnalysis.positions.reduce((sum: number, p: any) => sum + (p.pnl || 0), 0) >= 0 ? 'text-profit' : 'text-loss'
                                                    }`}>
                                                    ₹{riskAnalysis.positions.reduce((sum: number, p: any) => sum + (p.pnl || 0), 0).toLocaleString()}
                                                </td>
                                                <td className={`text-right py-4 px-2 ${riskAnalysis.positions.reduce((sum: number, p: any) => sum + (p.pnl || 0), 0) >= 0 ? 'text-profit' : 'text-loss'
                                                    }`}>
                                                    {(() => {
                                                        const totalInvested = riskAnalysis.positions.reduce((sum: number, p: any) => sum + (p.invested_value || 0), 0);
                                                        const totalPnl = riskAnalysis.positions.reduce((sum: number, p: any) => sum + (p.pnl || 0), 0);
                                                        const totalPnlPct = totalInvested > 0 ? (totalPnl / totalInvested * 100) : 0;
                                                        return `${totalPnlPct >= 0 ? '+' : ''}${totalPnlPct.toFixed(2)}%`;
                                                    })()}
                                                </td>
                                            </tr>
                                        </tfoot>
                                    </table>
                                </div>
                            </div>
                        )}

                        {/* Summary Cards */}
                        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-3">
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

                        {/* Chart Visualizations */}
                        {riskAnalysis?.charts && (
                            <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
                                {/* Performance Chart - Spans 2 columns */}
                                <div className="lg:col-span-2 bg-card-dark rounded-lg border border-border-dark p-4">
                                    <div className="flex items-center justify-between mb-3">
                                        <h2 className="text-sm font-semibold">Portfolio Performance vs Nifty 50</h2>
                                        <div className="text-[10px] text-text-secondary">Last 1 Year</div>
                                    </div>
                                    <div className="h-48">
                                        <PerformanceChart data={riskAnalysis.charts.performance} />
                                    </div>
                                </div>

                                {/* Sector Allocation */}
                                <div className="bg-card-dark rounded-lg border border-border-dark p-4">
                                    <h2 className="text-sm font-semibold mb-3">Sector Allocation</h2>
                                    <div className="h-48">
                                        <SectorAllocationChart data={riskAnalysis.charts.sectors} />
                                    </div>
                                </div>

                                {/* Risk Analysis Matrix - Spans 2 columns */}
                                <div className="lg:col-span-2 bg-card-dark rounded-lg border border-border-dark p-4">
                                    <h2 className="text-sm font-semibold mb-3">Risk Analysis Matrix</h2>
                                    <div className="h-48 relative">
                                        <RiskScatterPlot data={riskAnalysis.charts.risk_scatter} />
                                    </div>
                                </div>
                            </div>
                        )}

                        {/* Detailed Risk Metrics - Always Visible */}
                        {riskAnalysis && (
                            <div className="bg-card-dark rounded-lg border border-border-dark p-4">
                                <h2 className="text-sm font-semibold mb-4">Risk Metrics</h2>
                                <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                                    {/* Market Risk Metrics */}
                                    <div>
                                        <h3 className="text-sm font-semibold mb-3 flex items-center gap-1.5">
                                            <span className="text-lg">🎯</span>
                                            Market Risk
                                        </h3>
                                        <div className="space-y-2">
                                            <MetricCard label="Value at Risk (95%)" value={`${((riskAnalysis.market_risk?.var_95 || 0) * 100).toFixed(2)}%`} />
                                            <MetricCard label="Conditional VaR (95%)" value={`${((riskAnalysis.market_risk?.cvar_95 || 0) * 100).toFixed(2)}%`} />
                                            <MetricCard label="Max Drawdown" value={`${((riskAnalysis.market_risk?.max_drawdown?.max_drawdown || 0) * 100).toFixed(2)}%`} />
                                            <MetricCard label="Average Volatility" value={`${((riskAnalysis.market_risk?.volatility || 0) * 100).toFixed(2)}%`} />
                                        </div>
                                    </div>

                                    {/* Portfolio Concentration */}
                                    <div>
                                        <h3 className="text-sm font-semibold mb-3 flex items-center gap-1.5">
                                            <span className="text-lg">⚡</span>
                                            Portfolio Concentration
                                        </h3>
                                        <div className="space-y-2">
                                            <MetricCard label="Concentration Risk" value={(riskAnalysis.portfolio_risk?.concentration || 0).toFixed(3)} />
                                            <MetricCard label="Number of Positions" value={positions.length.toString()} />
                                            <MetricCard label="HHI Index" value={(riskAnalysis.portfolio_risk?.hhi || 0).toFixed(2)} />
                                        </div>
                                    </div>

                                    {/* Fundamental Risk */}
                                    <div>
                                        <h3 className="text-sm font-semibold mb-3 flex items-center gap-1.5">
                                            <span className="text-lg">💰</span>
                                            Fundamental Risk
                                        </h3>
                                        <div className="space-y-2">
                                            <MetricCard label="Average Leverage (D/E)" value={(riskAnalysis.fundamental_risk?.avg_leverage || 0).toFixed(2)} />
                                            <MetricCard label="Financial Fragility Score" value={`${(riskAnalysis.fundamental_risk?.avg_fragility || 0).toFixed(1)}/100`} />
                                            <MetricCard label="Weighted Avg ROE" value={`${((riskAnalysis.fundamental_risk?.avg_roe || 0) * 100).toFixed(1)}%`} />
                                        </div>
                                    </div>
                                </div>
                            </div>
                        )}

                    </div>
                )}
            </div>
        </div>
    );
}

// Summary Card Component
function SummaryCard({ title, value, change, subtitle, icon }: any) {
    return (
        <div className="bg-card-dark rounded-lg border border-border-dark p-3 hover:border-border-light transition-all card-glow">
            <div className="flex items-start justify-between mb-2">
                <div className="text-xl">{icon}</div>
                {change !== undefined && (
                    <span className={`text-xs font-medium px-1.5 py-0.5 rounded ${change >= 0 ? 'bg-profit-bg text-profit' : 'bg-loss-bg text-loss'}`}>
                        {change >= 0 ? '+' : ''}{change}%
                    </span>
                )}
            </div>
            <div className="text-text-secondary text-xs mb-1">{title}</div>
            <div className="text-lg font-bold text-white mb-0.5">{value}</div>
            {subtitle && <div className="text-[10px] text-text-muted">{subtitle}</div>}
        </div>
    );
}

// Metric Card Component
function MetricCard({ label, value }: { label: string; value: string }) {
    return (
        <div className="bg-background-base rounded-lg p-2 border border-border-dark">
            <div className="text-[10px] text-text-secondary mb-0.5">{label}</div>
            <div className="text-sm font-semibold text-white">{value}</div>
        </div>
    );
}
