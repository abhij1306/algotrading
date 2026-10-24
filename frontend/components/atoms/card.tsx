import type { HTMLAttributes } from "react";
import { cn } from "@/lib/utils";

type DivProps = Readonly<HTMLAttributes<HTMLDivElement>>;

export function Card({ className, ...props }: DivProps) {
  return (
    <div
      className={cn(
        "rounded-lg border transition-all duration-200 bg-surface border-border shadow-sm",
        className,
      )}
      {...props}
    />
  );
}

export function CardHeader({ className, ...props }: DivProps) {
  return (
    <div
      className={cn("flex flex-col space-y-1.5 p-4 border-b border-border/50", className)}
      {...props}
    />
  );
}

export function CardTitle({ className, ...props }: Readonly<HTMLAttributes<HTMLHeadingElement>>) {
  return (
    <h3
      className={cn(
        "text-base font-semibold leading-none tracking-tight text-foreground",
        className,
      )}
      {...props}
    />
  );
}

export function CardContent({ className, ...props }: DivProps) {
  return <div className={cn("p-4", className)} {...props} />;
}
