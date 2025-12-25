"use client";

import React, { useState, useEffect } from 'react';
import PortfolioSidebar from './PortfolioSidebar';
import PortfolioDetail from './PortfolioDetail'; // We will create this next
import PortfolioEditor from './PortfolioEditor';
import { Loader2 } from "lucide-react";

export default function PortfolioLayout() {
    const [portfolios, setPortfolios] = useState<any[]>([]);
    const [selectedId, setSelectedId] = useState<string | number | null>('INDEX:NIFTY 50');
    const [isCreating, setIsCreating] = useState(false);
    const [loading, setLoading] = useState(true);

    useEffect(() => {
        fetchPortfolios();
    }, []);

    const fetchPortfolios = () => {
        setLoading(true);
        fetch('http://localhost:9000/api/portfolio/strategies')
            .then(res => res.json())
            .then(data => {
                setPortfolios(data);
                if (data.length > 0 && !selectedId) {
                    setSelectedId(data[0].id);
                }
                setLoading(false);
            })
            .catch(err => {
                console.error("Failed to fetch portfolios", err);
                setLoading(false);
            });
    };

    const handleCreate = async (portfolio: any) => {
        try {
            const res = await fetch('http://localhost:9000/api/portfolio/strategies', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(portfolio)
            });
            if (res.ok) {
                fetchPortfolios();
                setIsCreating(false);
            }
        } catch (e) {
            console.error("Failed to create portfolio", e);
        }
    };

    const isIndex = typeof selectedId === 'string' && selectedId.startsWith('INDEX:');
    let selectedPortfolio = portfolios.find(p => p.id === selectedId);

    // Index Definitions (Mirrors backend constants/indices.py)
    const INDEX_CONSTITUENTS: Record<string, string[]> = {
        "NIFTY 50": [
            "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "HDFC", "BHARTIARTL", "ITC",
            "KOTAKBANK", "LT", "SBIN", "AXISBANK", "ASIANPAINT", "MARUTI", "TITAN",
            "BAJFINANCE", "WIPRO", "ULTRACEMCO", "NESTLEIND", "ONGC", "SUNPHARMA", "TATASTEEL",
            "M&M", "NTPC", "POWERGRID", "JSWSTEEL", "GRASIM", "COALINDIA", "TATAMOTORS",
            "INDUSINDBK", "TECHM", "ADANIPORTS", "HINDALCO", "HCLTECH", "UPL", "HEROMOTOCO",
            "BAJAJFINSV", "DRREDDY", "CIPLA", "EICHERMOT", "DIVISLAB", "BRITANNIA", "SHREECEM",
            "BPCL", "TATACONSUM", "APOLLOHOSP", "ADANIENT", "SBILIFE", "HDFCLIFE", "BAJAJ-AUTO"
        ],
        "BANKNIFTY": [
            "HDFCBANK", "ICICIBANK", "KOTAKBANK", "SBIN", "AXISBANK", "INDUSINDBK",
            "BANDHANBNK", "FEDERALBNK", "IDFCFIRSTB", "AUBANK", "PNB", "BANKBARODA"
        ],
        "NIFTY 100": ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "SBIN", "BHARTIARTL", "ITC"], // Simplified for display
        "NIFTY 200": ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK"], // Simplified
        "NIFTY_MIDCAP_100": ["TRENT", "BEL", "TATAELXSI", "INDHOTEL", "COFORGE", "PERSISTENT", "PAGEIND", "PIIND", "L&TFH", "VOLTAS"],
        "NIFTY_FIN_SERVICE": ["HDFCBANK", "ICICIBANK", "SBIN", "KOTAKBANK", "AXISBANK", "BAJFINANCE", "BAJAJFINSV", "HDFCLIFE", "SBILIFE", "PFC"],
        "NIFTY_AUTO": ["MARUTI", "M&M", "TATAMOTORS", "HEROMOTOCO", "EICHERMOT", "BAJAJ-AUTO", "TVSMOTOR", "ASHOKLEY", "BHARATFORG", "MRF"],
        "NIFTY_FMCG": ["ITC", "HUL", "NESTLEIND", "BRITANNIA", "TATACONSUM", "VBL", "GODREJCP", "DABUR", "MARICO", "COLPAL"],
        "NIFTY_SMALLCAP_100": ["CDSL", "BSE", "IEX", "MCX", "KEI", "TANLA", "SUZLON", "KPITTECH", "IDFC"]
    };

    // Mock Index Portfolio object if selected
    if (isIndex && !selectedPortfolio) {
        const indexName = (selectedId as string).split(':')[1];
        const symbols = INDEX_CONSTITUENTS[indexName] || [];

        selectedPortfolio = {
            id: selectedId,
            name: indexName,
            status: 'LIVE',
            description: `Standard Market Benchmark Index consisting of ${symbols.length} constituents.`,
            initial_capital: 0,
            benchmark: indexName,
            composition: symbols.map(sym => ({
                strategy_id: sym, // Using strategy_id field to store symbol for display
                weight: (100 / symbols.length).toFixed(2) // Equal weight for display
            }))
        };
    }

    if (loading && portfolios.length === 0) {
        return (
            <div className="h-full flex items-center justify-center bg-[#050505]">
                <Loader2 className="w-6 h-6 animate-spin text-purple-500" />
            </div>
        );
    }

    return (
        <div className="flex h-full w-full bg-[#050505] overflow-hidden">
            {/* Sidebar (Left Pane) */}
            <PortfolioSidebar
                portfolios={portfolios}
                selectedId={selectedId}
                onSelect={(id) => {
                    setIsCreating(false);
                    setSelectedId(id);
                }}
                onCreateNew={() => setIsCreating(true)}
            />

            {/* Main Content (Right Pane) */}
            <div className="flex-1 h-full overflow-hidden bg-[#0A0A0A]">
                {isCreating ? (
                    <div className="h-full overflow-y-auto p-8">
                        <div className="max-w-3xl mx-auto">
                            <PortfolioEditor
                                onCancel={() => setIsCreating(false)}
                                onSave={handleCreate}
                            />
                        </div>
                    </div>
                ) : selectedPortfolio ? (
                    <PortfolioDetail portfolio={selectedPortfolio} />
                ) : (
                    <div className="h-full flex flex-col items-center justify-center text-gray-500">
                        <p>No portfolios found.</p>
                        <button onClick={() => setIsCreating(true)} className="text-purple-400 text-sm mt-2 hover:underline">Create your first one</button>
                    </div>
                )}
            </div>
        </div>
    );
}
