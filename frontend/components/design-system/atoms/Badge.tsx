'use client'

import * as React from 'react'
import { cva, type VariantProps } from 'class-variance-authority'
import { cn } from '@/lib/utils'

const badgeVariants = cva(
    'inline-flex items-center justify-center rounded font-medium transition-all duration-200',
    {
        variants: {
            variant: {
                // Profit with green glow
                profit:
                    'bg-profit/10 text-profit border border-profit/30 shadow-glow-profit',

                // Loss with red glow
                loss:
                    'bg-loss/10 text-loss border border-loss/30 shadow-glow-loss',

                // Primary with blue glow
                primary:
                    'bg-electric-blue/10 text-electric-blue border border-electric-blue/30 shadow-glow-sm',

                // Secondary neutral
                secondary:
                    'bg-white/5 text-text-secondary border border-glass-border',

                // Purple accent
                purple:
                    'bg-electric-purple/10 text-electric-purple border border-electric-purple/30 shadow-glow-purple',

                // Cyan accent
                cyan:
                    'bg-electric-cyan/10 text-electric-cyan border border-electric-cyan/30 shadow-glow-cyan',

                // Amber warning
                amber:
                    'bg-electric-amber/10 text-electric-amber border border-electric-amber/30 shadow-glow-amber',

                // Outline only
                outline:
                    'bg-transparent border border-glass-border text-text-secondary hover:bg-white/5',
            },
            size: {
                sm: 'h-5 px-2 text-[10px]',
                md: 'h-6 px-2.5 text-xs',
                lg: 'h-7 px-3 text-sm',
            },
            shape: {
                pill: 'rounded-full',
                square: 'rounded-md',
            },
            pulse: {
                true: 'animate-pulse-subtle',
                false: '',
            },
        },
        defaultVariants: {
            variant: 'secondary',
            size: 'md',
            shape: 'pill',
            pulse: false,
        },
    }
)

export interface BadgeProps
    extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {
    dot?: boolean
    icon?: React.ReactNode
}

const Badge = React.forwardRef<HTMLSpanElement, BadgeProps>(
    (
        {
            className,
            variant,
            size,
            shape,
            pulse,
            dot = false,
            icon,
            children,
            ...props
        },
        ref
    ) => {
        return (
            <span
                className={cn(badgeVariants({ variant, size, shape, pulse }), className)}
                ref={ref}
                {...props}
            >
                {/* Status dot */}
                {dot && (
                    <span
                        className={cn(
                            'w-1.5 h-1.5 rounded-full mr-1.5',
                            variant === 'profit' && 'bg-profit',
                            variant === 'loss' && 'bg-loss',
                            variant === 'primary' && 'bg-electric-blue',
                            variant === 'purple' && 'bg-electric-purple',
                            variant === 'cyan' && 'bg-electric-cyan',
                            variant === 'amber' && 'bg-electric-amber',
                            pulse && 'animate-pulse'
                        )}
                    />
                )}

                {/* Icon */}
                {icon && <span className="mr-1">{icon}</span>}

                {children}
            </span>
        )
    }
)

Badge.displayName = 'Badge'

export { Badge, badgeVariants }
