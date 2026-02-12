'use client'

import React from 'react'
import { cn } from '@/lib/utils'

interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: 'primary' | 'secondary' | 'ghost' | 'profit' | 'loss'
  size?: 'sm' | 'md' | 'lg'
}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(({
  className,
  variant = 'secondary',
  size = 'md',
  children,
  disabled,
  ...props
}, ref) => {
  const variants = {
    primary: 'bg-[var(--color-primary)] text-white border-transparent hover:bg-[#2563EB]',
    secondary: 'bg-[var(--color-surface)] text-[var(--text-primary)] border-[var(--border-default)] hover:bg-[var(--color-elevated)] hover:border-[rgba(255,255,255,0.1)]',
    ghost: 'bg-transparent text-[var(--text-secondary)] border-transparent hover:bg-[var(--glass-highlight)] hover:text-[var(--text-primary)]',
    profit: 'bg-[var(--color-profit-bg)] text-[var(--color-profit)] border-transparent',
    loss: 'bg-[var(--color-loss-bg)] text-[var(--color-loss)] border-transparent',
  }

  const sizes = {
    sm: 'px-3 py-1.5 text-xs',
    md: 'px-4 py-2 text-sm',
    lg: 'px-6 py-3 text-base',
  }

  return (
    <button
      ref={ref}
      disabled={disabled}
      className={cn(
        'inline-flex items-center justify-center gap-2 rounded-lg font-medium transition-all cursor-pointer border',
        variants[variant],
        sizes[size],
        disabled && 'opacity-50 cursor-not-allowed',
        className
      )}
      {...props}
    >
      {children}
    </button>
  )
})

Button.displayName = 'Button'
