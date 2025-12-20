'use client'

import { useState, useEffect } from 'react';
import { Search, TrendingUp, Plus, X, List, LayoutDashboard, Briefcase, History, Zap } from 'lucide-react';

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
}

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
    const [activeTab, setActiveTab] = useState<'positions' | 'orders' | 'history'>('positions');

    // Sidebar & Signals State
    const [sidebarMode, setSidebarMode] = useState<'watchlist' | 'signals'>('watchlist');
    const [signals, setSignals] = useState<Signal[]>([]);

    const [searchQuery, setSearchQuery] = useState('');
    const [searchResults, setSearchResults] = useState<any[]>([]);
    const [showSearchDropdown, setShowSearchDropdown] = useState(false);
    const [showOrderModal, setShowOrderModal] = useState(false);

    // Load watchlist from localStorage
    useEffect(() => {
        const saved = localStorage.getItem('terminal_watchlist');
        if (saved) {
            try {
                const parsed = JSON.parse(saved);
                setWatchlist(parsed);
                if (parsed.length > 0) {
                    setSelectedSymbol(parsed[0].symbol);
                    setSelectedInstrumentType(parsed[0].instrument_type);
                    setPrice(parsed[0].ltp);
                    setSelectedLTP(parsed[0].ltp);
                }
            } catch { }
        }
    }, []);

    // Save watchlist
    useEffect(() => {
        if (watchlist.length > 0) {
            localStorage.setItem('terminal_watchlist', JSON.stringify(watchlist));
        } else {
            localStorage.removeItem('terminal_watchlist');
        }
    }, [watchlist]);

    // Fetch Signals
    useEffect(() => {
        if (sidebarMode === 'signals') {
            fetch('http://localhost:8000/api/signals')
                .then(res => res.json())
                .then(data => setSignals(data.signals || []))
                .catch(err => console.error("Failed to fetch signals", err));
        }
    }, [sidebarMode]);

    const isMarketOpen = () => {
        const now = new Date();
        const currentTime = now.getHours() * 60 + now.getMinutes();
        return currentTime >= 555 && currentTime <= 930;
    };

    const fetchWatchlistQuotes = async () => {
        try {
            const symbols = watchlist.map(w => w.symbol).join(',');
            if (!symbols) return;

            if (isMarketOpen()) {
                const res = await fetch(`http://localhost:8000/api/quotes/live?symbols=${symbols}`);
                const data = await res.json();
                if (data.quotes) {
                    setWatchlist(prev => prev.map(item => {
                        const quote = data.quotes[item.symbol];
                        return quote?.ltp > 0 ? { ...item, ltp: quote.ltp, change: quote.ltp - (quote.prev_close || quote.ltp), change_pct: quote.change_pct || 0 } : item;
                    }));
                }
            } else {
                const updatedWatchlist = await Promise.all(
                    watchlist.map(async (item) => {
                        if (item.instrument_type === 'EQ') {
                            try {
                                const res = await fetch(`http://localhost:8000/api/screener?symbol=${item.symbol}&limit=1`);
                                const data = await res.json();
                                if (data.data?.[0]) return { ...item, ltp: data.data[0].close || item.ltp, change: 0, change_pct: 0 };
                            } catch { }
                        }
                        return item;
                    })
                );
                setWatchlist(updatedWatchlist);
            }
        } catch { }
    };

    useEffect(() => {
        const interval = setInterval(fetchWatchlistQuotes, 5000);
        return () => clearInterval(interval);
    }, [watchlist]);

    useEffect(() => {
        if (selectedSymbol) {
            const item = watchlist.find(w => w.symbol === selectedSymbol && w.instrument_type === selectedInstrumentType);
            if (item && item.ltp > 0) {
                setPrice(item.ltp);
                setSelectedLTP(item.ltp);
            }
        }
    }, [watchlist, selectedSymbol, selectedInstrumentType]);

    // Fetch agent positions from Smart Trader
    const fetchAgentPositions = async () => {
        try {
            const response = await fetch('http://localhost:8000/api/smart-trader/positions');
            const data = await response.json();

            if (data.positions && Array.isArray(data.positions)) {
                // Transform agent positions to match Terminal format
                const transformed = data.positions.map((p: any) => ({
                    id: p.trade_id || p.id,
                    symbol: p.symbol,
                    type: p.side === 'LONG' ? 'BUY' : 'SELL',
                    quantity: p.quantity,
                    entry_price: p.entry_price,
                    current_price: p.current_price || p.entry_price,
                    pnl: p.unrealized_pnl || 0,
                    pnl_pct: ((p.unrealized_pnl || 0) / (p.entry_price * p.quantity)) * 100,
                    source: 'AGENT' as const
                }));

                setAgentPositions(transformed);
            }

            // Fetch P&L
            const pnlResponse = await fetch('http://localhost:8000/api/smart-trader/pnl');
            const pnlData = await pnlResponse.json();
            setAgentPnL(pnlData.total_pnl || 0);
        } catch (error) {
            console.error('Failed to fetch agent positions:', error);
        }
    };

    // Fetch agent positions on mount and every 30 seconds
    useEffect(() => {
        fetchAgentPositions();
        const interval = setInterval(fetchAgentPositions, 30000); // Every 30s
        return () => clearInterval(interval);
    }, []);

    const searchSymbols = async (query: string) => {
        if (query.length < 1) {
            setSearchResults([]);
            setShowSearchDropdown(false);
            return;
        }
        try {
            const res = await fetch(`http://localhost:8000/api/symbols/search?q=${query}`);
            const data = await res.json();
            setSearchResults(data.symbols || []);
            setShowSearchDropdown(data.symbols?.length > 0);
        } catch { }
    };

    const addToWatchlist = async (symbol: string, type: 'EQ' | 'FUT' | 'CE' | 'PE' = 'EQ') => {
        if (watchlist.find(w => w.symbol === symbol && w.instrument_type === type)) return;

        let ltp = 0, change_pct = 0;

        if (isMarketOpen()) {
            try {
                const res = await fetch(`http://localhost:8000/api/quotes/live?symbols=${symbol}`);
                const data = await res.json();
                ltp = data.quotes?.[symbol]?.ltp || 0;
                change_pct = data.quotes?.[symbol]?.change_pct || 0;
            } catch { }
        }

        if (ltp === 0 && type === 'EQ') {
            try {
                const res = await fetch(`http://localhost:8000/api/screener?symbol=${symbol}&limit=1`);
                const data = await res.json();
                ltp = data.data?.[0]?.close || 0;
            } catch { }
        }

        setWatchlist([...watchlist, { symbol, instrument_type: type, ltp, change: 0, change_pct }]);
        setSearchQuery('');
        setShowSearchDropdown(false);
    };

    const removeFromWatchlist = (symbol: string, type: 'EQ' | 'FUT' | 'CE' | 'PE') => {
        setWatchlist(watchlist.filter(w => !(w.symbol === symbol && w.instrument_type === type)));
        if (selectedSymbol === symbol && selectedInstrumentType === type) {
            setSelectedSymbol('');
            setPrice(0);
        }
    };

    const selectSymbol = (item: WatchlistItem) => {
        setSelectedSymbol(item.symbol);
        setSelectedInstrumentType(item.instrument_type);
        setPrice(item.ltp);
        setSelectedLTP(item.ltp);
    };

    const executeOrder = async () => {
        if (!selectedSymbol) {
            alert('Please select a symbol');
            return;
        }

        const orderPrice = orderMode === 'MARKET' ? price : price;

        if (tradingMode === 'PAPER') {
            try {
                const res = await fetch('http://localhost:8000/api/trading/paper/order', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        symbol: selectedSymbol,
                        type: orderType,
                        quantity: quantity,
                        price: orderPrice,
                        instrument_type: selectedInstrumentType
                    })
                });
                const data = await res.json();
                if (res.ok) {
                    alert(`Paper Order Executed: ${data.message || 'Success'}`);
                    fetchAgentPositions(); // Refresh positions
                } else {
                    alert(`Order Failed: ${data.error}`);
                }
            } catch (e) {
                alert('Network error placing order');
            }
        } else {
            alert('Live trading not enabled. Switch to Paper.');
        }
        // Keep modal open or closed? Let's close it for better UX if successful, but alert already happened.
        // setShowOrderModal(false); 
    };

    const closePosition = (posId: string) => {
        const pos = positions.find(p => p.id === posId);
        if (pos && confirm(`Close position?\n${pos.symbol} ${pos.type} ${pos.quantity}\nP&L: ₹${pos.pnl.toFixed(2)}`)) {
            setPositions(positions.filter(p => p.id !== posId));
        }
    };

    const closeAgentPosition = async (posId: string) => {
        try {
            const response = await fetch('http://localhost:8000/api/smart-trader/close-position', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ trade_id: posId })
            });

            if (response.ok) {
                await fetchAgentPositions();
                alert('Agent position closed successfully');
            } else {
                alert('Failed to close agent position');
            }
        } catch (error) {
            console.error('Error closing agent position:', error);
            alert('Error closing agent position');
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
            <div className="w-[350px] bg-card-dark border-r border-border-dark flex flex-col h-full overflow-hidden shrink-0">
                {/* Sidebar Mode Switch */}
                <div className="flex items-center p-2 border-b border-border-dark gap-2">
                    <button
                        onClick={() => setSidebarMode('watchlist')}
                        className={`flex-1 py-2 text-xs font-bold uppercase tracking-wider rounded transition-colors flex items-center justify-center gap-2 ${sidebarMode === 'watchlist' ? 'bg-white/10 text-white' : 'text-text-secondary hover:text-white hover:bg-white/5'
                            }`}
                    >
                        <List className="w-4 h-4" /> Watchlist
                    </button>
                    <button
                        onClick={() => setSidebarMode('signals')}
                        className={`flex-1 py-2 text-xs font-bold uppercase tracking-wider rounded transition-colors flex items-center justify-center gap-2 ${sidebarMode === 'signals' ? 'bg-purple-500/20 text-purple-400 border border-purple-500/30 shadow-[0_0_10px_rgba(168,85,247,0.2)]' : 'text-text-secondary hover:text-white hover:bg-white/5'
                            }`}
                    >
                        <Zap className="w-4 h-4" /> AI Signals
                    </button>
                </div>

                {sidebarMode === 'watchlist' ? (
                    <>
                        {/* Search Bar */}
                        <div className="p-4 border-b border-border-dark">
                            <div className="relative">
                                <Search className="absolute left-3 top-2.5 w-4 h-4 text-text-secondary" />
                                <input
                                    type="text"
                                    placeholder="Search eg: infy bse, nifty..."
                                    value={searchQuery}
                                    onChange={(e) => { setSearchQuery(e.target.value); searchSymbols(e.target.value); }}
                                    onFocus={() => searchQuery.length > 0 && setShowSearchDropdown(true)}
                                    onBlur={() => setTimeout(() => setShowSearchDropdown(false), 200)}
                                    className="w-full pl-10 pr-4 py-2 bg-background-dark border border-border-dark rounded text-xs font-medium text-text-primary outline-none focus:border-primary placeholder:text-text-secondary focus:shadow-[0_0_0_1px_rgba(59,130,246,0.3)] transition-all"
                                />
                                {showSearchDropdown && searchResults.length > 0 && (
                                    <div className="absolute z-[100] mt-1 bg-[#0a0a0a] rounded-lg shadow-2xl border border-white/10 max-h-60 overflow-y-auto w-80 left-0 text-text-primary backdrop-blur-xl">
                                        {searchResults.slice(0, 8).map((result) => (
                                            <button
                                                key={result.symbol}
                                                onClick={() => addToWatchlist(result.symbol, 'EQ')}
                                                className="w-full px-4 py-3 text-left hover:bg-white/5 text-sm border-b border-white/5 last:border-0 transition-colors flex justify-between items-center group"
                                            >
                                                <span className="font-bold text-gray-200">{result.symbol}</span>
                                                <Plus className="w-4 h-4 text-primary opacity-0 group-hover:opacity-100 transition-opacity" />
                                            </button>
                                        ))}
                                    </div>
                                )}
                            </div>
                        </div>

                        {/* Watchlist Content */}
                        <div className="flex-1 overflow-y-auto scrollbar-thin">
                            {watchlist.length === 0 ? (
                                <div className="flex flex-col items-center justify-center h-64 opacity-50">
                                    <TrendingUp className="w-8 h-8 mb-3 text-text-secondary" strokeWidth={1.5} />
                                    <p className="text-xs font-medium text-text-secondary">Watchlist empty</p>
                                </div>
                            ) : (
                                watchlist.map((item, idx) => (
                                    <div
                                        key={`${item.symbol}-${idx}`}
                                        className={`group relative px-4 py-3 border-b border-border-dark/50 cursor-pointer transition-all hover:bg-white/5
                                        ${selectedSymbol === item.symbol ? 'bg-primary/5 border-l-2 border-l-primary -ml-[2px]' : 'border-l-2 border-l-transparent -ml-[2px]'}`}
                                        onClick={() => selectSymbol(item)}
                                    >
                                        <div className="flex items-center justify-between">
                                            <div className="font-bold text-sm text-gray-200">{item.symbol}</div>
                                            <div className={`font-mono text-sm text-right font-medium ${item.change >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                                                {item.change >= 0 ? '+' : ''}{item.change_pct.toFixed(2)}%
                                            </div>
                                        </div>
                                        <div className="flex items-center justify-between mt-1">
                                            <span className="text-[10px] text-text-secondary uppercase tracking-wider">{item.instrument_type}</span>
                                            <span className="text-xs font-mono font-medium text-gray-400">
                                                {item.ltp.toLocaleString('en-IN')}
                                            </span>
                                        </div>

                                        {/* Hover Actions */}
                                        <div className="absolute right-4 top-1/2 -translate-y-1/2 flex gap-1.5 opacity-0 group-hover:opacity-100 bg-[#1a1a1a] shadow-xl border border-white/10 rounded px-1.5 py-1 transition-all z-10 scale-95 group-hover:scale-100">
                                            <button
                                                onClick={(e) => { e.stopPropagation(); initiateOrder('BUY', item); }}
                                                className="bg-blue-600 hover:bg-blue-500 text-white text-[10px] font-bold px-2 py-0.5 rounded transition-colors"
                                            >B</button>
                                            <button
                                                onClick={(e) => { e.stopPropagation(); initiateOrder('SELL', item); }}
                                                className="bg-red-500 hover:bg-red-400 text-white text-[10px] font-bold px-2 py-0.5 rounded transition-colors"
                                            >S</button>
                                            <button
                                                onClick={(e) => { e.stopPropagation(); removeFromWatchlist(item.symbol, item.instrument_type); }}
                                                className="text-gray-400 hover:text-red-400 hover:bg-red-500/10 p-1 rounded transition-colors ml-1"
                                            >
                                                <X className="w-3 h-3" />
                                            </button>
                                        </div>
                                    </div>
                                ))
                            )}
                        </div>
                    </>
                ) : (
                    // Signals Sidebar View
                    <div className="flex-1 overflow-y-auto scrollbar-thin p-3 space-y-3 bg-background-dark/30">
                        {signals.length === 0 ? (
                            <div className="flex flex-col items-center justify-center py-20 opacity-50">
                                <div className="relative">
                                    <div className="absolute inset-0 bg-purple-500/20 blur-xl rounded-full"></div>
                                    <Zap className="w-12 h-12 text-purple-500 mb-4 relative z-10" />
                                </div>
                                <h3 className="text-sm font-bold text-white">Scanning Markets...</h3>
                                <p className="text-xs text-gray-400 text-center px-4 mt-2 leading-relaxed">AI agents are analyzing price action and volume patterns.</p>
                            </div>
                        ) : (
                            signals.map((signal) => (
                                <div key={signal.id} className="bg-card-dark border border-white/10 rounded-xl p-3 hover:border-purple-500/50 transition-all group relative overflow-hidden hover:shadow-[0_0_15px_rgba(168,85,247,0.1)]">
                                    <div className={`absolute left-0 top-0 bottom-0 w-1 ${signal.direction === 'LONG' ? 'bg-blue-500' : 'bg-red-500'}`} />

                                    <div className="flex justify-between items-start mb-2 pl-2">
                                        <div>
                                            <h4 className="text-sm font-bold text-white tracking-tight">{signal.symbol}</h4>
                                            <div className="flex items-center gap-2 mt-1">
                                                <span className={`text-[9px] font-bold px-1.5 py-0.5 rounded border uppercase tracking-wider ${signal.direction === 'LONG'
                                                        ? 'bg-blue-500/10 border-blue-500/30 text-blue-400'
                                                        : 'bg-red-500/10 border-red-500/30 text-red-400'
                                                    }`}>
                                                    {signal.direction}
                                                </span>
                                                <span className="text-[9px] text-gray-500 font-mono">{signal.signal_family}</span>
                                            </div>
                                        </div>
                                        <div className="text-right">
                                            <div className="text-sm font-bold text-white">{Math.round(signal.confidence * 100)}%</div>
                                            <div className="text-[9px] text-gray-500 uppercase">Conf</div>
                                        </div>
                                    </div>

                                    <div className="bg-black/20 rounded p-2 mb-3 ml-1">
                                        <p className="text-[10px] text-gray-400 leading-relaxed line-clamp-2 italic">
                                            "{signal.reasoning}"
                                        </p>
                                    </div>

                                    <button
                                        onClick={() => {
                                            setSelectedSymbol(signal.symbol);
                                            setOrderType(signal.direction as any);
                                            setPrice(0);
                                            setOrderMode('MARKET');
                                            setShowOrderModal(true);
                                        }}
                                        className="w-full ml-1 py-1.5 bg-gradient-to-r from-purple-600/20 to-purple-600/10 hover:from-purple-600 hover:to-purple-500 text-purple-300 hover:text-white border border-purple-500/30 rounded-lg text-xs font-bold transition-all flex items-center justify-center gap-1.5 group-hover:shadow-lg shadow-purple-900/20"
                                    >
                                        <Zap className="w-3 h-3 group-hover:fill-current" /> Execute Trade
                                    </button>
                                </div>
                            ))
                        )}
                    </div>
                )}
            </div>

            {/* Main Content Area - Tabs */}
            <div className="flex-1 bg-background-dark h-full overflow-hidden flex flex-col">
                <div className="bg-card-dark flex-1 flex flex-col overflow-hidden">
                    {/* Tabs Header */}
                    <div className="flex items-center justify-between px-6 py-3 border-b border-border-dark bg-card-dark">
                        <div className="flex items-center gap-1 bg-black/20 p-1 rounded-lg border border-white/5">
                            <button
                                onClick={() => setActiveTab('positions')}
                                className={`flex items-center gap-2 px-4 py-1.5 rounded-md transition-all text-sm font-medium ${activeTab === 'positions'
                                    ? 'bg-primary text-white shadow-lg shadow-primary/20'
                                    : 'text-text-secondary hover:text-white hover:bg-white/5'
                                    }`}
                            >
                                <LayoutDashboard className="w-3.5 h-3.5" />
                                Positions
                            </button>
                            <button
                                onClick={() => setActiveTab('orders')}
                                className={`flex items-center gap-2 px-4 py-1.5 rounded-md transition-all text-sm font-medium ${activeTab === 'orders'
                                    ? 'bg-primary text-white shadow-lg shadow-primary/20'
                                    : 'text-text-secondary hover:text-white hover:bg-white/5'
                                    }`}
                            >
                                <List className="w-3.5 h-3.5" />
                                Orders
                            </button>
                            <button
                                onClick={() => setActiveTab('history')}
                                className={`flex items-center gap-2 px-4 py-1.5 rounded-md transition-all text-sm font-medium ${activeTab === 'history'
                                    ? 'bg-primary text-white shadow-lg shadow-primary/20'
                                    : 'text-text-secondary hover:text-white hover:bg-white/5'
                                    }`}
                            >
                                <History className="w-3.5 h-3.5" />
                                History
                            </button>
                        </div>

                        {/* Total P&L Display */}
                        <div className="flex items-center gap-6 text-sm">
                            <div className="flex items-center gap-3 px-4 py-1.5 bg-black/30 rounded border border-white/5">
                                <span className="text-text-secondary text-xs font-medium uppercase tracking-wider">Total P&L</span>
                                <span className={`font-bold font-mono text-lg ${totalPnL >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                                    {totalPnL >= 0 ? '+' : ''}₹{totalPnL.toFixed(2)}
                                </span>
                            </div>

                            {/* Trade Source Filters */}
                            <div className="flex items-center gap-3 border-l border-border-dark pl-6">
                                <label className="flex items-center gap-2 cursor-pointer group">
                                    <input
                                        type="checkbox"
                                        checked={showAgentTrades}
                                        onChange={(e) => setShowAgentTrades(e.target.checked)}
                                        className="w-3.5 h-3.5 accent-purple-500 rounded cursor-pointer"
                                    />
                                    <span className="text-text-secondary text-xs group-hover:text-purple-400 transition-colors">Agent</span>
                                </label>
                                <label className="flex items-center gap-2 cursor-pointer group">
                                    <input
                                        type="checkbox"
                                        checked={showManualTrades}
                                        onChange={(e) => setShowManualTrades(e.target.checked)}
                                        className="w-3.5 h-3.5 accent-primary rounded cursor-pointer"
                                    />
                                    <span className="text-text-secondary text-xs group-hover:text-blue-400 transition-colors">Manual</span>
                                </label>
                            </div>
                        </div>
                    </div>

                    {/* Tab Content */}
                    <div className="flex-1 overflow-auto bg-[#050505] p-6 relative">
                        {/* Background Grid Pattern */}
                        <div className="absolute inset-0 bg-[linear-gradient(rgba(255,255,255,0.02)_1px,transparent_1px),linear-gradient(90deg,rgba(255,255,255,0.02)_1px,transparent_1px)] bg-[size:40px_40px] pointer-events-none"></div>

                        {activeTab === 'orders' && (
                            <div className="flex flex-col items-center justify-center h-full text-center relative z-10">
                                <div className="w-20 h-20 bg-card-dark rounded-full flex items-center justify-center mb-6 text-text-secondary border border-white/5 shadow-2xl">
                                    <List className="w-10 h-10 opacity-50" />
                                </div>
                                <h3 className="text-xl font-bold text-white mb-2">No Active Orders</h3>
                                <p className="text-gray-500 text-sm max-w-sm mb-8 leading-relaxed">
                                    Orders placed will appear here. Use the Watchlist or AI Signals from the sidebar to find trading opportunities.
                                </p>
                            </div>
                        )}

                        {activeTab === 'positions' && (
                            allPositions.length === 0 ? (
                                <div className="flex flex-col items-center justify-center h-full text-center relative z-10">
                                    <div className="w-20 h-20 bg-card-dark rounded-full flex items-center justify-center mb-6 text-text-secondary border border-white/5 shadow-2xl">
                                        <LayoutDashboard className="w-10 h-10 opacity-50" />
                                    </div>
                                    <h3 className="text-xl font-bold text-white mb-2">Portfolio is Empty</h3>
                                    <p className="text-gray-500 text-sm mb-8">You don't have any open positions currently.</p>
                                    <button
                                        onClick={() => setSidebarMode('signals')}
                                        className="px-6 py-2.5 bg-purple-600/10 hover:bg-purple-600/20 text-purple-400 border border-purple-500/50 rounded-lg text-sm font-bold transition-all flex items-center gap-2 hover:shadow-[0_0_20px_rgba(168,85,247,0.2)]"
                                    >
                                        <Zap className="w-4 h-4" /> Check AI Signals
                                    </button>
                                </div>
                            ) : (
                                <div className="bg-card-dark rounded-xl border border-border-dark shadow-xl overflow-hidden relative z-10">
                                    <table className="w-full text-left text-sm">
                                        <thead className="bg-[#111] text-xs font-bold text-gray-500 uppercase tracking-wider border-b border-border-dark">
                                            <tr>
                                                <th className="p-4 pl-6">Instrument</th>
                                                <th className="p-4 text-right">Qty</th>
                                                <th className="p-4 text-right">Avg. Price</th>
                                                <th className="p-4 text-right">LTP</th>
                                                <th className="p-4 text-right">P&L</th>
                                                <th className="p-4 text-center">Action</th>
                                            </tr>
                                        </thead>
                                        <tbody className="divide-y divide-border-dark bg-[#0a0a0a]">
                                            {allPositions.map((pos) => (
                                                <tr key={pos.id} className="hover:bg-white/5 transition-colors group">
                                                    <td className="p-4 pl-6 font-bold text-white">
                                                        {pos.symbol}
                                                        <span className={`text-[10px] ml-3 px-2 py-0.5 rounded border ${pos.source === 'AGENT'
                                                            ? 'bg-purple-500/10 border-purple-500/20 text-purple-400'
                                                            : pos.type === 'BUY' ? 'bg-blue-500/10 border-blue-500/20 text-blue-400' : 'bg-red-500/10 border-red-500/20 text-red-400'
                                                            }`}>
                                                            {pos.source === 'AGENT' ? 'AI AGENT' : pos.type}
                                                        </span>
                                                    </td>
                                                    <td className="p-4 text-right text-gray-400 font-mono">{pos.quantity}</td>
                                                    <td className="p-4 text-right text-gray-400 font-mono">{pos.entry_price.toFixed(2)}</td>
                                                    <td className="p-4 text-right text-white font-mono font-medium">{pos.current_price.toFixed(2)}</td>
                                                    <td className={`p-4 text-right font-bold font-mono ${pos.pnl >= 0 ? 'text-green-500' : 'text-red-500'}`}>
                                                        {pos.pnl >= 0 ? '+' : ''}{pos.pnl.toFixed(2)}
                                                        <div className="text-[10px] opacity-60 font-medium mt-0.5">
                                                            {pos.pnl_pct.toFixed(2)}%
                                                        </div>
                                                    </td>
                                                    <td className="p-4 text-center">
                                                        <button
                                                            onClick={() => pos.source === 'AGENT' ? closeAgentPosition(pos.id) : closePosition(pos.id)}
                                                            className="text-xs bg-white/5 border border-white/10 hover:bg-red-500/10 hover:border-red-500/30 hover:text-red-400 text-gray-400 px-4 py-1.5 rounded-md transition-all font-medium opacity-0 group-hover:opacity-100"
                                                        >
                                                            Close
                                                        </button>
                                                    </td>
                                                </tr>
                                            ))}
                                        </tbody>
                                        <tfoot className="bg-[#111] border-t border-border-dark">
                                            <tr>
                                                <td colSpan={4} className="p-4 text-right text-xs font-bold text-gray-500 uppercase tracking-wider">Total Portfolio P&L</td>
                                                <td className={`p-4 text-right font-bold font-mono text-base ${totalPnL >= 0 ? 'text-green-500' : 'text-red-500'}`}>
                                                    {totalPnL >= 0 ? '+' : ''}₹{totalPnL.toFixed(2)}
                                                </td>
                                                <td></td>
                                            </tr>
                                        </tfoot>
                                    </table>
                                </div>
                            )
                        )}

                        {activeTab === 'history' && (
                            <div className="flex flex-col items-center justify-center h-full text-center relative z-10">
                                <div className="w-20 h-20 bg-card-dark rounded-full flex items-center justify-center mb-6 text-text-secondary border border-white/5 shadow-2xl">
                                    <History className="w-10 h-10 opacity-50" />
                                </div>
                                <h3 className="text-xl font-bold text-white mb-2">History Empty</h3>
                                <p className="text-gray-500 text-sm">No executed orders found in this session.</p>
                            </div>
                        )}
                    </div>
                </div>
            </div>

            {/* Existing Order Modal */}
            {showOrderModal && selectedSymbol && (
                <div className="fixed inset-0 z-[9999] bg-black/80 backdrop-blur-md flex items-center justify-center p-8 animate-in fade-in duration-200" onClick={() => setShowOrderModal(false)}>
                    <div className="bg-[#111] w-full max-w-[420px] max-h-[85vh] rounded-2xl shadow-2xl border border-white/10 flex flex-col overflow-hidden" onClick={e => e.stopPropagation()}>
                        {/* Modal Header */}
                        <div className={`p-6 shrink-0 ${orderType === 'BUY'
                                ? 'bg-gradient-to-r from-blue-600 to-blue-700'
                                : 'bg-gradient-to-r from-red-600 to-red-700'
                            }`}>
                            <div className="flex items-center justify-between mb-4">
                                <h3 className="text-2xl font-bold text-white tracking-tight">
                                    {orderType} {selectedSymbol}
                                </h3>
                                <button
                                    onClick={() => setShowOrderModal(false)}
                                    className="text-white/80 hover:text-white transition-colors p-1.5 hover:bg-white/20 rounded-full"
                                >
                                    <X className="w-5 h-5" />
                                </button>
                            </div>
                            <div className="flex items-center justify-between text-white/90">
                                <div className="flex flex-col">
                                    <span className="text-xs opacity-80 uppercase tracking-widest font-bold">LTP</span>
                                    <span className="text-xl font-mono font-bold">₹{selectedLTP.toLocaleString('en-IN')}</span>
                                </div>
                                <label className="flex items-center gap-3 cursor-pointer bg-black/20 px-3 py-1.5 rounded-lg border border-white/10">
                                    <span className="text-xs font-bold uppercase tracking-wide">Paper</span>
                                    <input
                                        type="checkbox"
                                        className="w-4 h-4 accent-white"
                                        checked={tradingMode === 'PAPER'}
                                        onChange={() => setTradingMode(tradingMode === 'LIVE' ? 'PAPER' : 'LIVE')}
                                    />
                                </label>
                            </div>
                        </div>

                        {/* Modal Body */}
                        <div className="p-8 overflow-y-auto flex-1">
                            {/* Quantity and Price */}
                            <div className="grid grid-cols-2 gap-6 mb-8">
                                <div>
                                    <label className="block text-xs text-secondary mb-2 font-bold uppercase tracking-wider">Quantity</label>
                                    <input
                                        type="number"
                                        value={quantity}
                                        onChange={(e) => setQuantity(parseInt(e.target.value) || 1)}
                                        className="w-full px-4 py-3 bg-[#1a1a1a] border border-white/10 rounded-xl text-lg font-mono font-bold text-white focus:border-blue-500 focus:outline-none transition-all"
                                    />
                                </div>
                                <div>
                                    <label className="block text-xs text-secondary mb-2 font-bold uppercase tracking-wider">Price</label>
                                    <input
                                        type="number"
                                        value={price}
                                        disabled={orderMode === 'MARKET'}
                                        onChange={(e) => setPrice(parseFloat(e.target.value) || 0)}
                                        className={`w-full px-4 py-3 bg-[#1a1a1a] border border-white/10 rounded-xl text-lg font-mono font-bold text-white focus:border-blue-500 focus:outline-none transition-all ${orderMode === 'MARKET' ? 'opacity-50 cursor-not-allowed' : ''}`}
                                    />
                                </div>
                            </div>

                            {/* Order Type */}
                            <div className="mb-8">
                                <label className="block text-xs text-secondary mb-3 font-bold uppercase tracking-wider">Order Type</label>
                                <div className="flex p-1 bg-[#1a1a1a] rounded-xl border border-white/5">
                                    <button
                                        onClick={() => setOrderMode('MARKET')}
                                        className={`flex-1 py-2.5 rounded-lg text-sm font-bold transition-all ${orderMode === 'MARKET'
                                            ? 'bg-white/10 text-white shadow-sm'
                                            : 'text-gray-500 hover:text-white'
                                            }`}
                                    >
                                        Market
                                    </button>
                                    <button
                                        onClick={() => setOrderMode('LIMIT')}
                                        className={`flex-1 py-2.5 rounded-lg text-sm font-bold transition-all ${orderMode === 'LIMIT'
                                            ? 'bg-white/10 text-white shadow-sm'
                                            : 'text-gray-500 hover:text-white'
                                            }`}
                                    >
                                        Limit
                                    </button>
                                    <button
                                        onClick={() => setOrderMode('SL')}
                                        className={`flex-1 py-2.5 rounded-lg text-sm font-bold transition-all ${orderMode === 'SL'
                                            ? 'bg-white/10 text-white shadow-sm'
                                            : 'text-gray-500 hover:text-white'
                                            }`}
                                    >
                                        Stop Loss
                                    </button>
                                </div>
                            </div>

                            {/* Margin Info */}
                            <div className="flex justify-between items-center p-4 bg-[#1a1a1a] rounded-xl mb-8 border border-white/5">
                                <span className="text-sm text-gray-400 font-medium">Margin required</span>
                                <span className="text-base font-bold text-white font-mono">₹{(price * quantity * 0.2).toLocaleString('en-IN')}</span>
                            </div>

                            {/* Action Button */}
                            <button
                                onClick={() => { executeOrder(); setShowOrderModal(false); }}
                                className={`w-full py-4 rounded-xl font-bold text-white shadow-lg transition-all hover:scale-[1.02] active:scale-95 text-lg flex items-center justify-center gap-3 ${orderType === 'BUY'
                                    ? 'bg-blue-600 hover:bg-blue-500 shadow-blue-500/25'
                                    : 'bg-red-600 hover:bg-red-500 shadow-red-500/25'
                                    }`}
                            >
                                <Zap className="w-5 h-5 fill-current" />
                                {orderType} {quantity} @ ₹{orderMode === 'MARKET' ? 'MKT' : price.toFixed(2)}
                            </button>
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
}
