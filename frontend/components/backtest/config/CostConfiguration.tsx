'use client';

import { Input } from '@/components/ui';

interface CostConfigurationProps {
  initialCapital: number;
  costs: {
    brokerage: number;
    slippage: number;
    stampDuty: number;
  };
  onCapitalChange: (capital: number) => void;
  onCostsChange: (costs: { brokerage: number; slippage: number; stampDuty: number }) => void;
}

export function CostConfiguration({
  initialCapital,
  costs,
  onCapitalChange,
  onCostsChange,
}: CostConfigurationProps) {
  return (
    <div className="space-y-3">
      <div className="space-y-1">
        <label className="text-xs text-muted-foreground">Initial Capital (₹)</label>
        <Input
          type="number"
          value={initialCapital}
          onChange={(e) => onCapitalChange(Number(e.target.value))}
          min={10000}
          step={10000}
        />
      </div>

      <div className="grid grid-cols-2 gap-2">
        <div className="space-y-1">
          <label className="text-xs text-muted-foreground">Brokerage (%)</label>
          <Input
            type="number"
            value={(costs.brokerage * 100).toFixed(3)}
            onChange={(e) =>
              onCostsChange({
                ...costs,
                brokerage: Number(e.target.value) / 100,
              })
            }
            min={0}
            max={1}
            step={0.001}
          />
        </div>
        <div className="space-y-1">
          <label className="text-xs text-muted-foreground">Slippage (%)</label>
          <Input
            type="number"
            value={(costs.slippage * 100).toFixed(3)}
            onChange={(e) =>
              onCostsChange({
                ...costs,
                slippage: Number(e.target.value) / 100,
              })
            }
            min={0}
            max={1}
            step={0.001}
          />
        </div>
      </div>

      <p className="text-xxs text-muted-foreground">
        💡 Typical NSE costs: 0.05-0.1% all-in (brokerage + STT + stamp duty + charges)
      </p>
    </div>
  );
}
