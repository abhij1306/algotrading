'use client';

import { useState } from 'react';
import { ArrowRightLeft, X, Check } from 'lucide-react';
import { Card, Button, Badge } from '@/components/ui';
import { BacktestListItem } from '@/lib/backtest/types';

interface BacktestComparisonProps {
  runs: BacktestListItem[];
  onCompare: (runIds: string[]) => void;
  onCancel: () => void;
}

export function BacktestComparison({ runs, onCompare, onCancel }: BacktestComparisonProps) {
  const [selectedRuns, setSelectedRuns] = useState<string[]>([]);

  const toggleRun = (runId: string) => {
    setSelectedRuns((prev) =>
      prev.includes(runId)
        ? prev.filter((id) => id !== runId)
        : [...prev, runId]
    );
  };

  const formatPercent = (value?: number) => {
    if (value === undefined) return '-';
    return `${(value * 100).toFixed(2)}%`;
  };

  const formatCurrency = (value?: number) => {
    if (value === undefined) return '-';
    return new Intl.NumberFormat('en-IN', {
      style: 'currency',
      currency: 'INR',
      maximumFractionDigits: 0,
    }).format(value);
  };

  return (
    <Card className="p-6">
      <div className="flex items-center justify-between mb-4">
        <div>
          <h3 className="text-lg font-semibold flex items-center gap-2">
            <ArrowRightLeft className="w-5 h-5" />
            Compare Backtests
          </h3>
          <p className="text-sm text-muted-foreground">
            Select 2-4 backtests to compare side by side
          </p>
        </div>
        <Button variant="ghost" size="sm" onClick={onCancel}>
          <X className="w-4 h-4" />
        </Button>
      </div>

      <div className="space-y-2 max-h-96 overflow-y-auto mb-4">
        {runs.map((run) => (
          <div
            key={run.runId}
            onClick={() => toggleRun(run.runId)}
            className={`p-3 border rounded-lg cursor-pointer transition-colors ${
              selectedRuns.includes(run.runId)
                ? 'border-primary bg-primary/5'
                : 'hover:bg-accent'
            }`}
          >
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-3">
                <div
                  className={`w-5 h-5 rounded border flex items-center justify-center ${
                    selectedRuns.includes(run.runId)
                      ? 'bg-primary border-primary'
                      : 'border-muted-foreground'
                  }`}
                >
                  {selectedRuns.includes(run.runId) && (
                    <Check className="w-3 h-3 text-primary-foreground" />
                  )}
                </div>
                <div>
                  <p className="font-medium text-sm">{run.runId}</p>
                  <p className="text-xs text-muted-foreground">
                    {run.assetType} • {run.strategy} • {run.dateRange.start} to {run.dateRange.end}
                  </p>
                </div>
              </div>
              <div className="text-right">
                <p
                  className={`text-sm font-medium ${
                    (run.totalReturn || 0) >= 0 ? 'text-green-500' : 'text-red-500'
                  }`}
                >
                  {formatPercent(run.totalReturn)}
                </p>
                <p className="text-xs text-muted-foreground">
                  Sharpe: {run.sharpeRatio?.toFixed(2) || '-'}
                </p>
              </div>
            </div>
          </div>
        ))}
      </div>

      <div className="flex items-center justify-between">
        <Badge variant="secondary">
          {selectedRuns.length} selected
        </Badge>
        <div className="flex gap-2">
          <Button variant="outline" onClick={onCancel}>
            Cancel
          </Button>
          <Button
            onClick={() => onCompare(selectedRuns)}
            disabled={selectedRuns.length < 2 || selectedRuns.length > 4}
          >
            Compare ({selectedRuns.length})
          </Button>
        </div>
      </div>
    </Card>
  );
}

interface ComparisonTableProps {
  runs: {
    runId: string;
    assetType: string;
    strategy: string;
    metrics: {
      totalReturn?: number;
      cagr?: number;
      sharpeRatio?: number;
      maxDrawdown?: number;
      winRate?: number;
      profitFactor?: number;
    };
  }[];
}

export function ComparisonTable({ runs }: ComparisonTableProps) {
  const formatPercent = (value?: number) => {
    if (value === undefined) return '-';
    return `${(value * 100).toFixed(2)}%`;
  };

  const formatRatio = (value?: number) => {
    if (value === undefined) return '-';
    return value.toFixed(2);
  };

  const metrics = [
    { key: 'totalReturn', label: 'Total Return', format: formatPercent },
    { key: 'cagr', label: 'CAGR', format: formatPercent },
    { key: 'sharpeRatio', label: 'Sharpe Ratio', format: formatRatio },
    { key: 'maxDrawdown', label: 'Max Drawdown', format: formatPercent },
    { key: 'winRate', label: 'Win Rate', format: formatPercent },
    { key: 'profitFactor', label: 'Profit Factor', format: formatRatio },
  ];

  const getBestValue = (key: string) => {
    const values = runs.map((r) => r.metrics[key as keyof typeof r.metrics]).filter((v): v is number => v !== undefined);
    if (values.length === 0) return null;

    // For drawdown, lower is better
    if (key === 'maxDrawdown') {
      return Math.max(...values); // Less negative is better
    }
    return Math.max(...values);
  };

  return (
    <Card className="p-6 overflow-x-auto">
      <h3 className="text-lg font-semibold mb-4">Comparison Results</h3>

      <table className="w-full">
        <thead>
          <tr className="border-b">
            <th className="text-left py-2 px-4">Metric</th>
            {runs.map((run) => (
              <th key={run.runId} className="text-center py-2 px-4">
                <div>
                  <p className="text-xs text-muted-foreground">{run.runId}</p>
                  <p className="text-sm">{run.strategy}</p>
                </div>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {metrics.map((metric) => {
            const bestValue = getBestValue(metric.key);

            return (
              <tr key={metric.key} className="border-b">
                <td className="py-3 px-4 font-medium">{metric.label}</td>
                {runs.map((run) => {
                  const value = run.metrics[metric.key as keyof typeof run.metrics];
                  const isBest = value !== undefined && value === bestValue;

                  return (
                    <td
                      key={run.runId}
                      className={`text-center py-3 px-4 ${
                        isBest ? 'bg-green-500/10 font-semibold' : ''
                      }`}
                    >
                      {metric.format(value)}
                    </td>
                  );
                })}
              </tr>
            );
          })}
        </tbody>
      </table>
    </Card>
  );
}
