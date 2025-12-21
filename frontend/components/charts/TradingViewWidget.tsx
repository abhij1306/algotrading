'use client'

import React, { useEffect, useRef, memo } from 'react';

interface TradingViewWidgetProps {
    symbol: string;
}

function TradingViewWidget({ symbol }: TradingViewWidgetProps) {
    const container = useRef<HTMLDivElement>(null);

    useEffect(
        () => {
            if (!container.current) return;

            // Clear previous widget
            container.current.innerHTML = "";

            const script = document.createElement("script");
            script.src = "https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js";
            script.type = "text/javascript";
            script.async = true;

            // Map symbol to TradingView format
            let tvSymbol = "NIFTY1!";

            if (symbol) {
                // Remove common suffixes
                let cleanSymbol = symbol.replace('.NS', '').replace('.BSE', '').replace('-EQ', '');

                // Handle Options (Synthetic or Real)
                if (cleanSymbol.includes('CE') || cleanSymbol.includes('PE')) {
                    // Fallback to underlying index chart since TV Widget doesn't support options history
                    // Use Futures (NIFTY1!) as Spot Index is often restricted
                    if (cleanSymbol.includes('BANKNIFTY')) tvSymbol = "BANKNIFTY1!";
                    else if (cleanSymbol.includes('NIFTY')) tvSymbol = "NIFTY1!";
                }

                // Handle common indices - Use Futures for availability
                else if (cleanSymbol === 'NIFTY 50' || cleanSymbol === 'NIFTY50') tvSymbol = "NIFTY1!";
                else if (cleanSymbol === 'NIFTY BANK' || cleanSymbol === 'BANKNIFTY') tvSymbol = "BANKNIFTY1!";
                else if (cleanSymbol === 'SENSEX') tvSymbol = "BSE:SENSEX";
                // Handle explicit exchanges
                else if (cleanSymbol.includes(':')) tvSymbol = cleanSymbol;
                // Default
                else tvSymbol = cleanSymbol;
            }

            script.innerHTML = JSON.stringify({
                "autosize": true,
                "symbol": tvSymbol,
                "interval": "D",
                "timezone": "Asia/Kolkata",
                "theme": "dark",
                "style": "1",
                "locale": "in",
                "enable_publishing": false,
                "withdateranges": true,
                "hide_side_toolbar": false,
                "allow_symbol_change": true,
                "details": true,
                "hotlist": false,
                "calendar": false,
                "support_host": "https://www.tradingview.com"
            });

            container.current.appendChild(script);
        },
        [symbol]
    );

    return (
        <div className="tradingview-widget-container h-full w-full bg-[#050505]" ref={container}>
            <div className="tradingview-widget-container__widget" style={{ height: "100%", width: "100%" }}></div>
        </div>
    );
}

export default memo(TradingViewWidget);
