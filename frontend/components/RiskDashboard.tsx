'use client'

import React, { useState } from 'react';
import CircularProgress from './CircularProgress';

interface Position {
    symbol: string;
    allocation: number;
}

interface RiskData {
    portfolio_metrics: {
        risk_grade: string;
        combined_risk_score: number;
        technical_risk_score: number;
        fundamental_risk_score: number;
        sharpe_ratio: number;
        volatility: number;
        var_95: number;
        beta: number;
    };
    position_metrics: Array<{
        symbol: string;
        allocation: number;
        risk_grade: string;
        technical_score: number;
        fundamental_score: number;
        combined_score: number;
        sharpe_ratio: number;
        debt_equity: number;
        roe: number;
    }>;
    warnings: string[];
}

export default function PortfolioAnalyzer() {
    const [positions, setPositions] = useState<Position[]>([]);
    const [riskData, setRiskData] = useState<RiskData | null>(null);
    const [loading, setLoading] = useState(false);
    const [showUpload, setShowUpload] = useState(false);
    const [inputTicker, setInputTicker] = useState('');
    const [availableSymbols, setAvailableSymbols] = useState<string[]>([]);

    // Fetch available symbols on mount
    React.useEffect(() => {
        fetch('http://localhost:8000/api/symbols')
            .then(res => res.json())
            .then(data => setAvailableSymbols(data.symbols || []))
            .catch(err => console.error('Failed to fetch symbols:', err));
    }, []);


    async function analyzeRisk() {
        if (positions.length === 0) {
            alert('Please add at least one position first');
            return;
        }

        // Check if all positions have allocations
        const missingAllocation = positions.some(p => !p.allocation || p.allocation === 0);
        if (missingAllocation) {
            alert('Please set allocation % for all positions');
            return;
        }

        // Validate total allocation = 100%
        const totalAllocation = positions.reduce((sum, p) => sum + (p.allocation || 0), 0);
        if (Math.abs(totalAllocation - 100) > 0.01) {
            alert(`Total allocation must equal 100%. Current total: ${totalAllocation.toFixed(1)}%\n\nTip: Click "Equal Alloc" to distribute evenly.`);
            return;
        }

        setLoading(true);
        try {
            const symbols = positions.map(p => p.symbol);
            const allocations = positions.map(p => p.allocation);

            console.log('Analyzing:', { symbols, allocations });

            const res = await fetch('http://localhost:8000/api/risk/comprehensive', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ symbols, allocations, lookback_days: 365 })
            });

            if (!res.ok) {
                const errorText = await res.text();
                console.error('API Error:', errorText);
                throw new Error('Analysis failed: ' + errorText);
            }

            const data = await res.json();
            console.log('Risk Data Received:', data);
            setRiskData(data);

            // Scroll to results
            setTimeout(() => {
                window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
            }, 100);
        } catch (e) {
            console.error('Analysis error:', e);
            alert('Error: ' + (e as Error).message);
        } finally {
            setLoading(false);
        }
    }

    function addPosition(symbol: string, allocation: number = 0) {
        if (!symbol) return;
        setPositions(prev => [...prev, { symbol: symbol.toUpperCase(), allocation }]);
    }

    function removePosition(index: number) {
        setPositions(prev => prev.filter((_, i) => i !== index));
    }

    function equalAllocation() {
        const equal = 100 / positions.length;
        setPositions(prev => prev.map(p => ({ ...p, allocation: equal })));
    }

    async function handleMultiFileUpload(files: FileList) {
        const uploadedSymbols: string[] = [];

        for (let i = 0; i < files.length; i++) {
            const file = files[i];
            const fileName = file.name;

            // Extract symbol from filename (e.g., "RELIANCE.xlsx" -> "RELIANCE")
            const symbolMatch = fileName.match(/^([A-Z]+)/i);
            const symbol = symbolMatch ? symbolMatch[1].toUpperCase() : prompt(`Enter symbol for ${fileName}:`);

            if (!symbol) continue;

            const formData = new FormData();
            formData.append('file', file);
            formData.append('symbol', symbol);

            try {
                const res = await fetch('http://localhost:8000/api/upload/financials', {
                    method: 'POST',
                    body: formData
                });
                const result = await res.json();
                if (res.ok) {
                    console.log(`✓ ${symbol}: ${result.message}`);
                    uploadedSymbols.push(symbol);
                } else {
                    console.error(`✗ ${symbol}: ${result.detail}`);
                }
            } catch (err) {
                console.error(`✗ ${symbol}:`, err);
            }
        }

        // Add all successfully uploaded symbols to positions
        setPositions(prev => {
            const newPositions = [...prev];
            uploadedSymbols.forEach(symbol => {
                if (!newPositions.find(p => p.symbol === symbol)) {
                    newPositions.push({ symbol, allocation: 0 });
                }
            });
            return newPositions;
        });

        alert(`Uploaded ${uploadedSymbols.length}/${files.length} file(s) successfully. Symbols added to positions.`);
        setShowUpload(false);
    }

    return (
        <div className="p-4 space-y-6">
            {/* Upload Modal */}
            {showUpload && (
                <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onClick={() => setShowUpload(false)}>
                    <div className="glass-card rounded-2xl p-6 max-w-xl mx-4 bg-white dark:bg-gray-900" onClick={e => e.stopPropagation()}>
                        <div className="flex justify-between items-center mb-4">
                            <h2 className="text-xl font-semibold">Upload Financial Data</h2>
                            <button onClick={() => setShowUpload(false)} className="text-2xl opacity-50 hover:opacity-100">&times;</button>
                        </div>
                        <p className="text-sm opacity-60 mb-6">
                            Upload Screener.in Excel files. Symbols will be auto-detected from filenames (e.g., RELIANCE.xlsx)
                        </p>
                        <label className="flex flex-col items-center justify-center w-full h-32 border-2 border-dashed border-gray-300 dark:border-gray-700 rounded-lg cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800/50 transition-colors">
                            <div className="flex flex-col items-center justify-center pt-5 pb-6">
                                <svg className="w-8 h-8 mb-4 text-gray-500" fill="none" viewBox="0 0 20 16">
                                    <path stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M13 13h3a3 3 0 0 0 0-6h-.025A5.56 5.56 0 0 0 16 6.5 5.5 5.5 0 0 0 5.207 5.021C5.137 5.017 5.071 5 5 5a4 4 0 0 0 0 8h2.167M10 15V6m0 0L8 8m2-2 2 2" />
                                </svg>
                                <p className="mb-2 text-sm text-gray-500"><span className="font-semibold">Click or drag multiple files</span></p>
                                <p className="text-xs text-gray-500">XLSX, XLS (Screener.in format)</p>
                            </div>
                            <input
                                type="file"
                                accept=".xlsx, .xls"
                                multiple
                                className="hidden"
                                onChange={(e) => e.target.files && handleMultiFileUpload(e.target.files)}
                            />
                        </label>
                    </div>
                </div>
            )}

            {/* Header */}
            <div className="glass-card rounded-2xl p-6 animate-fade-in">
                <div className="flex flex-col gap-4">
                    <div>
                        <h1 className="text-2xl font-bold">Portfolio Analyzer</h1>
                        <p className="text-sm opacity-60 mt-1">Technical + Fundamental Risk Assessment</p>
                    </div>

                    {/* Single Action Row */}
                    <div className="flex gap-3 items-center flex-wrap">
                        <div className="flex-1 min-w-[280px] flex items-center bg-white/50 dark:bg-black/20 rounded-lg p-1 border border-gray-200/50 dark:border-gray-700/50 focus-within:ring-2 focus-within:ring-blue-500/50 transition-all">
                            <input
                                type="text"
                                value={inputTicker}
                                onChange={(e) => setInputTicker(e.target.value.toUpperCase())}
                                onKeyDown={(e) => {
                                    if (e.key === 'Enter' && inputTicker) {
                                        addPosition(inputTicker);
                                        setInputTicker('');
                                    }
                                }}
                                placeholder="Add Symbol (e.g., RELIANCE)"
                                list="available-symbols"
                                className="flex-1 bg-transparent border-none outline-none px-3 py-2 text-sm"
                            />
                            <datalist id="available-symbols">
                                {availableSymbols.map(s => <option key={s} value={s} />)}
                            </datalist>
                            <button
                                onClick={() => {
                                    if (inputTicker) {
                                        addPosition(inputTicker);
                                        setInputTicker('');
                                    }
                                }}
                                className="px-4 py-2 rounded-md bg-blue-500 text-white hover:bg-blue-600 transition-all text-sm font-medium shadow-sm hover:shadow-md"
                            >
                                Add
                            </button>
                        </div>
                        <button onClick={() => setShowUpload(true)} className="px-4 py-2.5 rounded-lg bg-purple-500/10 text-purple-600 hover:bg-purple-500/20 transition-all text-sm font-medium whitespace-nowrap border border-purple-500/20">
                            📊 Upload
                        </button>
                        <button onClick={equalAllocation} disabled={positions.length === 0} className="px-4 py-2.5 rounded-lg bg-blue-500/10 text-blue-600 hover:bg-blue-500/20 transition-all disabled:opacity-50 text-sm font-medium whitespace-nowrap border border-blue-500/20">
                            Equal Alloc
                        </button>
                        <button onClick={analyzeRisk} disabled={loading || positions.length === 0} className="px-4 py-2.5 rounded-lg bg-green-500/10 text-green-600 hover:bg-green-500/20 transition-all disabled:opacity-50 text-sm font-medium whitespace-nowrap border border-green-500/20">
                            {loading ? 'Analyzing...' : '🎯 Analyze'}
                        </button>
                    </div>
                </div>
            </div>

            {/* Positions Manager */}
            {positions.length > 0 && (
                <div className="glass-card rounded-2xl p-6 animate-fade-in">
                    <div className="flex justify-between items-center mb-6">
                        <h3 className="text-sm font-semibold opacity-70 uppercase tracking-wide">Positions ({positions.length})</h3>
                        <div className="text-sm">
                            <span className="opacity-60">Total: </span>
                            <span className={`font-bold ${Math.abs(positions.reduce((sum, p) => sum + (p.allocation || 0), 0) - 100) < 0.01 ? 'text-green-600' : 'text-red-500'}`}>
                                {positions.reduce((sum, p) => sum + (p.allocation || 0), 0).toFixed(1)}%
                            </span>
                            <span className="opacity-60 ml-1">/ 100%</span>
                        </div>
                    </div>
                    <div className="space-y-3">
                        {positions.map((pos, idx) => (
                            <div key={idx} className="flex items-center gap-3 p-4 rounded-lg bg-white/40 dark:bg-white/5 border border-gray-100/50 dark:border-gray-800/50 hover:border-gray-200 dark:hover:border-gray-700 transition-colors">
                                <div className="flex-1 font-semibold">{pos.symbol}</div>
                                <input
                                    type="number"
                                    value={pos.allocation || ''}
                                    placeholder="Allocation %"
                                    onChange={(e) => {
                                        const val = Number(e.target.value);
                                        setPositions(prev => prev.map((p, i) => i === idx ? { ...p, allocation: val } : p));
                                    }}
                                    className="w-28 px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-700 bg-white/50 dark:bg-black/30 text-right focus:outline-none focus:ring-2 focus:ring-blue-500/50 transition-all"
                                />
                                <button onClick={() => removePosition(idx)} className="text-red-500 hover:bg-red-50 dark:hover:bg-red-900/20 rounded-lg px-3 py-2 transition-colors">✕</button>
                            </div>
                        ))}
                    </div>
                </div>
            )}


            {/* Results */}
            {riskData ? (
                <>
                    {/* Risk Grade with Circular Progress */}
                    <div className="glass-card rounded-2xl p-8 animate-slide-up">
                        <div className="flex flex-col md:flex-row items-center justify-center gap-8">
                            <CircularProgress
                                value={riskData.portfolio_metrics.combined_risk_score}
                                grade={riskData.portfolio_metrics.risk_grade}
                            />
                            <div className="flex flex-col gap-4 text-center md:text-left">
                                <div>
                                    <div className="text-sm opacity-60 uppercase tracking-wide mb-1">Technical Risk</div>
                                    <div className="text-2xl font-bold">{riskData.portfolio_metrics.technical_risk_score.toFixed(1)}<span className="text-base opacity-50">/10</span></div>
                                </div>
                                <div>
                                    <div className="text-sm opacity-60 uppercase tracking-wide mb-1">Fundamental Risk</div>
                                    <div className="text-2xl font-bold">{riskData.portfolio_metrics.fundamental_risk_score.toFixed(1)}<span className="text-base opacity-50">/10</span></div>
                                </div>
                            </div>
                        </div>
                    </div>

                    {/* Warnings */}
                    {riskData.warnings.length > 0 && (
                        <div className="glass-card rounded-2xl p-6 bg-red-50/50 dark:bg-red-900/10 border-2 border-red-200 dark:border-red-800 animate-fade-in">
                            <h3 className="text-sm font-semibold text-red-600 mb-4 uppercase tracking-wide">⚠️ WARNINGS ({riskData.warnings.length})</h3>
                            <div className="space-y-2">
                                {riskData.warnings.map((w, i) => (
                                    <div key={i} className="text-sm p-3 rounded bg-white/50 dark:bg-black/20">{w}</div>
                                ))}
                            </div>
                        </div>
                    )}

                    {/* Metrics */}
                    <div className="grid grid-cols-2 md:grid-cols-4 gap-4 animate-fade-in">
                        <MetricCard label="Sharpe Ratio" value={riskData.portfolio_metrics.sharpe_ratio?.toFixed(2) || '0.00'} />
                        <MetricCard label="Volatility" value={`${(riskData.portfolio_metrics.volatility * 100 || 0).toFixed(1)}%`} />
                        <MetricCard label="VaR (95%)" value={`${(riskData.portfolio_metrics.var_95 * 100 || 0).toFixed(1)}%`} />
                        <MetricCard label="Beta" value={riskData.portfolio_metrics.beta?.toFixed(2) || '1.00'} />
                    </div>

                    {/* Position Breakdown */}
                    <div className="glass-card rounded-2xl p-6 overflow-x-auto animate-slide-up">
                        <h3 className="text-sm font-semibold opacity-70 mb-6 uppercase tracking-wide">Position Breakdown</h3>
                        <table className="w-full text-sm">
                            <thead>
                                <tr className="border-b border-gray-200 dark:border-gray-700">
                                    <th className="text-left p-3">Symbol</th>
                                    <th className="text-right p-3">Alloc</th>
                                    <th className="text-center p-3">Grade</th>
                                    <th className="text-right p-3">Tech</th>
                                    <th className="text-right p-3">Fund</th>
                                    <th className="text-right p-3">Sharpe</th>
                                    <th className="text-right p-3">D/E</th>
                                    <th className="text-right p-3">ROE</th>
                                </tr>
                            </thead>
                            <tbody>
                                {riskData.position_metrics.map((pos, i) => (
                                    <tr key={i} className="border-b border-gray-100 dark:border-gray-800">
                                        <td className="p-3 font-semibold">{pos.symbol}</td>
                                        <td className="p-3 text-right">{(pos.allocation * 100).toFixed(1)}%</td>
                                        <td className="p-3 text-center">
                                            <span className={`px-3 py-1 rounded-full text-xs font-bold ${pos.risk_grade.startsWith('A') ? 'bg-green-100 text-green-700' :
                                                pos.risk_grade.startsWith('B') ? 'bg-blue-100 text-blue-700' :
                                                    pos.risk_grade.startsWith('C') ? 'bg-yellow-100 text-yellow-700' : 'bg-red-100 text-red-700'
                                                }`}>
                                                {pos.risk_grade}
                                            </span>
                                        </td>
                                        <td className="p-3 text-right">{pos.technical_score?.toFixed(1) || '-'}</td>
                                        <td className="p-3 text-right">{pos.fundamental_score?.toFixed(1) || '-'}</td>
                                        <td className="p-3 text-right">{pos.sharpe_ratio?.toFixed(2) || '-'}</td>
                                        <td className="p-3 text-right">{pos.debt_equity?.toFixed(2) || '-'}</td>
                                        <td className="p-3 text-right">{(pos.roe * 100)?.toFixed(1) || '-'}%</td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                </>
            ) : (
                <div className="glass-card rounded-2xl p-12 text-center opacity-40">
                    <p className="text-lg">No Analysis Yet</p>
                    <p className="text-sm mt-2">Add positions and click Analyze</p>
                </div>
            )}
        </div>
    );
}

function MetricCard({ label, value }: { label: string; value: string }) {
    return (
        <div className="glass-card rounded-xl p-5 hover:shadow-lg transition-all duration-300">
            <div className="text-xs opacity-60 mb-2 uppercase tracking-wide">{label}</div>
            <div className="text-2xl font-bold tabular-nums">{value}</div>
        </div>
    );
}
