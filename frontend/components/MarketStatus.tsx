'use client'

import { useEffect, useState } from 'react'

export default function MarketStatus() {
    const [isMarketOpen, setIsMarketOpen] = useState(false)

    useEffect(() => {
        const checkMarketStatus = () => {
            const now = new Date()

            // Get IST time
            const istOffset = 5.5 * 60 * 60 * 1000 // IST is UTC+5:30
            const istTime = new Date(now.getTime() + istOffset)

            const hours = istTime.getUTCHours()
            const minutes = istTime.getUTCMinutes()
            const dayOfWeek = istTime.getUTCDay() // 0 = Sunday, 6 = Saturday

            // Market hours: 9:15 AM to 3:30 PM IST, Monday-Friday
            const marketStartMinutes = 9 * 60 + 15 // 9:15 AM
            const marketEndMinutes = 15 * 60 + 30 // 3:30 PM
            const currentMinutes = hours * 60 + minutes

            const isWeekday = dayOfWeek >= 1 && dayOfWeek <= 5 // Monday-Friday
            const isDuringMarketHours = currentMinutes >= marketStartMinutes && currentMinutes <= marketEndMinutes

            setIsMarketOpen(isWeekday && isDuringMarketHours)
        }

        // Check immediately
        checkMarketStatus()

        // Check every 30 seconds
        const interval = setInterval(checkMarketStatus, 30000)

        return () => clearInterval(interval)
    }, [])

    return (
        <div className={`flex items-center gap-2 px-3 py-1.5 rounded-lg border text-xs font-bold ${isMarketOpen
                ? 'bg-green-600/10 border-green-500/30 text-green-400'
                : 'bg-gray-600/10 border-gray-500/30 text-gray-400'
            }`}>
            <div className={`w-2 h-2 rounded-full ${isMarketOpen ? 'bg-green-400 animate-pulse' : 'bg-gray-400'}`} />
            {isMarketOpen ? 'MARKET ONLINE' : 'MARKET OFFLINE'}
        </div>
    )
}
