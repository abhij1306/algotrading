import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

/** A status label: profit, loss and warning for states, outline and muted for neutral tags. */
const badgeVariants = cva(
  "inline-flex h-5 items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium transition-colors",
  {
    variants: {
      variant: {
        outline: "bg-transparent border-border text-foreground-muted",
        profit: "bg-profit-bg text-profit border-transparent",
        loss: "bg-loss-bg text-loss border-transparent",
        warning: "bg-warning-bg text-warning border-transparent",
        muted: "bg-background-secondary text-foreground-muted border-transparent",
      },
    },
  },
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLDivElement>, VariantProps<typeof badgeVariants> {}

export function Badge({ className, variant, ...props }: Readonly<BadgeProps>) {
  return <div className={cn(badgeVariants({ variant }), className)} {...props} />;
}
