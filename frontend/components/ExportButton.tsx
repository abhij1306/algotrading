'use client'

interface Stock {
    symbol: string
    close: number
    ema20: number
    ema50: number
    atr_pct: number
    rsi: number
    vol_percentile: number
    intraday_score?: number
    swing_score?: number
}

interface Props {
    data: Stock[]
    filename: string
}

export default function ExportButton({ data, filename }: Props) {
    const exportToCSV = () => {
        if (data.length === 0) {
            alert('No data to export')
            return
        }

        // Create CSV headers
        const headers = ['Symbol', 'Close', 'EMA20', 'EMA50', 'ATR%', 'RSI', 'Vol %ile', 'Score']

        // Create CSV rows
        const rows = data.map(stock => [
            stock.symbol,
            stock.close.toFixed(2),
            stock.ema20.toFixed(2),
            stock.ema50.toFixed(2),
            stock.atr_pct.toFixed(2),
            stock.rsi.toFixed(1),
            Math.round(stock.vol_percentile),
            (stock.intraday_score || stock.swing_score || 0).toFixed(0)
        ])

        // Combine headers and rows
        const csvContent = [
            headers.join(','),
            ...rows.map(row => row.join(','))
        ].join('\n')

        // Create blob and download
        const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' })
        const link = document.createElement('a')
        const url = URL.createObjectURL(blob)

        link.setAttribute('href', url)
        link.setAttribute('download', `${filename}_${new Date().toISOString().split('T')[0]}.csv`)
        link.style.visibility = 'hidden'

        document.body.appendChild(link)
        link.click()
        document.body.removeChild(link)
    }

    return (
        <button
            onClick={exportToCSV}
            className="bg-green-600 hover:bg-green-700 text-white px-4 py-2 rounded-lg font-semibold transition flex items-center gap-2 shadow-lg shadow-green-500/30"
        >
            <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 10v6m0 0l-3-3m3 3l3-3m2 8H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
            </svg>
            Export CSV ({data.length})
        </button>
    )
}
