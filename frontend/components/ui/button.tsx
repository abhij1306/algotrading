import * as React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

/**
 * Button Component
 * 
 * A comprehensive button component with trading-specific variants.
 * Supports profit/loss states, loading states, and multiple sizes.
 */
const buttonVariants = cva(
  // Base styles
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-lg text-sm font-medium transition-all duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-primary)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--color-base)] disabled:pointer-events-none disabled:opacity-50 [&_svg]:pointer-events-none [&_svg]:size-4 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        // Primary Actions
        default:
          "bg-[var(--color-primary)] text-white hover:bg-[var(--color-primary-bright)] active:bg-[var(--color-primary-muted)]",
        
        // Buy (Green - Trading)
        buy:
          "bg-[var(--color-profit)] text-black font-semibold hover:bg-[var(--color-profit-bright)] active:bg-[var(--color-profit-muted)]",
        
        // Sell (Red - Trading)
        sell:
          "bg-[var(--color-loss)] text-white hover:bg-[var(--color-loss-bright)] active:bg-[var(--color-loss-muted)]",
        
        // Secondary Actions
        secondary:
          "bg-[var(--color-elevated)] text-[var(--text-primary)] border border-[var(--border-default)] hover:bg-[var(--color-overlay)] hover:border-[var(--border-strong)]",
        
        // Ghost (Minimal)
        ghost:
          "text-[var(--text-secondary)] hover:text-[var(--text-primary)] hover:bg-[var(--glass-highlight)]",
        
        // Outline
        outline:
          "border border-[var(--border-default)] text-[var(--text-primary)] hover:bg-[var(--glass-highlight)] hover:border-[var(--border-strong)]",
        
        // Destructive
        destructive:
          "bg-[var(--color-loss)] text-white hover:bg-[var(--color-loss-bright)] active:bg-[var(--color-loss-muted)]",
        
        // Profit (Success)
        profit:
          "bg-[var(--color-profit)] text-black font-semibold hover:bg-[var(--color-profit-bright)] active:bg-[var(--color-profit-muted)]",
        
        // Loss (Danger)
        loss:
          "bg-[var(--color-loss)] text-white hover:bg-[var(--color-loss-bright)] active:bg-[var(--color-loss-muted)]",
        
        // Glass
        glass:
          "bg-[var(--glass-bg)] backdrop-blur-xl border border-[var(--glass-border)] text-[var(--text-primary)] hover:bg-[rgba(24,24,27,0.8)] hover:border-[var(--glass-border-strong)]",
        
        // Link
        link:
          "text-[var(--color-primary)] underline-offset-4 hover:underline",
      },
      size: {
        xs: "h-6 px-2 text-xs rounded-md",
        sm: "h-8 px-3 text-sm",
        default: "h-10 px-4",
        lg: "h-12 px-6 text-base",
        xl: "h-14 px-8 text-lg",
        icon: "h-10 w-10",
        "icon-sm": "h-8 w-8",
        "icon-lg": "h-12 w-12",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  }
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean;
  loading?: boolean;
}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, loading = false, children, disabled, ...props }, ref) => {
    const Comp = asChild ? Slot : "button";
    
    return (
      <Comp
        className={cn(buttonVariants({ variant, size }), className)}
        ref={ref}
        disabled={disabled || loading}
        {...props}
      >
        {loading ? (
          <>
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
            <span>Loading...</span>
          </>
        ) : (
          children
        )}
      </Comp>
    );
  }
);
Button.displayName = "Button";

export { Button, buttonVariants };
