'use client';

import { useEffect, useMemo, useState } from 'react';
import { useRouter } from 'next/navigation';
import { Badge, Button, Card, CardContent, CardHeader, CardTitle } from '@/components/ui';
import { apiClient } from '@/lib/api-client';

type BacktestStatus = {
  data_ready: boolean;
  instrument_capabilities: Record<string, { enabled: boolean; note: string }>;
  universe_ranges: Record<string, { available: boolean; min_date: string | null; max_date: string | null; rows: number }>;
  stock_range: { available: boolean; min_date: string | null; max_date: string | null; rows: number };
  supported_strategies: Array<{ id: string; name: string }>;
};

type BacktestRunItem = {
  job_id: string;
  status: 'running' | 'completed' | 'failed';
  created_at: string;
  params: {
    name?: string;
    instrument_type?: string;
    start_date?: string;
    end_date?: string;
    selection?: { mode?: string; universe?: string };
    strategies?: Array<{ strategy_id: string; weight: number }>;
  };
};

export default function BacktestPage() {
  const router = useRouter();
  const [status, setStatus] = useState<BacktestStatus | null>(null);
  const [runs, setRuns] = useState<BacktestRunItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const load = async () => {
      setLoading(true);
      setError(null);
      const [statusRes, runsRes] = await Promise.all([
        apiClient.get<BacktestStatus>('/api/backtest/status'),
        apiClient.get<{ runs: BacktestRunItem[] }>('/api/backtest/runs'),
      ]);

      if (statusRes.error) {
        setError(statusRes.error.message);
      } else {
        setStatus(statusRes.data ?? null);
      }

      if (!runsRes.error && runsRes.data?.runs) {
        setRuns(runsRes.data.runs.slice(0, 10));
      }
      setLoading(false);
    };
    load();
  }, []);

  const universes = useMemo(() => Object.entries(status?.universe_ranges ?? {}), [status]);

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">Backtest Lab</h1>
          <p className="text-sm text-foreground-secondary mt-1">
            Build and run single or portfolio strategy backtests across universe/symbol scopes.
          </p>
        </div>
        <Button onClick={() => router.push('/backtest/new')}>New Run</Button>
      </div>

      {loading && <div className="text-sm text-foreground-secondary">Loading backtest capabilities...</div>}
      {error && <div className="text-sm text-loss">{error}</div>}

      {!loading && status && (
        <>
          <div className="grid gap-4 md:grid-cols-3">
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm">Equity Capability</CardTitle>
              </CardHeader>
              <CardContent className="text-sm">
                <Badge variant={status.instrument_capabilities.equity?.enabled ? 'default' : 'outline'}>
                  {status.instrument_capabilities.equity?.enabled ? 'Enabled' : 'Disabled'}
                </Badge>
                <div className="mt-2 text-foreground-secondary">{status.instrument_capabilities.equity?.note}</div>
              </CardContent>
            </Card>

            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm">Options Capability</CardTitle>
              </CardHeader>
              <CardContent className="text-sm">
                <Badge variant={status.instrument_capabilities.options?.enabled ? 'default' : 'outline'}>
                  {status.instrument_capabilities.options?.enabled ? 'Enabled' : 'Blocked'}
                </Badge>
                <div className="mt-2 text-foreground-secondary">{status.instrument_capabilities.options?.note}</div>
              </CardContent>
            </Card>

            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm">Stock Snapshot Range</CardTitle>
              </CardHeader>
              <CardContent className="text-sm">
                {status.stock_range.available ? (
                  <div>
                    <div>{status.stock_range.min_date} to {status.stock_range.max_date}</div>
                    <div className="text-foreground-secondary mt-1">{status.stock_range.rows} rows</div>
                  </div>
                ) : (
                  <div className="text-foreground-secondary">Unavailable</div>
                )}
              </CardContent>
            </Card>
          </div>

          <Card>
            <CardHeader>
              <CardTitle className="text-base">Universe Coverage</CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              {universes.length === 0 ? (
                <div className="text-sm text-foreground-secondary">No universe snapshots available.</div>
              ) : (
                universes.map(([name, meta]) => (
                  <div key={name} className="flex items-center justify-between border border-border rounded px-3 py-2 text-sm">
                    <div className="font-medium">{name}</div>
                    <div className="text-foreground-secondary">
                      {meta.available ? `${meta.min_date} to ${meta.max_date} (${meta.rows} rows)` : 'Unavailable'}
                    </div>
                  </div>
                ))
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="text-base">Strategy Catalog</CardTitle>
            </CardHeader>
            <CardContent className="flex flex-wrap gap-2">
              {status.supported_strategies.map((s) => (
                <Badge key={s.id} variant="outline">{s.name}</Badge>
              ))}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="text-base">Recent Runs</CardTitle>
            </CardHeader>
            <CardContent>
              {runs.length === 0 ? (
                <div className="text-sm text-foreground-secondary">No runs yet.</div>
              ) : (
                <div className="overflow-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="text-left text-foreground-secondary">
                        <th className="py-2 pr-2">Run</th>
                        <th className="py-2 pr-2">Status</th>
                        <th className="py-2 pr-2">Instrument</th>
                        <th className="py-2 pr-2">Range</th>
                        <th className="py-2 pr-2">Scope</th>
                        <th className="py-2 pr-2"></th>
                      </tr>
                    </thead>
                    <tbody>
                      {runs.map((r) => (
                        <tr key={r.job_id} className="border-t border-border">
                          <td className="py-2 pr-2 font-mono text-xs">{r.job_id}</td>
                          <td className="py-2 pr-2">
                            <Badge variant={r.status === 'completed' ? 'profit' : r.status === 'failed' ? 'loss' : 'outline'}>
                              {r.status}
                            </Badge>
                          </td>
                          <td className="py-2 pr-2">{(r.params.instrument_type || 'equity').toUpperCase()}</td>
                          <td className="py-2 pr-2">{r.params.start_date} to {r.params.end_date}</td>
                          <td className="py-2 pr-2">
                            {r.params.selection?.mode === 'symbols' ? 'Symbols' : (r.params.selection?.universe || 'NIFTY50')}
                          </td>
                          <td className="py-2 pr-2 text-right">
                            <Button size="sm" variant="outline" onClick={() => router.push(`/backtest/results/${r.job_id}`)}>
                              View
                            </Button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}
