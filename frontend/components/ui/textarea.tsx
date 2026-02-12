import * as React from "react";
import { cn } from "@/lib/utils";

/**
 * Textarea Component
 * 
 * Multi-line text input with consistent styling.
 */

export interface TextareaProps
  extends React.TextareaHTMLAttributes<HTMLTextAreaElement> {
  variant?: "default" | "glass";
  error?: boolean;
}

const Textarea = React.forwardRef<HTMLTextAreaElement, TextareaProps>(
  ({ className, variant = "default", error = false, ...props }, ref) => {
    const variants = {
      default: "bg-[var(--color-surface)] border-[var(--border-default)]",
      glass: "bg-[var(--glass-bg)] backdrop-blur-xl border-[var(--glass-border)]",
    };

    return (
      <textarea
        className={cn(
          "flex min-h-[80px] w-full rounded-lg border px-4 py-3 text-sm text-[var(--text-primary)] transition-all duration-150",
          "placeholder:text-[var(--text-muted)]",
          "focus:outline-none focus:ring-2 focus:ring-[var(--color-primary)] focus:border-[var(--color-primary)]",
          "disabled:cursor-not-allowed disabled:opacity-50 disabled:bg-[var(--color-elevated)]",
          variants[variant],
          error && "border-[var(--color-loss)] focus:ring-[var(--color-loss)] focus:border-[var(--color-loss)]",
          className
        )}
        ref={ref}
        {...props}
      />
    );
  }
);
Textarea.displayName = "Textarea";

export { Textarea };
