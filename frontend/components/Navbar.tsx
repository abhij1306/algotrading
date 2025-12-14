import { useState } from 'react';

type NavTab = 'screener' | 'portfolio' | 'strategies';

interface NavbarProps {
    activeTab: NavTab;
    onTabChange: (tab: NavTab) => void;
}

export default function Navbar({ activeTab, onTabChange }: NavbarProps) {
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
                </div>

                {/* Right Actions */}
                <div className="flex items-center gap-4">
                    {/* Placeholder for future actions */}
                </div>
            </div>
        </nav>
    );
}
