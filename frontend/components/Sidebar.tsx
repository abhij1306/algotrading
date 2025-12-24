'use client'

import Link from 'next/link'
import { usePathname } from 'next/navigation'
import {
    Search,
    TrendingUp,
    BarChart3,
    Terminal,
    Settings,
    HelpCircle,
    Brain,
    Grid3x3,
    Shield,
    Activity,
    Zap
} from 'lucide-react'

import SmartTraderLogo from './brand/SmartTraderLogo'

const navigation = [
    { name: 'Screener', href: '/screener', icon: Search, description: 'Live market scanning' },
    { name: 'Analyst', href: '/analyst', icon: TrendingUp, description: 'Portfolio analysis' },
    {
        name: 'Quant',
        href: '/quant/strategies',  // Default route
        icon: Brain,
        description: 'Strategies & Risk',
        subItems: [
            { name: 'Strategies', href: '/quant/strategies', icon: Grid3x3, description: 'Strategy Library' },
            { name: 'Portfolios', href: '/quant/portfolios', icon: TrendingUp, description: 'Portfolio Research' },
            { name: 'Governance', href: '/quant/governance', icon: Shield, description: 'Risk Policies' },
            { name: 'Backtest', href: '/quant/backtest', icon: BarChart3, description: 'Simulation' },
            { name: 'Live Monitor', href: '/quant/monitoring', icon: Activity, description: 'Real-time tracking' }
        ]
    },
    { name: 'Trader', href: '/trader', icon: Zap, description: 'Execution Terminal' },
]

const secondaryNav: { name: string; href: string; icon: any }[] = []

export default function Sidebar() {
    const pathname = usePathname()

    return (
        <aside className="w-64 bg-[#0A0A0A] border-r border-white/10 flex flex-col h-full">
            {/* Logo */}
            <div className="h-16 flex items-center px-6 border-b border-white/10">
                <div className="flex items-center gap-3">
                    <SmartTraderLogo className="w-10 h-10" />
                    <div>
                        <h1 className="text-white font-bold text-sm">SmartTrader</h1>
                        <p className="text-[9px] text-gray-600 font-mono">v2.0.0</p>
                    </div>
                </div>
            </div>

            {/* Primary Navigation */}
            <nav className="flex-1 px-3 py-4 space-y-1 overflow-y-auto">
                <div className="text-[9px] font-bold text-gray-600 uppercase tracking-widest px-3 mb-2">
                    Core Modules
                </div>
                {/* Main Navigation */}
                {navigation.map((item) => {
                    const hasSubItems = item.subItems && item.subItems.length > 0;
                    const isActive = hasSubItems
                        ? item.subItems.some(sub => pathname?.startsWith(sub.href))
                        : pathname === item.href || pathname?.startsWith(`${item.href}/`);

                    return (
                        <div key={item.name}>
                            <Link
                                href={item.href}
                                className={`flex items-center gap-3 px-3 py-2.5 rounded-lg transition-all ${isActive
                                        ? 'bg-white/10 text-white border-l-2 border-cyan-500'
                                        : 'text-gray-400 hover:bg-white/5 hover:text-white'
                                    }`}
                            >
                                <item.icon className="w-5 h-5" />
                                <div className="flex-1">
                                    <div className="text-sm font-medium">{item.name}</div>
                                    <div className="text-[10px] text-gray-600">{item.description}</div>
                                </div>
                            </Link>

                            {/* Sub-items (for Quant module) */}
                            {hasSubItems && isActive && (
                                <div className="ml-8 mt-1 space-y-0.5 border-l border-white/10 pl-3">
                                    {item.subItems.map((subItem) => {
                                        const isSubActive = pathname === subItem.href;
                                        return (
                                            <Link
                                                key={subItem.name}
                                                href={subItem.href}
                                                className={`flex items-center gap-2 px-2 py-1.5 rounded text-xs transition-all ${isSubActive
                                                        ? 'bg-cyan-500/10 text-cyan-400'
                                                        : 'text-gray-500 hover:text-gray-300 hover:bg-white/5'
                                                    }`}
                                            >
                                                <subItem.icon className="w-3.5 h-3.5" />
                                                <span>{subItem.name}</span>
                                            </Link>
                                        );
                                    })}
                                </div>
                            )}
                        </div>
                    );
                })}

                <div className="pt-4">
                    <div className="text-[9px] font-bold text-gray-600 uppercase tracking-widest px-3 mb-2">
                        Research
                    </div>
                    {secondaryNav.map((item) => {
                        const isActive = pathname === item.href
                        const Icon = item.icon

                        return (
                            <Link
                                key={item.name}
                                href={item.href}
                                className={`
                  flex items-center gap-3 px-3 py-2 rounded-lg transition-all
                  ${isActive
                                        ? 'bg-white/5 text-gray-200'
                                        : 'text-gray-500 hover:text-gray-300 hover:bg-white/5'
                                    }
                `}
                            >
                                <Icon className="w-4 h-4 flex-shrink-0" />
                                <span className="text-xs font-medium truncate">{item.name}</span>
                            </Link>
                        )
                    })}
                </div>
            </nav>

            {/* Footer */}
            <div className="p-3 border-t border-white/10 space-y-1">
                <button className="w-full flex items-center gap-3 px-3 py-2 rounded-lg text-gray-500 hover:text-gray-300 hover:bg-white/5 transition-all text-sm">
                    <Settings className="w-4 h-4" />
                    <span className="text-xs">Settings</span>
                </button>
                <button className="w-full flex items-center gap-3 px-3 py-2 rounded-lg text-gray-500 hover:text-gray-300 hover:bg-white/5 transition-all text-sm">
                    <HelpCircle className="w-4 h-4" />
                    <span className="text-xs">Help</span>
                </button>
            </div>
        </aside>
    )
}
