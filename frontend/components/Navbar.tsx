import { useState, useEffect } from 'react';
import { Sparkles } from 'lucide-react';

type NavTab = 'screener' | 'portfolio' | 'strategies' | 'signals' | 'terminal';

interface NavbarProps {
    activeTab: NavTab;
    onTabChange: (tab: NavTab) => void;
}

export default function Navbar({ activeTab, onTabChange }: NavbarProps) {
    const [isMarketOpen, setIsMarketOpen] = useState(false);
    const [currentTime, setCurrentTime] = useState('');

    useEffect(() => {
        const checkMarketStatus = () => {
            const now = new Date();

            // System is already in IST, use local time directly
            const day = now.getDay(); // 0 = Sunday, 6 = Saturday
            const hours = now.getHours();
            const minutes = now.getMinutes();
            const totalMinutes = hours * 60 + minutes;

            // Market hours: 9:15 AM to 3:30 PM IST (Monday to Friday)
            const marketOpen = 9 * 60 + 15; // 9:15 AM
            const marketClose = 15 * 60 + 30; // 3:30 PM

            const isWeekday = day >= 1 && day <= 5;
            const isDuringMarketHours = totalMinutes >= marketOpen && totalMinutes <= marketClose;

            setIsMarketOpen(isWeekday && isDuringMarketHours);
            setCurrentTime(now.toLocaleTimeString('en-IN', {
                hour: '2-digit',
                minute: '2-digit',
                hour12: false
            }));
        };

        checkMarketStatus();
        const interval = setInterval(checkMarketStatus, 10000); // Update every 10 seconds

        return () => clearInterval(interval);
    }, []);

    return (
        <nav className="border-b border-border-dark bg-background-dark/80 backdrop-blur-md sticky top-0 z-50">
            <div className="max-w-[1920px] mx-auto px-6 h-16 flex items-center justify-between">
                {/* Branding */}
                <div className="flex items-center gap-2">
                    <div className="w-8 h-8 bg-primary rounded-lg flex items-center justify-center text-white font-bold text-lg shadow-lg shadow-primary/20">
                        S
                    </div>
                    <span className="text-xl font-bold text-white tracking-tight">Smart Trader</span>
                </div>

                {/* Navigation Links */}
                <div className="flex items-center gap-1 bg-card-dark p-1 rounded-lg border border-border-dark">
                    <button
                        onClick={() => onTabChange('screener')}
                        className={`px-4 py-1.5 rounded-md text-sm font-medium transition-all duration-200 ${activeTab === 'screener'
                            ? 'bg-primary text-white shadow-sm'
                            : 'text-text-secondary hover:text-white hover:bg-white/5'
                            }`}
                    >
                        Screener
                    </button>
                    <button
                        onClick={() => onTabChange('portfolio')}
                        className={`px-4 py-1.5 rounded-md text-sm font-medium transition-all duration-200 ${activeTab === 'portfolio'
                            ? 'bg-primary text-white shadow-sm'
                            : 'text-text-secondary hover:text-white hover:bg-white/5'
                            }`}
                    >
                        Portfolio Risk
                    </button>
                    <button
                        onClick={() => onTabChange('strategies')}
                        className={`px-4 py-1.5 rounded-md text-sm font-medium transition-all duration-200 ${activeTab === 'strategies'
                            ? 'bg-primary text-white shadow-sm'
                            : 'text-text-secondary hover:text-white hover:bg-white/5'
                            }`}
                    >
                        Backtest
                    </button>
                    <button
                        onClick={() => onTabChange('signals')}
                        className={`px-4 py-1.5 rounded-md text-sm font-medium transition-all duration-200 ${activeTab === 'signals'
                            ? 'bg-primary text-white shadow-sm'
                            : 'text-text-secondary hover:text-white hover:bg-white/5'
                            }`}
                    >
                        Smart Trader
                    </button>
                    <button
                        onClick={() => onTabChange('terminal')}
                        className={`px-4 py-1.5 rounded-md text-sm font-medium transition-all duration-200 ${activeTab === 'terminal'
                            ? 'bg-primary text-white shadow-sm'
                            : 'text-text-secondary hover:text-white hover:bg-white/5'
                            }`}
                    >
                        Terminal
                    </button>
                </div>

                {/* Right Actions - Market Status */}
                <div className="flex items-center gap-3">
                    <div className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-card-dark border border-border-dark">
                        <div className="flex items-center gap-1.5">
                            <div className={`w-2 h-2 rounded-full ${isMarketOpen ? 'bg-green-500 animate-pulse' : 'bg-red-500'}`} />
                            <span className="text-xs font-medium text-gray-300">
                                {isMarketOpen ? 'Market Open' : 'Market Closed'}
                            </span>
                        </div>
                        <div className="h-4 w-px bg-border-dark" />
                        <span className="text-xs font-mono text-gray-400">{currentTime}</span>
                    </div>
                </div>
            </div>
        </nav>
    );
}
