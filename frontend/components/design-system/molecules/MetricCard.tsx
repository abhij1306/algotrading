'use client'

import * as React from 'react'
import { Heading, Data, Badge } from '../atoms'
import { cn } from '@/lib/utils'
import { TrendingUp, TrendingDown } from 'lucide-react'

export interface MetricCardProps {
    title: string
    value: string | number
    change?: number
    changeLabel?: string
    trend?: 'up' | 'down' | 'neutral'
    icon?: React.ReactNode
    sparkline?: React.ReactNode
    className?: string
    glow?: boolean
    onClick?: () => void
}

const MetricCard = React.forwardRef<HTMLDivElement, MetricCardProps>(
    (
        {
            title,
            value,
            change,
            changeLabel,
            trend = 'neutral',
            icon,
            sparkline,
            className,
            glow = false,
            onClick,
        },
        ref
    ) => {
        const isClickable = !!onClick
        const isPositive = trend === 'up' || (change !== undefined && change > 0)
        const isNegative = trend === 'down' || (change !== undefined && change < 0)

        return (
            <div
                ref={ref}
                onClick={onClick}
                className={cn(
                    'glass-progressive rounded-xl p-5 transition-all duration-200',
                    isClickable && 'cursor-pointer hover:shadow-neomorph',
                    glow && isPositive && 'shadow-glow-profit',
                    glow && isNegative && 'shadow-glow-loss',
                    className
                )}
            >
                {/* Header */}
                <div className="flex items-start justify-between mb-3">
                    <div className="flex-1">
                        <p className="text-sm text-text-secondary mb-1">{title}</p>
                        <Data
                            value={value}
                            size="2xl"
                            variant={isPositive ? 'profit' : isNegative ? 'loss' : 'default'}
                            glow={glow}
                            className="font-bold"
                        />
                    </div>

                    {icon && (
                        <div className="flex items-center justify-center w-10 h-10 rounded-lg bg-white/5 text-text-secondary">
                            {icon}
                        </div>
                    )}
                </div>

                {/* Change indicator */}
                {change !== undefined && (
                    <div className="flex items-center gap-2 mt-3">
                        <Badge
                            variant={isPositive ? 'profit' : isNegative ? 'loss' : 'secondary'}
                            size="sm"
                            className="flex items-center gap-1"
                        >
                            {isPositive ? (
                                <TrendingUp className="w-3 h-3" />
                            ) : isNegative ? (
                                <TrendingDown className="w-3 h-3" />
                            ) : null}
                            <Data
                                value={Math.abs(change)}
                                suffix="%"
                                size="sm"
                                variant={isPositive ? 'profit' : isNegative ? 'loss' : 'default'}
                            />
                        </Badge>
                        {changeLabel && (
                            <span className="text-xs text-text-tertiary">{changeLabel}</span>
                        )}
                    </div>
                )}

                {/* Sparkline */}
                {sparkline && (
                    <div className="mt-4 h-12 opacity-60">
                        {sparkline}
                    </div>
                )}
            </div>
        )
    }
)

MetricCard.displayName = 'MetricCard'

export { MetricCard }
