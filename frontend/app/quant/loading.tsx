import { SkeletonGlassCard } from "@/components/ui/SkeletonGlassCard";
import { PageContainer } from "@/components/ui/PageContainer";

export default function Loading() {
    return (
        <PageContainer>
            <div className="space-y-6">
                {/* Header Skeleton */}
                <div className="flex justify-between items-center">
                    <div className="h-8 w-48 bg-white/5 rounded animate-pulse" />
                    <div className="h-8 w-32 bg-white/5 rounded animate-pulse" />
                </div>

                {/* KPI Grid */}
                <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
                    {[...Array(4)].map((_, i) => (
                        <div key={i} className="h-24 bg-white/5 rounded-xl animate-pulse border border-white/5" />
                    ))}
                </div>

                {/* Main Content Area */}
                <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
                    <div className="lg:col-span-2">
                        <SkeletonGlassCard className="h-[400px]" />
                    </div>
                    <div>
                        <SkeletonGlassCard className="h-[400px]" />
                    </div>
                </div>
            </div>
        </PageContainer>
    );
}
