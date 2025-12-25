'use client'

import * as React from 'react'
import { cva, type VariantProps } from 'class-variance-authority'
import { cn } from '@/lib/utils'

const inputVariants = cva(
    'w-full rounded-lg border backdrop-blur-glass font-ui text-sm transition-all duration-200 focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-50',
    {
        variants: {
            variant: {
                // Frosted glass default
                default:
                    'bg-obsidian-surface/60 border-glass-border text-text-primary placeholder:text-text-tertiary focus:border-electric-blue focus:shadow-glow-sm focus:bg-obsidian-surface/80',

                // Valid state with green glow
                valid:
                    'bg-obsidian-surface/60 border-profit/50 text-text-primary focus:border-profit focus:shadow-glow-profit focus:bg-obsidian-surface/80',

                // Error state with red glow
                error:
                    'bg-obsidian-surface/60 border-loss/50 text-text-primary focus:border-loss focus:shadow-glow-loss focus:bg-obsidian-surface/80',

                // Neumorphic variant
                neomorph:
                    'bg-gradient-to-br from-obsidian-surface to-obsidian-elevated border-glass-border text-text-primary shadow-neomorph-inset focus:shadow-neomorph focus:border-electric-blue',
            },
            inputSize: {
                sm: 'h-8 px-3 py-1 text-xs',
                md: 'h-10 px-4 py-2',
                lg: 'h-12 px-5 py-3 text-base',
            },
        },
        defaultVariants: {
            variant: 'default',
            inputSize: 'md',
        },
    }
)

export interface InputProps
    extends Omit<React.InputHTMLAttributes<HTMLInputElement>, 'size'>,
    VariantProps<typeof inputVariants> {
    label?: string
    error?: string
    icon?: React.ReactNode
    iconPosition?: 'left' | 'right'
    floatingLabel?: boolean
}

const Input = React.forwardRef<HTMLInputElement, InputProps>(
    (
        {
            className,
            variant,
            inputSize,
            type = 'text',
            label,
            error,
            icon,
            iconPosition = 'left',
            floatingLabel = false,
            placeholder,
            ...props
        },
        ref
    ) => {
        const [isFocused, setIsFocused] = React.useState(false)
        const [hasValue, setHasValue] = React.useState(false)

        const handleFocus = () => setIsFocused(true)
        const handleBlur = (e: React.FocusEvent<HTMLInputElement>) => {
            setIsFocused(false)
            setHasValue(e.target.value !== '')
        }

        const effectiveVariant = error ? 'error' : variant

        return (
            <div className="w-full">
                <div className="relative">
                    {/* Floating label */}
                    {floatingLabel && label && (
                        <label
                            className={cn(
                                'absolute left-4 text-text-tertiary transition-all duration-200 pointer-events-none',
                                isFocused || hasValue
                                    ? 'top-1 text-xs text-electric-blue'
                                    : 'top-1/2 -translate-y-1/2 text-sm'
                            )}
                        >
                            {label}
                        </label>
                    )}

                    {/* Static label */}
                    {!floatingLabel && label && (
                        <label className="block text-sm font-medium text-text-secondary mb-2">
                            {label}
                        </label>
                    )}

                    {/* Icon left */}
                    {icon && iconPosition === 'left' && (
                        <span className="absolute left-3 top-1/2 -translate-y-1/2 text-text-tertiary">
                            {icon}
                        </span>
                    )}

                    <input
                        type={type}
                        className={cn(
                            inputVariants({ variant: effectiveVariant, inputSize }),
                            icon && iconPosition === 'left' && 'pl-10',
                            icon && iconPosition === 'right' && 'pr-10',
                            floatingLabel && (isFocused || hasValue) && 'pt-5 pb-1',
                            className
                        )}
                        ref={ref}
                        onFocus={handleFocus}
                        onBlur={handleBlur}
                        placeholder={floatingLabel ? '' : placeholder}
                        {...props}
                    />

                    {/* Icon right */}
                    {icon && iconPosition === 'right' && (
                        <span className="absolute right-3 top-1/2 -translate-y-1/2 text-text-tertiary">
                            {icon}
                        </span>
                    )}

                    {/* Focus glow overlay */}
                    {isFocused && (
                        <div
                            className={cn(
                                'absolute inset-0 rounded-lg pointer-events-none transition-opacity duration-200',
                                error
                                    ? 'bg-loss/5'
                                    : variant === 'valid'
                                        ? 'bg-profit/5'
                                        : 'bg-electric-blue/5'
                            )}
                        />
                    )}
                </div>

                {/* Error message */}
                {error && (
                    <p className="mt-1.5 text-xs text-loss flex items-center gap-1">
                        <svg
                            className="w-3.5 h-3.5"
                            fill="currentColor"
                            viewBox="0 0 20 20"
                        >
                            <path
                                fillRule="evenodd"
                                d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-7 4a1 1 0 11-2 0 1 1 0 012 0zm-1-9a1 1 0 00-1 1v4a1 1 0 102 0V6a1 1 0 00-1-1z"
                                clipRule="evenodd"
                            />
                        </svg>
                        {error}
                    </p>
                )}
            </div>
        )
    }
)

Input.displayName = 'Input'

export { Input, inputVariants }
