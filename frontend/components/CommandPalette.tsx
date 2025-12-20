"use client"

import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { Command } from 'cmdk'
import './CommandPalette.css'

export function CommandPalette() {
    const [open, setOpen] = useState(false)
    const router = useRouter()

    // Global keyboard shortcut (Cmd+K / Ctrl+K)
    useEffect(() => {
        const down = (e: KeyboardEvent) => {
            if (e.key === 'k' && (e.metaKey || e.ctrlKey)) {
                e.preventDefault()
                setOpen((open) => !open)
            }
        }

        document.addEventListener('keydown', down)
        return () => document.removeEventListener('keydown', down)
    }, [])

    return (
        <>
            {/* Backdrop blur overlay */}
            {open && (
                <div
                    className="fixed inset-0 bg-black/40 backdrop-blur-sm z-40 animate-fade-in"
                    onClick={() => setOpen(false)}
                />
            )}

            {/* Command palette */}
            <Command.Dialog
                open={open}
                onOpenChange={setOpen}
                label="Command Palette"
                className="fixed top-[20%] left-1/2 -translate-x-1/2 w-[600px] max-w-[90vw] z-50
                   glass-strong rounded-xl shadow-2xl animate-fade-in"
            >
                {/* Accessibility Fix: Visually hidden title */}
                <div className="sr-only" style={{ position: 'absolute', width: 1, height: 1, padding: 0, margin: -1, overflow: 'hidden', clip: 'rect(0, 0, 0, 0)', whiteSpace: 'nowrap', borderWidth: 0 }}>
                    <h2>Command Palette</h2>
                </div>

                {/* Input - transparent bg, white text */}
                <Command.Input
                    placeholder="Ask SmarTrader Agents or Search symbols..."
                    className="w-full px-4 py-3 bg-transparent text-white border-none 
                     focus:outline-none placeholder:text-gray-500 text-sm"
                />

                <Command.List className="max-h-[400px] overflow-y-auto p-2">
                    {/* Recent / Suggested */}
                    <Command.Group heading="Recent" className="text-xs text-gray-500 px-2 py-1 uppercase tracking-wide">
                        <Command.Item className="px-3 py-2 rounded-lg hover:bg-white/5 cursor-pointer flex items-center gap-2 transition-colors">
                            <span className="font-mono font-semibold text-cyan-400">RELIANCE</span>
                            <span className="text-gray-500 text-sm">Reliance Industries</span>
                        </Command.Item>
                        <Command.Item className="px-3 py-2 rounded-lg hover:bg-white/5 cursor-pointer flex items-center gap-2 transition-colors">
                            <span className="font-mono font-semibold text-cyan-400">INFY</span>
                            <span className="text-gray-500 text-sm">Infosys Limited</span>
                        </Command.Item>
                    </Command.Group>

                    {/* Agents & Actions */}
                    <Command.Group heading="Agents & Actions" className="text-xs text-gray-500 px-2 py-1 mt-2 uppercase tracking-wide">
                        <Command.Item
                            onSelect={() => {
                                router.push('/?view=trader')
                                setOpen(false)
                            }}
                            className="px-3 py-2 rounded-lg hover:bg-white/5 cursor-pointer flex items-center gap-3 transition-colors group"
                        >
                            <span className="text-lg group-hover:scale-110 transition-transform">⚡</span>
                            <span className="flex-1 font-bold text-gray-200 group-hover:text-white">Trader Agent (Live Terminal)</span>
                            <kbd className="ml-auto text-xs bg-white/10 px-1.5 py-0.5 rounded font-mono text-gray-400">⌘T</kbd>
                        </Command.Item>

                        <Command.Item
                            onSelect={() => {
                                router.push('/?view=analyst')
                                setOpen(false)
                            }}
                            className="px-3 py-2 rounded-lg hover:bg-white/5 cursor-pointer flex items-center gap-3 transition-colors group"
                        >
                            <span className="text-lg group-hover:scale-110 transition-transform">🕵️</span>
                            <span className="flex-1 text-gray-300 group-hover:text-white">Analyst Agent (Portfolio)</span>
                            <kbd className="ml-auto text-xs bg-white/10 px-1.5 py-0.5 rounded font-mono text-gray-400">⌘P</kbd>
                        </Command.Item>

                        <Command.Item
                            onSelect={() => {
                                router.push('/?view=tester')
                                setOpen(false)
                            }}
                            className="px-3 py-2 rounded-lg hover:bg-white/5 cursor-pointer flex items-center gap-3 transition-colors group"
                        >
                            <span className="text-lg group-hover:scale-110 transition-transform">🧪</span>
                            <span className="flex-1 text-gray-300 group-hover:text-white">Tester Agent (Backtest Lab)</span>
                            <kbd className="ml-auto text-xs bg-white/10 px-1.5 py-0.5 rounded font-mono text-gray-400">⌘B</kbd>
                        </Command.Item>

                        <Command.Item
                            onSelect={() => {
                                router.push('/?view=screener')
                                setOpen(false)
                            }}
                            className="px-3 py-2 rounded-lg hover:bg-white/5 cursor-pointer flex items-center gap-3 transition-colors group"
                        >
                            <span className="text-lg group-hover:scale-110 transition-transform">🔍</span>
                            <span className="flex-1 text-gray-300 group-hover:text-white">Screener Agent (Scanner)</span>
                            <kbd className="ml-auto text-xs bg-white/10 px-1.5 py-0.5 rounded font-mono text-gray-400">⌘S</kbd>
                        </Command.Item>

                        <div className="h-px bg-white/5 my-1 mx-2"></div>

                        <Command.Item
                            onSelect={() => {
                                window.open('/api/fyers/login', '_blank')
                                setOpen(false)
                            }}
                            className="px-3 py-2 rounded-lg hover:bg-white/5 cursor-pointer flex items-center gap-3 transition-colors text-gray-400 hover:text-cyan-400"
                        >
                            <span className="text-lg">🔑</span>
                            <span className="flex-1">Refresh Fyers API Token</span>
                        </Command.Item>
                    </Command.Group>

                    {/* Empty state */}
                    <Command.Empty className="px-4 py-8 text-center text-gray-500 text-sm">
                        No results found.
                    </Command.Empty>
                </Command.List>
            </Command.Dialog>
        </>
    )
}
