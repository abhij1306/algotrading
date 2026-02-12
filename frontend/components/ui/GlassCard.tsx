"use client"

import { cn } from "@/lib/utils";

interface GlassCardProps extends React.HTMLAttributes<HTMLDivElement> {
  children: React.ReactNode;
  hoverEffect?: boolean;
}

export function GlassCard({ 
  children, 
  className, 
  hoverEffect = false,
  ...props 
}: GlassCardProps) {
  return (
    <div
      className={cn(
        "rounded-xl p-4 transition-all duration-200",
        "bg-[var(--glass-bg)] backdrop-blur-xl",
        "border border-[var(--glass-border)]",
        hoverEffect && "hover:border-[var(--glass-border-strong)] hover:bg-[rgba(24,24,27,0.8)]",
        className
      )}
      {...props}
    >
      {children}
    </div>
  );
}
