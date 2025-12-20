'use client'

export default function ZeroStateScreener() {
    return (
        <div className="h-full flex items-center justify-center p-12">
            <div className="text-center max-w-md">
                <div className="text-6xl mb-4">📊</div>
                <h2 className="text-2xl font-semibold mb-2">No Stocks Found</h2>
                <p className="text-text-secondary mb-6">
                    Try adjusting your filters or search for a specific symbol
                </p>
            </div>
        </div>
    )
}
