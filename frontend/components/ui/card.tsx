import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const cardVariants = cva("rounded-lg border transition-all duration-200", {
  variants: {
    variant: {
      default: "bg-surface border-border shadow-sm",
      glass: "bg-surface/80 backdrop-blur-md border-border-subtle shadow-md",
      elevated: "bg-elevated border-border shadow-md",
      outline: "bg-transparent border-border hover:bg-surface/50",
      void: "bg-background border-none shadow-none",
      flat: "bg-background-secondary border-transparent",
    },
  },
  defaultVariants: {
    variant: "default",
  },
});

export interface CardProps
  extends React.HTMLAttributes<HTMLDivElement>, VariantProps<typeof cardVariants> {}

const Card = React.forwardRef<HTMLDivElement, CardProps>(
  ({ className, variant, ...props }, ref) => (
    <div ref={ref} className={cn(cardVariants({ variant, className }))} {...props} />
  )
);
Card.displayName = "Card";

const CardHeader = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div
      ref={ref}
      className={cn("flex flex-col space-y-1.5 p-4 border-b border-border/50", className)}
      {...props}
    />
  )
);
CardHeader.displayName = "CardHeader";

const CardTitle = React.forwardRef<HTMLParagraphElement, React.HTMLAttributes<HTMLHeadingElement>>(
  ({ className, ...props }, ref) => (
    <h3
      ref={ref}
      className={cn(
        "text-base font-semibold leading-none tracking-tight text-foreground",
        className
      )}
      {...props}
    />
  )
);
CardTitle.displayName = "CardTitle";

const CardDescription = React.forwardRef<
  HTMLParagraphElement,
  React.HTMLAttributes<HTMLParagraphElement>
>(({ className, ...props }, ref) => (
  <p ref={ref} className={cn("text-xs text-foreground-muted mt-1.5", className)} {...props} />
));
CardDescription.displayName = "CardDescription";

const CardContent = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => <div ref={ref} className={cn("p-4", className)} {...props} />
);
CardContent.displayName = "CardContent";

const CardFooter = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div
      ref={ref}
      className={cn(
        "flex items-center p-4 border-t border-border/50 bg-background-secondary/30",
        className
      )}
      {...props}
    />
  )
);
CardFooter.displayName = "CardFooter";

/**
 * Metric Card
 *
 * Specialized card for displaying key performance indicators (KPIs)
 */
export interface MetricCardProps extends CardProps {
  label: string;
  value: string | number;
  change?: number;
  icon?: React.ReactNode;
  trend?: "up" | "down" | "neutral";
  subValue?: string;
}

const MetricCard = React.forwardRef<HTMLDivElement, MetricCardProps>(
  ({ label, value, change, icon, subValue, className, ...props }, ref) => {
    return (
      <Card ref={ref} className={cn("p-4", className)} {...props}>
        <div className="flex items-center justify-between mb-2">
          <span className="text-xs font-medium text-foreground-muted uppercase tracking-wider">
            {label}
          </span>
          {icon && <div className="text-foreground-muted h-4 w-4">{icon}</div>}
        </div>
        <div className="flex items-end justify-between">
          <div>
            <div className="text-2xl font-semibold font-mono tracking-tight text-foreground">
              {value}
            </div>
            {subValue && <div className="text-xs text-foreground-muted mt-1">{subValue}</div>}
          </div>
          {change !== undefined && (
            <div
              className={cn(
                "text-xs font-semibold flex items-center gap-0.5 px-1.5 py-0.5 rounded",
                change > 0
                  ? "text-profit bg-profit-bg"
                  : change < 0
                    ? "text-loss bg-loss-bg"
                    : "text-foreground-muted bg-background-secondary"
              )}
            >
              {change > 0 ? "+" : ""}
              {change}%
            </div>
          )}
        </div>
      </Card>
    );
  }
);
MetricCard.displayName = "MetricCard";

export { Card, CardHeader, CardFooter, CardTitle, CardDescription, CardContent, MetricCard };
