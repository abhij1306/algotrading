"use client"

interface ProgressBarProps {
    current: number
    total: number
    task: string
    startTime?: number
}

export function ProgressBar({ current, total, task, startTime = Date.now() }: ProgressBarProps) {
    const percent = (current / total) * 100
    const elapsed = Date.now() - startTime
    const rate = current / (elapsed / 1000)  // items per second
    const remaining = total - current
    const eta = remaining / rate

    const formatETA = (seconds: number) => {
        if (isNaN(seconds) || !isFinite(seconds)) return '...'
        if (seconds < 60) return `${Math.round(seconds)}s`
        if (seconds < 3600) return `${Math.round(seconds / 60)}m`
        return `${Math.round(seconds / 3600)}h`
    }

    const formatNumber = (num: number) => {
        return num.toLocaleString('en-IN')
    }

    return (
        <div className="w-full p-4 glass rounded-lg">
            {/* Header */}
            <div className="flex justify-between text-sm mb-2">
                <span className="text-gray-300">{task}</span>
                <span className="font-mono text-gray-400 tabular-nums">
                    {formatNumber(current)} / {formatNumber(total)} ({percent.toFixed(1)}%)
                </span>
            </div>

            {/* Progress bar */}
            <div className="w-full bg-white/10 rounded-full h-2 overflow-hidden">
                <div
                    className="bg-gradient-to-r from-blue-500 to-blue-400 h-2 rounded-full 
                     transition-all duration-300 ease-out"
                    style={{ width: `${Math.min(100, percent)}%` }}
                />
            </div>

            {/* Footer */}
            <div className="flex justify-between text-xs text-gray-500 mt-2">
                <span className="tabular-nums">
                    {rate > 0 ? `${rate.toFixed(1)} items/sec` : 'Calculating...'}
                </span>
                <span className="tabular-nums">
                    ETA: {formatETA(eta)}
                </span>
            </div>
        </div>
    )
}

// Compact version for inline use
export function ProgressBarCompact({ current, total }: { current: number; total: number }) {
    const percent = (current / total) * 100

    return (
        <div className="flex items-center gap-2">
            <div className="flex-1 bg-white/10 rounded-full h-1.5 overflow-hidden">
                <div
                    className="bg-blue-500 h-1.5 rounded-full transition-all duration-300"
                    style={{ width: `${Math.min(100, percent)}%` }}
                />
            </div>
            <span className="text-xs text-gray-400 tabular-nums min-w-[3rem]">
                {percent.toFixed(0)}%
            </span>
        </div>
    )
}
