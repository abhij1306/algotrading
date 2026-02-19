"use client";

import { cn } from "@/lib/utils";

interface PageContainerProps {
  children: React.ReactNode;
  className?: string;
  scrollable?: boolean;
  fullWidth?: boolean;
}

export function PageContainer({
  children,
  className,
  scrollable = true,
  fullWidth = false
}: PageContainerProps) {
  return (
    <div
      className={cn(
        "flex flex-col w-full bg-background transition-colors duration-200",
        scrollable ? "min-h-0 flex-1 overflow-auto" : "h-full overflow-hidden",
        className
      )}
    >
      <div className={cn(
        "flex-1 w-full",
        fullWidth ? "p-0" : "p-4 md:p-6 space-y-6 max-w-[1920px] mx-auto"
      )}>
        {children}
      </div>
    </div>
  );
}
