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
    primary: 'btn-primary',
    secondary: 'btn-secondary',
    ghost: 'btn-ghost',
    profit: 'bg-[var(--color-profit-bg)] text-[var(--color-profit)] border-transparent hover:bg-[var(--color-profit)] hover:text-white',
    loss: 'bg-[var(--color-loss-bg)] text-[var(--color-loss)] border-transparent hover:bg-[var(--color-loss)] hover:text-white',
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
        'btn',
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
