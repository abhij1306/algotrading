'use client'

import React from 'react'
import { cn } from '@/lib/utils'

interface InputProps extends React.InputHTMLAttributes<HTMLInputElement> {
  label?: string
  error?: string
}

export const Input = React.forwardRef<HTMLInputElement, InputProps>((props, ref) => {
  const { className, label, error, id: providedId, ...rest } = props
  const generatedId = React.useId()
  const id = providedId || generatedId

  return (
    <div className="space-y-1">
      {label && (
        <label htmlFor={id} className="block text-xs font-medium text-[var(--text-secondary)] uppercase tracking-wider">
          {label}
        </label>
      )}
      <input
        ref={ref}
        id={id}
        className={cn(
          'w-full bg-[var(--color-surface)] border border-[var(--border-default)] rounded-lg px-3 py-2 text-sm text-[var(--text-primary)] transition-all',
          'focus:outline-none focus:border-[var(--color-primary)] focus:shadow-[0_0_0_2px_var(--color-primary-bg)]',
          'placeholder:text-[var(--text-muted)]',
          error && 'border-[var(--color-loss)] focus:border-[var(--color-loss)]',
          className
        )}
        {...rest}
      />
      {error && <p className="text-xs text-[var(--color-loss)]">{error}</p>}
    </div>
  )
})

Input.displayName = 'Input'
