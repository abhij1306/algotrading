'use client'

import Terminal from '@/components/Terminal'
import { GlassCard } from '@/components/ui/GlassCard'

export default function TerminalPage() {
  return (
    <div className="h-full w-full bg-[var(--color-base)] flex flex-col overflow-hidden p-6">
      <GlassCard className="flex-1 flex flex-col overflow-hidden">
        <Terminal />
      </GlassCard>
    </div>
  )
}
