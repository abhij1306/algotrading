"use client"

interface SparklineProps {
    data: number[]
    width?: number
    height?: number
    color?: string
    showDots?: boolean
}

export function Sparkline({
    data,
    width = 80,
    height = 24,
    color = '#3B82F6',
    showDots = false,
}: SparklineProps) {
    if (!data || data.length === 0) {
        return <div style={{ width, height }} className="bg-white/5 rounded" />
    }

    const min = Math.min(...data)
    const max = Math.max(...data)
    const range = max - min || 1

    // Normalize data to 0-1 range
    const normalized = data.map((val) => (val - min) / range)

    // Create SVG path
    const points = normalized.map((val, i) => {
        const x = (i / (data.length - 1)) * width
        const y = height - val * height
        return `${x},${y}`
    })

    const pathD = `M ${points.join(' L ')}`

    // Determine color based on trend
    const trend = data[data.length - 1] - data[0]
    const strokeColor = trend > 0 ? '#00E396' : trend < 0 ? '#FF4560' : color

    return (
        <svg
            width={width}
            height={height}
            className="inline-block"
            style={{ verticalAlign: 'middle' }}
        >
            {/* Area fill */}
            <defs>
                <linearGradient id={`gradient-${Math.random()}`} x1="0%" y1="0%" x2="0%" y2="100%">
                    <stop offset="0%" stopColor={strokeColor} stopOpacity="0.3" />
                    <stop offset="100%" stopColor={strokeColor} stopOpacity="0" />
                </linearGradient>
            </defs>

            {/* Fill area under line */}
            <path
                d={`${pathD} L ${width},${height} L 0,${height} Z`}
                fill={`url(#gradient-${Math.random()})`}
            />

            {/* Line */}
            <path
                d={pathD}
                fill="none"
                stroke={strokeColor}
                strokeWidth="1.5"
                strokeLinecap="round"
                strokeLinejoin="round"
            />

            {/* Dots at data points */}
            {showDots &&
                normalized.map((val, i) => {
                    const x = (i / (data.length - 1)) * width
                    const y = height - val * height
                    return (
                        <circle
                            key={i}
                            cx={x}
                            cy={y}
                            r="1.5"
                            fill={strokeColor}
                            opacity="0.6"
                        />
                    )
                })}
        </svg>
    )
}

// Compact version for dense tables
export function SparklineCompact({ data }: { data: number[] }) {
    return <Sparkline data={data} width={60} height={16} showDots={false} />
}
