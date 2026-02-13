"use client"

import { cn } from "@/lib/utils";
import React from "react";

interface GlassCardProps extends React.HTMLAttributes<HTMLDivElement> {
  children: React.ReactNode;
  hoverEffect?: boolean;
}

/**
 * @deprecated Use Card with variant="glass" instead
 */
export function GlassCard({
  children,
  className,
  hoverEffect = false,
  ...props
}: GlassCardProps) {
  return (
    <div
      className={cn(
        "glass-card p-4",
        hoverEffect && "hover:border-[var(--color-primary)]/50",
        className
      )}
      {...props}
    >
      {children}
    </div>
  );
}
