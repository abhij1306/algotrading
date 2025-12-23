import { SkeletonGlassCard } from "@/components/ui/SkeletonGlassCard";
import { PageContainer } from "@/components/ui/PageContainer";

export default function Loading() {
    return (
        <PageContainer>
            <div className="space-y-6">
                {/* Header */}
                <div className="flex items-center gap-4">
                    <div className="h-10 w-10 rounded-full bg-white/5 animate-pulse" />
                    <div className="h-8 w-64 bg-white/5 rounded animate-pulse" />
                </div>

                {/* Portfolio Metrics */}
                <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                    {[...Array(3)].map((_, i) => (
                        <SkeletonGlassCard key={i} className="h-32" />
                    ))}
                </div>

                {/* Charts Area */}
                <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
                    <SkeletonGlassCard className="h-[350px]" />
                    <SkeletonGlassCard className="h-[350px]" />
                </div>
            </div>
        </PageContainer>
    );
}
