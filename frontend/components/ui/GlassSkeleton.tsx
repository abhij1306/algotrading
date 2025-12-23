
import React from "react";
import { cn } from "@/lib/utils";

interface GlassSkeletonProps extends React.HTMLAttributes<HTMLDivElement> { }

export function GlassSkeleton({ className, ...props }: GlassSkeletonProps) {
    return (
        <div
            className={cn(
                "animate-pulse rounded-md bg-white/5 border border-white/5",
                "shadow-[0_0_10px_rgba(255,255,255,0.02)]",
                className
            )}
            {...props}
        />
    );
}
