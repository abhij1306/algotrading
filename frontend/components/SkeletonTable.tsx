'use client'

interface SkeletonTableProps {
  rows?: number
  columns?: number
}

export default function SkeletonTable({ rows = 10, columns = 8 }: SkeletonTableProps) {
  return (
    <div className="w-full">
      {/* Header */}
      <div className="grid gap-2 mb-3" style={{ gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))` }}>
        {Array.from({ length: columns }).map((_, i) => (
          <div key={`header-${i}`} className="h-6 bg-[var(--color-elevated)] rounded animate-pulse" />
        ))}
      </div>
      {/* Rows */}
      {Array.from({ length: rows }).map((_, rowIndex) => (
        <div 
          key={`row-${rowIndex}`} 
          className="grid gap-2 py-2 border-t border-[var(--border-subtle)]"
          style={{ gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))` }}
        >
          {Array.from({ length: columns }).map((_, colIndex) => (
            <div 
              key={`cell-${rowIndex}-${colIndex}`} 
              className="h-6 bg-[var(--color-surface)] rounded animate-pulse" 
            />
          ))}
        </div>
      ))}
    </div>
  )
}
