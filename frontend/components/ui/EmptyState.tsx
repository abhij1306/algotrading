import React from 'react';
import { LucideIcon } from 'lucide-react';

interface EmptyStateProps {
    icon?: LucideIcon;
    title: string;
    description: string;
    actionLabel?: string;
    onAction?: () => void;
    className?: string;
}

export default function EmptyState({
    icon: Icon,
    title,
    description,
    actionLabel,
    onAction,
    className = ""
}: EmptyStateProps) {
    return (
        <div className={`flex flex-col items-center justify-center p-8 text-center h-full animate-in fade-in duration-500 ${className}`}>
            {Icon && (
                <div className="p-4 rounded-full bg-white/5 mb-4 ring-1 ring-white/10 shadow-xl shadow-black/20">
                    <Icon className="w-8 h-8 text-gray-500" />
                </div>
            )}
            <h3 className="text-sm font-bold text-gray-200 uppercase tracking-widest mb-1">{title}</h3>
            <p className="text-xs text-gray-500 max-w-[250px] mb-6 leading-relaxed font-mono">{description}</p>

            {actionLabel && onAction && (
                <button
                    onClick={onAction}
                    className="group relative px-4 py-2 bg-white/5 hover:bg-white/10 text-gray-300 hover:text-white rounded-lg border border-white/10 hover:border-white/20 transition-all text-xs font-bold uppercase tracking-wider overflow-hidden"
                >
                    <span className="relative z-10">{actionLabel}</span>
                    <div className="absolute inset-0 bg-gradient-to-r from-cyan-500/10 to-purple-500/10 opacity-0 group-hover:opacity-100 transition-opacity" />
                </button>
            )}
        </div>
    );
}
