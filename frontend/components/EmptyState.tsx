import { LucideIcon } from 'lucide-react'

interface EmptyStateProps {
    icon: LucideIcon
    title: string
    description: string
    actionLabel?: string
    onAction?: () => void
    iconColor?: string
}

export default function EmptyState({
    icon: Icon,
    title,
    description,
    actionLabel,
    onAction,
    iconColor = 'text-gray-500'
}: EmptyStateProps) {
    return (
        <div className="flex flex-col items-center justify-center h-full text-center p-8">
            <div className="w-20 h-20 bg-[#0A0A0A] rounded-full flex items-center justify-center mb-6 border border-white/5 shadow-2xl">
                <Icon className={`w-10 h-10 ${iconColor} opacity-50`} />
            </div>
            <h3 className="text-xl font-bold text-white mb-2">{title}</h3>
            <p className="text-gray-500 text-sm max-w-md mb-8 leading-relaxed">
                {description}
            </p>
            {actionLabel && onAction && (
                <button
                    onClick={onAction}
                    className="px-6 py-2.5 bg-cyan-600/10 hover:bg-cyan-600/20 text-cyan-400 border border-cyan-500/50 rounded-lg text-sm font-bold transition-all hover:shadow-[0_0_20px_rgba(34,211,238,0.2)]"
                >
                    {actionLabel}
                </button>
            )}
        </div>
    )
}
