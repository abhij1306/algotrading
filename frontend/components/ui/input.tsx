import * as React from "react";
import { cn } from "@/lib/utils";

/**
 * Input Component
 * 
 * Form input with consistent styling for trading applications.
 * Supports different variants and sizes.
 */

export interface InputProps
  extends React.InputHTMLAttributes<HTMLInputElement> {
  variant?: "default" | "glass" | "ghost";
  inputSize?: "sm" | "default" | "lg";
  error?: boolean;
  icon?: React.ReactNode;
  iconPosition?: "left" | "right";
}

const Input = React.forwardRef<HTMLInputElement, InputProps>(
  ({ 
    className, 
    type, 
    variant = "default", 
    inputSize = "default",
    error = false,
    icon,
    iconPosition = "left",
    ...props 
  }, ref) => {
    const variants = {
      default: "bg-[var(--color-surface)] border-[var(--border-default)]",
      glass: "bg-[var(--glass-bg)] backdrop-blur-xl border-[var(--glass-border)]",
      ghost: "bg-transparent border-transparent hover:border-[var(--border-default)]",
    };

    const sizes = {
      sm: "h-8 px-3 text-sm",
      default: "h-10 px-4 text-sm",
      lg: "h-12 px-4 text-base",
    };

    const iconPadding = icon 
      ? iconPosition === "left" 
        ? "pl-10" 
        : "pr-10" 
      : "";

    return (
      <div className="relative w-full">
        {icon && (
          <div 
            className={cn(
              "absolute inset-y-0 flex items-center pointer-events-none text-[var(--text-muted)]",
              iconPosition === "left" ? "left-3" : "right-3"
            )}
          >
            {icon}
          </div>
        )}
        <input
          type={type}
          className={cn(
            // Base styles
            "flex w-full rounded-lg border text-[var(--text-primary)] transition-all duration-150",
            "placeholder:text-[var(--text-muted)]",
            "focus:outline-none focus:ring-2 focus:ring-[var(--color-primary)] focus:ring-offset-0 focus:border-[var(--color-primary)]",
            "disabled:cursor-not-allowed disabled:opacity-50 disabled:bg-[var(--color-elevated)]",
            // Variant styles
            variants[variant],
            // Size styles
            sizes[inputSize],
            // Icon padding
            iconPadding,
            // Error state
            error && "border-[var(--color-loss)] focus:ring-[var(--color-loss)] focus:border-[var(--color-loss)]",
            className
          )}
          ref={ref}
          {...props}
        />
      </div>
    );
  }
);
Input.displayName = "Input";

// Number Input with formatting
export interface NumberInputProps extends Omit<InputProps, 'type' | 'onChange' | 'value'> {
  value?: number;
  onChange?: (value: number) => void;
  min?: number;
  max?: number;
  step?: number;
  format?: "currency" | "percent" | "decimal";
  precision?: number;
}

const NumberInput = React.forwardRef<HTMLInputElement, NumberInputProps>(
  ({ 
    value, 
    onChange, 
    min, 
    max, 
    step = 1,
    format,
    precision = 2,
    className,
    ...props 
  }, ref) => {
    const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
      const newValue = parseFloat(e.target.value);
      if (!isNaN(newValue) && onChange) {
        onChange(newValue);
      }
    };

    const displayValue = value !== undefined 
      ? format === "currency"
        ? `$${value.toFixed(precision)}`
        : format === "percent"
          ? `${value.toFixed(precision)}%`
          : value.toString()
      : "";

    return (
      <Input
        ref={ref}
        type="number"
        value={displayValue}
        onChange={handleChange}
        min={min}
        max={max}
        step={step}
        className={cn("font-mono tabular-nums", className)}
        {...props}
      />
    );
  }
);
NumberInput.displayName = "NumberInput";

// Search Input
export interface SearchInputProps extends Omit<InputProps, 'type' | 'icon'> {
  onClear?: () => void;
}

const SearchInput = React.forwardRef<HTMLInputElement, SearchInputProps>(
  ({ className, onClear, ...props }, ref) => {
    return (
      <Input
        ref={ref}
        type="search"
        icon={
          <svg 
            className="w-4 h-4" 
            fill="none" 
            stroke="currentColor" 
            viewBox="0 0 24 24"
          >
            <path 
              strokeLinecap="round" 
              strokeLinejoin="round" 
              strokeWidth={2} 
              d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" 
            />
          </svg>
        }
        iconPosition="left"
        className={className}
        {...props}
      />
    );
  }
);
SearchInput.displayName = "SearchInput";

export { Input, NumberInput, SearchInput };
