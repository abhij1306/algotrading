'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { Play, ChevronLeft, ChevronRight, Check, Sparkles } from 'lucide-react';
import { Button, Card, PageContainer } from '@/components/ui';
import { AssetTypeSelector } from '@/components/backtest/config/AssetTypeSelector';
import { SymbolSelector } from '@/components/backtest/config/SymbolSelector';
import { DateRangePicker } from '@/components/backtest/config/DateRangePicker';
import { StrategySelector } from '@/components/backtest/config/StrategySelector';
import { CostConfiguration } from '@/components/backtest/config/CostConfiguration';
import { OptionsConfigPanel } from '@/components/backtest/config/OptionsConfigPanel';
import { QuickStartPresets } from '@/components/backtest/config/QuickStartPresets';
import { ConfigPreview } from '@/components/backtest/config/ConfigPreview';
import { mockBacktestAPI } from '@/lib/backtest/mock-api';
import { BacktestConfig, AssetType } from '@/lib/backtest/types';
import { DEFAULT_CAPITAL, DEFAULT_COSTS } from '@/lib/backtest/constants';

type Step = {
  id: string;
  title: string;
  description: string;
};

const STEPS: Step[] = [
  { id: 'asset', title: 'Asset', description: 'Select type' },
  { id: 'symbol', title: 'Symbol', description: 'Choose instrument' },
  { id: 'dates', title: 'Dates', description: 'Time range' },
  { id: 'strategy', title: 'Strategy', description: 'Configure' },
  { id: 'capital', title: 'Capital', description: 'Set amount' },
];

export default function NewBacktestPage() {
  const router = useRouter();
  const [isRunning, setIsRunning] = useState(false);
  const [currentStep, setCurrentStep] = useState(0);

  // Form state
  const [assetType, setAssetType] = useState<AssetType>('stock');
  const [selectedSymbol, setSelectedSymbol] = useState('');
  const [selectedUniverse, setSelectedUniverse] = useState('NIFTY50');
  const [dateRange, setDateRange] = useState({ start: '2023-01-01', end: '2023-12-31' });
  const [initialCapital, setInitialCapital] = useState(DEFAULT_CAPITAL);
  const [strategy, setStrategy] = useState('momentum');
  const [costs, setCosts] = useState<{ brokerage: number; slippage: number; stampDuty: number }>(DEFAULT_COSTS);

  const [optionConfig, setOptionConfig] = useState({
    type: 'CE' as const,
    strikeSelection: 'ATM' as const,
    expirySelection: 'weekly' as const,
    rollStrategy: 'at_expiry' as const,
  });

  const handleRunBacktest = async () => {
    setIsRunning(true);

    try {
      let config: Partial<BacktestConfig> = {
        id: `config-${Date.now()}`,
        name: `${assetType === 'option' ? optionConfig.type : assetType} Strategy`,
        assetType,
        dateRange,
        initialCapital,
        costs,
      };

      if (assetType === 'stock') {
        config = {
          ...config,
          symbols: selectedSymbol ? [selectedSymbol] : ['NIFTY'],
          positionSizing: { type: 'percent_of_equity', value: 100 },
          maxPositions: 1,
          longShort: 'long',
        };
      } else if (assetType === 'option') {
        config = {
          ...config,
          underlying: selectedSymbol || 'NIFTY',
          optionSelection: {
            type: optionConfig.type,
            strikeSelection: optionConfig.strikeSelection,
            expirySelection: optionConfig.expirySelection,
            rollStrategy: optionConfig.rollStrategy,
          },
          strategy: 'long_call',
        };
      } else if (assetType === 'index') {
        config = {
          ...config,
          universe: selectedUniverse,
          reconstruction: true,
          selectionCriteria: { type: 'top_n', metric: 'momentum', n: 10, lookbackDays: 90 },
          rebalancing: { frequency: 'monthly', dayOfMonth: 1 },
        };
      }

      const result = await mockBacktestAPI.runBacktest(config as BacktestConfig);
      router.push(`/backtest/results/${result.runId}`);
    } catch (error) {
      console.error('Backtest failed:', error);
      alert('Backtest failed. Please try again.');
    } finally {
      setIsRunning(false);
    }
  };

  const handlePresetSelect = (config: BacktestConfig) => {
    setAssetType(config.assetType);
    setDateRange(config.dateRange);
    setInitialCapital(config.initialCapital);
    setCosts({
      brokerage: config.costs.brokerage,
      slippage: config.costs.slippage,
      stampDuty: config.costs.stampDuty ?? 0.0002,
    });

    if (config.assetType === 'stock') {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any -- Dynamic config access
      setSelectedSymbol((config as any).symbols[0] || '');
    } else if (config.assetType === 'option') {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any -- Dynamic config access
      setSelectedSymbol((config as any).underlying || '');
      // eslint-disable-next-line @typescript-eslint/no-explicit-any -- Dynamic config access
      setOptionConfig((config as any).optionSelection || optionConfig);
    } else if (config.assetType === 'index') {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any -- Dynamic config access
      setSelectedUniverse((config as any).universe || 'NIFTY50');
    }
  };

  const isStepValid = (step: number) => {
    switch (step) {
      case 0: return true; // Asset type always selected
      case 1: return assetType === 'index' ? true : selectedSymbol !== '';
      case 2: return dateRange.start && dateRange.end;
      case 3: return true; // Strategy always selected
      case 4: return initialCapital >= 10000;
      default: return false;
    }
  };

  const canProceed = isStepValid(currentStep);
  const allStepsValid = STEPS.every((_, i) => isStepValid(i));

  const renderStepContent = () => {
    switch (currentStep) {
      case 0:
        return (
          <div className="space-y-6">
            <div>
              <h2 className="text-xl font-semibold mb-2">Select Asset Type</h2>
              <p className="text-foreground-secondary">Choose the type of instrument you want to backtest</p>
            </div>
            <AssetTypeSelector value={assetType} onChange={setAssetType} />
          </div>
        );

      case 1:
        return (
          <div className="space-y-6">
            <div>
              <h2 className="text-xl font-semibold mb-2">
                {assetType === 'index' ? 'Select Universe' : 'Select Symbol'}
              </h2>
              <p className="text-foreground-secondary">
                {assetType === 'index'
                  ? 'Choose the index universe for reconstruction'
                  : 'Choose the stock or option to trade'}
              </p>
            </div>
            <SymbolSelector
              assetType={assetType}
              selectedSymbol={selectedSymbol}
              selectedUniverse={selectedUniverse}
              onSymbolChange={setSelectedSymbol}
              onUniverseChange={setSelectedUniverse}
            />
          </div>
        );

      case 2:
        return (
          <div className="space-y-6">
            <div>
              <h2 className="text-xl font-semibold mb-2">Date Range</h2>
              <p className="text-foreground-secondary">Select the time period for backtesting</p>
            </div>
            <DateRangePicker value={dateRange} onChange={setDateRange} />
          </div>
        );

      case 3:
        return (
          <div className="space-y-6">
            <div>
              <h2 className="text-xl font-semibold mb-2">Strategy Configuration</h2>
              <p className="text-foreground-secondary">Choose and configure your trading strategy</p>
            </div>
            <StrategySelector assetType={assetType} value={strategy} onChange={setStrategy} />
            {assetType === 'option' && (
              <Card className="p-4 mt-4">
                <h3 className="text-sm font-semibold mb-3">Option Settings</h3>
                <OptionsConfigPanel config={optionConfig} onChange={setOptionConfig} />
              </Card>
            )}
          </div>
        );

      case 4:
        return (
          <div className="space-y-6">
            <div>
              <h2 className="text-xl font-semibold mb-2">Capital & Costs</h2>
              <p className="text-foreground-secondary">Set your initial capital and trading costs</p>
            </div>
            <CostConfiguration
              initialCapital={initialCapital}
              costs={costs}
              onCapitalChange={setInitialCapital}
              onCostsChange={setCosts}
            />
          </div>
        );

      default:
        return null;
    }
  };

  return (
    <PageContainer className="py-6">
      {/* Header */}
      <div className="flex items-center justify-between mb-8">
        <div className="flex items-center gap-4">
          <Button variant="ghost" size="sm" onClick={() => router.push('/backtest')}>
            <ChevronLeft className="w-4 h-4 mr-1" />
            Back
          </Button>
          <div>
            <h1 className="text-2xl font-semibold">New Backtest</h1>
            <p className="text-sm text-foreground-secondary">
              Configure your strategy in {STEPS.length} simple steps
            </p>
          </div>
        </div>

        <Button
          onClick={handleRunBacktest}
          disabled={!allStepsValid || isRunning}
          size="lg"
        >
          {isRunning ? (
            <>
              <div className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin mr-2" />
              Running...
            </>
          ) : (
            <>
              <Play className="w-4 h-4 mr-2" />
              Run Backtest
            </>
          )}
        </Button>
      </div>

      {/* Quick Start Presets */}
      <Card className="p-4 mb-6 border-primary/20 bg-primary/5">
        <div className="flex items-center gap-2 mb-3">
          <Sparkles className="w-4 h-4 text-primary" />
          <h3 className="text-sm font-semibold">Quick Start Presets</h3>
        </div>
        <QuickStartPresets onSelect={handlePresetSelect} />
      </Card>

      {/* Stepper */}
      <div className="flex items-center justify-between mb-8">
        {STEPS.map((step, index) => {
          const isActive = index === currentStep;
          const isCompleted = index < currentStep;
          const isValid = isStepValid(index);

          return (
            <div key={step.id} className="flex items-center flex-1">
              <button
                onClick={() => setCurrentStep(index)}
                className="flex items-center gap-3 group"
              >
                <div className={`
                  w-10 h-10 rounded-full flex items-center justify-center font-semibold text-sm
                  transition-colors
                  ${isActive ? 'bg-primary text-white' : ''}
                  ${isCompleted && isValid ? 'bg-green-500 text-white' : ''}
                  ${!isActive && !isCompleted ? 'bg-background-tertiary text-foreground-secondary' : ''}
                `}>
                  {isCompleted && isValid ? (
                    <Check className="w-5 h-5" />
                  ) : (
                    index + 1
                  )}
                </div>
                <div className="text-left hidden sm:block">
                  <div className={`font-medium ${isActive ? 'text-foreground' : 'text-foreground-secondary'}`}>
                    {step.title}
                  </div>
                  <div className="text-xs text-foreground-secondary">{step.description}</div>
                </div>
              </button>

              {index < STEPS.length - 1 && (
                <div className={`
                  flex-1 h-0.5 mx-4 transition-colors
                  ${index < currentStep ? 'bg-green-500' : 'bg-background-tertiary'}
                `} />
              )}
            </div>
          );
        })}
      </div>

      {/* Main Content Area */}
      <div className="grid grid-cols-1 lg:grid-cols-[1fr_400px] gap-6">
        {/* Left: Current Step */}
        <Card className="p-6">
          {renderStepContent()}

          {/* Navigation */}
          <div className="flex items-center justify-between mt-8 pt-6 border-t">
            <Button
              variant="outline"
              onClick={() => setCurrentStep(Math.max(0, currentStep - 1))}
              disabled={currentStep === 0}
            >
              <ChevronLeft className="w-4 h-4 mr-2" />
              Previous
            </Button>

            <Button
              onClick={() => setCurrentStep(Math.min(STEPS.length - 1, currentStep + 1))}
              disabled={currentStep === STEPS.length - 1 || !canProceed}
            >
              Next
              <ChevronRight className="w-4 h-4 ml-2" />
            </Button>
          </div>
        </Card>

        {/* Right: Live Preview */}
        <div className="space-y-4">
          <ConfigPreview
            assetType={assetType}
            symbol={selectedSymbol}
            universe={selectedUniverse}
            dateRange={dateRange}
            initialCapital={initialCapital}
            strategy={strategy}
            optionConfig={assetType === 'option' ? optionConfig : undefined}
            isValid={allStepsValid}
          />
        </div>
      </div>
    </PageContainer>
  );
}
