import * as React from "react";
import { cn } from "@/lib/utils";

/**
 * Card Component System
 * 
 * A flexible card system with glass morphism support.
 * Designed for trading terminal dashboards and data displays.
 */

// Base Card
const Card = React.forwardRef<
  HTMLDivElement,
  React.HTMLAttributes<HTMLDivElement> & {
    variant?: "default" | "glass" | "elevated" | "outline";
  }
>(({ className, variant = "default", ...props }, ref) => {
  const variants = {
    default: "bg-[var(--color-surface)] border border-[var(--border-default)]",
    glass: "glass-card",
    elevated: "bg-[var(--color-elevated)] border border-[var(--border-default)] shadow-lg",
    outline: "bg-transparent border border-[var(--border-default)]",
  };

  return (
    <div
      ref={ref}
      className={cn(
        "rounded-xl text-[var(--text-primary)]",
        variants[variant],
        className
      )}
      {...props}
    />
  );
});
Card.displayName = "Card";

// Card Header
const CardHeader = React.forwardRef<
  HTMLDivElement,
  React.HTMLAttributes<HTMLDivElement>
>(({ className, ...props }, ref) => (
  <div
    ref={ref}
    className={cn("flex flex-col space-y-1.5 p-4", className)}
    {...props}
  />
));
CardHeader.displayName = "CardHeader";

// Card Title
const CardTitle = React.forwardRef<
  HTMLParagraphElement,
  React.HTMLAttributes<HTMLHeadingElement>
>(({ className, ...props }, ref) => (
  <h3
    ref={ref}
    className={cn(
      "text-lg font-semibold leading-none tracking-tight text-[var(--text-primary)]",
      className
    )}
    {...props}
  />
));
CardTitle.displayName = "CardTitle";

// Card Description
const CardDescription = React.forwardRef<
  HTMLParagraphElement,
  React.HTMLAttributes<HTMLParagraphElement>
>(({ className, ...props }, ref) => (
  <p
    ref={ref}
    className={cn("text-sm text-[var(--text-secondary)]", className)}
    {...props}
  />
));
CardDescription.displayName = "CardDescription";

// Card Content
const CardContent = React.forwardRef<
  HTMLDivElement,
  React.HTMLAttributes<HTMLDivElement>
>(({ className, ...props }, ref) => (
  <div ref={ref} className={cn("p-4 pt-0", className)} {...props} />
));
CardContent.displayName = "CardContent";

// Card Footer
const CardFooter = React.forwardRef<
  HTMLDivElement,
  React.HTMLAttributes<HTMLDivElement>
>(({ className, ...props }, ref) => (
  <div
    ref={ref}
    className={cn("flex items-center p-4 pt-0", className)}
    {...props}
  />
));
CardFooter.displayName = "CardFooter";

// Specialized: Metric Card for Trading
interface MetricCardProps extends React.HTMLAttributes<HTMLDivElement> {
  title: string;
  value: string | number;
  change?: number;
  changeLabel?: string;
  icon?: React.ReactNode;
  variant?: "default" | "profit" | "loss" | "neutral";
}

const MetricCard = React.forwardRef<HTMLDivElement, MetricCardProps>(
  ({ className, title, value, change, changeLabel, icon, variant = "default", ...props }, ref) => {
    const variantStyles = {
      default: "",
      profit: "border-l-2 border-l-[var(--color-profit)]",
      loss: "border-l-2 border-l-[var(--color-loss)]",
      neutral: "border-l-2 border-l-[var(--color-primary)]",
    };

    const changeColor = change === undefined 
      ? "text-[var(--text-secondary)]" 
      : change >= 0 
        ? "text-[var(--color-profit)]" 
        : "text-[var(--color-loss)]";

    return (
      <Card
        ref={ref}
        className={cn("p-4", variantStyles[variant], className)}
        {...props}
      >
        <div className="flex items-start justify-between">
          <div className="space-y-1">
            <p className="text-sm text-[var(--text-secondary)]">{title}</p>
            <p className="text-2xl font-semibold font-mono tabular-nums">
              {value}
            </p>
            {change !== undefined && (
              <p className={cn("text-sm font-medium", changeColor)}>
                {change >= 0 ? "+" : ""}{change.toFixed(2)}%
                {changeLabel && <span className="text-[var(--text-muted)] ml-1">{changeLabel}</span>}
              </p>
            )}
          </div>
          {icon && (
            <div className="p-2 rounded-lg bg-[var(--glass-highlight)] text-[var(--text-secondary)]">
              {icon}
            </div>
          )}
        </div>
      </Card>
    );
  }
);
MetricCard.displayName = "MetricCard";

export { 
  Card, 
  CardHeader, 
  CardFooter, 
  CardTitle, 
  CardDescription, 
  CardContent,
  MetricCard 
};
