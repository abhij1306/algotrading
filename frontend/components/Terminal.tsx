// @ts-nocheck
'use client'

import { useState, useEffect } from 'react';
import { Search, TrendingUp, Plus, X, List, LayoutDashboard, Briefcase, History, Zap, ShieldCheck } from 'lucide-react';
import ActionCenter from './smart-trader/ActionCenter';
import TradingViewWidget from './charts/TradingViewWidget';
import { useWebSocket } from '@/hooks/useWebSocket';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import {
  Table,
  TableHeader,
  TableBody,
  TableRow,
  TableHead,
  TableCell
} from '@/components/ui/table';
import { apiClient } from '@/lib/api-client';
import { Price, PriceChange } from '@/components/ui/price';

interface WatchlistItem {
    symbol: string;
    ltp: number;
    change: number;
    change_pct: number;
    instrument_type: 'EQ' | 'FUT' | 'CE' | 'PE';
}

interface Position {
    id: string;
    symbol: string;
    type: 'BUY' | 'SELL';
    quantity: number;
    entry_price: number;
    current_price: number;
    pnl: number;
    pnl_pct: number;
    source?: 'MANUAL' | 'AGENT';
}

interface Signal {
    id: string;
    symbol: string;
    direction: 'LONG' | 'SHORT';
    confidence: number;
    reasoning: string;
    timestamp: string;
    signal_family: string;
    option_details?: {
        symbol: string;
        option_type: 'CE' | 'PE';
        strike: number;
        premium: number;
        quantity: number;
    };
    confidence_level: 'HIGH' | 'MEDIUM' | 'LOW';
}

/**
 * Renders the trading terminal UI for viewing a watchlist, real-time market ticks, signals, positions, and placing orders.
 *
 * Manages watchlist and position state, initializes and subscribes to backend live data, fetches signals and agent positions, provides search and watchlist management, and exposes order placement and position-closing actions. The UI includes a left sidebar (watchlist/signals/actions), a main area with chart/positions/orders/history tabs, and an order modal.
 *
 * @returns The React element for the Terminal trading interface.
 */
export default function Terminal() {
    const [tradingMode, setTradingMode] = useState<'PAPER' | 'LIVE'>('PAPER');
    const [watchlist, setWatchlist] = useState<WatchlistItem[]>([]);
    const [positions, setPositions] = useState<Position[]>([]);
    const [agentPositions, setAgentPositions] = useState<Position[]>([]);
    const [agentPnL, setAgentPnL] = useState(0);
    const [showAgentTrades, setShowAgentTrades] = useState(true);
    const [showManualTrades, setShowManualTrades] = useState(true);

    const [selectedSymbol, setSelectedSymbol] = useState<string>('');
    const [selectedInstrumentType, setSelectedInstrumentType] = useState<'EQ' | 'FUT' | 'CE' | 'PE'>('EQ');
    const [selectedLTP, setSelectedLTP] = useState(0);

    const [orderType, setOrderType] = useState<'BUY' | 'SELL'>('BUY');
    const [quantity, setQuantity] = useState(50);
    const [price, setPrice] = useState(0);
    const [orderMode, setOrderMode] = useState<'MARKET' | 'LIMIT' | 'SL'>('MARKET');
    const [activeTab, setActiveTab] = useState<'chart' | 'positions' | 'orders' | 'history'>('chart');

    // Sidebar & Signals State
    const [sidebarMode, setSidebarMode] = useState<'watchlist' | 'signals' | 'actions'>('watchlist');
    const [signals, setSignals] = useState<Signal[]>([]);
    const [loadingSignals, setLoadingSignals] = useState(false);

    const [searchQuery, setSearchQuery] = useState('');
    const [searchResults, setSearchResults] = useState<Array<{symbol: string, name?: string, sector?: string}>>([]);
    const [showSearchDropdown, setShowSearchDropdown] = useState(false);
    const [showOrderModal, setShowOrderModal] = useState(false);

    // WebSocket
    const { isConnected, lastMessage } = useWebSocket();

    const fetchWatchlist = async () => {
        const result = await apiClient.get<WatchlistItem[]>('/api/market/watchlist');
        if (result.data) {
            setWatchlist(result.data);
            if (result.data.length > 0 && !selectedSymbol) {
                selectSymbol(result.data[0]);
            }
        }
    };

    const refreshSignals = async () => {
        setLoadingSignals(true);
        const result = await apiClient.get<{signals: Signal[]}>('/api/signals?limit=50');
        if (result.data) {
            const filtered = (result.data.signals || []).filter((s: Signal) =>
                ['HIGH', 'MEDIUM'].includes(s.confidence_level)
            );
            setSignals(filtered);
        }
        setLoadingSignals(false);
    };

    const searchSymbols = async (query: string) => {
        if (query.length < 1) {
            setSearchResults([]);
            setShowSearchDropdown(false);
            return;
        }
        const result = await apiClient.get<any>(`/api/market/search/?query=${query}`);
        if (result.data) {
            const symbols = Array.isArray(result.data) ? result.data : (result.data.symbols || []);
            setSearchResults(symbols);
            setShowSearchDropdown(symbols.length > 0);
        }
    };

    const fetchAgentPositions = async () => {
        const posResult = await apiClient.get<{positions: any[]}>('/api/smart-trader/positions');
        if (posResult.data?.positions) {
            const transformed = posResult.data.positions.map((p: any) => ({
                id: (p.trade_id || p.id) as string,
                symbol: p.symbol as string,
                type: (p.side === 'LONG' ? 'BUY' : 'SELL') as 'BUY' | 'SELL',
                quantity: p.quantity as number,
                entry_price: p.entry_price as number,
                current_price: (p.current_price || p.entry_price) as number,
                pnl: (p.unrealized_pnl || 0) as number,
                pnl_pct: ((p.unrealized_pnl || 0) / ((p.entry_price as number) * (p.quantity as number))) * 100,
                source: 'AGENT' as const
            }));
            setAgentPositions(transformed);
        }

        const pnlResult = await apiClient.get<{total_pnl: number}>('/api/smart-trader/pnl');
        if (pnlResult.data) {
            setAgentPnL(pnlResult.data.total_pnl || 0);
        }
    };

    const selectSymbol = (item: WatchlistItem) => {
        setSelectedSymbol(item.symbol);
        setSelectedInstrumentType(item.instrument_type);
        setPrice(item.ltp);
        setSelectedLTP(item.ltp);
    };

    useEffect(() => {
        fetchWatchlist();
    }, []);

    // Handle Live Ticks from WebSocket
    useEffect(() => {
        if (!lastMessage) return;

        // Backend wraps ticks in {type: 'ticker', data: tick}
        let tick = lastMessage;
        if (lastMessage.type === 'ticker' && lastMessage.data) {
            tick = lastMessage.data;
        }

        if (tick.symbol && tick.ltp) {
            const rawSym = tick.symbol.replace('NSE:', '').replace('-EQ', '');

            setWatchlist(prev => prev.map(item => {
                if (item.symbol === rawSym) {
                    const ltp = tick.ltp!;
                    // Estimate prevClose if we don't have it explicitly to keep change % stable
                    const prevClose = item.ltp / (1 + (item.change_pct || 0) / 100);
                    const change = ltp - prevClose;
                    const change_pct = prevClose !== 0 ? (change / prevClose) * 100 : 0;

                    return {
                        ...item,
                        ltp: ltp,
                        change: tick.ch ?? change,
                        change_pct: tick.chp ?? change_pct
                    };
                }
                return item;
            }));

            if (rawSym === selectedSymbol) {
                setSelectedLTP(tick.ltp);
            }
        }
    }, [lastMessage, selectedSymbol]);

    // Ensure Backend WebSocket is connected and subscribed
    useEffect(() => {
        if (isConnected && watchlist.length > 0) {
            const triggerConnection = async () => {
                try {
                    // 1. Trigger backend connection to Fyers if needed
                    await apiClient.post('/api/websocket/connect');

                    // 2. Subscribe to current watchlist symbols
                    const symbols = watchlist.map(w => w.symbol);
                    const fyersSymbols = symbols.map(s => s.includes(':') ? s : `NSE:${s}-EQ`);
                    await apiClient.post('/api/websocket/subscribe', { symbols: fyersSymbols });

                    console.log('[Terminal] Live data initialized');
                } catch (err) {
                    console.error('[Terminal] Failed to initialize live data:', err);
                }
            };

            triggerConnection();
        }
    }, [isConnected, watchlist.length]);

    useEffect(() => {
        if (sidebarMode === 'signals') {
            refreshSignals();
        }
    }, [sidebarMode]);

    const triggerScan = async () => {
        setLoadingSignals(true);
        await apiClient.post('/api/smart-trader/scan');
        setTimeout(refreshSignals, 2000);
    };

    useEffect(() => {
        const interval = setInterval(fetchWatchlist, 10000);
        return () => clearInterval(interval);
    }, []);

    useEffect(() => {
        const timer = setTimeout(() => {
            if (searchQuery) searchSymbols(searchQuery);
            else {
                setSearchResults([]);
                setShowSearchDropdown(false);
            }
        }, 300);
        return () => clearTimeout(timer);
    }, [searchQuery]);

    useEffect(() => {
        fetchAgentPositions();
        const interval = setInterval(fetchAgentPositions, 30000);
        return () => clearInterval(interval);
    }, []);

    const addToWatchlist = async (symbol: string, type: 'EQ' | 'FUT' | 'CE' | 'PE' = 'EQ') => {
        await apiClient.post('/api/market/watchlist', { symbol, instrument_type: type });
        fetchWatchlist();
        setSearchQuery('');
        setShowSearchDropdown(false);
    };

    const removeFromWatchlist = async (symbol: string, type: 'EQ' | 'FUT' | 'CE' | 'PE') => {
        await apiClient.delete(`/api/market/watchlist/${symbol}`);
        fetchWatchlist();
        if (selectedSymbol === symbol) {
            setSelectedSymbol('');
        }
    };

    const executeOrder = async () => {
        if (!selectedSymbol) return;
        const orderPrice = price;

        if (tradingMode === 'PAPER') {
            const result = await apiClient.post<any>('/api/trading/paper/order', {
                symbol: selectedSymbol,
                type: orderType,
                quantity: quantity,
                price: orderPrice,
                instrument_type: selectedInstrumentType
            });
            if (result.data) {
                alert(`Order Executed: ${result.data.message || 'Success'}`);
                fetchAgentPositions();
            } else {
                alert(`Order Failed: ${result.error?.message || 'Unknown Error'}`);
            }
        } else {
            alert('Live trading not enabled. Switch to Paper.');
        }
    };

    const closePosition = (posId: string) => {
        const pos = positions.find(p => p.id === posId);
        if (pos && confirm(`Close position?\n${pos.symbol} ${pos.type} ${pos.quantity}`)) {
            setPositions(positions.filter(p => p.id !== posId));
        }
    };

    const closeAgentPosition = async (posId: string) => {
        const result = await apiClient.post('/api/smart-trader/close-position', { trade_id: posId });
        if (result.data) {
            await fetchAgentPositions();
            alert('Position closed');
        } else {
            alert('Failed to close position');
        }
    };

    const allPositions = [
        ...positions.map(p => ({ ...p, source: 'MANUAL' as const })),
        ...agentPositions
    ].filter(p => {
        if (!showAgentTrades && p.source === 'AGENT') return false;
        if (!showManualTrades && p.source === 'MANUAL') return false;
        return true;
    });

    const manualPnL = positions.reduce((sum, p) => sum + p.pnl, 0);
    const totalPnL = manualPnL + agentPnL;

    const initiateOrder = (type: 'BUY' | 'SELL', item: WatchlistItem) => {
        selectSymbol(item);
        setOrderType(type);
        setOrderMode('MARKET');
        setQuantity(1);
        setPrice(item.ltp);
        setShowOrderModal(true);
    };

    return (
        <div className="flex h-full gap-0 bg-background-dark max-w-full overflow-hidden relative">
            {/* Left Sidebar */}
            <div className="w-[350px] bg-card-dark border-r border-border-dark flex flex-col h-full shrink-0 relative z-[100]">
                {/* Sidebar Header */}
                <div className="grid grid-cols-2 p-3 border-b border-border-dark gap-2">
                    <Button
                        variant={sidebarMode === 'watchlist' ? 'secondary' : 'ghost'}
                        size="sm"
                        onClick={() => setSidebarMode('watchlist')}
                        className="uppercase tracking-wider font-bold"
                    >
                        <List className="w-4 h-4" /> Watchlist
                    </Button>
                    <Button
                        variant={sidebarMode === 'signals' ? 'secondary' : 'ghost'}
                        size="sm"
                        onClick={() => setSidebarMode('signals')}
                        className={`uppercase tracking-wider font-bold ${sidebarMode === 'signals' ? 'text-purple-400' : ''}`}
                    >
                        <Zap className="w-4 h-4" /> Signals
                    </Button>
                </div>

                {sidebarMode === 'watchlist' ? (
                    <>
                        {/* Search Bar */}
                        <div className="p-4 border-b border-border-dark relative">
                            <div className="relative">
                                <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-text-secondary z-10" />
                                <Input
                                    placeholder="Search symbols..."
                                    value={searchQuery}
                                    onChange={(e) => setSearchQuery(e.target.value)}
                                    onFocus={() => searchQuery.length > 0 && setShowSearchDropdown(true)}
                                    onBlur={() => setTimeout(() => setShowSearchDropdown(false), 200)}
                                    className="pl-10 h-10"
                                />
                                {showSearchDropdown && searchResults.length > 0 && (
                                    <div className="absolute z-[9999] mt-1 bg-[var(--color-surface)] rounded-lg shadow-xl border border-[var(--border-default)] max-h-60 overflow-y-auto w-full left-0">
                                        {searchResults.slice(0, 8).map((result) => (
                                            <button
                                                key={result.symbol}
                                                onClick={() => addToWatchlist(result.symbol, 'EQ')}
                                                className="w-full px-4 py-3 text-left hover:bg-[var(--glass-highlight)] text-sm border-b border-[var(--border-subtle)] last:border-0 flex justify-between items-center group"
                                            >
                                                <span className="font-bold text-[var(--text-primary)]">{result.symbol}</span>
                                                <Plus className="w-4 h-4 text-[var(--color-primary)] opacity-0 group-hover:opacity-100" />
                                            </button>
                                        ))}
                                    </div>
                                )}
                            </div>
                        </div>

                        {/* Watchlist Content */}
                        <div className="flex-1 overflow-y-auto scrollbar-hide">
                            {watchlist.length === 0 ? (
                                <div className="flex flex-col items-center justify-center h-64 opacity-50">
                                    <TrendingUp className="w-8 h-8 mb-3 text-text-secondary" strokeWidth={1.5} />
                                    <p className="text-xs font-medium text-text-secondary uppercase">Watchlist empty</p>
                                </div>
                            ) : (
                                watchlist.map((item, idx) => (
                                    <div
                                        key={`${item.symbol}-${idx}`}
                                        className={`group relative px-4 py-3 border-b border-border-dark/50 cursor-pointer transition-all hover:bg-white/5
                                        ${selectedSymbol === item.symbol ? 'bg-primary/5 border-l-2 border-l-primary' : 'border-l-2 border-l-transparent'}`}
                                        onClick={() => selectSymbol(item)}
                                    >
                                        <div className="flex items-center justify-between">
                                            <div className="font-bold text-sm text-[var(--text-primary)]">{item.symbol}</div>
                                            <div className={`font-mono text-sm text-right font-medium ${item.change >= 0 ? 'text-[var(--color-profit)]' : 'text-[var(--color-loss)]'}`}>
                                                {item.change >= 0 ? '+' : ''}{item.change_pct.toFixed(2)}%
                                            </div>
                                        </div>
                                        <div className="flex items-center justify-between mt-1">
                                            <span className="text-[10px] text-text-secondary uppercase tracking-wider">{item.instrument_type}</span>
                                            <span className="text-xs font-mono font-medium text-[var(--text-secondary)]">
                                                {item.ltp.toLocaleString('en-IN', { minimumFractionDigits: 2 })}
                                            </span>
                                        </div>

                                        {/* Hover Actions */}
                                        <div className="absolute right-4 top-1/2 -translate-y-1/2 flex gap-1.5 opacity-0 group-hover:opacity-100 bg-[var(--color-overlay)] shadow-xl border border-[var(--border-default)] rounded px-1.5 py-1 transition-all z-10">
                                            <button
                                                onClick={(e) => { e.stopPropagation(); initiateOrder('BUY', item); }}
                                                className="bg-[var(--color-primary)] hover:opacity-90 text-white text-[10px] font-bold px-2 py-0.5 rounded"
                                            >B</button>
                                            <button
                                                onClick={(e) => { e.stopPropagation(); initiateOrder('SELL', item); }}
                                                className="bg-[var(--color-loss)] hover:opacity-90 text-white text-[10px] font-bold px-2 py-0.5 rounded"
                                            >S</button>
                                            <button
                                                onClick={(e) => { e.stopPropagation(); removeFromWatchlist(item.symbol, item.instrument_type); }}
                                                className="text-[var(--text-muted)] hover:text-[var(--color-loss)] p-1"
                                            >
                                                <X className="w-3 h-3" />
                                            </button>
                                        </div>
                                    </div>
                                ))
                            )}
                        </div>
                    </>
                ) : sidebarMode === 'signals' ? (
                    <div className="flex flex-col h-full">
                        <div className="p-3 border-b border-border-dark flex justify-between items-center">
                            <span className="text-[10px] font-bold text-[var(--text-muted)] uppercase tracking-wider">Signals</span>
                            <Button variant="ghost" size="sm" onClick={triggerScan} disabled={loadingSignals} className="h-7 text-[10px]">
                                <Zap className={`w-3 h-3 ${loadingSignals ? 'animate-spin' : ''}`} /> Scan
                            </Button>
                        </div>

                        <div className="flex-1 overflow-y-auto p-3 space-y-3">
                            {loadingSignals ? (
                                <div className="flex flex-col items-center justify-center py-20 opacity-50 text-center">
                                    <Zap className="w-12 h-12 text-purple-500 mb-4 animate-pulse" />
                                    <h3 className="text-sm font-bold">Scanning Markets...</h3>
                                </div>
                            ) : signals.length === 0 ? (
                                <div className="flex flex-col items-center justify-center py-20 opacity-40 text-center">
                                    <ShieldCheck className="w-8 h-8 mb-2" />
                                    <p className="text-xs">No active signals</p>
                                </div>
                            ) : (
                                signals.map((signal) => (
                                    <Card key={signal.id} variant="glass" className="p-3 hover:border-purple-500/50 transition-all group relative overflow-hidden">
                                        <div className={`absolute left-0 top-0 bottom-0 w-1 ${signal.direction === 'LONG' ? 'bg-[var(--color-primary)]' : 'bg-[var(--color-loss)]'}`} />
                                        <div className="flex justify-between items-start mb-2 pl-2">
                                            <div>
                                                <div className="flex items-center gap-2">
                                                    <h4 className="text-sm font-bold">{signal.symbol}</h4>
                                                </div>
                                                <div className="flex gap-2 mt-1">
                                                    <span className={`text-[9px] font-bold px-1 py-0.5 rounded uppercase border ${signal.direction === 'LONG' ? 'text-blue-400 border-blue-400/30' : 'text-red-400 border-red-400/30'}`}>
                                                        {signal.direction}
                                                    </span>
                                                </div>
                                            </div>
                                            <div className="text-right">
                                                <div className="text-sm font-bold">{Math.round(signal.confidence * 100)}%</div>
                                                <div className="text-[9px] text-[var(--text-muted)] uppercase">Score</div>
                                            </div>
                                        </div>
                                        <div className="bg-black/20 rounded p-2 mb-3 text-[10px] text-[var(--text-secondary)] italic">
                                            {Array.isArray(signal.reasoning) ? signal.reasoning[0] : signal.reasoning}
                                        </div>
                                        <Button
                                            variant="primary"
                                            size="sm"
                                            className="w-full h-8 text-xs bg-purple-600 hover:bg-purple-500"
                                            onClick={() => {
                                                setSelectedSymbol(signal.symbol);
                                                setOrderType(signal.direction === 'LONG' ? 'BUY' : 'SELL');
                                                setShowOrderModal(true);
                                            }}
                                        >
                                            Execute
                                        </Button>
                                    </Card>
                                ))
                            )}
                        </div>
                    </div>
                ) : (
                    <div className="flex-1 overflow-y-auto">
                        <ActionCenter />
                    </div>
                )}
            </div>

            {/* Main Content Area */}
            <div className="flex-1 bg-background-dark h-full flex flex-col overflow-hidden">
                <div className="flex items-center justify-between px-6 py-3 border-b border-border-dark bg-card-dark">
                    <div className="flex items-center gap-1 bg-black/20 p-1 rounded-lg">
                        {['chart', 'positions', 'orders', 'history'].map((tab) => (
                            <button
                                key={tab}
                                onClick={() => setActiveTab(tab as any)}
                                className={`px-4 py-1.5 rounded-md text-sm font-medium transition-all ${activeTab === tab ? 'bg-[var(--color-primary)] text-white' : 'text-text-secondary hover:text-white'}`}
                            >
                                {tab.charAt(0).toUpperCase() + tab.slice(1)}
                            </button>
                        ))}
                    </div>

                    <div className="flex items-center gap-2 px-2 py-1 bg-black/40 rounded-lg border border-white/10">
                        <Button variant={tradingMode === 'PAPER' ? 'primary' : 'ghost'} size="sm" className="h-7 text-[10px]" onClick={() => setTradingMode('PAPER')}>PAPER</Button>
                        <Button variant={tradingMode === 'LIVE' ? 'loss' : 'ghost'} size="sm" className="h-7 text-[10px]" onClick={() => setTradingMode('LIVE')}>LIVE</Button>
                    </div>

                    <div className="flex items-center gap-4">
                        <div className="px-4 py-1.5 bg-black/30 rounded border border-white/5 flex items-center gap-3">
                            <span className="text-[10px] text-text-muted uppercase font-bold">Total P&L</span>
                            <span className={`font-bold font-mono ${totalPnL >= 0 ? 'text-[var(--color-profit)]' : 'text-[var(--color-loss)]'}`}>
                                ₹{totalPnL.toFixed(2)}
                            </span>
                        </div>
                    </div>
                </div>

                <div className="flex-1 p-6 overflow-hidden relative">
                    <div className="absolute inset-0 bg-[linear-gradient(rgba(255,255,255,0.01)_1px,transparent_1px),linear-gradient(90deg,rgba(255,255,255,0.01)_1px,transparent_1px)] bg-[size:40px_40px] pointer-events-none"></div>

                    {activeTab === 'chart' && (
                        <Card variant="glass" className="h-full w-full p-0 overflow-hidden relative z-10">
                            {selectedSymbol ? <TradingViewWidget symbol={selectedSymbol} /> : <div className="flex h-full items-center justify-center text-[var(--text-muted)]">Select symbol</div>}
                        </Card>
                    )}

                    {activeTab === 'positions' && (
                        <div className="relative z-10 h-full overflow-auto">
                            {allPositions.length === 0 ? (
                                <div className="h-full flex flex-col items-center justify-center text-[var(--text-muted)]">
                                    <Briefcase className="w-12 h-12 mb-4 opacity-20" />
                                    <p>No open positions</p>
                                </div>
                            ) : (
                                <Card variant="glass" className="p-0 overflow-hidden">
                                    <Table>
                                        <TableHeader>
                                            <TableRow variant="ghost">
                                                <TableHead>Instrument</TableHead>
                                                <TableHead numeric>Qty</TableHead>
                                                <TableHead numeric>Entry</TableHead>
                                                <TableHead numeric>LTP</TableHead>
                                                <TableHead numeric>P&L</TableHead>
                                                <TableHead className="text-center">Action</TableHead>
                                            </TableRow>
                                        </TableHeader>
                                        <TableBody>
                                            {allPositions.map((pos) => (
                                                <TableRow key={pos.id}>
                                                    <TableCell className="font-bold">
                                                        {pos.symbol}
                                                        <span className="ml-2 text-[9px] uppercase opacity-60">{pos.source}</span>
                                                    </TableCell>
                                                    <TableCell numeric>{pos.quantity}</TableCell>
                                                    <TableCell numeric><Price value={pos.entry_price} /></TableCell>
                                                    <TableCell numeric><Price value={pos.current_price} /></TableCell>
                                                    <TableCell numeric>
                                                        <PriceChange change={pos.pnl} changePercent={pos.pnl_pct} />
                                                    </TableCell>
                                                    <TableCell className="text-center">
                                                        <Button variant="ghost" size="sm" onClick={() => pos.source === 'AGENT' ? closeAgentPosition(pos.id) : closePosition(pos.id)}>Close</Button>
                                                    </TableCell>
                                                </TableRow>
                                            ))}
                                        </TableBody>
                                    </Table>
                                </Card>
                            )}
                        </div>
                    )}

                    {(activeTab === 'orders' || activeTab === 'history') && (
                        <div className="h-full flex flex-col items-center justify-center text-[var(--text-muted)]">
                            <History className="w-12 h-12 mb-4 opacity-20" />
                            <p>No data available</p>
                        </div>
                    )}
                </div>
            </div>

            {/* Order Modal */}
            {showOrderModal && selectedSymbol && (
                <div className="fixed inset-0 z-[9999] bg-black/80 backdrop-blur-md flex items-center justify-center p-4" onClick={() => setShowOrderModal(false)}>
                    <Card variant="default" className="w-full max-w-md overflow-hidden bg-[var(--color-base)]" onClick={e => e.stopPropagation()}>
                        <div className={`p-6 ${orderType === 'BUY' ? 'bg-[var(--color-primary)]' : 'bg-[var(--color-loss)]'} text-white`}>
                            <div className="flex justify-between items-center mb-4">
                                <h3 className="text-xl font-bold">{orderType} {selectedSymbol}</h3>
                                <X className="cursor-pointer" onClick={() => setShowOrderModal(false)} />
                            </div>
                            <div className="flex justify-between items-end">
                                <div>
                                    <p className="text-[10px] uppercase opacity-70">Last Traded Price</p>
                                    <p className="text-2xl font-mono font-bold">₹{selectedLTP.toFixed(2)}</p>
                                </div>
                                <div className="text-[10px] font-bold border border-white/20 px-2 py-1 rounded bg-white/10 uppercase tracking-widest">{tradingMode}</div>
                            </div>
                        </div>
                        <div className="p-6 space-y-6">
                            <div className="grid grid-cols-2 gap-4">
                                <Input label="Quantity" type="number" value={quantity} onChange={e => setQuantity(parseInt(e.target.value))} />
                                <Input label="Price" type="number" value={price} disabled={orderMode === 'MARKET'} onChange={e => setPrice(parseFloat(e.target.value))} />
                            </div>
                            <Button variant={orderType === 'BUY' ? 'primary' : 'loss'} className="w-full py-6 text-lg font-bold" onClick={() => { executeOrder(); setShowOrderModal(false); }}>
                                {orderType} {quantity} UNITS
                            </Button>
                        </div>
                    </Card>
                </div>
            )}
        </div>
    );
}