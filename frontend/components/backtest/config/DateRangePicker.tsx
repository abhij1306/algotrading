'use client';

import { useMemo } from 'react';
import { Calendar } from 'lucide-react';
import { Input } from '@/components/ui';
import { DATE_PRESETS } from '@/lib/backtest/constants';
import { cn } from '@/lib/utils';

interface DateRangePickerProps {
  value: { start: string; end: string };
  onChange: (range: { start: string; end: string }) => void;
}

export function DateRangePicker({ value, onChange }: DateRangePickerProps) {
  const marketDays = useMemo(() => {
    // Simplified calculation - assumes ~252 trading days per year
    const start = new Date(value.start);
    const end = new Date(value.end);
    const diffTime = Math.abs(end.getTime() - start.getTime());
    const diffDays = Math.ceil(diffTime / (1000 * 60 * 60 * 24));
    return Math.floor(diffDays * 0.7); // ~70% are trading days
  }, [value]);

  const handlePresetClick = (presetValue: string) => {
    if (presetValue === 'ytd') {
      const now = new Date();
      const startOfYear = new Date(now.getFullYear(), 0, 1);
      onChange({
        start: startOfYear.toISOString().split('T')[0],
        end: now.toISOString().split('T')[0],
      });
    } else if (presetValue.includes(':')) {
      const [start, end] = presetValue.split(':');
      onChange({ start, end });
    } else if (presetValue === '1y') {
      const now = new Date();
      const oneYearAgo = new Date(now.getFullYear() - 1, now.getMonth(), now.getDate());
      onChange({
        start: oneYearAgo.toISOString().split('T')[0],
        end: now.toISOString().split('T')[0],
      });
    } else if (presetValue === '3y') {
      const now = new Date();
      const threeYearsAgo = new Date(now.getFullYear() - 3, now.getMonth(), now.getDate());
      onChange({
        start: threeYearsAgo.toISOString().split('T')[0],
        end: now.toISOString().split('T')[0],
      });
    } else if (presetValue === '5y') {
      const now = new Date();
      const fiveYearsAgo = new Date(now.getFullYear() - 5, now.getMonth(), now.getDate());
      onChange({
        start: fiveYearsAgo.toISOString().split('T')[0],
        end: now.toISOString().split('T')[0],
      });
    }
  };

  return (
    <div className="space-y-3">
      {/* Date Inputs */}
      <div className="grid grid-cols-2 gap-2">
        <div className="space-y-1">
          <label className="text-xs text-muted-foreground">Start Date</label>
          <div className="relative">
            <Calendar className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
            <Input
              type="date"
              value={value.start}
              onChange={(e) => onChange({ ...value, start: e.target.value })}
              className="pl-9"
            />
          </div>
        </div>
        <div className="space-y-1">
          <label className="text-xs text-muted-foreground">End Date</label>
          <div className="relative">
            <Calendar className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
            <Input
              type="date"
              value={value.end}
              onChange={(e) => onChange({ ...value, end: e.target.value })}
              className="pl-9"
            />
          </div>
        </div>
      </div>

      {/* Quick Presets */}
      <div className="flex flex-wrap gap-1">
        {DATE_PRESETS.map((preset) => (
          <button
            key={preset.label}
            onClick={() => handlePresetClick(preset.value)}
            className={cn(
              'text-xxs px-2 py-1 rounded border transition-colors',
              'hover:bg-accent hover:border-primary/50'
            )}
          >
            {preset.label}
          </button>
        ))}
      </div>

      {/* Market Days Info */}
      <div className="flex items-center gap-2 text-xs text-muted-foreground">
        <Calendar className="w-3 h-3" />
        <span>~{marketDays} trading days in selected range</span>
      </div>
    </div>
  );
}
