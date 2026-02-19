'use client';

import { getMockScenarios } from '@/lib/backtest/mock-api';
import { BacktestConfig } from '@/lib/backtest/types';

interface QuickStartPresetsProps {
  onSelect: (config: BacktestConfig) => void;
}

export function QuickStartPresets({ onSelect }: QuickStartPresetsProps) {
  const scenarios = getMockScenarios();

  return (
    <div className="space-y-2">
      {scenarios.slice(0, 3).map((scenario) => (
        <button
          key={scenario.id}
          onClick={() => onSelect(scenario.config as BacktestConfig)}
          className="w-full text-left p-2 rounded border border-border hover:border-primary/50 hover:bg-accent transition-colors"
        >
          <div className="font-medium text-sm">{scenario.name}</div>
          <div className="text-xxs text-muted-foreground line-clamp-1">
            {scenario.description}
          </div>
        </button>
      ))}
    </div>
  );
}
