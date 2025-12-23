import { ReactNode } from "react";

interface MetricBadgeProps {
    label: string;
    value?: string | number | ReactNode;
    trend?: 'up' | 'down' | 'neutral';
    className?: string;
}

export const MetricBadge = ({ label, value, trend, className = "" }: MetricBadgeProps) => {
    const trendColor =
        trend === 'up' ? 'text-profit bg-profit/10' :
            trend === 'down' ? 'text-loss bg-loss/10' :
                'text-cyan-400 bg-cyan-500/10';

    return (
        <div className={`inline-flex items-center gap-2 px-2 py-0.5 rounded-full text-xs font-medium border border-white/5 ${trendColor} ${className}`}>
            <span className="opacity-70 uppercase tracking-wider">{label}</span>
            {value && <span className="font-mono font-bold">{value}</span>}
        </div>
    );
};
