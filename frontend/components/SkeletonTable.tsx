
'use client'

export default function SkeletonTable({ rows = 15 }: { rows?: number }) {
    return (
        <div className="h-full overflow-hidden animate-pulse">
            {/* Header Placeholder */}
            <div className="sticky top-0 z-10 glass-subtle border-b border-white/5">
                <div className="grid grid-cols-8 gap-4 px-4 py-2">
                    {[...Array(8)].map((_, i) => (
                        <div key={i} className="h-4 bg-white/5 rounded w-16"></div>
                    ))}
                </div>
            </div>

            {/* Rows */}
            <div className="divide-y divide-white/5">
                {[...Array(rows)].map((_, i) => (
                    <div key={i} className="grid grid-cols-8 gap-4 px-4 h-row-dense items-center">
                        <div className="h-4 bg-white/5 rounded w-20"></div> {/* Symbol */}
                        <div className="h-4 bg-white/5 rounded w-16 place-self-end"></div> {/* Price */}
                        <div className="h-4 bg-white/5 rounded w-12 place-self-end"></div> {/* ATR */}
                        <div className="h-4 bg-white/5 rounded w-10 place-self-end"></div> {/* RSI */}
                        <div className="h-4 bg-white/5 rounded w-10 place-self-end"></div> {/* Vol */}
                        <div className="h-4 bg-white/5 rounded w-14 place-self-end"></div> {/* Trend 7D */}
                        <div className="h-4 bg-white/5 rounded w-14 place-self-end"></div> {/* Trend 30D */}
                        <div className="h-4 bg-white/5 rounded w-12 place-self-end"></div> {/* Score */}
                    </div>
                ))}
            </div>
        </div>
    )
}
