'use client';

import { TrendingUp, Layers, Globe } from 'lucide-react';
import { cn } from '@/lib/utils';
import { AssetType } from '@/lib/backtest/types';
import { ASSET_TYPE_OPTIONS } from '@/lib/backtest/constants';

interface AssetTypeSelectorProps {
  value: AssetType;
  onChange: (type: AssetType) => void;
}

const iconMap = {
  TrendingUp,
  Layers,
  Globe,
};

export function AssetTypeSelector({ value, onChange }: AssetTypeSelectorProps) {
  return (
    <div className="grid grid-cols-3 gap-2">
      {ASSET_TYPE_OPTIONS.map((option) => {
        const Icon = iconMap[option.icon as keyof typeof iconMap];
        const isSelected = value === option.value;

        return (
          <button
            key={option.value}
            onClick={() => onChange(option.value)}
            className={cn(
              'flex flex-col items-center gap-2 p-3 rounded-lg border transition-all duration-200',
              'hover:border-primary/50 hover:bg-primary/5',
              isSelected
                ? 'border-primary bg-primary/10 ring-1 ring-primary'
                : 'border-border bg-card'
            )}
          >
            <div
              className={cn(
                'p-2 rounded-md transition-colors',
                isSelected ? 'bg-primary text-primary-foreground' : 'bg-muted'
              )}
            >
              <Icon className="w-5 h-5" />
            </div>
            <div className="text-center">
              <div
                className={cn(
                  'text-sm font-medium',
                  isSelected ? 'text-primary' : 'text-foreground'
                )}
              >
                {option.label}
              </div>
              <div className="text-xxs text-muted-foreground mt-0.5 leading-tight">
                {option.description}
              </div>
            </div>
          </button>
        );
      })}
    </div>
  );
}
