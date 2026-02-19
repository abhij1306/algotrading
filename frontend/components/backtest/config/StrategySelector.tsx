'use client';

import { STRATEGY_OPTIONS } from '@/lib/backtest/constants';
import { AssetType, StrategyType } from '@/lib/backtest/types';
import { Button } from '@/components/ui/button';

interface StrategySelectorProps {
  assetType: AssetType;
  value: string;
  onChange: (strategy: string) => void;
}

export function StrategySelector({ assetType, value, onChange }: StrategySelectorProps) {
  const filteredStrategies = STRATEGY_OPTIONS.filter(
    (s) => s.supportedAssets.includes(assetType) || s.value === 'custom'
  );

  return (
    <div className="space-y-2">
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="w-full px-3 py-2 rounded-md border border-input bg-background text-sm"
      >
        {filteredStrategies.map((strategy) => (
          <option key={strategy.value} value={strategy.value}>
            {strategy.label} - {strategy.description}
          </option>
        ))}
      </select>

      <Button variant="link" size="sm" className="w-full">
        Open Visual Strategy Builder →
      </Button>
    </div>
  );
}
