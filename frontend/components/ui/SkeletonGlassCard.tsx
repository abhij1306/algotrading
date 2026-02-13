"use client"

import { Skeleton } from "./skeleton";
import { cn } from "@/lib/utils";

interface SkeletonGlassCardProps {
  lines?: number;
  className?: string;
}

export function SkeletonGlassCard({ lines = 3, className }: SkeletonGlassCardProps) {
  return (
    <div
      className={cn(
        "glass-card p-4",
        className
      )}
    >
      <div className="space-y-3">
        <Skeleton className="h-4 w-1/3" />
        <Skeleton className="h-6 w-full" />
        {Array.from({ length: lines - 2 }).map((_, i) => (
          <Skeleton key={i} className="h-3 w-4/5" />
        ))}
      </div>
    </div>
  );
}
