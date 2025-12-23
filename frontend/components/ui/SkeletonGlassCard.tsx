import { GlassCard } from "@/components/ui/GlassCard";

interface SkeletonGlassCardProps {
    className?: string;
}

export const SkeletonGlassCard = ({ className = "" }: SkeletonGlassCardProps) => {
    return (
        <GlassCard className={`relative overflow-hidden ${className}`}>
            <div className="animate-pulse flex flex-col h-full w-full p-4 space-y-4">
                {/* Header Line */}
                <div className="h-4 bg-white/5 rounded w-1/3"></div>

                {/* Content Block */}
                <div className="flex-1 bg-white/5 rounded w-full opacity-50"></div>

                {/* Footer Line */}
                <div className="h-3 bg-white/5 rounded w-1/4"></div>
            </div>

            {/* Shimmer Effect */}
            <div className="absolute inset-0 -translate-x-full animate-[shimmer_2s_infinite] bg-gradient-to-r from-transparent via-white/5 to-transparent"></div>
        </GlassCard>
    );
};
