'use client'

import { useState, useEffect } from 'react';
import { Search, TrendingUp, Plus, X, List, LayoutDashboard, Briefcase, History } from 'lucide-react';

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
    source?: 'MANUAL' | 'AGENT';  // Add source tracking
}

export default function Terminal() {
    const [tradingMode, setTradingMode] = useState<'PAPER' | 'LIVE'>('PAPER');
    const [watchlist, setWatchlist] = useState<WatchlistItem[]>([]);
    const [positions, setPositions] = useState<Position[]>([]);  // Manual positions
    const [agentPositions, setAgentPositions] = useState<Position[]>([]);  // Agent positions
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
            // Don't show error to user, just silently fail
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

    const placeOrder = () => {
        if (!selectedSymbol) {
            alert('Please select a symbol');
            return;
        }

        const orderPrice = orderMode === 'MARKET' ? price : price;
        const newPosition: Position = {
            id: Date.now().toString(),
            symbol: selectedSymbol,
            type: orderType,
            quantity,
            entry_price: orderPrice,
            current_price: orderPrice,
            pnl: 0,
            pnl_pct: 0
        };

        setPositions([...positions, newPosition]);
        alert(`${orderType} Order Placed:\n${quantity} x ${selectedSymbol} @ ₹${orderPrice.toFixed(2)}`);
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
                // Refresh agent positions
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

    // Merge agent and manual positions
    const allPositions = [
        ...positions.map(p => ({ ...p, source: 'MANUAL' as const })),
        ...agentPositions
    ].filter(p => {
        if (!showAgentTrades && p.source === 'AGENT') return false;
        if (!showManualTrades && p.source === 'MANUAL') return false;
        return true;
    });

    // Calculate total P&L (manual + agent)
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
            {/* Left Sidebar: Watchlist */}
            <div className="w-[350px] bg-card-dark border-r border-border-dark flex flex-col h-full overflow-hidden shrink-0">
                <div className="p-4 border-b border-border-dark">
                    <div className="relative">
                        <Search className="absolute left-3 top-2.5 w-4 h-4 text-text-secondary" />
                        <input
                            type="text"
                            placeholder="Search eg: infy bse, nifty fut..."
                            value={searchQuery}
                            onChange={(e) => { setSearchQuery(e.target.value); searchSymbols(e.target.value); }}
                            onFocus={() => searchQuery.length > 0 && setShowSearchDropdown(true)}
                            onBlur={() => setTimeout(() => setShowSearchDropdown(false), 200)}
                            className="w-full pl-10 pr-4 py-2 bg-background-dark border border-border-dark rounded text-sm text-text-primary outline-none focus:border-primary focus:shadow-[0_0_0_2px_rgba(59,130,246,0.1)] transition-all placeholder:text-text-secondary"
                        />
                        <div className="absolute right-3 top-2.5 flex items-center gap-1.5 opacity-40">
                            <span className="text-[10px] bg-background-dark border border-border-dark px-1.5 py-0.5 rounded text-text-secondary font-medium">Ctrl + K</span>
                        </div>
                    </div>
                    {showSearchDropdown && searchResults.length > 0 && (
                        <div className="absolute z-50 mt-1 bg-card-dark rounded shadow-xl border border-border-dark max-h-60 overflow-y-auto w-80 left-4 text-text-primary">
                            {searchResults.slice(0, 8).map((result) => (
                                <button
                                    key={result.symbol}
                                    onClick={() => addToWatchlist(result.symbol, 'EQ')}
                                    className="w-full px-4 py-3 text-left hover:bg-white/5 text-sm border-b border-border-dark last:border-0 transition-colors flex justify-between items-center group"
                                >
                                    <span className="font-medium text-text-primary">{result.symbol}</span>
                                    <Plus className="w-4 h-4 text-primary opacity-0 group-hover:opacity-100 transition-opacity" />
                                </button>
                            ))}
                        </div>
                    )}
                </div>

                <div className="px-4 py-2 border-b border-border-dark bg-background-dark/50 flex justify-between text-[11px] font-medium text-text-secondary uppercase tracking-wide">
                    <span>Watchlist ({watchlist.length})</span>
                </div>

                <div className="flex-1 overflow-y-auto scrollbar-thin">
                    {watchlist.length === 0 ? (
                        <div className="flex flex-col items-center justify-center h-64 opacity-40">
                            <TrendingUp className="w-10 h-10 mb-3 text-text-secondary" strokeWidth={1.5} />
                            <p className="text-sm font-medium text-text-secondary">Nothing here yet</p>
                            <p className="text-xs text-text-secondary mt-1">Use the search to add instruments</p>
                        </div>
                    ) : (
                        watchlist.map((item, idx) => (
                            <div
                                key={`${item.symbol}-${idx}`}
                                className={`group relative px-4 py-3.5 border-b border-border-dark cursor-pointer transition-all hover:bg-white/5
                                    ${selectedSymbol === item.symbol ? 'bg-primary/10 border-l-2 border-l-primary -ml-[2px]' : 'border-l-2 border-l-transparent -ml-[2px]'}`}
                                onClick={() => selectSymbol(item)}
                            >
                                <div className="flex items-center justify-between">
                                    <div className={`font-medium text-sm ${item.change >= 0 ? 'text-text-primary' : 'text-text-primary'}`}>{item.symbol}</div>
                                    <div className={`font-medium text-sm text-right ${item.change >= 0 ? 'text-green-500' : 'text-red-500'}`}>
                                        {item.change >= 0 ? '+' : ''}{item.change_pct.toFixed(2)}%
                                    </div>
                                </div>
                                <div className="flex items-center justify-between mt-1">
                                    <span className="text-[10px] text-text-secondary">{item.instrument_type}</span>
                                    <span className={`text-xs font-medium ${item.change >= 0 ? 'text-text-secondary' : 'text-text-secondary'}`}>
                                        {item.ltp.toLocaleString('en-IN')}
                                    </span>
                                </div>

                                {/* Hover Actions */}
                                <div className="absolute right-4 top-1/2 -translate-y-1/2 flex gap-2 opacity-0 group-hover:opacity-100 bg-card-dark shadow-lg border border-border-dark rounded px-1 py-1 transition-all z-10">
                                    <button
                                        onClick={(e) => { e.stopPropagation(); initiateOrder('BUY', item); }}
                                        className="bg-blue-600 hover:bg-blue-700 text-white text-[10px] font-bold px-2.5 py-1 rounded transition-colors"
                                    >B</button>
                                    <button
                                        onClick={(e) => { e.stopPropagation(); initiateOrder('SELL', item); }}
                                        className="bg-red-500 hover:bg-red-600 text-white text-[10px] font-bold px-2.5 py-1 rounded transition-colors"
                                    >S</button>
                                    <button
                                        onClick={(e) => { e.stopPropagation(); removeFromWatchlist(item.symbol, item.instrument_type); }}
                                        className="text-text-secondary hover:text-red-500 hover:bg-red-500/10 p-1 rounded transition-colors"
                                    >
                                        <X className="w-3.5 h-3.5" />
                                    </button>
                                </div>
                            </div>
                        ))
                    )}
                </div>
            </div>

            {/* Right Side: Tabbed Data View */}
            <div className="flex-1 bg-background-dark h-full overflow-hidden flex flex-col">
                <div className="bg-card-dark flex-1 flex flex-col overflow-hidden">
                    {/* Tabs Header - Icon Based like Portfolio Risk */}
                    <div className="flex items-center justify-between px-6 py-4 border-b border-border-dark bg-card-dark">
                        <div className="flex items-center gap-2">
                            <button
                                onClick={() => setActiveTab('positions')}
                                className={`flex items-center gap-2 px-4 py-2 rounded transition-all ${activeTab === 'positions'
                                    ? 'bg-primary text-white'
                                    : 'bg-background-dark text-text-secondary hover:text-white hover:bg-white/5'
                                    }`}
                            >
                                <LayoutDashboard className="w-4 h-4" />
                                <span className="text-sm font-medium">Positions List</span>
                            </button>
                            <button
                                onClick={() => setActiveTab('orders')}
                                className={`flex items-center gap-2 px-4 py-2 rounded transition-all ${activeTab === 'orders'
                                    ? 'bg-primary text-white'
                                    : 'bg-background-dark text-text-secondary hover:text-white hover:bg-white/5'
                                    }`}
                            >
                                <List className="w-4 h-4" />
                                <span className="text-sm font-medium">Orders</span>
                            </button>
                            <button
                                onClick={() => setActiveTab('history')}
                                className={`flex items-center gap-2 px-4 py-2 rounded transition-all ${activeTab === 'history'
                                    ? 'bg-primary text-white'
                                    : 'bg-background-dark text-text-secondary hover:text-white hover:bg-white/5'
                                    }`}
                            >
                                <Briefcase className="w-4 h-4" />
                                <span className="text-sm font-medium">History</span>
                            </button>
                        </div>

                        {/* Total P&L Display and Filters */}
                        <div className="flex items-center gap-6 text-sm">
                            <div className="flex items-center gap-2">
                                <span className="text-text-secondary">Total P&L:</span>
                                <span className={`font-bold ${totalPnL >= 0 ? 'text-green-500' : 'text-red-500'}`}>
                                    {totalPnL >= 0 ? '+' : ''}₹{totalPnL.toFixed(2)}
                                </span>
                            </div>

                            {/* Trade Source Filters */}
                            <div className="flex items-center gap-3 border-l border-border-dark pl-6">
                                <label className="flex items-center gap-2 cursor-pointer">
                                    <input
                                        type="checkbox"
                                        checked={showAgentTrades}
                                        onChange={(e) => setShowAgentTrades(e.target.checked)}
                                        className="w-4 h-4 accent-purple-500"
                                    />
                                    <span className="text-text-secondary text-xs">Agent</span>
                                </label>
                                <label className="flex items-center gap-2 cursor-pointer">
                                    <input
                                        type="checkbox"
                                        checked={showManualTrades}
                                        onChange={(e) => setShowManualTrades(e.target.checked)}
                                        className="w-4 h-4 accent-primary"
                                    />
                                    <span className="text-text-secondary text-xs">Manual</span>
                                </label>
                            </div>
                        </div>
                    </div>

                    {/* Tab Content */}
                    <div className="flex-1 overflow-auto bg-background-dark p-6">
                        {activeTab === 'orders' && (
                            <div className="flex flex-col items-center justify-center h-full text-center">
                                <div className="w-16 h-16 bg-card-dark rounded-full flex items-center justify-center mb-4 text-text-secondary">
                                    <List className="w-8 h-8" />
                                </div>
                                <h3 className="text-lg font-medium text-white mb-2">No orders placed</h3>
                                <p className="text-text-secondary text-sm max-w-sm mb-6">
                                    You haven't placed any orders today. Use the search bar to find instruments and place an order.
                                </p>
                                <button
                                    className="bg-primary text-white px-6 py-2 rounded font-medium text-sm hover:bg-blue-600 transition-colors shadow-lg shadow-blue-500/20"
                                    onClick={() => document.querySelector('input')?.focus()}
                                >
                                    Get started
                                </button>
                            </div>
                        )}

                        {activeTab === 'positions' && (
                            allPositions.length === 0 ? (
                                <div className="flex flex-col items-center justify-center h-full text-center">
                                    <div className="w-16 h-16 bg-card-dark rounded-full flex items-center justify-center mb-4 text-text-secondary">
                                        <LayoutDashboard className="w-8 h-8" />
                                    </div>
                                    <h3 className="text-lg font-medium text-white mb-2">No open positions</h3>
                                    <p className="text-text-secondary text-sm mb-6">You don't have any open positions currently.</p>
                                </div>
                            ) : (
                                <div className="bg-card-dark rounded border border-border-dark shadow-sm overflow-hidden">
                                    <table className="w-full text-left text-sm">
                                        <thead className="bg-background-dark text-xs font-semibold text-text-secondary uppercase tracking-wider border-b border-border-dark">
                                            <tr>
                                                <th className="p-4">Instrument</th>
                                                <th className="p-4 text-right">Qty</th>
                                                <th className="p-4 text-right">Avg.</th>
                                                <th className="p-4 text-right">LTP</th>
                                                <th className="p-4 text-right">P&L</th>
                                                <th className="p-4 text-center">Action</th>
                                            </tr>
                                        </thead>
                                        <tbody className="divide-y divide-border-dark">
                                            {allPositions.map((pos) => (
                                                <tr key={pos.id} className="hover:bg-white/5 transition-colors">
                                                    <td className="p-4 font-medium text-white">
                                                        {pos.symbol}
                                                        <span className={`text-[10px] ml-2 px-1.5 py-0.5 rounded ${pos.source === 'AGENT'
                                                            ? 'bg-purple-500/20 text-purple-400'
                                                            : pos.type === 'BUY' ? 'bg-blue-500/20 text-blue-400' : 'bg-red-500/20 text-red-400'
                                                            }`}>
                                                            {pos.source === 'AGENT' ? 'AGENT' : pos.type}
                                                        </span>
                                                    </td>
                                                    <td className="p-4 text-right text-text-secondary">{pos.quantity}</td>
                                                    <td className="p-4 text-right text-text-secondary">{pos.entry_price.toFixed(2)}</td>
                                                    <td className="p-4 text-right text-white font-medium">{pos.current_price.toFixed(2)}</td>
                                                    <td className={`p-4 text-right font-medium ${pos.pnl >= 0 ? 'text-green-500' : 'text-red-500'}`}>
                                                        {pos.pnl >= 0 ? '+' : ''}{pos.pnl.toFixed(2)}
                                                        <div className="text-[10px] opacity-70">
                                                            ({pos.pnl_pct.toFixed(2)}%)
                                                        </div>
                                                    </td>
                                                    <td className="p-4 text-center">
                                                        <button
                                                            onClick={() => pos.source === 'AGENT' ? closeAgentPosition(pos.id) : closePosition(pos.id)}
                                                            className="text-xs bg-background-dark border border-border-dark hover:bg-red-500/10 hover:border-red-500/30 hover:text-red-400 text-text-secondary px-3 py-1.5 rounded transition-all"
                                                        >
                                                            Exit
                                                        </button>
                                                    </td>
                                                </tr>
                                            ))}
                                        </tbody>
                                    </table>
                                </div>
                            )
                        )}

                        {activeTab === 'history' && (
                            <div className="flex flex-col items-center justify-center h-full text-center">
                                <div className="w-16 h-16 bg-card-dark rounded-full flex items-center justify-center mb-4 text-text-secondary">
                                    <Briefcase className="w-8 h-8" />
                                </div>
                                <h3 className="text-lg font-medium text-white mb-2">No executed orders</h3>
                            </div>
                        )}
                    </div>
                </div>
            </div>

            {/* Simplified Order Modal */}
            {showOrderModal && selectedSymbol && (
                <div className="fixed inset-0 z-[9999] bg-black/60 backdrop-blur-sm flex items-center justify-center p-8" onClick={() => setShowOrderModal(false)}>
                    <div className="bg-card-dark w-full max-w-[420px] max-h-[85vh] rounded-xl shadow-2xl border border-border-dark flex flex-col overflow-hidden" onClick={e => e.stopPropagation()}>
                        {/* Modal Header - High Contrast */}
                        <div className={`p-5 shrink-0 ${orderType === 'BUY' ? 'bg-blue-600' : 'bg-red-600'}`}>
                            <div className="flex items-center justify-between mb-3">
                                <h3 className="text-xl font-bold text-white">
                                    {orderType} {selectedSymbol}
                                </h3>
                                <button
                                    onClick={() => setShowOrderModal(false)}
                                    className="text-white/80 hover:text-white transition-colors p-1 hover:bg-white/20 rounded"
                                >
                                    <X className="w-5 h-5" />
                                </button>
                            </div>
                            <div className="flex items-center gap-3 text-sm text-white/90">
                                <span>LTP:</span>
                                <span className="font-semibold">₹{selectedLTP.toLocaleString('en-IN')}</span>
                                <span>•</span>
                                <label className="flex items-center gap-2 cursor-pointer">
                                    <input
                                        type="checkbox"
                                        className="w-4 h-4"
                                        checked={tradingMode === 'PAPER'}
                                        onChange={() => setTradingMode(tradingMode === 'LIVE' ? 'PAPER' : 'LIVE')}
                                    />
                                    <span className="text-xs font-medium">Paper Trading</span>
                                </label>
                            </div>
                        </div>

                        {/* Modal Body */}
                        <div className="p-6 overflow-y-auto flex-1">
                            {/* Quantity and Price */}
                            <div className="grid grid-cols-2 gap-4 mb-6">
                                <div>
                                    <label className="block text-xs text-text-secondary mb-2 font-medium">Quantity</label>
                                    <input
                                        type="number"
                                        value={quantity}
                                        onChange={(e) => setQuantity(parseInt(e.target.value) || 1)}
                                        className="w-full px-4 py-3 bg-background-dark border border-border-dark rounded text-sm font-semibold text-white focus:border-primary focus:outline-none"
                                    />
                                </div>
                                <div>
                                    <label className="block text-xs text-text-secondary mb-2 font-medium">Price</label>
                                    <input
                                        type="number"
                                        value={price}
                                        disabled={orderMode === 'MARKET'}
                                        onChange={(e) => setPrice(parseFloat(e.target.value) || 0)}
                                        className={`w-full px-4 py-3 bg-background-dark border border-border-dark rounded text-sm font-semibold text-white focus:border-primary focus:outline-none ${orderMode === 'MARKET' ? 'opacity-50 cursor-not-allowed' : ''}`}
                                    />
                                </div>
                            </div>

                            {/* Order Type */}
                            <div className="mb-6">
                                <label className="block text-xs text-text-secondary mb-3 font-medium">Order Type</label>
                                <div className="flex gap-3">
                                    <button
                                        onClick={() => setOrderMode('MARKET')}
                                        className={`flex-1 py-2.5 rounded text-sm font-medium transition-all ${orderMode === 'MARKET'
                                            ? 'bg-primary text-white'
                                            : 'bg-background-dark text-text-secondary hover:bg-white/5'
                                            }`}
                                    >
                                        Market
                                    </button>
                                    <button
                                        onClick={() => setOrderMode('LIMIT')}
                                        className={`flex-1 py-2.5 rounded text-sm font-medium transition-all ${orderMode === 'LIMIT'
                                            ? 'bg-primary text-white'
                                            : 'bg-background-dark text-text-secondary hover:bg-white/5'
                                            }`}
                                    >
                                        Limit
                                    </button>
                                    <button
                                        onClick={() => setOrderMode('SL')}
                                        className={`flex-1 py-2.5 rounded text-sm font-medium transition-all ${orderMode === 'SL'
                                            ? 'bg-primary text-white'
                                            : 'bg-background-dark text-text-secondary hover:bg-white/5'
                                            }`}
                                    >
                                        Stop Loss
                                    </button>
                                </div>
                            </div>

                            {/* Margin Info */}
                            <div className="flex justify-between items-center p-4 bg-background-dark rounded mb-6">
                                <span className="text-sm text-text-secondary">Margin required</span>
                                <span className="text-sm font-bold text-white">₹{(price * quantity * 0.2).toLocaleString('en-IN')}</span>
                            </div>

                            {/* Action Button */}
                            <button
                                onClick={() => { placeOrder(); setShowOrderModal(false); }}
                                className={`w-full py-3.5 rounded-lg font-bold text-white shadow-lg transition-all hover:scale-[1.02] active:scale-95 ${orderType === 'BUY'
                                    ? 'bg-blue-600 hover:bg-blue-700'
                                    : 'bg-red-500 hover:bg-red-600'
                                    }`}
                            >
                                {orderType} {quantity} @ ₹{orderMode === 'MARKET' ? 'Market' : price.toFixed(2)}
                            </button>
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
}
