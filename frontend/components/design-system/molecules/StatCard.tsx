'use client'

import * as React from 'react'
import { Heading, Data } from '../atoms'
import { cn } from '@/lib/utils'
import { TrendingUp, TrendingDown, Minus } from 'lucide-react'

export interface StatCardProps {
    icon: React.ReactNode
    iconColor?: 'blue' | 'purple' | 'cyan' | 'green' | 'red' | 'amber'
    title: string
    value: string | number
    change?: number
    changeType?: 'percentage' | 'absolute'
    trend?: 'up' | 'down' | 'neutral'
    className?: string
    size?: 'sm' | 'md' | 'lg'
}

const StatCard = React.forwardRef<HTMLDivElement, StatCardProps>(
    (
        {
            icon,
            iconColor = 'blue',
            title,
            value,
            change,
            changeType = 'percentage',
            trend,
            className,
            size = 'md',
        },
        ref
    ) => {
        // Auto-detect trend from change if not provided
        const effectiveTrend = trend || (change !== undefined && change > 0 ? 'up' : change !== undefined && change < 0 ? 'down' : 'neutral')
        const isPositive = effectiveTrend === 'up'
        const isNegative = effectiveTrend === 'down'

        const iconColorClasses = {
            blue: 'bg-electric-blue/10 text-electric-blue border-electric-blue/30',
            purple: 'bg-electric-purple/10 text-electric-purple border-electric-purple/30',
            cyan: 'bg-electric-cyan/10 text-electric-cyan border-electric-cyan/30',
            green: 'bg-profit/10 text-profit border-profit/30',
            red: 'bg-loss/10 text-loss border-loss/30',
            amber: 'bg-electric-amber/10 text-electric-amber border-electric-amber/30',
        }

        const sizeClasses = {
            sm: 'p-4',
            md: 'p-5',
            lg: 'p-6',
        }

        const iconSizeClasses = {
            sm: 'w-10 h-10 text-base',
            md: 'w-12 h-12 text-lg',
            lg: 'w-14 h-14 text-xl',
        }

        const valueSizeClasses = {
            sm: 'xl',
            md: '2xl',
            lg: '2xl',
        } as const

        return (
            <div
                ref={ref}
                className={cn(
                    'glass rounded-xl border border-glass-border transition-all duration-200 hover:shadow-neomorph',
                    sizeClasses[size],
                    className
                )}
            >
                <div className="flex items-start gap-4">
                    {/* Icon */}
                    <div
                        className={cn(
                            'flex items-center justify-center rounded-lg border transition-all duration-200',
                            iconSizeClasses[size],
                            iconColorClasses[iconColor]
                        )}
                    >
                        {icon}
                    </div>

                    {/* Content */}
                    <div className="flex-1 min-w-0">
                        <p className="text-sm text-text-secondary mb-1">{title}</p>

                        <Data
                            value={value}
                            size={valueSizeClasses[size]}
                            variant={isPositive ? 'profit' : isNegative ? 'loss' : 'default'}
                            glow={isPositive || isNegative}
                            className="font-bold"
                        />

                        {/* Change indicator */}
                        {change !== undefined && (
                            <div className="flex items-center gap-1.5 mt-2">
                                {isPositive && <TrendingUp className="w-3.5 h-3.5 text-profit" />}
                                {isNegative && <TrendingDown className="w-3.5 h-3.5 text-loss" />}
                                {!isPositive && !isNegative && <Minus className="w-3.5 h-3.5 text-text-tertiary" />}

                                <Data
                                    value={Math.abs(change)}
                                    suffix={changeType === 'percentage' ? '%' : ''}
                                    size="sm"
                                    variant={isPositive ? 'profit' : isNegative ? 'loss' : 'muted'}
                                />
                            </div>
                        )}
                    </div>
                </div>
            </div>
        )
    }
)

StatCard.displayName = 'StatCard'

export { StatCard }
