/**
 * Market Hours Utility
 * Checks if current time is within market trading hours (9:15 AM - 3:30 PM IST)
 */

export interface MarketStatus {
    isOpen: boolean;
    message: string;
    nextSession?: string;
}

/**
 * Check if market is currently open
 * NSE trading hours: 9:15 AM - 3:30 PM IST (Mon-Fri)
 */
export function isMarketOpen(): MarketStatus {
    const now = new Date();

    // Convert to IST (UTC+5:30)
    const istOffset = 5.5 * 60 * 60 * 1000;
    const istTime = new Date(now.getTime() + istOffset);

    const day = istTime.getUTCDay(); // 0 = Sunday, 6 = Saturday
    const hours = istTime.getUTCHours();
    const minutes = istTime.getUTCMinutes();
    const currentMinutes = hours * 60 + minutes;

    // Check if weekend
    if (day === 0 || day === 6) {
        return {
            isOpen: false,
            message: 'Market Closed (Weekend)',
            nextSession: 'Monday 9:15 AM'
        };
    }

    // Market hours: 9:15 AM (555 minutes) to 3:30 PM (930 minutes)
    const marketOpen = 9 * 60 + 15;  // 555 minutes
    const marketClose = 15 * 60 + 30; // 930 minutes

    if (currentMinutes < marketOpen) {
        return {
            isOpen: false,
            message: 'Market Closed (Pre-market)',
            nextSession: 'Today 9:15 AM'
        };
    }

    if (currentMinutes > marketClose) {
        return {
            isOpen: false,
            message: 'Market Closed (Post-market)',
            nextSession: 'Tomorrow 9:15 AM'
        };
    }

    return {
        isOpen: true,
        message: 'Market Open',
    };
}

/**
 * Get IST time string
 */
export function getISTTime(): string {
    const now = new Date();
    const istOffset = 5.5 * 60 * 60 * 1000;
    const istTime = new Date(now.getTime() + istOffset);

    return istTime.toISOString().substr(11, 8) + ' IST';
}

/**
 * Check if it's a holiday (basic check - can be enhanced with holiday API)
 */
export function isHoliday(): boolean {
    // TODO: Integrate with NSE holiday calendar API
    // For now, just check weekends
    const now = new Date();
    const day = now.getDay();
    return day === 0 || day === 6;
}
