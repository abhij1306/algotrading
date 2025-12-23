"use client"

import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { Command } from 'cmdk'
import * as Dialog from '@radix-ui/react-dialog'
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
        <Dialog.Root open={open} onOpenChange={setOpen}>
            <Dialog.Portal>
                {/* Backdrop overlay */}
                <Dialog.Overlay className="fixed inset-0 bg-black/40 backdrop-blur-sm z-[49] animate-fade-in" />

                {/* Dialog content */}
                <Dialog.Content
                    className="fixed top-[20%] left-1/2 -translate-x-1/2 w-[600px] max-w-[90vw] z-[var(--z-overlay)]
                       glass-strong rounded-xl shadow-2xl animate-fade-in border border-white/10"
                    aria-describedby={undefined}
                >
                    {/* Visually hidden title for accessibility */}
                    <Dialog.Title className="sr-only">Command Palette</Dialog.Title>

                    {/* Command component */}
                    <Command>
                        {/* Input */}
                        <Command.Input
                            placeholder="Ask SmartTrader or Search symbols..."
                            className="w-full px-4 py-3 bg-transparent text-white border-none 
                             focus:outline-none placeholder:text-gray-500 text-sm font-ui"
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

                            {/* Modules & Actions */}
                            <Command.Group heading="Modules & Actions" className="text-xs text-gray-500 px-2 py-1 mt-2 uppercase tracking-wide">
                                <Command.Item
                                    onSelect={() => {
                                        router.push('/terminal')
                                        setOpen(false)
                                    }}
                                    className="px-3 py-2 rounded-lg hover:bg-white/5 cursor-pointer flex items-center gap-3 transition-colors group"
                                >
                                    <span className="text-lg group-hover:scale-110 transition-transform">⚡</span>
                                    <span className="flex-1 font-bold text-gray-200 group-hover:text-white">Terminal</span>
                                    <kbd className="ml-auto text-xs bg-white/10 px-1.5 py-0.5 rounded font-mono text-gray-400">⌘T</kbd>
                                </Command.Item>

                                <Command.Item
                                    onSelect={() => {
                                        router.push('/analyst')
                                        setOpen(false)
                                    }}
                                    className="px-3 py-2 rounded-lg hover:bg-white/5 cursor-pointer flex items-center gap-3 transition-colors group"
                                >
                                    <span className="text-lg group-hover:scale-110 transition-transform">🕵️</span>
                                    <span className="flex-1 text-gray-300 group-hover:text-white">Analyst</span>
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
                                    <span className="flex-1 text-gray-300 group-hover:text-white">Tester</span>
                                    <kbd className="ml-auto text-xs bg-white/10 px-1.5 py-0.5 rounded font-mono text-gray-400">⌘B</kbd>
                                </Command.Item>

                                <Command.Item
                                    onSelect={() => {
                                        router.push('/screener')
                                        setOpen(false)
                                    }}
                                    className="px-3 py-2 rounded-lg hover:bg-white/5 cursor-pointer flex items-center gap-3 transition-colors group"
                                >
                                    <span className="text-lg group-hover:scale-110 transition-transform">🔍</span>
                                    <span className="flex-1 text-gray-300 group-hover:text-white">Screener</span>
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
                    </Command>
                </Dialog.Content>
            </Dialog.Portal>
        </Dialog.Root>
    )
}
