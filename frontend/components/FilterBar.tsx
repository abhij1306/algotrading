'use client'

interface Props {
    filterScore: number
    setFilterScore: (score: number) => void
    totalCount: number
    filteredCount: number
}

export default function FilterBar({ filterScore, setFilterScore, totalCount, filteredCount }: Props) {
    return (
        <div className="bg-slate-800/50 rounded-lg p-4 mb-4 border border-purple-500/30">
            <div className="flex flex-col md:flex-row items-center gap-4">
                <div className="flex-1">
                    <label className="block text-sm font-medium text-gray-300 mb-2">
                        Minimum Score: {filterScore}
                    </label>
                    <input
                        type="range"
                        min="0"
                        max="100"
                        step="5"
                        value={filterScore}
                        onChange={(e) => setFilterScore(Number(e.target.value))}
                        className="w-full h-2 bg-slate-700 rounded-lg appearance-none cursor-pointer accent-purple-500"
                    />
                    <div className="flex justify-between text-xs text-gray-500 mt-1">
                        <span>0</span>
                        <span>50</span>
                        <span>100</span>
                    </div>
                </div>
                <div className="text-center">
                    <p className="text-sm text-gray-400">Showing</p>
                    <p className="text-2xl font-bold text-purple-400">
                        {filteredCount} / {totalCount}
                    </p>
                </div>
            </div>
        </div>
    )
}
