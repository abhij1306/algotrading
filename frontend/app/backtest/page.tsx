'use client';

import { useRouter } from 'next/navigation';
import { useState, useEffect, memo } from 'react';
import { Plus, Clock, CheckCircle2, XCircle, ChevronRight, BarChart3, Loader2 } from 'lucide-react';
import { Button } from '@/components/ui';
import { apiClient } from '@/lib/api-client';

interface BacktestRun {
  id: string;
  name: string;
  status: 'completed' | 'running' | 'failed' | 'pending';
  created_at: string;
  total_return?: number;
  sharpe_ratio?: number;
  max_drawdown?: number;
}

const StatusIcon = memo(function StatusIcon({ status }: { status: string }) {
  switch (status) {
    case 'completed': return <CheckCircle2 className="w-4 h-4 text-profit" />;
    case 'running': return <Loader2 className="w-4 h-4 text-primary animate-spin" />;
    case 'failed': return <XCircle className="w-4 h-4 text-loss" />;
    default: return <Clock className="w-4 h-4 text-foreground-muted" />;
  }
});

function formatDate(dateStr: string) {
  const date = new Date(dateStr);
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffMins = Math.floor(diffMs / 60000);
  const diffHours = Math.floor(diffMs / 3600000);
  const diffDays = Math.floor(diffMs / 86400000);

  if (diffMins < 60) return `${diffMins}m ago`;
  if (diffHours < 24) return `${diffHours}h ago`;
  if (diffDays < 7) return `${diffDays}d ago`;
  return date.toLocaleDateString('en-IN', { month: 'short', day: 'numeric' });
}

export default function BacktestPage() {
  const router = useRouter();
  const [recentBacktests, setRecentBacktests] = useState<BacktestRun[]>([]);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    const fetchBacktests = async () => {
      setIsLoading(true);
      const res = await apiClient.get('/api/backtest/runs?limit=10');
      if (res.data) {
        setRecentBacktests(res.data.runs || []);
      }
      setIsLoading(false);
    };
    fetchBacktests();
  }, []);

  return (
    <div className="flex flex-col h-screen">
      {/* Header */}
      <header className="flex items-center justify-between px-4 py-2 border-b border-border bg-surface">
        <h1 className="text-base font-semibold">Backtest</h1>
        <Button onClick={() => router.push('/backtest/new')} size="sm">
          <Plus className="w-4 h-4 mr-1" />
          New
        </Button>
      </header>

      {/* Content */}
      <div className="flex-1 overflow-auto">
        {isLoading ? (
          <div className="flex items-center justify-center h-full">
            <Loader2 className="w-5 h-5 animate-spin text-foreground-muted" />
          </div>
        ) : recentBacktests.length === 0 ? (
          <div className="flex items-center justify-center h-full">
            <div className="text-center">
              <BarChart3 className="w-10 h-10 mx-auto text-foreground-muted mb-3" />
              <h2 className="text-base font-medium mb-1">No Backtests</h2>
              <p className="text-sm text-foreground-muted mb-4">Create your first backtest</p>
              <Button onClick={() => router.push('/backtest/new')} size="sm">
                <Plus className="w-4 h-4 mr-1" />
                New Backtest
              </Button>
            </div>
          </div>
        ) : (
          <table className="w-full">
            <thead className="text-xs text-foreground-muted border-b border-border bg-surface">
              <tr>
                <th className="py-2 pl-4 text-left font-normal">Backtest</th>
                <th className="py-2 text-right font-normal">Return</th>
                <th className="py-2 text-right font-normal">Sharpe</th>
                <th className="py-2 text-right font-normal">Max DD</th>
                <th className="py-2 pr-4"></th>
              </tr>
            </thead>
            <tbody className="text-sm">
              {recentBacktests.map((backtest) => (
                <tr
                  key={backtest.id}
                  onClick={() => router.push(`/backtest/results/${backtest.id}`)}
                  className="cursor-pointer hover:bg-surface transition-colors border-b border-border"
                >
                  <td className="py-2.5 pl-4">
                    <div className="flex items-center gap-2">
                      <StatusIcon status={backtest.status} />
                      <div>
                        <div className="font-medium">{backtest.name}</div>
                        <div className="text-xs text-foreground-muted">
                          {backtest.id} • {backtest.status} • {formatDate(backtest.created_at)}
                        </div>
                      </div>
                    </div>
                  </td>
                  {backtest.status === 'completed' ? (
                    <>
                      <td className={`py-2.5 text-right tabular-nums ${(backtest.total_return ?? 0) >= 0 ? 'text-profit' : 'text-loss'}`}>
                        {(backtest.total_return ?? 0) >= 0 ? '+' : ''}{(backtest.total_return ?? 0).toFixed(1)}%
                      </td>
                      <td className="py-2.5 text-right tabular-nums">
                        {(backtest.sharpe_ratio ?? 0).toFixed(2)}
                      </td>
                      <td className="py-2.5 text-right tabular-nums text-loss">
                        {(backtest.max_drawdown ?? 0).toFixed(1)}%
                      </td>
                    </>
                  ) : (
                    <>
                      <td className="py-2.5 text-right text-foreground-muted">--</td>
                      <td className="py-2.5 text-right text-foreground-muted">--</td>
                      <td className="py-2.5 text-right text-foreground-muted">--</td>
                    </>
                  )}
                  <td className="py-2.5 pr-4 text-right">
                    <ChevronRight className="w-4 h-4 text-foreground-muted inline" />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* Footer */}
      {!isLoading && recentBacktests.length > 0 && (
        <div className="px-4 py-2 border-t border-border bg-surface">
          <Button onClick={() => router.push('/backtest/runs')} variant="ghost" size="sm" className="w-full">
            View All <ChevronRight className="w-4 h-4 ml-1" />
          </Button>
        </div>
      )}
    </div>
  );
}
