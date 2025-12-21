import React, { useState, useEffect } from 'react';

const TradingModeToggle = () => {
    const [mode, setMode] = useState<'PAPER' | 'LIVE'>('PAPER');
    const [loading, setLoading] = useState(false);

    useEffect(() => {
        fetchStatus();
    }, []);

    const fetchStatus = async () => {
        try {
            const res = await fetch('http://localhost:8000/api/unified/status');
            const data = await res.json();
            setMode(data.mode);
        } catch (e) {
            console.error("Failed to fetch mode", e);
        }
    };

    const toggleMode = async () => {
        if (mode === 'PAPER') {
            if (!confirm("⚠️ WARNING: Switching to LIVE TRADING. Real orders will be placed. Are you sure?")) return;
        }

        setLoading(true);
        const newMode = mode === 'PAPER' ? 'LIVE' : 'PAPER';
        try {
            const res = await fetch('http://localhost:8000/api/unified/toggle-mode', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ mode: newMode })
            });
            const data = await res.json();
            setMode(data.mode as any);
        } catch (e) {
            alert("Failed to switch mode");
        } finally {
            setLoading(false);
        }
    };

    return (
        <div className="flex items-center gap-2">
            <span className={`text-[10px] font-bold tracking-widest ${mode === 'LIVE' ? 'text-red-500' : 'text-green-500'}`}>
                {mode === 'LIVE' ? 'LIVE' : 'PAPER'}
            </span>

            <button
                onClick={toggleMode}
                disabled={loading}
                className={`
          relative inline-flex h-5 w-9 items-center rounded-full transition-colors focus:outline-none 
          ${mode === 'LIVE' ? 'bg-red-900/50 border border-red-500/50' : 'bg-green-900/50 border border-green-500/50'}
        `}
            >
                <span
                    className={`
            inline-block h-3 w-3 transform rounded-full bg-current transition-transform
            ${mode === 'LIVE' ? 'translate-x-5 text-red-500' : 'translate-x-1 text-green-500'}
          `}
                />
            </button>
        </div>
    );
};

export default TradingModeToggle;
