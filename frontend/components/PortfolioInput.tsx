'use client'

import React, { useState } from 'react';
import { Search, Plus, X, Upload, TrendingUp } from 'lucide-react';

interface Position {
    symbol: string;
    investedValue: number;
    quantity?: number;
    avgBuyPrice?: number;
}

interface PortfolioInputProps {
    onPortfolioCreated?: (portfolioId: number) => void;
}

export default function PortfolioInput({ onPortfolioCreated }: PortfolioInputProps) {
    const [portfolioName, setPortfolioName] = useState('');
    const [description, setDescription] = useState('');
    const [positions, setPositions] = useState<Position[]>([]);
    const [currentSymbol, setCurrentSymbol] = useState('');
    const [currentValue, setCurrentValue] = useState('');
    const [searchResults, setSearchResults] = useState<any[]>([]);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState('');

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

    // Add position
    const addPosition = () => {
        if (!currentSymbol || !currentValue) {
            setError('Please enter symbol and invested value');
            return;
        }

        const value = parseFloat(currentValue);
        if (isNaN(value) || value <= 0) {
            setError('Please enter a valid invested value');
            return;
        }

        setPositions([...positions, {
            symbol: currentSymbol.toUpperCase(),
            investedValue: value
        }]);

        setCurrentSymbol('');
        setCurrentValue('');
        setSearchResults([]);
        setError('');
    };

    // Remove position
    const removePosition = (index: number) => {
        setPositions(positions.filter((_, i) => i !== index));
    };

    // Equal allocation
    const equalAllocation = () => {
        if (positions.length === 0) return;

        const totalValue = positions.reduce((sum, p) => sum + p.investedValue, 0);
        const equalValue = totalValue / positions.length;

        setPositions(positions.map(p => ({
            ...p,
            investedValue: equalValue
        })));
    };

    // Create portfolio
    const createPortfolio = async () => {
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
            const response = await fetch('http://localhost:8000/api/portfolios', {
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

            if (!response.ok) {
                const errorData = await response.json();
                throw new Error(errorData.detail || 'Failed to create portfolio');
            }

            const data = await response.json();

            // Reset form
            setPortfolioName('');
            setDescription('');
            setPositions([]);

            if (onPortfolioCreated) {
                onPortfolioCreated(data.id);
            }
        } catch (err: any) {
            setError(err.message || 'Failed to create portfolio');
        } finally {
            setLoading(false);
        }
    };

    const totalInvested = positions.reduce((sum, p) => sum + p.investedValue, 0);

    return (
        <div className="h-full flex flex-col p-4 max-w-7xl mx-auto">
            {/* Compact Header */}
            <div className="flex items-center gap-2 mb-4">
                <TrendingUp className="w-6 h-6 text-blue-600" />
                <h1 className="text-xl font-semibold text-gray-900 dark:text-white">Create Portfolio</h1>
            </div>

            {/* Two Column Layout */}
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 flex-1 overflow-hidden">
                {/* Left Column: Input Form */}
                <div className="space-y-4 overflow-y-auto pr-2">
                    {/* Portfolio Details - Compact */}
                    <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4 space-y-3">
                        <div>
                            <label className="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">
                                Portfolio Name *
                            </label>
                            <input
                                type="text"
                                value={portfolioName}
                                onChange={(e) => setPortfolioName(e.target.value)}
                                placeholder="My Growth Portfolio"
                                className="w-full px-3 py-2 text-sm rounded-lg border border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-white focus:ring-2 focus:ring-blue-500 focus:border-transparent transition-all"
                            />
                        </div>

                        <div>
                            <label className="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">
                                Description (Optional)
                            </label>
                            <textarea
                                value={description}
                                onChange={(e) => setDescription(e.target.value)}
                                placeholder="High growth tech stocks..."
                                rows={2}
                                className="w-full px-3 py-2 text-sm rounded-lg border border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-white focus:ring-2 focus:ring-blue-500 focus:border-transparent transition-all resize-none"
                            />
                        </div>
                    </div>

                    {/* Add Stocks - Compact */}
                    <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4 space-y-3">
                        <h2 className="text-sm font-semibold text-gray-900 dark:text-white">Add Stocks</h2>

                        <div className="grid grid-cols-2 gap-3">
                            <div className="relative">
                                <label className="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">
                                    Stock Symbol *
                                </label>
                                <div className="relative">
                                    <Search className="absolute left-2 top-1/2 transform -translate-y-1/2 w-4 h-4 text-gray-400" />
                                    <input
                                        type="text"
                                        value={currentSymbol}
                                        onChange={(e) => {
                                            setCurrentSymbol(e.target.value);
                                            searchSymbols(e.target.value);
                                        }}
                                        placeholder="INFY, TCS..."
                                        className="w-full pl-8 pr-3 py-2 text-sm rounded-lg border border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-white focus:ring-2 focus:ring-blue-500 focus:border-transparent transition-all"
                                    />
                                </div>

                                {/* Search Results */}
                                {searchResults.length > 0 && (
                                    <div className="absolute z-10 w-full mt-1 bg-white dark:bg-gray-800 rounded-lg shadow-lg border border-gray-200 dark:border-gray-700 max-h-40 overflow-y-auto">
                                        {searchResults.map((result, idx) => (
                                            <button
                                                key={idx}
                                                onClick={() => {
                                                    setCurrentSymbol(result.symbol);
                                                    setSearchResults([]);
                                                }}
                                                className="w-full px-3 py-2 text-left hover:bg-gray-50 dark:hover:bg-gray-700 transition-colors"
                                            >
                                                <div className="font-medium text-gray-900 dark:text-white text-sm">{result.symbol}</div>
                                                <div className="text-xs text-gray-500 dark:text-gray-400">{result.name}</div>
                                            </button>
                                        ))}
                                    </div>
                                )}
                            </div>

                            <div>
                                <label className="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">
                                    Invested Value (₹) *
                                </label>
                                <input
                                    type="number"
                                    value={currentValue}
                                    onChange={(e) => setCurrentValue(e.target.value)}
                                    placeholder="100000"
                                    className="w-full px-3 py-2 text-sm rounded-lg border border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-white focus:ring-2 focus:ring-blue-500 focus:border-transparent transition-all"
                                />
                            </div>
                        </div>

                        <button
                            onClick={addPosition}
                            className="w-full px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors flex items-center justify-center gap-2 font-medium text-sm"
                        >
                            <Plus className="w-4 h-4" />
                            Add Stock
                        </button>
                    </div>

                    {/* Error Message */}
                    {error && (
                        <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg p-3 text-red-700 dark:text-red-400 text-sm">
                            {error}
                        </div>
                    )}
                </div>

                {/* Right Column: Positions List */}
                <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4 flex flex-col overflow-hidden">
                    <div className="flex items-center justify-between mb-3">
                        <h2 className="text-sm font-semibold text-gray-900 dark:text-white">
                            Portfolio Positions ({positions.length})
                        </h2>
                        {positions.length > 0 && (
                            <button
                                onClick={equalAllocation}
                                className="text-xs text-blue-600 dark:text-blue-400 hover:text-blue-700 dark:hover:text-blue-300 font-medium"
                            >
                                Equal Allocation
                            </button>
                        )}
                    </div>

                    {positions.length === 0 ? (
                        <div className="flex-1 flex items-center justify-center text-center">
                            <div>
                                <div className="text-4xl mb-2">📊</div>
                                <p className="text-sm text-gray-500 dark:text-gray-400">No stocks added yet</p>
                                <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">Add stocks to build your portfolio</p>
                            </div>
                        </div>
                    ) : (
                        <>
                            <div className="flex-1 overflow-y-auto space-y-2 mb-3">
                                {positions.map((pos, idx) => {
                                    const allocation = totalInvested > 0 ? (pos.investedValue / totalInvested * 100) : 0;
                                    return (
                                        <div
                                            key={idx}
                                            className="flex items-center justify-between p-3 rounded-lg border border-gray-200 dark:border-gray-700 hover:border-gray-300 dark:hover:border-gray-600 transition-colors"
                                        >
                                            <div className="flex-1 min-w-0">
                                                <div className="font-semibold text-gray-900 dark:text-white text-sm">{pos.symbol}</div>
                                                <div className="text-xs text-gray-500 dark:text-gray-400 truncate">
                                                    ₹{pos.investedValue.toLocaleString()} • {allocation.toFixed(1)}%
                                                </div>
                                            </div>
                                            <button
                                                onClick={() => removePosition(idx)}
                                                className="p-1.5 text-gray-400 hover:text-red-600 dark:hover:text-red-400 hover:bg-red-50 dark:hover:bg-red-900/20 rounded-lg transition-colors ml-2"
                                            >
                                                <X className="w-4 h-4" />
                                            </button>
                                        </div>
                                    );
                                })}
                            </div>

                            <div className="pt-3 border-t border-gray-200 dark:border-gray-700">
                                <div className="flex justify-between text-sm font-semibold mb-3">
                                    <span className="text-gray-700 dark:text-gray-300">Total Invested</span>
                                    <span className="text-blue-600 dark:text-blue-400">₹{totalInvested.toLocaleString()}</span>
                                </div>

                                <button
                                    onClick={createPortfolio}
                                    disabled={loading || positions.length === 0}
                                    className="w-full px-4 py-2.5 bg-gradient-to-r from-blue-600 to-purple-600 text-white rounded-lg hover:from-blue-700 hover:to-purple-700 transition-all font-semibold text-sm disabled:opacity-50 disabled:cursor-not-allowed shadow-md"
                                >
                                    {loading ? 'Creating...' : 'Create Portfolio & Analyze Risk'}
                                </button>
                            </div>
                        </>
                    )}
                </div>
            </div>
        </div>
    );
}
