import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";
import { formatPercent } from "@/lib/utils";

/**
 * Badge Component
 *
 * Status indicators and labels for trading applications.
 * Includes financial-specific variants for profit/loss/neutral states.
 */
const badgeVariants = cva(
  "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium transition-colors border",
  {
    variants: {
      variant: {
        // Default
        default: "bg-primary/10 text-primary border-transparent",

        // Secondary
        secondary: "bg-background-secondary text-foreground-muted border-border",

        // Outline
        outline: "bg-transparent border-border text-foreground-muted",

        // Profit (Success)
        profit: "bg-profit-bg text-profit border-transparent",

        // Loss (Danger)
        loss: "bg-loss-bg text-loss border-transparent",

        // Warning
        warning: "bg-warning-bg text-warning border-transparent",

        // Neutral
        neutral: "bg-background-tertiary text-foreground-secondary border-transparent",

        // Live/Pulse
        live: "bg-profit-bg text-profit border-profit/20",

        // Muted
        muted: "bg-background-secondary text-foreground-muted border-transparent",
      },
      size: {
        xs: "h-4 px-1.5 text-xxs",
        sm: "h-5 px-2 text-xs",
        default: "h-5 px-2 text-xs",
        lg: "h-6 px-2.5 text-sm",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  }
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLDivElement>, VariantProps<typeof badgeVariants> {
  pulse?: boolean;
  icon?: React.ReactNode;
}

function Badge({ className, variant, size, pulse = false, icon, children, ...props }: Readonly<BadgeProps>) {
  return (
    <div className={cn(badgeVariants({ variant, size }), className)} {...props}>
      {pulse && (
        <span className="relative flex h-1.5 w-1.5 mr-1">
          <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-current opacity-75" />
          <span className="relative inline-flex rounded-full h-1.5 w-1.5 bg-current" />
        </span>
      )}
      {icon}
      {children}
    </div>
  );
}

/**
 * Specialized Status Badge
 */
export function StatusBadge({ status, className }: Readonly<{ status: string; className?: string }>) {
  const normalizedStatus = status.toLowerCase();

  const getVariant = (): BadgeProps["variant"] => {
    if (["active", "open", "live", "running", "success"].includes(normalizedStatus))
      return "profit";
    if (["inactive", "closed", "stopped", "failed", "error"].includes(normalizedStatus))
      return "loss";
    if (["pending", "warning", "maintenance"].includes(normalizedStatus)) return "warning";
    return "neutral";
  };

  return (
    <Badge variant={getVariant()} className={cn("capitalize", className)}>
      {status}
    </Badge>
  );
}

/**
 * Price Change Badge
 */
export function ChangeBadge({ value, className }: Readonly<{ value: number; className?: string }>) {
  return (
    <Badge variant={value >= 0 ? "profit" : "loss"} className={className}>
      {formatPercent(value)}
    </Badge>
  );
}

export { Badge, badgeVariants };
