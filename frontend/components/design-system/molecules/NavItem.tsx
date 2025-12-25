'use client'

import * as React from 'react'
import Link from 'next/link'
import { Badge } from '../atoms'
import { cn } from '@/lib/utils'
import { ChevronRight } from 'lucide-react'

export interface NavItemProps {
    href: string
    icon: React.ReactNode
    label: string
    description?: string
    isActive?: boolean
    badge?: string | number
    badgeVariant?: 'primary' | 'profit' | 'loss' | 'secondary'
    hasSubItems?: boolean
    isExpanded?: boolean
    onClick?: () => void
}

const NavItem = React.forwardRef<HTMLAnchorElement, NavItemProps>(
    (
        {
            href,
            icon,
            label,
            description,
            isActive = false,
            badge,
            badgeVariant = 'secondary',
            hasSubItems = false,
            isExpanded = false,
            onClick,
        },
        ref
    ) => {
        return (
            <Link
                href={href}
                ref={ref}
                onClick={onClick}
                className={cn(
                    'group relative flex items-center gap-3 px-3 py-2.5 rounded-lg transition-all duration-200',
                    isActive
                        ? 'bg-white/10 text-white shadow-neomorph border-l-2 border-electric-cyan'
                        : 'text-text-secondary hover:bg-white/5 hover:text-white hover:shadow-neomorph-sm'
                )}
            >
                {/* Icon with glow on active */}
                <span
                    className={cn(
                        'flex-shrink-0 transition-all duration-200',
                        isActive && 'text-electric-cyan drop-shadow-[0_0_8px_rgba(6,182,212,0.6)]'
                    )}
                >
                    {icon}
                </span>

                {/* Label and description */}
                <div className="flex-1 min-w-0">
                    <div className="text-sm font-medium truncate">{label}</div>
                    {description && (
                        <div className="text-[10px] text-text-tertiary truncate">
                            {description}
                        </div>
                    )}
                </div>

                {/* Badge */}
                {badge !== undefined && (
                    <Badge variant={badgeVariant} size="sm" pulse={isActive}>
                        {badge}
                    </Badge>
                )}

                {/* Expand indicator for sub-items */}
                {hasSubItems && (
                    <ChevronRight
                        className={cn(
                            'w-4 h-4 text-text-tertiary transition-transform duration-200',
                            isExpanded && 'rotate-90'
                        )}
                    />
                )}

                {/* Active glow effect */}
                {isActive && (
                    <div className="absolute inset-0 rounded-lg bg-electric-cyan/5 pointer-events-none" />
                )}

                {/* Hover shine effect */}
                <div className="absolute inset-0 rounded-lg bg-gradient-to-r from-transparent via-white/5 to-transparent -translate-x-full group-hover:translate-x-full transition-transform duration-700 pointer-events-none" />
            </Link>
        )
    }
)

NavItem.displayName = 'NavItem'

export { NavItem }
