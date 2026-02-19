'use client';

interface OptionsConfig {
  type: 'CE' | 'PE' | 'both';
  strikeSelection: 'ATM' | 'ITM' | 'OTM' | 'percent_otm';
  expirySelection: 'weekly' | 'monthly' | 'days_to_expiry';
  rollStrategy: 'none' | 'at_expiry' | 'days_before' | 'delta_based';
}

interface OptionsConfigPanelProps {
  config: OptionsConfig;
  onChange: (config: OptionsConfig) => void;
}

export function OptionsConfigPanel({ config, onChange }: OptionsConfigPanelProps) {
  const updateConfig = (key: keyof OptionsConfig, value: string) => {
    onChange({ ...config, [key]: value });
  };

  return (
    <div className="space-y-3">
      {/* Option Type */}
      <div className="space-y-1">
        <label className="text-xs text-muted-foreground">Option Type</label>
        <div className="flex gap-2">
          {(['CE', 'PE', 'both'] as const).map((type) => (
            <button
              key={type}
              onClick={() => updateConfig('type', type)}
              className={`flex-1 py-1.5 text-xs rounded border transition-colors ${
                config.type === type
                  ? 'bg-primary text-primary-foreground border-primary'
                  : 'bg-background hover:bg-accent'
              }`}
            >
              {type === 'CE' ? 'Call' : type === 'PE' ? 'Put' : 'Both'}
            </button>
          ))}
        </div>
      </div>

      {/* Strike Selection */}
      <div className="space-y-1">
        <label className="text-xs text-muted-foreground">Strike Selection</label>
        <select
          value={config.strikeSelection}
          onChange={(e) => updateConfig('strikeSelection', e.target.value)}
          className="w-full px-3 py-1.5 rounded-md border border-input bg-background text-sm"
        >
          <option value="ATM">ATM (At The Money)</option>
          <option value="ITM">ITM (In The Money)</option>
          <option value="OTM">OTM (Out of The Money)</option>
          <option value="percent_otm">% OTM</option>
        </select>
      </div>

      {/* Expiry Selection */}
      <div className="space-y-1">
        <label className="text-xs text-muted-foreground">Expiry Selection</label>
        <select
          value={config.expirySelection}
          onChange={(e) => updateConfig('expirySelection', e.target.value)}
          className="w-full px-3 py-1.5 rounded-md border border-input bg-background text-sm"
        >
          <option value="weekly">Weekly</option>
          <option value="monthly">Monthly</option>
          <option value="days_to_expiry">Fixed DTE</option>
        </select>
      </div>

      {/* Roll Strategy */}
      <div className="space-y-1">
        <label className="text-xs text-muted-foreground">Roll Strategy</label>
        <select
          value={config.rollStrategy}
          onChange={(e) => updateConfig('rollStrategy', e.target.value)}
          className="w-full px-3 py-1.5 rounded-md border border-input bg-background text-sm"
        >
          <option value="none">No Roll</option>
          <option value="at_expiry">At Expiry</option>
          <option value="days_before">Days Before Expiry</option>
          <option value="delta_based">Delta Based</option>
        </select>
      </div>
    </div>
  );
}
