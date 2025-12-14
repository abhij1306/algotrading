"use client";

import { useState } from 'react';
import StrategyConfiguration from '@/components/strategies/StrategyConfiguration';
import EquityCurve from '@/components/strategies/EquityCurve';
import PerformanceMetrics from '@/components/strategies/PerformanceMetrics';
import RiskProfile from '@/components/strategies/RiskProfile';
import TradesTable from '@/components/strategies/TradesTable';

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
        <div className="min-h-screen bg-gradient-to-br from-slate-900 via-slate-800 to-slate-900">
            {/* Header */}
            <div className="border-b border-slate-700 bg-slate-900/50 backdrop-blur-sm">
                <div className="px-6 py-4">
                    <div className="flex items-center justify-between">
                        <div>
                            <h1 className="text-2xl font-bold text-white">Strategies</h1>
                            <p className="text-sm text-slate-400 mt-1">
                                Backtest algorithmic trading strategies
                            </p>
                        </div>
                        {backtestResults && (
                            <button className="px-4 py-2 bg-slate-700 hover:bg-slate-600 text-white rounded-lg text-sm transition-colors">
                                Export Report
                            </button>
                        )}
                    </div>
                </div>
            </div>

            <div className="flex h-[calc(100vh-73px)]">
                {/* Left Sidebar - Configuration */}
                <div className="w-80 border-r border-slate-700 bg-slate-900/30 overflow-y-auto">
                    <StrategyConfiguration
                        onRunBacktest={handleRunBacktest}
                        isRunning={isRunning}
                    />
                </div>

                {/* Main Content */}
                <div className="flex-1 overflow-y-auto">
                    {error && (
                        <div className="m-6 p-4 bg-red-500/10 border border-red-500/50 rounded-lg">
                            <p className="text-red-400 text-sm">{error}</p>
                        </div>
                    )}

                    {isRunning && (
                        <div className="flex items-center justify-center h-full">
                            <div className="text-center">
                                <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-green-500 mx-auto"></div>
                                <p className="text-slate-400 mt-4">Running backtest...</p>
                            </div>
                        </div>
                    )}

                    {!isRunning && !backtestResults && !error && (
                        <div className="flex items-center justify-center h-full">
                            <div className="text-center text-slate-400">
                                <svg className="w-20 h-20 mx-auto mb-4 text-slate-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" />
                                </svg>
                                <p className="text-lg">Configure strategy and run backtest</p>
                                <p className="text-sm mt-2">Results will appear here</p>
                            </div>
                        </div>
                    )}

                    {backtestResults && !isRunning && (
                        <div className="p-6 space-y-6">
                            {/* Strategy Info Header */}
                            <div className="bg-slate-800/50 rounded-lg p-4 border border-slate-700">
                                <div className="flex items-center justify-between">
                                    <div>
                                        <h2 className="text-xl font-bold text-white">
                                            {backtestResults.strategy === 'ORB' && 'Opening Range Breakout (ORB)'}
                                        </h2>
                                        <p className="text-sm text-slate-400 mt-1">
                                            {backtestResults.symbol} • {backtestResults.period?.start} to {backtestResults.period?.end} ({backtestResults.period?.days} days)
                                        </p>
                                    </div>
                                    <div className={`text-3xl font-bold ${backtestResults.total_return >= 0 ? 'text-green-400' : 'text-red-400'
                                        }`}>
                                        {backtestResults.total_return >= 0 ? '+' : ''}
                                        {backtestResults.total_return?.toFixed(2)}%
                                    </div>
                                </div>
                            </div>

                            {/* Metrics Grid */}
                            <div className="grid grid-cols-3 gap-4">
                                {/* Performance Metrics */}
                                <PerformanceMetrics metrics={backtestResults.metrics?.performance} />

                                {/* Risk Profile */}
                                <RiskProfile metrics={backtestResults.metrics?.risk} />

                                {/* Trade Statistics */}
                                <div className="bg-slate-800/50 rounded-lg p-6 border border-slate-700">
                                    <h3 className="text-sm font-semibold text-slate-400 mb-4 flex items-center">
                                        <svg className="w-4 h-4 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2" />
                                        </svg>
                                        TRADE STATISTICS
                                    </h3>
                                    <div className="space-y-4">
                                        <div>
                                            <div className="text-sm text-slate-400">Total Trades</div>
                                            <div className="text-2xl font-bold text-white mt-1">
                                                {backtestResults.summary?.total_trades || 0}
                                            </div>
                                            <div className="text-xs text-slate-500 mt-1">
                                                {backtestResults.summary?.winning_trades || 0} Wins • {backtestResults.summary?.losing_trades || 0} Losses
                                            </div>
                                        </div>

                                        <div className="pt-4 border-t border-slate-700">
                                            <div className="flex justify-between text-sm mb-2">
                                                <span className="text-slate-400">Avg Trade Duration</span>
                                                <span className="text-white font-medium">
                                                    {backtestResults.metrics?.trade_analysis?.avg_trade_duration_minutes?.toFixed(0) || 0}m
                                                </span>
                                            </div>
                                            <div className="flex justify-between text-sm">
                                                <span className="text-slate-400">Avg Win/Loss Ratio</span>
                                                <span className="text-white font-medium">
                                                    {backtestResults.metrics?.performance?.profit_factor?.toFixed(2) || 'N/A'}
                                                </span>
                                            </div>
                                        </div>
                                    </div>
                                </div>
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
        </div>
    );
}
