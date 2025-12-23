/**
 * Online/Offline Mode Toggle Component
 * Displays market status and allows manual override
 */
'use client';

import React, { useState, useEffect } from 'react';
import { Wifi, WifiOff, Clock } from 'lucide-react';
import { isMarketOpen, getISTTime, type MarketStatus } from '@/lib/market-hours';

export function OnlineOfflineToggle() {
    const [marketStatus, setMarketStatus] = useState<MarketStatus>({
        isOpen: false,
        message: 'Checking...'
    });
    const [manualMode, setManualMode] = useState<'auto' | 'online' | 'offline'>('auto');
    const [currentTime, setCurrentTime] = useState<string>('');

    useEffect(() => {
        // Check market status every minute
        const updateStatus = () => {
            const status = isMarketOpen();
            setMarketStatus(status);
            setCurrentTime(getISTTime());
        };

        updateStatus();
        const interval = setInterval(updateStatus, 60000); // Update every minute

        return () => clearInterval(interval);
    }, []);

    const isOnline = manualMode === 'online' || (manualMode === 'auto' && marketStatus.isOpen);

    const handleToggle = () => {
        if (manualMode === 'auto') {
            setManualMode('online');
        } else if (manualMode === 'online') {
            setManualMode('offline');
        } else {
            setManualMode('auto');
        }
    };

    const getStatusColor = () => {
        if (isOnline) return 'text-green-400 border-green-500/30 bg-green-500/10';
        return 'text-gray-400 border-gray-500/30 bg-gray-500/10';
    };

    const getStatusText = () => {
        if (manualMode === 'online') return 'ONLINE (Manual)';
        if (manualMode === 'offline') return 'OFFLINE (Manual)';
        return marketStatus.message.toUpperCase();
    };

    return (
        <div className="flex items-center gap-2">
            {/* Time Display */}
            <div className="flex items-center gap-1.5 text-xs text-gray-400">
                <Clock className="h-3.5 w-3.5" />
                <span className="font-mono">{currentTime}</span>
            </div>

            {/* Status Indicator */}
            <button
                onClick={handleToggle}
                className={`flex items-center gap-2 px-3 py-1.5 rounded-lg border transition-all text-xs font-mono uppercase tracking-wider ${getStatusColor()} hover:brightness-110`}
                title={`Click to toggle mode. Current: ${manualMode}\n${marketStatus.nextSession ? `Next session: ${marketStatus.nextSession}` : ''}`}
            >
                {isOnline ? (
                    <Wifi className="h-3.5 w-3.5" />
                ) : (
                    <WifiOff className="h-3.5 w-3.5" />
                )}
                <span>{getStatusText()}</span>
            </button>

            {/* Mode indicator dot */}
            {manualMode !== 'auto' && (
                <div
                    className="h-2 w-2 rounded-full bg-yellow-500 animate-pulse"
                    title="Manual mode active"
                />
            )}
        </div>
    );
}

/**
 * Hook to check if live data should be fetched
 */
export function useIsOnline() {
    const [marketStatus, setMarketStatus] = useState<MarketStatus>({
        isOpen: false,
        message: 'Checking...'
    });
    const [manualMode, setManualMode] = useState<'auto' | 'online' | 'offline'>('auto');

    useEffect(() => {
        const updateStatus = () => {
            setMarketStatus(isMarketOpen());
        };

        updateStatus();
        const interval = setInterval(updateStatus, 60000);

        return () => clearInterval(interval);
    }, []);

    const isOnline = manualMode === 'online' || (manualMode === 'auto' && marketStatus.isOpen);

    return {
        isOnline,
        marketStatus,
        manualMode,
        setManualMode
    };
}
