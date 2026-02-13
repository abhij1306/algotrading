import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

/**
 * Badge Component
 * 
 * Status indicators and labels for trading applications.
 * Includes financial-specific variants for profit/loss/neutral states.
 */
const badgeVariants = cva(
  "inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-xs font-medium transition-colors",
  {
    variants: {
      variant: {
        // Default
        default:
          "bg-[var(--color-primary-bg)] text-[var(--color-primary-bright)]",
        
        // Secondary
        secondary:
          "bg-[var(--color-elevated)] text-[var(--text-secondary)] border border-[var(--border-default)]",
        
        // Outline
        outline:
          "border border-[var(--border-default)] text-[var(--text-secondary)]",
        
        // Profit (Success)
        profit:
          "bg-[var(--color-profit-bg)] text-[var(--color-profit)]",
        
        // Loss (Danger)
        loss:
          "bg-[var(--color-loss-bg)] text-[var(--color-loss)]",
        
        // Warning
        warning:
          "bg-[var(--color-warning-bg)] text-[var(--color-warning)]",
        
        // Info
        info:
          "bg-[var(--color-info-bg)] text-[var(--color-info)]",
        
        // Neutral
        neutral:
          "bg-[var(--glass-highlight)] text-[var(--text-secondary)]",
        
        // Live/Pulse
        live:
          "bg-[var(--color-profit-bg)] text-[var(--color-profit)]",
        
        // Muted
        muted:
          "bg-[var(--glass-highlight)] text-[var(--text-muted)]",
      },
      size: {
        sm: "px-1.5 py-0.5 text-[10px]",
        default: "px-2 py-0.5 text-xs",
        lg: "px-2.5 py-1 text-sm",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  }
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLDivElement>,
    VariantProps<typeof badgeVariants> {
  pulse?: boolean;
  icon?: React.ReactNode;
}

function Badge({ 
  className, 
  variant, 
  size, 
  pulse = false, 
  icon,
  children, 
  ...props 
}: BadgeProps) {
  return (
    <div 
      className={cn(badgeVariants({ variant, size }), className)} 
      {...props}
    >
      {pulse && (
        <span className="relative flex h-2 w-2">
          <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-current opacity-75" />
          <span className="relative inline-flex rounded-full h-2 w-2 bg-current" />
        </span>
      )}
      {icon}
      {children}
    </div>
  );
}

// Specialized: Status Badge
interface StatusBadgeProps extends Omit<BadgeProps, 'variant'> {
  status: "online" | "offline" | "loading" | "error" | "success";
}

function StatusBadge({ status, ...props }: StatusBadgeProps) {
  const statusConfig = {
    online: { variant: "profit" as const, pulse: true, label: "Online" },
    offline: { variant: "muted" as const, pulse: false, label: "Offline" },
    loading: { variant: "warning" as const, pulse: true, label: "Loading" },
    error: { variant: "loss" as const, pulse: false, label: "Error" },
    success: { variant: "profit" as const, pulse: false, label: "Success" },
  };

  const config = statusConfig[status];

  return (
    <Badge variant={config.variant} pulse={config.pulse} {...props}>
      {config.label}
    </Badge>
  );
}

// Specialized: Change Badge (for price changes)
interface ChangeBadgeProps extends Omit<BadgeProps, 'variant' | 'children'> {
  value: number;
  showSign?: boolean;
  suffix?: string;
}

function ChangeBadge({ 
  value, 
  showSign = true, 
  suffix,
  className,
  ...props 
}: ChangeBadgeProps) {
  // Handle undefined/null values
  const safeValue = value ?? 0;
  const isPositive = safeValue >= 0;
  const variant = safeValue === 0 ? "neutral" : isPositive ? "profit" : "loss";

  return (
    <Badge variant={variant} className={cn("font-mono tabular-nums", className)} {...props}>
      {showSign && (isPositive ? "+" : "")}
      {safeValue.toFixed(2)}
      {suffix && <span className="text-[var(--text-muted)]">{suffix}</span>}
    </Badge>
  );
}

export { Badge, badgeVariants, StatusBadge, ChangeBadge };
