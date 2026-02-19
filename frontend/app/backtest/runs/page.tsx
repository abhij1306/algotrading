'use client';

import { useState, useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { Plus, ArrowLeft, Trash2, BarChart2 } from 'lucide-react';
import { Card, Button, Badge, Skeleton } from '@/components/ui';
import { BacktestListItem } from '@/lib/backtest/types';
import { backtestApi } from '@/lib/backtest/api';
import { BacktestComparison, ComparisonTable } from '@/components/backtest/results/BacktestComparison';

export default function BacktestRunsPage() {
  const router = useRouter();
  const [runs, setRuns] = useState<BacktestListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [comparing, setComparing] = useState(false);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- Complex comparison type
  const [comparisonRuns, setComparisonRuns] = useState<any[] | null>(null);

  useEffect(() => {
    loadRuns();
  }, []);

  async function loadRuns() {
    try {
      setLoading(true);
      const response = await backtestApi.listRuns({ limit: 50 });

      if (response.success && response.data) {
        // Map API response to our type
        const apiData = response.data as unknown as Array<{
          run_id: string;
          name: string;
          asset_type: string;
          strategy: string;
          start_date: string;
          end_date: string;
          initial_capital: number;
          final_capital: number;
          total_return: number;
          sharpe_ratio: number;
          max_drawdown: number;
          status: 'running' | 'completed' | 'failed';
          created_at: string;
        }>;

        setRuns(apiData.map((r) => ({
          runId: r.run_id,
          name: r.name,
          assetType: r.asset_type as 'stock' | 'option' | 'index',
          strategy: r.strategy,
          dateRange: { start: r.start_date, end: r.end_date },
          initialCapital: r.initial_capital,
          finalCapital: r.final_capital,
          totalReturn: r.total_return,
          sharpeRatio: r.sharpe_ratio,
          maxDrawdown: r.max_drawdown,
          status: r.status,
          createdAt: r.created_at,
        })));
      } else {
        setError(response.error || 'Failed to load runs');
      }
    } catch {
      setError('Failed to load backtest runs');
    } finally {
      setLoading(false);
    }
  }

  async function handleDelete(runId: string) {
    if (!confirm(`Delete backtest ${runId}?`)) return;

    const response = await backtestApi.deleteRun(runId);
    if (response.success) {
      setRuns(runs.filter((r) => r.runId !== runId));
    } else {
      alert('Failed to delete: ' + response.error);
    }
  }

  async function handleCompare(runIds: string[]) {
    // Fetch full details for comparison
    const details = await Promise.all(
      runIds.map(async (id) => {
        const response = await backtestApi.getResults(id);
        if (response.success && response.data) {
          return {
            runId: id,
            assetType: response.data.config?.assetType || 'unknown',
            strategy: response.data.config?.assetType || 'unknown',
            metrics: response.data.metrics || {},
          };
        }
        return null;
      })
    );

    setComparisonRuns(details.filter(Boolean));
    setComparing(false);
  }

  const formatPercent = (value?: number) => {
    if (value === undefined) return '-';
    return `${(value * 100).toFixed(2)}%`;
  };

  if (loading) {
    return (
      <div className="p-6 space-y-4">
        <Skeleton className="h-8 w-64" />
        {[1, 2, 3].map((i) => (
          <Skeleton key={i} className="h-24" />
        ))}
      </div>
    );
  }

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-4">
          <Button variant="ghost" size="sm" onClick={() => router.push('/backtest')}>
            <ArrowLeft className="w-4 h-4" />
          </Button>
          <div>
            <h1 className="text-2xl font-semibold">Backtest Runs</h1>
            <p className="text-sm text-foreground-secondary">
              {runs.length} runs • {runs.filter((r) => r.status === 'completed').length} completed
            </p>
          </div>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" onClick={() => setComparing(true)}>
            <BarChart2 className="w-4 h-4 mr-2" />
            Compare
          </Button>
          <Button onClick={() => router.push('/backtest/new')}>
            <Plus className="w-4 h-4 mr-2" />
            New Backtest
          </Button>
        </div>
      </div>

      {/* Comparison Mode */}
      {comparing && (
        <BacktestComparison
          runs={runs}
          onCompare={handleCompare}
          onCancel={() => setComparing(false)}
        />
      )}

      {/* Comparison Results */}
      {comparisonRuns && (
        <div className="space-y-4">
          <div className="flex items-center justify-between">
            <h2 className="text-lg font-semibold">Comparison Results</h2>
            <Button variant="ghost" size="sm" onClick={() => setComparisonRuns(null)}>
              Clear
            </Button>
          </div>
          <ComparisonTable runs={comparisonRuns} />
        </div>
      )}

      {/* Error */}
      {error && (
        <Card className="p-4 bg-red-500/10 border-red-500/30">
          <p className="text-red-500">{error}</p>
          <Button variant="outline" size="sm" onClick={loadRuns} className="mt-2">
            Retry
          </Button>
        </Card>
      )}

      {/* Runs List */}
      <div className="space-y-3">
        {runs.map((run) => (
          <Card
            key={run.runId}
            className="p-4 hover:border-primary/50 transition-colors cursor-pointer"
            onClick={() => router.push(`/backtest/results/${run.runId}`)}
          >
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-4">
                <div>
                  <div className="flex items-center gap-2">
                    <h3 className="font-semibold">{run.runId}</h3>
                    <Badge
                      variant={
                        run.status === 'completed'
                          ? 'default'
                          : run.status === 'running'
                          ? 'secondary'
                          : 'loss'
                      }
                    >
                      {run.status}
                    </Badge>
                  </div>
                  <p className="text-sm text-foreground-secondary">
                    {run.assetType} • {run.strategy} • {run.dateRange.start} to {run.dateRange.end}
                  </p>
                </div>
              </div>

              <div className="flex items-center gap-6">
                {/* Metrics */}
                {run.status === 'completed' && (
                  <div className="flex items-center gap-6 text-right">
                    <div>
                      <p className="text-xs text-foreground-secondary">Return</p>
                      <p
                        className={`font-medium ${
                          (run.totalReturn || 0) >= 0 ? 'text-green-500' : 'text-red-500'
                        }`}
                      >
                        {formatPercent(run.totalReturn)}
                      </p>
                    </div>
                    <div>
                      <p className="text-xs text-foreground-secondary">Sharpe</p>
                      <p className="font-medium">{run.sharpeRatio?.toFixed(2) || '-'}</p>
                    </div>
                    <div>
                      <p className="text-xs text-foreground-secondary">Max DD</p>
                      <p className="font-medium text-red-500">
                        {formatPercent(run.maxDrawdown)}
                      </p>
                    </div>
                  </div>
                )}

                {/* Actions */}
                <div className="flex items-center gap-2" onClick={(e) => e.stopPropagation()}>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => handleDelete(run.runId)}
                  >
                    <Trash2 className="w-4 h-4 text-red-500" />
                  </Button>
                </div>
              </div>
            </div>
          </Card>
        ))}
      </div>

      {/* Empty State */}
      {runs.length === 0 && !loading && !error && (
        <Card className="p-8 text-center">
          <p className="text-foreground-secondary mb-4">No backtest runs yet</p>
          <Button onClick={() => router.push('/backtest/new')}>
            <Plus className="w-4 h-4 mr-2" />
            Run Your First Backtest
          </Button>
        </Card>
      )}
    </div>
  );
}
