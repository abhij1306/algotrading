'use client'

import * as React from 'react'
import { cva, type VariantProps } from 'class-variance-authority'
import { cn } from '@/lib/utils'

const iconButtonVariants = cva(
    'inline-flex items-center justify-center rounded-full transition-all duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-50 relative overflow-hidden group',
    {
        variants: {
            variant: {
                // Neumorphic default
                default:
                    'bg-obsidian-surface border border-glass-border text-text-primary shadow-neomorph-sm hover:shadow-neomorph hover:text-white',

                // Primary with glow
                primary:
                    'bg-electric-blue/10 border border-electric-blue/30 text-electric-blue hover:bg-electric-blue/20 hover:shadow-glow-sm',

                // Ghost transparent
                ghost:
                    'bg-transparent text-text-secondary hover:bg-white/5 hover:text-text-primary',

                // Danger
                danger:
                    'bg-loss/10 border border-loss/30 text-loss hover:bg-loss/20 hover:shadow-glow-loss',

                // Profit
                profit:
                    'bg-profit/10 border border-profit/30 text-profit hover:bg-profit/20 hover:shadow-glow-profit',
            },
            size: {
                sm: 'h-8 w-8 text-sm',
                md: 'h-10 w-10',
                lg: 'h-12 w-12 text-lg',
            },
        },
        defaultVariants: {
            variant: 'default',
            size: 'md',
        },
    }
)

export interface IconButtonProps
    extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof iconButtonVariants> {
    tooltip?: string
    icon: React.ReactNode
}

const IconButton = React.forwardRef<HTMLButtonElement, IconButtonProps>(
    ({ className, variant, size, tooltip, icon, onClick, ...props }, ref) => {
        const [ripples, setRipples] = React.useState<
            Array<{ x: number; y: number; id: number }>
        >([])
        const [showTooltip, setShowTooltip] = React.useState(false)

        const handleClick = (e: React.MouseEvent<HTMLButtonElement>) => {
            const button = e.currentTarget
            const rect = button.getBoundingClientRect()
            const x = e.clientX - rect.left
            const y = e.clientY - rect.top

            const newRipple = { x, y, id: Date.now() }
            setRipples((prev) => [...prev, newRipple])

            setTimeout(() => {
                setRipples((prev) => prev.filter((r) => r.id !== newRipple.id))
            }, 600)

            onClick?.(e)
        }

        return (
            <div className="relative inline-flex">
                <button
                    className={cn(iconButtonVariants({ variant, size }), className)}
                    ref={ref}
                    onClick={handleClick}
                    onMouseEnter={() => setShowTooltip(true)}
                    onMouseLeave={() => setShowTooltip(false)}
                    {...props}
                >
                    {/* Ripple effects */}
                    {ripples.map((ripple) => (
                        <span
                            key={ripple.id}
                            className="absolute rounded-full bg-white/30 pointer-events-none animate-ripple"
                            style={{
                                left: ripple.x,
                                top: ripple.y,
                                width: 20,
                                height: 20,
                                transform: 'translate(-50%, -50%)',
                            }}
                        />
                    ))}

                    {icon}

                    {/* Subtle rotate on hover */}
                    <span className="absolute inset-0 rounded-full bg-gradient-to-r from-transparent via-white/5 to-transparent translate-x-[-200%] group-hover:translate-x-[200%] transition-transform duration-500" />
                </button>

                {/* Tooltip */}
                {tooltip && showTooltip && (
                    <div className="absolute bottom-full left-1/2 -translate-x-1/2 mb-2 px-2 py-1 bg-obsidian-elevated border border-glass-border rounded text-xs text-text-primary whitespace-nowrap pointer-events-none z-50 animate-fade-in shadow-neomorph">
                        {tooltip}
                        <div className="absolute top-full left-1/2 -translate-x-1/2 -mt-1 border-4 border-transparent border-t-obsidian-elevated" />
                    </div>
                )}
            </div>
        )
    }
)

IconButton.displayName = 'IconButton'

export { IconButton, iconButtonVariants }
