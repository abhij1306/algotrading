'use client'

import * as React from 'react'
import { cva, type VariantProps } from 'class-variance-authority'
import { cn } from '@/lib/utils'

const buttonVariants = cva(
    'inline-flex items-center justify-center whitespace-nowrap rounded-lg font-medium transition-all duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-50 relative overflow-hidden group',
    {
        variants: {
            variant: {
                // Neumorphic primary button with glow (OCSWAP Style: Cyan -> Blue)
                primary:
                    'bg-gradient-to-r from-cyan-500 to-blue-600 text-white shadow-glow-cyan hover:shadow-glow-blue border border-cyan-400/20 active:scale-[0.98]',

                // Secondary with subtle neumorphism
                secondary:
                    'bg-obsidian-surface border border-white/10 text-text-primary shadow-neomorph-sm hover:border-cyan-500/50 hover:shadow-glow-sm hover:bg-white/5',

                // Ghost with glassmorphism
                ghost:
                    'bg-transparent hover:bg-cyan-500/10 text-text-secondary hover:text-cyan-400',

                // Danger with red glow
                danger:
                    'bg-gradient-to-br from-loss to-red-700 text-white shadow-neomorph hover:shadow-glow-loss active:shadow-neomorph-inset',

                // Profit variant with green glow
                profit:
                    'bg-gradient-to-br from-profit to-green-600 text-white shadow-neomorph hover:shadow-glow-profit active:shadow-neomorph-inset',

                // Outline with glow on hover
                outline:
                    'border border-white/10 bg-transparent text-text-primary hover:bg-cyan-500/10 hover:border-cyan-500 hover:shadow-glow-cyan',
            },
            size: {
                sm: 'h-8 px-3 text-xs rounded-lg', // Increased radius
                md: 'h-10 px-5 text-sm rounded-xl', // OCSWAP style rounded
                lg: 'h-12 px-6 text-base rounded-xl',
                icon: 'h-10 w-10 rounded-xl',
            },
            glow: {
                true: 'animate-glow-pulse',
                false: '',
            },
        },
        defaultVariants: {
            variant: 'primary',
            size: 'md',
            glow: false,
        },
    }
)

export interface ButtonProps
    extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
    asChild?: boolean
    loading?: boolean
    icon?: React.ReactNode
    iconPosition?: 'left' | 'right'
}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
    (
        {
            className,
            variant,
            size,
            glow,
            loading = false,
            icon,
            iconPosition = 'left',
            children,
            onClick,
            ...props
        },
        ref
    ) => {
        const [ripples, setRipples] = React.useState<
            Array<{ x: number; y: number; id: number }>
        >([])

        const handleClick = (e: React.MouseEvent<HTMLButtonElement>) => {
            const button = e.currentTarget
            const rect = button.getBoundingClientRect()
            const x = e.clientX - rect.left
            const y = e.clientY - rect.top

            const newRipple = { x, y, id: Date.now() }
            setRipples((prev) => [...prev, newRipple])

            // Remove ripple after animation
            setTimeout(() => {
                setRipples((prev) => prev.filter((r) => r.id !== newRipple.id))
            }, 600)

            onClick?.(e)
        }

        return (
            <button
                className={cn(buttonVariants({ variant, size, glow }), className)}
                ref={ref}
                onClick={handleClick}
                disabled={loading || props.disabled}
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

                {/* Loading spinner */}
                {loading && (
                    <span className="mr-2">
                        <svg
                            className="animate-spin h-4 w-4"
                            xmlns="http://www.w3.org/2000/svg"
                            fill="none"
                            viewBox="0 0 24 24"
                        >
                            <circle
                                className="opacity-25"
                                cx="12"
                                cy="12"
                                r="10"
                                stroke="currentColor"
                                strokeWidth="4"
                            />
                            <path
                                className="opacity-75"
                                fill="currentColor"
                                d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"
                            />
                        </svg>
                    </span>
                )}

                {/* Icon left */}
                {icon && iconPosition === 'left' && !loading && (
                    <span className="mr-2">{icon}</span>
                )}

                {children}

                {/* Icon right */}
                {icon && iconPosition === 'right' && !loading && (
                    <span className="ml-2">{icon}</span>
                )}

                {/* Subtle shine effect on hover */}
                <span className="absolute inset-0 rounded-lg bg-gradient-to-r from-transparent via-white/10 to-transparent translate-x-[-200%] group-hover:translate-x-[200%] transition-transform duration-700" />
            </button>
        )
    }
)

Button.displayName = 'Button'

export { Button, buttonVariants }
