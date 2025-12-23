import { SkeletonGlassCard } from "@/components/ui/SkeletonGlassCard";
import { GlassCard } from "@/components/ui/GlassCard";
import { PageContainer } from "@/components/ui/PageContainer";

export default function Loading() {
    return (
        <PageContainer>
            <div className="space-y-6">
                {/* Filters Bar Skeleton */}
                <div className="flex gap-4 overflow-x-auto pb-2">
                    {[...Array(5)].map((_, i) => (
                        <div key={i} className="h-10 w-32 bg-white/5 rounded-full animate-pulse border border-white/5 flex-shrink-0" />
                    ))}
                </div>

                {/* Table Skeleton */}
                <GlassCard className="p-0 overflow-hidden">
                    <div className="space-y-1">
                        {/* Header Row */}
                        <div className="h-12 bg-white/5 border-b border-white/5" />

                        {/* Data Rows */}
                        {[...Array(10)].map((_, i) => (
                            <div key={i} className="h-12 bg-transparent border-b border-white/5 animate-pulse" />
                        ))}
                    </div>
                </GlassCard>
            </div>
        </PageContainer>
    );
}
