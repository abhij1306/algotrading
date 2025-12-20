/**
 * Utility functions for the AlgoTrading platform
 */

/**
 * Format number as Indian currency
 */
export function formatCurrency(value: number, decimals: number = 2): string {
    return new Intl.NumberFormat('en-IN', {
        style: 'currency',
        currency: 'INR',
        minimumFractionDigits: decimals,
        maximumFractionDigits: decimals,
    }).format(value)
}

/**
 * Format number with Indian number system (lakhs, crores)
 */
export function formatIndianNumber(value: number): string {
    if (value >= 10000000) {
        return `${(value / 10000000).toFixed(2)}Cr`
    }
    if (value >= 100000) {
        return `${(value / 100000).toFixed(2)}L`
    }
    if (value >= 1000) {
        return `${(value / 1000).toFixed(2)}K`
    }
    return value.toFixed(0)
}

/**
 * Format percentage with color
 */
export function formatPercentage(value: number, decimals: number = 2): string {
    const sign = value > 0 ? '+' : ''
    return `${sign}${value.toFixed(decimals)}%`
}

/**
 * Get color class for profit/loss
 */
export function getProfitLossColor(value: number): string {
    if (value > 0) return 'text-profit'
    if (value < 0) return 'text-loss'
    return 'text-gray-400'
}

/**
 * Format date to Indian format
 */
export function formatDate(date: Date | string): string {
    const d = typeof date === 'string' ? new Date(date) : date
    return new Intl.DateTimeFormat('en-IN', {
        day: '2-digit',
        month: 'short',
        year: 'numeric',
    }).format(d)
}

/**
 * Check if market is open (NSE hours: 9:15 AM - 3:30 PM IST)
 */
export function isMarketOpen(): boolean {
    const now = new Date()
    const hours = now.getHours()
    const minutes = now.getMinutes()
    const day = now.getDay()

    // Weekend
    if (day === 0 || day === 6) return false

    // Before 9:15 AM
    if (hours < 9 || (hours === 9 && minutes < 15)) return false

    // After 3:30 PM
    if (hours > 15 || (hours === 15 && minutes >= 30)) return false

    return true
}

/**
 * Copy text to clipboard
 */
export async function copyToClipboard(text: string): Promise<boolean> {
    try {
        await navigator.clipboard.writeText(text)
        return true
    } catch (err) {
        console.error('Failed to copy:', err)
        return false
    }
}
