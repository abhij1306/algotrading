'use client'

import * as React from 'react'
import { Input, Label } from '../atoms'
import type { InputProps } from '../atoms'
import { cn } from '@/lib/utils'

export interface FormFieldProps extends Omit<InputProps, 'label' | 'error'> {
    label?: string
    error?: string
    helperText?: string
    required?: boolean
    containerClassName?: string
}

const FormField = React.forwardRef<HTMLInputElement, FormFieldProps>(
    (
        {
            label,
            error,
            helperText,
            required,
            containerClassName,
            className,
            ...inputProps
        },
        ref
    ) => {
        return (
            <div className={cn('space-y-2', containerClassName)}>
                {label && (
                    <Label required={required} size="md" variant="primary">
                        {label}
                    </Label>
                )}

                <Input
                    ref={ref}
                    className={className}
                    error={error}
                    {...inputProps}
                />

                {helperText && !error && (
                    <p className="text-xs text-text-tertiary flex items-center gap-1">
                        <svg
                            className="w-3.5 h-3.5"
                            fill="currentColor"
                            viewBox="0 0 20 20"
                        >
                            <path
                                fillRule="evenodd"
                                d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-7-4a1 1 0 11-2 0 1 1 0 012 0zM9 9a1 1 0 000 2v3a1 1 0 001 1h1a1 1 0 100-2v-3a1 1 0 00-1-1H9z"
                                clipRule="evenodd"
                            />
                        </svg>
                        {helperText}
                    </p>
                )}
            </div>
        )
    }
)

FormField.displayName = 'FormField'

export { FormField }
