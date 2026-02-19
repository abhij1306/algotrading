'use client';

import { useState, useEffect } from 'react';
import { useParams, useRouter } from 'next/navigation';
import { ArrowLeft, BarChart3, List, Calculator, TrendingDown, Shuffle, Repeat } from 'lucide-react';
import { Card, Button, Skeleton, Badge } from '@/components/ui';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/Tabs';
import { BacktestResult } from '@/lib/backtest/types';
import {
  EquityCurveChart,
  DrawdownChart,
  MetricCard,
  MonthlyReturnsHeatmap,
  TradeList,
  MonteCarloChart,
  StatisticsPanel,
} from '@/components/backtest/results';

const TABS = [
  { id: 'overview', label: 'Overview', icon: BarChart3 },
  { id: 'trades', label: 'Trades', icon: List },
  { id: 'stats', label: 'Statistics', icon: Calculator },
  { id: 'drawdown', label: 'Drawdown', icon: TrendingDown },
  { id: 'monte-carlo', label: 'Monte Carlo', icon: Shuffle },
  { id: 'walk-forward', label: 'Walk Forward', icon: Repeat },
];

export default function BacktestResultsPage() {
  const params = useParams();
  const router = useRouter();
  const runId = params.runId as string;

  const [result, setResult] = useState<BacktestResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState('overview');

  useEffect(() => {
    async function loadResult() {
      try {
        setLoading(true);

        // Try to get from sessionStorage (set by backtest runner)
        const stored = sessionStorage.getItem(`backtest-${runId}`);
        if (stored) {
          setResult(JSON.parse(stored));
        } else {
        setError('Backtest result not found');
      }
      } catch (err) {
        setError('Failed to load backtest results');
        console.error(err);
      } finally {
        setLoading(false);
      }
    }

    loadResult();
  }, [runId]);

  const formatCurrency = (value: number) => {
    return new Intl.NumberFormat('en-IN', {
      style: 'currency',
      currency: 'INR',
      maximumFractionDigits: 0,
    }).format(value);
  };

  const formatPercent = (value: number) => {
    return `${(value * 100).toFixed(1)}%`;
  };

  const formatRatio = (value: number) => {
    return value.toFixed(2);
  };

  if (loading) {
    return (
      <div className="p-6 space-y-6">
        <Skeleton className="h-8 w-64" />
        <div className="grid grid-cols-4 gap-4">
          {[1, 2, 3, 4].map((i) => (
            <Skeleton key={i} className="h-32" />
          ))}
        </div>
        <Skeleton className="h-96" />
      </div>
    );
  }

  if (error || !result) {
    return (
      <div className="p-6">
        <Card className="p-8 text-center">
          <h2 className="text-xl font-semibold text-red-500 mb-2">Error</h2>
          <p className="text-foreground-secondary mb-4">{error || 'Failed to load results'}</p>
          <Button onClick={() => router.push('/backtest')}>
            <ArrowLeft className="w-4 h-4 mr-2" />
            Back to Backtests
          </Button>
        </Card>
      </div>
    );
  }

  const { metrics, config } = result;

  const metricsData: {
    title: string;
    value: string;
    subtitle: string;
    status: 'good' | 'warning' | 'danger' | 'neutral';
    tooltip: string;
  }[] = [
    {
      title: 'Total Return',
      value: formatPercent(metrics.totalReturn),
      subtitle: `${formatCurrency(config.initialCapital * (1 + metrics.totalReturn))} final`,
      status: metrics.totalReturn > 0 ? 'good' : metrics.totalReturn < 0 ? 'danger' : 'neutral',
      tooltip: 'Total return over the backtest period',
    },
    {
      title: 'CAGR',
      value: formatPercent(metrics.cagr),
      subtitle: 'Annualized return',
      status: metrics.cagr > 0.15 ? 'good' : metrics.cagr > 0.08 ? 'warning' : 'neutral',
      tooltip: 'Compound Annual Growth Rate',
    },
    {
      title: 'Sharpe Ratio',
      value: formatRatio(metrics.sharpeRatio),
      subtitle: metrics.sharpeRatio > 1 ? 'Good risk-adjusted return' : 'Below optimal',
      status: metrics.sharpeRatio > 1.5 ? 'good' : metrics.sharpeRatio > 1 ? 'warning' : 'neutral',
      tooltip: 'Risk-adjusted return measure',
    },
    {
      title: 'Max Drawdown',
      value: formatPercent(metrics.maxDrawdown),
      subtitle: `${metrics.maxDrawdownDuration} days to recover`,
      status: Math.abs(metrics.maxDrawdown) < 0.15 ? 'good' : Math.abs(metrics.maxDrawdown) < 0.25 ? 'warning' : 'danger',
      tooltip: 'Largest peak-to-trough decline',
    },
    {
      title: 'Win Rate',
      value: formatPercent(metrics.winRate),
      subtitle: `${metrics.winningTrades} / ${metrics.totalTrades} trades`,
      status: metrics.winRate > 0.55 ? 'good' : metrics.winRate > 0.45 ? 'warning' : 'neutral',
      tooltip: 'Percentage of winning trades',
    },
    {
      title: 'Profit Factor',
      value: formatRatio(metrics.profitFactor),
      subtitle: metrics.profitFactor > 1.5 ? 'Strong edge' : metrics.profitFactor > 1 ? 'Profitable' : 'Losing',
      status: metrics.profitFactor > 1.5 ? 'good' : metrics.profitFactor > 1 ? 'warning' : 'danger',
      tooltip: 'Gross profit / Gross loss',
    },
    {
      title: 'Calmar Ratio',
      value: formatRatio(metrics.calmarRatio),
      subtitle: 'Return / Max Drawdown',
      status: metrics.calmarRatio > 1 ? 'good' : metrics.calmarRatio > 0.5 ? 'warning' : 'neutral',
      tooltip: 'Return relative to maximum drawdown',
    },
    {
      title: 'Sortino Ratio',
      value: formatRatio(metrics.sortinoRatio),
      subtitle: 'Downside risk-adjusted return',
      status: metrics.sortinoRatio > 1.5 ? 'good' : metrics.sortinoRatio > 1 ? 'warning' : 'neutral',
      tooltip: 'Sharpe ratio using only downside deviation',
    },
  ];

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <div className="flex items-center gap-3 mb-2">
            <button
              onClick={() => router.push('/backtest')}
              className="p-2 rounded-lg transition-all text-foreground-secondary hover:bg-background-tertiary"
            >
              <ArrowLeft className="w-5 h-5" />
            </button>
            <h1 className="text-2xl font-semibold text-foreground">
              Backtest Results
            </h1>
            <span className="px-2 py-1 rounded text-xs font-medium bg-background-tertiary text-foreground-secondary border border-border">
              {runId}
            </span>
          </div>
          <p className="text-sm ml-12 text-foreground-secondary">
            {/* eslint-disable-next-line @typescript-eslint/no-explicit-any -- Dynamic config access */}
            {config.assetType.toUpperCase()} • {(config as any).strategy || 'Custom Strategy'} •{' '}
            {config.dateRange.start} to {config.dateRange.end}
          </p>
        </div>
        <div className="flex gap-2">
          <Button
            onClick={() => router.push('/backtest/new')}
            variant="primary"
          >
            New Backtest
          </Button>
        </div>
      </div>

      {/* Metrics Grid */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        {metricsData.map((metric) => (
          <MetricCard key={metric.title} {...metric} />
        ))}
      </div>

      {/* Tabs */}
      <Tabs value={activeTab} onValueChange={setActiveTab}>
        <TabsList className="grid grid-cols-6 w-full">
          {TABS.map((tab) => (
            <TabsTrigger key={tab.id} value={tab.id} className="flex items-center gap-2">
              <tab.icon className="w-4 h-4" />
              <span className="hidden sm:inline">{tab.label}</span>
            </TabsTrigger>
          ))}
        </TabsList>

        {/* Overview Tab */}
        <TabsContent value="overview" className="space-y-6">
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
            <div className="lg:col-span-2">
              <EquityCurveChart
                equityCurve={result.equityCurve}
                benchmarkCurve={result.benchmarkComparison?.equityCurve}
                initialCapital={result.config.initialCapital}
                height={400}
              />
            </div>
            <div>
              <MonthlyReturnsHeatmap returns={result.monthlyReturns} />
            </div>
          </div>

          {/* Benchmark Comparison */}
          {result.benchmarkComparison && (
            <Card className="p-4">
              <h3 className="text-sm font-semibold mb-4">Benchmark Comparison</h3>
              <div className="grid grid-cols-3 gap-4 text-center">
                <div>
                  <p className="text-xs text-foreground-secondary">Strategy Return</p>
                  <p className={`text-xl font-semibold ${metrics.totalReturn >= 0 ? 'text-green-500' : 'text-red-500'}`}>
                    {formatPercent(metrics.totalReturn)}
                  </p>
                </div>
                <div>
                  <p className="text-xs text-foreground-secondary">{result.benchmarkComparison.symbol} Return</p>
                  <p className={`text-xl font-semibold ${result.benchmarkComparison.totalReturn >= 0 ? 'text-green-500' : 'text-red-500'}`}>
                    {formatPercent(result.benchmarkComparison.totalReturn)}
                  </p>
                </div>
                <div>
                  <p className="text-xs text-foreground-secondary">Alpha</p>
                  <p className={`text-xl font-semibold ${(metrics.totalReturn - result.benchmarkComparison.totalReturn) >= 0 ? 'text-green-500' : 'text-red-500'}`}>
                    {formatPercent(metrics.totalReturn - result.benchmarkComparison.totalReturn)}
                  </p>
                </div>
              </div>
            </Card>
          )}
        </TabsContent>

        {/* Trades Tab */}
        <TabsContent value="trades">
          <TradeList trades={result.trades} />
        </TabsContent>

        {/* Statistics Tab */}
        <TabsContent value="stats">
          <StatisticsPanel result={result} />
        </TabsContent>

        {/* Drawdown Tab */}
        <TabsContent value="drawdown">
          <DrawdownChart equityCurve={result.equityCurve} height={400} />
        </TabsContent>

        {/* Monte Carlo Tab */}
        <TabsContent value="monte-carlo">
          {result.monteCarlo ? (
            <MonteCarloChart result={result.monteCarlo} height={400} />
          ) : (
            <Card className="p-8 text-center">
              <p className="text-foreground-secondary">Monte Carlo simulation not available</p>
            </Card>
          )}
        </TabsContent>

        {/* Walk Forward Tab */}
        <TabsContent value="walk-forward">
          {result.walkForward ? (
            <Card className="p-4">
              <div className="flex items-center justify-between mb-4">
                <div>
                  <h3 className="text-sm font-semibold">Walk-Forward Analysis</h3>
                  <p className="text-xs text-foreground-secondary">
                    {result.walkForward.windows.length} windows • {result.walkForward.isRobust ? 'Robust' : 'Not Robust'}
                  </p>
                </div>
                <Badge variant={result.walkForward.isRobust ? 'default' : 'secondary'}>
                  {result.walkForward.isRobust ? 'Robust' : 'Degraded'}
                </Badge>
              </div>

              <div className="grid grid-cols-3 gap-4 mb-6">
                <div className="text-center p-4 bg-background-tertiary rounded-lg">
                  <p className="text-xs text-foreground-secondary">Avg In-Sample Sharpe</p>
                  <p className="text-lg font-semibold">{result.walkForward.avgInSampleSharpe.toFixed(2)}</p>
                </div>
                <div className="text-center p-4 bg-background-tertiary rounded-lg">
                  <p className="text-xs text-foreground-secondary">Avg Out-of-Sample Sharpe</p>
                  <p className="text-lg font-semibold">{result.walkForward.avgOutOfSampleSharpe.toFixed(2)}</p>
                </div>
                <div className="text-center p-4 bg-background-tertiary rounded-lg">
                  <p className="text-xs text-foreground-secondary">Avg Degradation</p>
                  <p className="text-lg font-semibold text-yellow-500">
                      {(result.walkForward.avgDegradation * 100).toFixed(1)}%
                  </p>
                </div>
              </div>

              <div className="space-y-2">
                {result.walkForward.windows.map((window, i) => (
                  <div key={i} className="flex items-center justify-between p-3 border rounded-lg">
                    <span className="text-sm font-medium">Window {window.window}</span>
                    <div className="flex items-center gap-6">
                      <div className="text-right">
                        <p className="text-xs text-foreground-secondary">In-Sample</p>
                        <p className="text-sm font-medium">{window.inSampleSharpe.toFixed(2)}</p>
                      </div>
                      <div className="text-right">
                        <p className="text-xs text-foreground-secondary">Out-of-Sample</p>
                        <p className="text-sm font-medium">{window.outOfSampleSharpe.toFixed(2)}</p>
                      </div>
                      <Badge variant={window.degradation < 0.30 ? 'default' : 'secondary'} className="text-xs">
                        {(window.degradation * 100).toFixed(0)}% deg
                      </Badge>
                    </div>
                  </div>
                ))}
              </div>
            </Card>
          ) : (
            <Card className="p-8 text-center">
              <p className="text-foreground-secondary">Walk-forward analysis not available</p>
            </Card>
          )}
        </TabsContent>
      </Tabs>
    </div>
  );
}
