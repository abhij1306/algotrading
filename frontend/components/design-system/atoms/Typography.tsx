'use client'

import * as React from 'react'
import { cva, type VariantProps } from 'class-variance-authority'
import { cn } from '@/lib/utils'

// Heading variants
const headingVariants = cva('font-ui font-semibold tracking-tight', {
    variants: {
        level: {
            h1: 'text-4xl lg:text-5xl',
            h2: 'text-3xl lg:text-4xl',
            h3: 'text-2xl lg:text-3xl',
            h4: 'text-xl lg:text-2xl',
            h5: 'text-lg lg:text-xl',
            h6: 'text-base lg:text-lg',
        },
        gradient: {
            true: 'bg-gradient-to-r from-electric-blue to-electric-purple bg-clip-text text-transparent',
            false: 'text-text-primary',
        },
    },
    defaultVariants: {
        level: 'h2',
        gradient: false,
    },
})

export interface HeadingProps
    extends React.HTMLAttributes<HTMLHeadingElement>,
    VariantProps<typeof headingVariants> {
    as?: 'h1' | 'h2' | 'h3' | 'h4' | 'h5' | 'h6'
}

const Heading = React.forwardRef<HTMLHeadingElement, HeadingProps>(
    ({ className, level, gradient, as, children, ...props }, ref) => {
        const Component = as || level || 'h2'
        return (
            <Component
                className={cn(headingVariants({ level: level || as, gradient }), className)}
                ref={ref}
                {...props}
            >
                {children}
            </Component>
        )
    }
)
Heading.displayName = 'Heading'

// Text variants
const textVariants = cva('font-ui', {
    variants: {
        variant: {
            body: 'text-base text-text-primary',
            small: 'text-sm text-text-secondary',
            tiny: 'text-xs text-text-tertiary',
            lead: 'text-lg text-text-primary',
            muted: 'text-sm text-text-tertiary',
        },
        weight: {
            normal: 'font-normal',
            medium: 'font-medium',
            semibold: 'font-semibold',
            bold: 'font-bold',
        },
    },
    defaultVariants: {
        variant: 'body',
        weight: 'normal',
    },
})

export interface TextProps
    extends React.HTMLAttributes<HTMLParagraphElement>,
    VariantProps<typeof textVariants> {
    as?: 'p' | 'span' | 'div'
}

const Text = React.forwardRef<HTMLParagraphElement, TextProps>(
    ({ className, variant, weight, as = 'p', children, ...props }, ref) => {
        const Component = as
        return (
            <Component
                className={cn(textVariants({ variant, weight }), className)}
                ref={ref as any}
                {...props}
            >
                {children}
            </Component>
        )
    }
)
Text.displayName = 'Text'

// Label variants
const labelVariants = cva(
    'font-ui font-medium leading-none peer-disabled:cursor-not-allowed peer-disabled:opacity-50',
    {
        variants: {
            size: {
                sm: 'text-xs',
                md: 'text-sm',
                lg: 'text-base',
            },
            variant: {
                default: 'text-text-secondary',
                primary: 'text-text-primary',
                muted: 'text-text-tertiary',
            },
        },
        defaultVariants: {
            size: 'md',
            variant: 'default',
        },
    }
)

export interface LabelProps
    extends React.LabelHTMLAttributes<HTMLLabelElement>,
    VariantProps<typeof labelVariants> {
    required?: boolean
}

const Label = React.forwardRef<HTMLLabelElement, LabelProps>(
    ({ className, size, variant, required, children, ...props }, ref) => {
        return (
            <label
                className={cn(labelVariants({ size, variant }), className)}
                ref={ref}
                {...props}
            >
                {children}
                {required && <span className="text-loss ml-1">*</span>}
            </label>
        )
    }
)
Label.displayName = 'Label'

// Data display for financial figures
const dataVariants = cva('font-data tabular-nums', {
    variants: {
        size: {
            sm: 'text-sm',
            md: 'text-base',
            lg: 'text-lg',
            xl: 'text-xl',
            '2xl': 'text-2xl',
        },
        variant: {
            default: 'text-text-primary',
            profit: 'text-profit',
            loss: 'text-loss',
            muted: 'text-text-secondary',
        },
        glow: {
            true: '',
            false: '',
        },
    },
    compoundVariants: [
        {
            variant: 'profit',
            glow: true,
            className: 'glow-profit',
        },
        {
            variant: 'loss',
            glow: true,
            className: 'glow-loss',
        },
    ],
    defaultVariants: {
        size: 'md',
        variant: 'default',
        glow: false,
    },
})

export interface DataProps
    extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof dataVariants> {
    value: string | number
    prefix?: string
    suffix?: string
}

const Data = React.forwardRef<HTMLSpanElement, DataProps>(
    ({ className, size, variant, glow, value, prefix, suffix, ...props }, ref) => {
        return (
            <span
                className={cn(dataVariants({ size, variant, glow }), className)}
                ref={ref}
                {...props}
            >
                {prefix && <span className="opacity-70">{prefix}</span>}
                {value}
                {suffix && <span className="opacity-70">{suffix}</span>}
            </span>
        )
    }
)
Data.displayName = 'Data'

export { Heading, Text, Label, Data, headingVariants, textVariants, labelVariants, dataVariants }
