'use client';

import { useState, useEffect } from 'react';
import { usePathname, useRouter } from 'next/navigation';
import {
    LayoutDashboard,
    PieChart,
    LayoutGrid,
    Zap,
    ChevronLeft,
    ChevronRight,
    Settings,
    LogOut,
    Menu,
    Activity,
    Brain,
    Grid3x3,
    Briefcase,
    Shield,
    BarChart3,
    Radio
} from 'lucide-react';
import LoginButton from '@/components/LoginButton';
import { OnlineOfflineToggle } from '@/components/OnlineOfflineToggle';

export default function UnifiedSidebar() {
    const pathname = usePathname();
    const router = useRouter();
    const [isCollapsed, setIsCollapsed] = useState(false);
    const [isHovered, setIsHovered] = useState(false);

    // Auto-collapse logic for data-dense pages
    useEffect(() => {
        if (pathname?.includes('/screener') || pathname?.includes('/terminal')) {
            setIsCollapsed(true);
        } else {
            setIsCollapsed(false);
        }
    }, [pathname]);

    const MAIN_NAV = [
        { id: 'dashboard', label: 'Dashboard', icon: LayoutDashboard, path: '/quant' },
        { id: 'screener', label: 'Screener', icon: LayoutGrid, path: '/screener' },
        { id: 'analyst', label: 'Analyst', icon: PieChart, path: '/analyst' },
        { id: 'terminal', label: 'Terminal', icon: Zap, path: '/terminal' },
    ];

    // Sub-navigation for Quant Module
    const QUANT_SUBNAV = [
        { label: 'Live Monitor', icon: Activity, path: '/quant/monitoring' },
        { label: 'Signals', icon: Radio, path: '/quant/signals' },
        { label: 'Strategies', icon: Grid3x3, path: '/quant/strategies' },
        { label: 'Portfolios', icon: Briefcase, path: '/quant/portfolios' },
        { label: 'Governance', icon: Shield, path: '/quant/governance' },
        { label: 'Backtest', icon: BarChart3, path: '/quant/backtest' },
    ];

    const showQuantSubnav = pathname?.startsWith('/quant');

    return (
        <aside
            className={`h-screen bg-[#050505] border-r border-white/5 flex flex-col transition-all duration-300 ease-[cubic-bezier(0.25,0.1,0.25,1)] z-40 relative group ${isCollapsed && !isHovered ? 'w-16' : 'w-64'
                }`}
            onMouseEnter={() => setIsHovered(true)}
            onMouseLeave={() => setIsHovered(false)}
        >
            {/* Header / Logo */}
            <div className={`h-14 flex items-center ${isCollapsed && !isHovered ? 'justify-center' : 'px-6'} border-b border-white/5 bg-[#080808]/50 backdrop-blur-md shrink-0 transition-all overflow-hidden`}>
                <div className="flex items-center gap-3 min-w-max">
                    <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-cyan-600 to-blue-600 flex items-center justify-center shadow-lg shadow-cyan-500/20">
                        <span className="font-black text-white text-xs tracking-tighter">ST</span>
                    </div>
                    <div className={`flex flex-col transition-opacity duration-200 ${isCollapsed && !isHovered ? 'opacity-0 w-0' : 'opacity-100'}`}>
                        <span className="text-sm font-bold text-white tracking-widest whitespace-nowrap">SMAR<span className="text-cyan-400">TRADER</span></span>
                        <span className="text-[9px] text-gray-500 font-mono tracking-[0.2em] uppercase whitespace-nowrap">AI Native Platform</span>
                    </div>
                </div>
            </div>

            {/* Main Navigation */}
            <div className="flex-1 overflow-y-auto overflow-x-hidden py-4 custom-scrollbar">

                {/* Primary Pillars */}
                <div className="px-2 space-y-1">
                    {MAIN_NAV.map((item) => {
                        const isActive = pathname?.startsWith(item.path);
                        return (
                            <button
                                key={item.id}
                                onClick={() => router.push(item.path)}
                                className={`w-full flex items-center gap-3 px-3 py-2.5 rounded-xl transition-all duration-200 group/item relative ${isActive
                                    ? 'bg-cyan-500/10 text-cyan-400'
                                    : 'text-gray-500 hover:text-gray-200 hover:bg-white/5'
                                    }`}
                                title={isCollapsed && !isHovered ? item.label : ''}
                            >
                                <item.icon className={`w-5 h-5 shrink-0 transition-colors ${isActive ? 'text-cyan-400' : 'text-gray-500 group-hover/item:text-gray-300'}`} />
                                <span className={`text-xs font-bold tracking-wide transition-all whitespace-nowrap ${isCollapsed && !isHovered ? 'opacity-0 w-0 translate-x-4' : 'opacity-100 translate-x-0'}`}>
                                    {item.label}
                                </span>

                                {isActive && (
                                    <div className="absolute left-0 top-1/2 -translate-y-1/2 w-1 h-6 bg-cyan-500 rounded-r-full shadow-[0_0_8px_rgba(6,182,212,0.4)]" />
                                )}
                            </button>
                        );
                    })}
                </div>

                {/* Sub-Navigation (Always Visible) */}
                <>
                    <div className={`h-px bg-white/5 mx-4 my-4 transition-opacity ${isCollapsed && !isHovered ? 'opacity-0' : 'opacity-100'}`} />

                    <div className="px-2 space-y-1">
                        <div className={`px-3 mb-2 text-[10px] font-bold text-gray-600 uppercase tracking-widest transition-all whitespace-nowrap overflow-hidden ${isCollapsed && !isHovered ? 'h-0 opacity-0' : 'h-auto opacity-100'}`}>
                            Quant Modules
                        </div>
                        {QUANT_SUBNAV.map((item) => {
                            const isActive = pathname === item.path;
                            return (
                                <button
                                    key={item.path}
                                    onClick={() => router.push(item.path)}
                                    className={`w-full flex items-center gap-3 px-3 py-2 rounded-lg transition-all duration-200 relative group/sub ${isActive
                                        ? 'text-cyan-400 bg-white/5'
                                        : 'text-gray-500 hover:text-gray-300 hover:bg-white/5'
                                        }`}
                                >
                                    <item.icon className={`w-4 h-4 shrink-0 transition-colors ${isActive ? 'text-cyan-400' : 'text-gray-600 group-hover/sub:text-gray-400'}`} />
                                    <span className={`text-xs font-medium transition-all whitespace-nowrap ${isCollapsed && !isHovered ? 'opacity-0 w-0 translate-x-4' : 'opacity-100 translate-x-0'}`}>
                                        {item.label}
                                    </span>
                                </button>
                            );
                        })}
                    </div>
                </>

            </div>

            {/* Footer / System Status */}
            <div className={`shrink-0 border-t border-white/5 bg-[#080808]/80 backdrop-blur transition-all ${isCollapsed && !isHovered ? 'items-center justify-center' : ''}`}>
                {/* Online/Offline Toggle */}
                <div className={`p-2 border-b border-white/5 ${isCollapsed && !isHovered ? 'hidden' : ''}`}>
                    <OnlineOfflineToggle />
                </div>
                <div className="p-3">
                    <LoginButton collapsed={isCollapsed && !isHovered} />
                </div>
            </div>

            {/* Collapse Toggle (Desktop Only) */}
            <button
                onClick={() => setIsCollapsed(!isCollapsed)}
                className="absolute -right-3 top-20 w-6 h-6 bg-[#0A0A0A] border border-white/10 rounded-full flex items-center justify-center text-gray-500 hover:text-white transition-all hover:scale-110 shadow-xl z-50 group/toggle"
            >
                <ChevronLeft className={`w-3 h-3 transition-transform ${isCollapsed ? 'rotate-180' : ''}`} />
            </button>
        </aside>
    );
}
