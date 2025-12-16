'use client'

import { useState, useEffect } from 'react';
import SignalCard from './SignalCard';

interface SmartTraderStatus {
    success: boolean;
    is_running: boolean;
    signal_count: number;
    generators: string[];
}

interface Signal {
    id: string;
    symbol: string;
    direction: string;
    timeframe: string;
    confluence_count: number;
    aggregate_strength: number;
    confidence_level: string;
    final_confidence: number;
    signal_families: string[];
    signal_names: string[];
    reasons: string[];
    llm_narrative: string | null;
    risk_flags: string[];
    timestamp: string;
}

export default function SmartTraderDashboard() {
    const [status, setStatus] = useState<SmartTraderStatus | null>(null);
    const [signals, setSignals] = useState<Signal[]>([]);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [confidenceFilter, setConfidenceFilter] = useState('ALL');
    const [familyFilter, setFamilyFilter] = useState('ALL');

    // Fetch status
    const fetchStatus = async () => {
        try {
            const res = await fetch('http://localhost:8000/api/smart-trader/status');
            const data = await res.json();
            if (data.success) {
                setStatus(data);
            }
        } catch (err: any) {
            console.error('Status fetch error:', err);
        }
    };

    // Fetch signals
    const fetchSignals = async () => {
        try {
            let url = 'http://localhost:8000/api/smart-trader/signals?limit=50';
            if (confidenceFilter !== 'ALL') {
                url += `&confidence_level=${confidenceFilter}`;
            }
            if (familyFilter !== 'ALL') {
                url += `&signal_family=${familyFilter}`;
            }

            const res = await fetch(url);
            const data = await res.json();
            if (data.success) {
                setSignals(data.signals || []);
            }
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
                headers: { 'Content-Type': 'application/json' }
            });
            const data = await res.json();

            if (data.success) {
                fetchStatus();
                // Trigger initial scan
                await handleManualScan();
            } else {
                setError(data.message || 'Failed to start scanner');
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
    const handleTakeTrade = async (signalId: string) => {
        try {
            const res = await fetch(`http://localhost:8000/api/smart-trader/execute/${signalId}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' }
            });
            const data = await res.json();

            if (data.success) {
                alert(`✅ Trade executed successfully!\n\nTrade ID: ${data.trade_id}\n\nCheck the Terminal tab to see your position.`);
                fetchStatus();
                fetchSignals();
            } else {
                // Show the actual error message from backend
                const errorMsg = data.error || data.message || 'Unknown error occurred';
                alert(`❌ Trade failed:\n\n${errorMsg}`);
            }
        } catch (err: any) {
            console.error('Trade execution error:', err);
            alert(`❌ Network error:\n\n${err.message || 'Failed to connect to server'}`);
        }
    };

    // Manual Scan
    const handleManualScan = async () => {
        try {
            const res = await fetch('http://localhost:8000/api/smart-trader/scan', { method: 'POST' });
            const data = await res.json();
            if (data.success) {
                setTimeout(fetchSignals, 2000);
            }
        } catch (err: any) {
            console.error('Scan error:', err);
        }
    };

    // Poll for updates
    useEffect(() => {
        fetchStatus();
        fetchSignals();

        const interval = setInterval(() => {
            fetchStatus();
            if (status?.is_running) {
                fetchSignals();
            }
        }, 10000); // Poll every 10 seconds

        return () => clearInterval(interval);
    }, [confidenceFilter, familyFilter]);

    // Confidence filters
    const confidenceFilters = [
        { id: 'ALL', label: 'All Confidence', emoji: '📊' },
        { id: 'HIGH', label: 'High', emoji: '🟢' },
        { id: 'MEDIUM', label: 'Medium', emoji: '🟡' },
        { id: 'LOW', label: 'Low', emoji: '⚪' }
    ];

    // Signal family filters
    const familyFilters = [
        { id: 'ALL', label: 'All Families' },
        { id: 'MOMENTUM', label: '📈 Momentum' },
        { id: 'VOLUME', label: '🔊 Volume' },
        { id: 'RANGE_EXPANSION', label: '📏 Range' },
        { id: 'REVERSAL', label: '🔄 Reversal' },
        { id: 'INDEX_ALIGNMENT', label: '📍 Index' }
    ];

    // Get counts for filters
    const getConfidenceCount = (level: string) => {
        if (level === 'ALL') return signals.length;
        return signals.filter(s => s.confidence_level === level).length;
    };

    const getFamilyCount = (family: string) => {
        if (family === 'ALL') return signals.length;
        return signals.filter(s => s.signal_families.includes(family)).length;
    };

    return (
        <div className="space-y-4">
            {/* Header with Stats */}
            <div className="bg-gradient-to-r from-card-dark via-card-dark to-primary/5 rounded-xl border border-border-dark p-4">
                <div className="flex items-center justify-between">
                    <div>
                        <h2 className="text-xl font-bold text-white">Smart Trader</h2>
                        <p className="text-xs opacity-60">Deterministic Signals + LLM Enhancement</p>
                    </div>

                    {/* Stats Row */}
                    <div className="flex items-center gap-6">
                        {status && (
                            <>
                                <div className="text-center">
                                    <div className="text-xs opacity-60">Active Signals</div>
                                    <div className="font-bold text-primary">{status.signal_count}</div>
                                </div>
                                <div className="text-center">
                                    <div className="text-xs opacity-60">Generators</div>
                                    <div className="font-bold text-white">{status.generators.length}</div>
                                </div>
                            </>
                        )}

                        {/* Start/Stop Button */}
                        <div className="flex items-center gap-2">
                            {!status?.is_running ? (
                                <button
                                    onClick={handleStart}
                                    disabled={loading}
                                    className="px-5 py-2 bg-green-600 hover:bg-green-700 text-white rounded-lg font-medium transition-colors disabled:opacity-50"
                                >
                                    {loading ? 'Starting...' : '▶ Start Scanner'}
                                </button>
                            ) : (
                                <>
                                    <button
                                        onClick={handleManualScan}
                                        className="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg font-medium transition-colors"
                                    >
                                        🔄 Scan Now
                                    </button>
                                    <button
                                        onClick={handleStop}
                                        className="px-5 py-2 bg-red-600 hover:bg-red-700 text-white rounded-lg font-medium transition-colors"
                                    >
                                        ⏹ Stop
                                    </button>
                                </>
                            )}
                        </div>
                    </div>
                </div>

                {/* Confidence Level Filters */}
                <div className="mt-4">
                    <div className="text-xs font-medium text-gray-400 mb-2">Confidence Level</div>
                    <div className="flex gap-2 overflow-x-auto pb-2 scrollbar-thin">
                        {confidenceFilters.map(filter => (
                            <button
                                key={filter.id}
                                onClick={() => setConfidenceFilter(filter.id)}
                                className={`px-3 py-1.5 rounded-full text-xs font-medium whitespace-nowrap transition-colors flex items-center gap-2 ${confidenceFilter === filter.id
                                    ? 'bg-primary text-white'
                                    : 'bg-white/5 text-gray-400 hover:bg-white/10'
                                    }`}
                            >
                                {filter.emoji} {filter.label}
                                <span className={`px-1.5 py-0.5 rounded-full text-[10px] ${confidenceFilter === filter.id ? 'bg-white/20' : 'bg-black/20'
                                    }`}>
                                    {getConfidenceCount(filter.id)}
                                </span>
                            </button>
                        ))}
                    </div>
                </div>

                {/* Signal Family Filters */}
                <div className="mt-3">
                    <div className="text-xs font-medium text-gray-400 mb-2">Signal Family</div>
                    <div className="flex gap-2 overflow-x-auto pb-2 scrollbar-thin">
                        {familyFilters.map(filter => (
                            <button
                                key={filter.id}
                                onClick={() => setFamilyFilter(filter.id)}
                                className={`px-3 py-1.5 rounded-full text-xs font-medium whitespace-nowrap transition-colors flex items-center gap-2 ${familyFilter === filter.id
                                    ? 'bg-primary text-white'
                                    : 'bg-white/5 text-gray-400 hover:bg-white/10'
                                    }`}
                            >
                                {filter.label}
                                <span className={`px-1.5 py-0.5 rounded-full text-[10px] ${familyFilter === filter.id ? 'bg-white/20' : 'bg-black/20'
                                    }`}>
                                    {getFamilyCount(filter.id)}
                                </span>
                            </button>
                        ))}
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
