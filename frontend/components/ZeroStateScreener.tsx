'use client'

import { Search, FilterX } from 'lucide-react'

export default function ZeroStateScreener() {
  return (
    <div className="flex flex-col items-center justify-center h-full text-center py-12">
      <div className="w-16 h-16 rounded-2xl bg-[var(--color-elevated)] flex items-center justify-center mb-4">
        <Search className="w-8 h-8 text-[var(--text-tertiary)]" />
      </div>
      <h3 className="text-[var(--text-primary)] font-semibold text-lg mb-2">No stocks found</h3>
      <p className="text-[var(--text-secondary)] text-sm max-w-sm mb-4">
        Try adjusting your filters or search query to find stocks matching your criteria.
      </p>
      <button 
        className="flex items-center gap-2 px-4 py-2 bg-[var(--color-elevated)] hover:bg-[var(--border-default)] rounded-lg text-sm text-[var(--text-primary)] transition-colors"
        onClick={() => {
          // Reset filters logic would go here
          window.location.reload()
        }}
      >
        <FilterX className="w-4 h-4" />
        Reset Filters
      </button>
    </div>
  )
}
