"use client";

import { useState } from 'react';
import StrategyConfiguration from '../../components/strategies/StrategyConfiguration';
import EquityCurve from '../../components/strategies/EquityCurve';
import TradesTable from '../../components/strategies/TradesTable';
import PerformanceMetrics from '../../components/strategies/PerformanceMetrics';

export default function StrategiesPage() {
    const [backtestResults, setBacktestResults] = useState<any>(null);
    const [isRunning, setIsRunning] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const handleRunBacktest = async (config: any) => {
        setIsRunning(true);
        setError(null);

        try {
            const response = await fetch('http://localhost:8000/api/strategies/backtest', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    strategy_name: config.strategy,
                    symbol: config.symbol,
                    start_date: config.startDate,
                    end_date: config.endDate,
                    timeframe: config.timeframe,
                    initial_capital: config.initialCapital || 100000,
                    params: {
                        opening_range_minutes: config.openingRangeMinutes,
                        stop_loss_pct: config.stopLoss,
                        take_profit_pct: config.takeProfit,
                        max_positions_per_day: config.maxPositions,
                        trade_type: config.segment
                    }
                }),
            });

            if (!response.ok) {
                const errorData = await response.json();
                throw new Error(errorData.detail || 'Backtest failed');
            }

            const data = await response.json();
            setBacktestResults(data);
        } catch (err: any) {
            setError(err.message);
            console.error('Backtest error:', err);
        } finally {
            setIsRunning(false);
        }
    };

    return (
        <div className="grid grid-cols-12 gap-6 animate-in fade-in slide-in-from-bottom-4 h-full">
            {/* Left Sidebar - Configuration */}
            <div className="col-span-3 bg-card-dark rounded-xl border border-border-dark p-4 h-[calc(100vh-140px)] sticky top-24 overflow-y-auto">
                <StrategyConfiguration
                    onRunBacktest={handleRunBacktest}
                    isRunning={isRunning}
                />
            </div>

            {/* Main Content */}
            <div className="col-span-9 space-y-4 h-[calc(100vh-140px)] overflow-y-auto pr-2">
                {error && (
                    <div className="p-4 bg-red-500/10 border border-red-500/50 rounded-lg">
                        <p className="text-red-400 text-sm">{error}</p>
                    </div>
                )}

                {isRunning && (
                    <div className="h-full flex items-center justify-center">
                        <div className="bg-card-dark rounded-xl border border-border-dark p-12 text-center shadow-lg">
                            <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-primary mx-auto mb-4"></div>
                            <p className="opacity-60 text-sm">Running backtest...</p>
                        </div>
                    </div>
                )}

                {!isRunning && !backtestResults && !error && (
                    <div className="flex items-center justify-center h-full opacity-40">
                        <div className="text-center">
                            <div className="text-6xl mb-4 grayscale">🚀</div>
                            <p className="text-lg font-medium">Ready to Backtest</p>
                            <p className="text-sm mt-2 max-w-xs mx-auto">Configure your strategy parameters in the sidebar and click run to simulate performance.</p>
                        </div>
                    </div>
                )}


                {backtestResults && !isRunning && (
                    <div className="space-y-6 animate-in fade-in zoom-in-95 duration-300">
                        {/* Performance Metrics - 4 Card Layout */}
                        <PerformanceMetrics results={backtestResults} />




                        {/* Trade Statistics */}
                        <div className="grid grid-cols-3 gap-4">
                            <div className="bg-card-dark rounded-xl border border-border-dark p-5 shadow-sm">
                                <h3 className="text-xs font-bold opacity-60 mb-4 uppercase flex items-center gap-2">
                                    <svg className="w-4 h-4 opacity-70" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2" /></svg>
                                    Trade Statistics
                                </h3>
                                <div className="space-y-4">
                                    <div className="bg-background-dark rounded-lg p-3 border border-border-dark">
                                        <div className="text-xs opacity-60 mb-1">Total Trades</div>
                                        <div className="text-2xl font-bold text-white">
                                            {backtestResults.summary?.total_trades || 0}
                                        </div>
                                        <div className="flex items-center gap-2 mt-2 text-[10px] font-medium uppercase tracking-wider">
                                            <span className="text-green-500 bg-green-500/10 px-1.5 py-0.5 rounded">{backtestResults.summary?.winning_trades || 0} Wins</span>
                                            <span className="text-red-500 bg-red-500/10 px-1.5 py-0.5 rounded">{backtestResults.summary?.losing_trades || 0} Losses</span>
                                        </div>
                                    </div>

                                    <div className="space-y-2 pt-2">
                                        <div className="flex justify-between text-sm py-1 border-b border-border-dark last:border-0">
                                            <span className="opacity-60">Avg Trade Duration</span>
                                            <span className="font-mono">
                                                {backtestResults.metrics?.trade_analysis?.avg_trade_duration_minutes?.toFixed(0) || 0}m
                                            </span>
                                        </div>
                                        <div className="flex justify-between text-sm py-1 border-b border-border-dark last:border-0">
                                            <span className="opacity-60">Profit Factor</span>
                                            <span className="font-mono">
                                                {backtestResults.metrics?.performance?.profit_factor?.toFixed(2) || 'N/A'}
                                            </span>
                                        </div>
                                    </div>
                                </div>
                            </div>
                            <div></div>
                            <div></div>
                        </div>

                        {/* Equity Curve */}
                        <EquityCurve
                            equityCurve={backtestResults.equity_curve || []}
                            initialCapital={backtestResults.initial_capital}
                        />

                        {/* Trades Table */}
                        <TradesTable trades={backtestResults.trades || []} />
                    </div>
                )}
            </div>
        </div>
    );
}
