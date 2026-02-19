"use client";

import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-md text-sm font-semibold transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/20 disabled:pointer-events-none disabled:opacity-50 active:scale-[0.98]",
  {
    variants: {
      variant: {
        primary: "bg-primary text-primary-fg hover:bg-primary-hover shadow-sm hover:shadow-md",
        secondary:
          "bg-background-secondary text-foreground hover:bg-background-tertiary border border-border",
        ghost: "text-foreground-muted hover:bg-background-secondary hover:text-foreground",
        profit: "bg-profit text-white hover:opacity-90 shadow-sm",
        loss: "bg-loss text-white hover:opacity-90 shadow-sm",
        outline:
          "bg-transparent border border-border hover:bg-background-secondary hover:text-foreground",
        link: "text-primary underline-offset-4 hover:underline",
        destructive: "bg-loss text-white hover:bg-loss/90",
      },
      size: {
        xs: "h-6 px-2 text-xxs",
        sm: "h-8 px-3 text-xs",
        default: "h-9 px-4 py-2",
        lg: "h-10 px-8",
        icon: "h-9 w-9",
      },
    },
    defaultVariants: {
      variant: "secondary",
      size: "default",
    },
  }
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>, VariantProps<typeof buttonVariants> {}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, ...props }, ref) => {
    return (
      <button className={cn(buttonVariants({ variant, size, className }))} ref={ref} {...props} />
    );
  }
);
Button.displayName = "Button";

export { Button, buttonVariants };
