'use client';

import { usePathname } from 'next/navigation';
import MarketStatus from '../MarketStatus';
import LoginButton from '../LoginButton';
import { SidebarTrigger } from 'lucide-react'; // Placeholder if we needed a mobile trigger, but we'll stick to desktop focus

export default function GlobalHeader() {
    const pathname = usePathname();

    // Helper to get a clean title based on route
    const getPageTitle = (path: string) => {
        if (path.startsWith('/quant')) return 'QUANT RESEARCH';
        if (path === '/screener') return 'MARKET SCREENER';
        if (path === '/analyst') return 'PORTFOLIO ANALYST';
        if (path === '/terminal') return 'LIVE TERMINAL';
        return 'DASHBOARD';
    };

    return (
        <header className="h-14 border-b border-white/5 bg-[#080808]/50 backdrop-blur-md flex items-center justify-between px-6 shrink-0 z-30">
            {/* Left: Context / Title */}
            <div className="flex items-center gap-4">
                <div className="flex flex-col">
                    <h1 className="text-sm font-black tracking-[0.2em] text-gray-200 uppercase">
                        {getPageTitle(pathname || '')}
                    </h1>
                </div>
            </div>

            {/* Right: System Status */}
            <div className="flex items-center gap-4">
                <MarketStatus />
                <div className="h-4 w-[1px] bg-white/10"></div>
                <LoginButton />
            </div>
        </header>
    );
}
