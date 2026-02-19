import * as React from "react";
import { cn } from "@/lib/utils";

/**
 * Skeleton Component
 *
 * Loading placeholder with shimmer animation.
 */

interface SkeletonProps extends React.HTMLAttributes<HTMLDivElement> {
  variant?: "default" | "circular" | "text" | "card";
  width?: string | number;
  height?: string | number;
}

const Skeleton = React.forwardRef<HTMLDivElement, SkeletonProps>(
  ({ className, variant = "default", width, height, style, ...props }, ref) => {
    const variants = {
      default: "rounded-lg",
      circular: "rounded-full",
      text: "rounded h-4",
      card: "rounded-xl",
    };

    return (
      <div
        ref={ref}
        className={cn(
          "animate-shimmer bg-gradient-to-r from-transparent via-[rgba(255,255,255,0.05)] to-transparent bg-[length:200%_100%]",
          "bg-elevated",
          variants[variant],
          className
        )}
        style={{
          width: width,
          height: height,
          ...style,
        }}
        {...props}
      />
    );
  }
);
Skeleton.displayName = "Skeleton";

// Skeleton Card
const SkeletonCard = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div
      ref={ref}
      className={cn("rounded-xl bg-surface p-4 space-y-3 shadow-md", className)}
      {...props}
    >
      <Skeleton className="h-4 w-1/3" />
      <Skeleton className="h-8 w-1/2" />
      <Skeleton className="h-3 w-2/3" />
    </div>
  )
);
SkeletonCard.displayName = "SkeletonCard";

// Skeleton Table
const SkeletonTable = React.forwardRef<
  HTMLDivElement,
  React.HTMLAttributes<HTMLDivElement> & { rows?: number; columns?: number }
>(({ className, rows = 5, columns = 4, ...props }, ref) => (
  <div ref={ref} className={cn("space-y-2", className)} {...props}>
    {/* Header */}
    <div className="flex gap-4 p-2">
      {Array.from({ length: columns }).map((_, i) => (
        <Skeleton key={i} className="h-4 flex-1" />
      ))}
    </div>
    {/* Rows */}
    {Array.from({ length: rows }).map((_, rowIndex) => (
      <div key={rowIndex} className="flex gap-4 p-2">
        {Array.from({ length: columns }).map((_, colIndex) => (
          <Skeleton key={colIndex} className="h-4 flex-1" />
        ))}
      </div>
    ))}
  </div>
));
SkeletonTable.displayName = "SkeletonTable";

// Skeleton Metric
const SkeletonMetric = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div ref={ref} className={cn("rounded-xl bg-surface p-4 shadow-sm", className)} {...props}>
      <div className="flex items-start justify-between">
        <div className="space-y-2">
          <Skeleton className="h-3 w-20" />
          <Skeleton className="h-7 w-28" />
          <Skeleton className="h-3 w-16" />
        </div>
        <Skeleton className="h-8 w-8 rounded-lg" />
      </div>
    </div>
  )
);
SkeletonMetric.displayName = "SkeletonMetric";

// Skeleton Glass Card
const SkeletonGlassCard = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div
      ref={ref}
      className={cn(
        "rounded-xl border border-border-subtle bg-surface/80 backdrop-blur-xl p-4 space-y-3",
        className
      )}
      {...props}
    >
      <Skeleton className="h-4 w-1/3" />
      <Skeleton className="h-8 w-1/2" />
      <Skeleton className="h-3 w-2/3" />
    </div>
  )
);
SkeletonGlassCard.displayName = "SkeletonGlassCard";

export { Skeleton, SkeletonCard, SkeletonTable, SkeletonMetric, SkeletonGlassCard };
