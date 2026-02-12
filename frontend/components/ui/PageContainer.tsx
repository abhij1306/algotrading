"use client"

import { cn } from "@/lib/utils";

interface PageContainerProps {
  children: React.ReactNode;
  className?: string;
}

export function PageContainer({ children, className }: PageContainerProps) {
  return (
    <div
      className={cn(
        "min-h-screen bg-[var(--color-base)]",
        className
      )}
    >
      {children}
    </div>
  );
}
