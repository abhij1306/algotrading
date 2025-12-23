'use client'

import { useRouter, usePathname } from 'next/navigation'
import {
    LayoutGrid,
    PieChart,
    LayoutDashboard,
    Zap
} from 'lucide-react'
import LoginButton from './LoginButton'
import MarketStatus from './MarketStatus'

const PILLARS = [
    { id: 'screener', label: 'SCREENER', icon: LayoutGrid, path: '/screener', role: 'Data Foundation' },
    { id: 'analyst', label: 'ANALYST', icon: PieChart, path: '/analyst', role: 'Insight & Risk' },
    { id: 'dashboard', label: 'DASHBOARD', icon: LayoutDashboard, path: '/quant', role: 'Quant Research' },
    { id: 'terminal', label: 'TERMINAL', icon: Zap, path: '/terminal', role: 'Live Execution' },
]

export default function Navbar() {
    const router = useRouter()
    const pathname = usePathname()

    const activePillar = PILLARS.find(p =>
        pathname === p.path || (p.path !== '/' && pathname?.startsWith(p.path))
    ) || PILLARS[0]

    return (
        <nav className="h-14 border-b border-white/5 bg-[#080808]/80 backdrop-blur-md flex items-center px-4 justify-between shrink-0 z-50">

            {/* Logo Section */}
            <div
                className="flex items-center gap-3 group cursor-pointer"
                onClick={() => router.push('/')}
            >
                <div className="relative w-9 h-9 flex items-center justify-center transition-transform group-hover:scale-105 duration-500">
                    <div className="absolute inset-0 bg-gradient-to-tr from-cyan-600/30 to-purple-600/30 rounded-xl blur-md group-hover:blur-lg transition-all"></div>
                    <div className="absolute inset-0 border border-white/10 rounded-xl bg-[#0A0A0A]/80 backdrop-blur-xl"></div>
                    <div className="relative font-black tracking-tighter text-transparent bg-clip-text bg-gradient-to-tr from-cyan-400 to-purple-400 text-sm">
                        ST
                    </div>
                </div>

                <div className="h-8 w-[1px] bg-white/5 mx-1"></div>

                <div className="flex flex-col justify-center">
                    <span className="font-bold text-sm tracking-widest text-white leading-none">
                        SMAR<span className="text-cyan-400">TRADER</span>
                    </span>
                    <span className="text-[9px] text-gray-500 font-mono tracking-[0.2em] mt-0.5 flex items-center gap-1.5">
                        AI NATIVE <div className="w-1 h-1 rounded-full bg-green-500 shadow-[0_0_5px_rgba(34,197,94,0.6)] animate-pulse"></div>
                    </span>
                </div>
            </div>

            {/* Main Pillars */}
            <div className="flex items-center bg-white/5 rounded-full p-1 border border-white/5">
                {PILLARS.map((pillar) => (
                    <button
                        key={pillar.id}
                        onClick={() => router.push(pillar.path)}
                        className={`relative px-6 py-1.5 rounded-full text-xs font-bold transition-all duration-300 flex items-center gap-2 ${activePillar.id === pillar.id
                            ? 'text-white'
                            : 'text-gray-500 hover:text-gray-300 hover:bg-white/5'
                            }`}
                    >
                        {activePillar.id === pillar.id && (
                            <div className="absolute inset-0 rounded-full bg-gradient-to-r from-cyan-500/10 to-purple-500/10 border border-white/5"></div>
                        )}
                        <pillar.icon className={`w-3.5 h-3.5 ${activePillar.id === pillar.id ? 'text-cyan-400' : ''}`} />
                        <span className="relative z-10">{pillar.label}</span>
                    </button>
                ))}
            </div>

            {/* Right Side Content */}
            <div className="flex items-center gap-3">
                <div className="hidden md:flex flex-col items-end mr-2">
                    <span className="text-[10px] text-gray-500 font-bold uppercase tracking-wider">Active Pillar</span>
                    <span className="text-xs text-cyan-400 font-mono truncate">{activePillar.role}</span>
                </div>

                <div className="h-8 w-[1px] bg-white/10 hidden sm:block"></div>

                <MarketStatus />

                <div className="h-8 w-[1px] bg-white/10"></div>

                <LoginButton />
            </div>
        </nav>
    )
}
