'use client'

import { useState, useEffect } from 'react';
import SignalCard from './SignalCard';

interface SmartTraderStatus {
    is_running: boolean;
    market_open: boolean;
    last_scan_time: string | null;
    active_signals: number;
}

interface Signal {
    id: string;
    rank: number;
    symbol: string;
    instrument_type: string;
    direction: string;
    momentum_score: number;
    composite_score: number;
    confidence: string;
    entry_price: number;
    stop_loss: number;
    target: number;
    reasons: string[];
    risk_reward_ratio: number;
    timestamp: string;
}

export default function SmartTraderDashboard() {
    const [status, setStatus] = useState<SmartTraderStatus | null>(null);
    const [signals, setSignals] = useState<Signal[]>([]);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);

    // Fetch status
    const fetchStatus = async () => {
        try {
            const res = await fetch('http://localhost:8000/api/smart-trader/status');
            const data = await res.json();
            setStatus(data);
        } catch (err: any) {
            console.error('Status fetch error:', err);
        }
    };

    // Fetch signals
    const fetchSignals = async () => {
        try {
            const res = await fetch('http://localhost:8000/api/smart-trader/signals');
            const data = await res.json();
            setSignals(data.signals || []);
        } catch (err: any) {
            console.error('Signals fetch error:', err);
        }
    };

    // Start Smart Trader
    const handleStart = async () => {
        setLoading(true);
        setError(null);
        try {
            const res = await fetch('http://localhost:8000/api/smart-trader/start', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({})
            });
            const data = await res.json();

            if (data.status === 'started' || data.status === 'already_running') {
                fetchStatus();
                fetchSignals();
            } else if (data.status === 'market_closed') {
                setError(data.message);
            }
        } catch (err: any) {
            setError(err.message);
        } finally {
            setLoading(false);
        }
    };

    // Stop Smart Trader
    const handleStop = async () => {
        try {
            await fetch('http://localhost:8000/api/smart-trader/stop', { method: 'POST' });
            fetchStatus();
        } catch (err: any) {
            setError(err.message);
        }
    };

    // Execute trade from signal
    const handleTakeTrade = async (signalId: string, tradeType: string = 'SPOT', optionType?: string) => {
        try {
            const res = await fetch('http://localhost:8000/api/smart-trader/execute-trade', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    signal_id: signalId,
                    trade_type: tradeType,
                    option_type: optionType
                })
            });
            const data = await res.json();

            if (data.status === 'rejected') {
                alert(`Trade rejected: ${data.message}`);
            } else if (data.status === 'success') {
                alert('Trade executed successfully! View in Terminal tab.');
                fetchStatus();
            }
        } catch (err: any) {
            alert(`Trade error: ${err.message}`);
        }
    };

    // Manual Scan
    const handleManualScan = async () => {
        try {
            await fetch('http://localhost:8000/api/smart-trader/scan', { method: 'POST' });
            setTimeout(fetchSignals, 2000);
        } catch (err: any) {
            console.error('Scan error:', err);
        }
    };

    // Poll for updates
    useEffect(() => {
        fetchStatus();

        const statusInterval = setInterval(() => {
            fetchStatus();
            if (status?.is_running) {
                fetchSignals();
            }
        }, 10000);

        return () => {
            clearInterval(statusInterval);
        };
    }, [status?.is_running]);

    return (
        <div className="space-y-4">
            {/* Header with Stats */}
            <div className="bg-gradient-to-r from-card-dark via-card-dark to-primary/5 rounded-xl border border-border-dark p-4">
                <div className="flex items-center justify-between">
                    <div>
                        <h2 className="text-xl font-bold text-white">Trading Signals</h2>
                        <p className="text-xs opacity-60">AI-Powered Market Opportunities</p>
                    </div>

                    {/* Stats Row */}
                    <div className="flex items-center gap-6">
                        {status && (
                            <>
                                <div className="text-center">
                                    <div className="text-xs opacity-60">Active Signals</div>
                                    <div className="font-bold text-primary">{signals.length}</div>
                                </div>
                                <div className="text-center">
                                    <div className="text-xs opacity-60">Market</div>
                                    <div className={`font-bold ${status.market_open ? 'text-green-400' : 'text-red-400'}`}>
                                        {status.market_open ? '🟢 OPEN' : '🔴 CLOSED'}
                                    </div>
                                </div>
                            </>
                        )}

                        {/* Single Start/Stop Button */}
                        <div className="flex items-center gap-2">
                            {!status?.is_running ? (
                                <button
                                    onClick={() => { handleStart(); handleManualScan(); }}
                                    disabled={loading}
                                    className="px-5 py-2 bg-green-600 hover:bg-green-700 text-white rounded-lg font-medium transition-colors disabled:opacity-50"
                                >
                                    {loading ? 'Starting...' : '▶ Start Scanner'}
                                </button>
                            ) : (
                                <button
                                    onClick={handleStop}
                                    className="px-5 py-2 bg-red-600 hover:bg-red-700 text-white rounded-lg font-medium transition-colors"
                                >
                                    ⏹ Stop Scanner
                                </button>
                            )}
                        </div>
                    </div>
                </div>

                {error && (
                    <div className="mt-3 p-3 bg-red-500/10 border border-red-500/30 rounded-lg text-red-400 text-sm">
                        {error}
                    </div>
                )}
            </div>

            {/* Signals Grid */}
            <div className="bg-card-dark rounded-xl border border-border-dark p-4 min-h-[600px]">
                {signals.length === 0 ? (
                    <div className="text-center py-24 opacity-40">
                        <div className="text-6xl mb-4">📊</div>
                        <p className="text-lg">No signals available</p>
                        <p className="text-sm mt-2">Click "Start Scanner" to find trading opportunities</p>
                    </div>
                ) : (
                    <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
                        {signals.map((signal) => (
                            <SignalCard
                                key={signal.id}
                                signal={signal}
                                onTakeTrade={handleTakeTrade}
                            />
                        ))}
                    </div>
                )}
            </div>
        </div>
    );
}
