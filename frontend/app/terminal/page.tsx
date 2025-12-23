'use client'

import Terminal from '@/components/Terminal'

export default function TerminalPage() {
    return (
        <div className="h-full w-full bg-deep-space flex flex-col overflow-hidden p-6 relative">
            {/* Ambient Background */}
            <div className="absolute top-0 left-0 w-full h-full bg-[radial-gradient(circle_at_top_right,_var(--tw-gradient-stops))] from-cyan-900/10 via-[#050505] to-[#050505] pointer-events-none" />

            <div className="h-full w-full relative z-10 glass-card border border-white/10 bg-[#0A0A0A]/60 backdrop-blur-md shadow-2xl overflow-hidden flex flex-col">
                <Terminal />
            </div>
        </div>
    )
}
