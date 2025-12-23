import { ReactNode } from "react";

interface GlassCardProps {
    children: ReactNode;
    className?: string;
}

export const GlassCard = ({ children, className = "" }: GlassCardProps) => {
    return (
        <div className={`relative border border-white/5 bg-gradient-to-br from-white/5 to-transparent rounded-xl backdrop-blur-md shadow-xl overflow-hidden ${className}`}>
            {/* Inner Glow Hack */}
            <div className="absolute inset-0 bg-gradient-to-br from-white/5 to-transparent opacity-50 pointer-events-none" />
            <div className="relative z-0">
                {children}
            </div>
        </div>
    );
};
