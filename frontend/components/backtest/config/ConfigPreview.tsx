'use client';

import { LineChart, TrendingUp, Calendar, Wallet, CheckCircle, AlertCircle } from 'lucide-react';
import { Card, Badge } from '@/components/ui';
import { cn } from '@/lib/utils';
import { AssetType } from '@/lib/backtest/types';

interface ConfigPreviewProps {
  assetType: AssetType;
  symbol: string;
  universe: string;
  dateRange: { start: string; end: string };
  initialCapital: number;
  strategy: string;
  optionConfig?: {
    type: 'CE' | 'PE' | 'both';
    strikeSelection: string;
    expirySelection: string;
  };
  isValid: boolean;
}

export function ConfigPreview({
  assetType,
  symbol,
  universe,
  dateRange,
  initialCapital,
  strategy,
  optionConfig,
  isValid,
}: ConfigPreviewProps) {
  const displaySymbol = assetType === 'index' ? universe : symbol || 'Not selected';

  const formatCurrency = (amount: number) => {
    return new Intl.NumberFormat('en-IN', {
      style: 'currency',
      currency: 'INR',
      maximumFractionDigits: 0,
    }).format(amount);
  };

  return (
    <div className="space-y-4">
      {/* Validation Status */}
      <Card className={cn(
        "p-4 flex items-center gap-3",
        isValid ? "border-green-500/50 bg-green-500/5" : "border-yellow-500/50 bg-yellow-500/5"
      )}>
        {isValid ? (
          <CheckCircle className="w-5 h-5 text-green-500" />
        ) : (
          <AlertCircle className="w-5 h-5 text-yellow-500" />
        )}
        <div>
          <div className="font-medium text-sm">
            {isValid ? 'Configuration Valid' : 'Configuration Incomplete'}
          </div>
          <div className="text-xs text-muted-foreground">
            {isValid
              ? 'Ready to run backtest'
              : 'Please complete all required fields'}
          </div>
        </div>
      </Card>

      {/* Configuration Summary */}
      <Card className="p-4">
        <h3 className="text-sm font-semibold mb-3">Configuration Summary</h3>
        <div className="space-y-2">
          <div className="flex justify-between items-center py-1 border-b border-border/50">
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <TrendingUp className="w-4 h-4" />
              Asset Type
            </div>
            <Badge variant="outline" className="capitalize">
              {assetType}
            </Badge>
          </div>

          <div className="flex justify-between items-center py-1 border-b border-border/50">
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <LineChart className="w-4 h-4" />
              {assetType === 'index' ? 'Universe' : 'Symbol'}
            </div>
            <span className="font-medium">{displaySymbol}</span>
          </div>

          <div className="flex justify-between items-center py-1 border-b border-border/50">
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Calendar className="w-4 h-4" />
              Period
            </div>
            <span className="font-medium text-xs">
              {dateRange.start} to {dateRange.end}
            </span>
          </div>

          <div className="flex justify-between items-center py-1 border-b border-border/50">
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Wallet className="w-4 h-4" />
              Initial Capital
            </div>
            <span className="font-medium">{formatCurrency(initialCapital)}</span>
          </div>

          <div className="flex justify-between items-center py-1">
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <LineChart className="w-4 h-4" />
              Strategy
            </div>
            <span className="font-medium capitalize">{strategy.replace('_', ' ')}</span>
          </div>
        </div>
      </Card>

      {/* Options Summary (if applicable) */}
      {assetType === 'option' && optionConfig && (
        <Card className="p-4">
          <h3 className="text-sm font-semibold mb-3">Options Configuration</h3>
          <div className="grid grid-cols-2 gap-2 text-sm">
            <div className="text-muted-foreground">Option Type:</div>
            <div className="font-medium">{optionConfig.type}</div>

            <div className="text-muted-foreground">Strike:</div>
            <div className="font-medium uppercase">{optionConfig.strikeSelection}</div>

            <div className="text-muted-foreground">Expiry:</div>
            <div className="font-medium capitalize">{optionConfig.expirySelection}</div>
          </div>
        </Card>
      )}

      {/* Estimated Runtime */}
      {isValid && (
        <Card className="p-4 bg-muted/50">
          <div className="flex items-center justify-between">
            <div>
              <div className="text-sm font-medium">Estimated Runtime</div>
              <div className="text-xs text-muted-foreground">
                Based on selected date range
              </div>
            </div>
            <div className="text-lg font-semibold text-primary">~2s</div>
          </div>
        </Card>
      )}

      {/* Chart Placeholder */}
      {isValid && (
        <Card className="p-4">
          <h3 className="text-sm font-semibold mb-3">Historical Price Preview</h3>
          <div className="h-48 bg-muted/50 rounded-lg flex items-center justify-center">
            <div className="text-center text-muted-foreground">
              <LineChart className="w-8 h-8 mx-auto mb-2 opacity-50" />
              <span className="text-sm">Price chart will appear here</span>
              <p className="text-xs mt-1">Run backtest to see full results</p>
            </div>
          </div>
        </Card>
      )}
    </div>
  );
}
