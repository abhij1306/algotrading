'use client';

import { memo } from 'react';
import { cn } from '@/lib/utils';
import { TrendingUp, TrendingDown, Minus } from 'lucide-react';

interface MetricCardProps {
  title: string;
  value: string | number;
  subtitle?: string;
  status: 'good' | 'warning' | 'danger' | 'neutral';
  tooltip?: string;
}

const statusConfig = {
  good: {
    bg: 'bg-green-500/10',
    border: 'border-green-500/30',
    text: 'text-green-500',
    icon: TrendingUp,
  },
  warning: {
    bg: 'bg-yellow-500/10',
    border: 'border-yellow-500/30',
    text: 'text-yellow-500',
    icon: Minus,
  },
  danger: {
    bg: 'bg-red-500/10',
    border: 'border-red-500/30',
    text: 'text-red-500',
    icon: TrendingDown,
  },
  neutral: {
    bg: 'bg-muted',
    border: 'border-border',
    text: 'text-muted-foreground',
    icon: Minus,
  },
};

export const MetricCard = memo(function MetricCard({ title, value, subtitle, status, tooltip }: MetricCardProps) {
  const config = statusConfig[status];
  const Icon = config.icon;

  return (
    <div
      className={cn(
        'relative overflow-hidden rounded-lg border p-4 transition-all',
        'hover:shadow-md hover:border-primary/30',
        config.bg,
        config.border
      )}
      title={tooltip}
    >
      <div className="flex items-start justify-between">
        <div>
          <p className="text-xs text-muted-foreground uppercase tracking-wider">{title}</p>
          <p className={cn('text-2xl font-semibold mt-1', config.text)}>{value}</p>
          {subtitle && (
            <p className="text-xs text-muted-foreground mt-1">{subtitle}</p>
          )}
        </div>
        <Icon className={cn('w-5 h-5', config.text)} />
      </div>
    </div>
  );
});
