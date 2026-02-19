'use client';

import { useEffect, useMemo, useState } from 'react';
import { useParams, useRouter } from 'next/navigation';
import { Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { Badge, Button, Card, CardContent, CardHeader, CardTitle } from '@/components/ui';
import { apiClient } from '@/lib/api-client';

type CurvePoint = { date: string; equity?: number; drawdown_pct?: number };

type BacktestJobResponse = {
  job_id: string;
  status: 'running' | 'completed' | 'failed';
  created_at: string;
  error?: string;
  params: {
    name?: string;
    instrument_type: string;
    selection: { mode: string; universe?: string; symbols?: string[] };
    start_date: string;
    end_date: string;
    initial_capital: number;
    strategies: Array<{ strategy_id: string; weight: number }>;
  };
  result?: {
    instrument_type: string;
    selection: { mode: string; scope: string };
    date_range: { start: string; end: string };
    metrics: {
      initial_capital: number;
      final_equity: number;
      total_return_pct: number;
      sharpe_ratio: number;
      max_drawdown_pct: number;
      total_trades: number;
      win_rate_pct: number;
    };
    equity_curve: Array<{ date: string; equity: number }>;
    benchmark_curve: Array<{ date: string; equity: number }>;
    drawdown_curve: Array<{ date: string; drawdown_pct: number }>;
    strategy_curves: Array<{ strategy_id: string; name: string; weight: number; equity_curve: Array<{ date: string; equity: number }> }>;
    trade_log: Array<{
      symbol: string;
      entry_date: string;
      exit_date: string;
      entry_price: number;
      exit_price: number;
      return_pct: number;
    }>;
  };
};

function MetricCard({ label, value }: { label: string; value: string }) {
  return (
    <Card>
      <CardContent className="p-3">
        <div className="text-xs text-foreground-secondary">{label}</div>
        <div className="text-lg font-semibold mt-1">{value}</div>
      </CardContent>
    </Card>
  );
}

export default function BacktestResultPage() {
  const params = useParams<{ runId: string }>();
  const router = useRouter();
  const runId = params.runId;
  const [payload, setPayload] = useState<BacktestJobResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    const poll = async () => {
      const res = await apiClient.get<BacktestJobResponse>(`/api/backtest/result/${runId}`);
      if (!active) return;
      if (res.error) {
        setError(res.error.message);
        return;
      }
      const data = res.data ?? null;
      setPayload(data);
      if (data?.status !== 'running') {
        return;
      }
    };

    poll();
    const id = setInterval(poll, 1200);
    return () => {
      active = false;
      clearInterval(id);
    };
  }, [runId]);

  const chartRows = useMemo(() => {
    const equity = payload?.result?.equity_curve ?? [];
    const benchmark = payload?.result?.benchmark_curve ?? [];
    const benchMap = new Map(benchmark.map((b) => [b.date, b.equity]));
    return equity.map((e) => ({
      date: e.date,
      equity: e.equity,
      benchmark: benchMap.get(e.date) ?? null,
    }));
  }, [payload]);

  const drawdownRows: CurvePoint[] = payload?.result?.drawdown_curve ?? [];
  const trades = payload?.result?.trade_log ?? [];
  const metrics = payload?.result?.metrics;

  return (
    <div className="p-6 space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">Backtest Result</h1>
          <div className="text-xs text-foreground-secondary mt-1 font-mono">{runId}</div>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" onClick={() => router.push('/backtest/new')}>New Run</Button>
          <Button variant="outline" onClick={() => router.push('/backtest')}>Back</Button>
        </div>
      </div>

      {error && <div className="text-sm text-loss">{error}</div>}

      {payload && (
        <Card>
          <CardContent className="p-4 flex flex-wrap items-center gap-3 text-sm">
            <Badge variant={payload.status === 'completed' ? 'profit' : payload.status === 'failed' ? 'loss' : 'outline'}>
              {payload.status}
            </Badge>
            <span className="text-foreground-secondary">Instrument:</span>
            <span>{payload.params.instrument_type.toUpperCase()}</span>
            <span className="text-foreground-secondary">Range:</span>
            <span>{payload.params.start_date} to {payload.params.end_date}</span>
            <span className="text-foreground-secondary">Selection:</span>
            <span>{payload.result?.selection.scope || payload.params.selection.mode}</span>
          </CardContent>
        </Card>
      )}

      {payload?.status === 'running' && (
        <div className="text-sm text-foreground-secondary">Backtest executing...</div>
      )}

      {payload?.status === 'failed' && (
        <Card>
          <CardContent className="p-4 text-sm text-loss">
            {payload.error || 'Backtest failed'}
          </CardContent>
        </Card>
      )}

      {payload?.status === 'completed' && metrics && (
        <>
          <div className="grid gap-3 md:grid-cols-3 lg:grid-cols-6">
            <MetricCard label="Total Return" value={`${metrics.total_return_pct.toFixed(2)}%`} />
            <MetricCard label="Final Equity" value={`₹${metrics.final_equity.toFixed(2)}`} />
            <MetricCard label="Sharpe" value={metrics.sharpe_ratio.toFixed(2)} />
            <MetricCard label="Max Drawdown" value={`${metrics.max_drawdown_pct.toFixed(2)}%`} />
            <MetricCard label="Trades" value={`${metrics.total_trades}`} />
            <MetricCard label="Win Rate" value={`${metrics.win_rate_pct.toFixed(2)}%`} />
          </div>

          <Card>
            <CardHeader><CardTitle className="text-base">Equity Curve vs Benchmark</CardTitle></CardHeader>
            <CardContent className="h-72">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={chartRows}>
                  <XAxis dataKey="date" tick={{ fontSize: 11 }} />
                  <YAxis tick={{ fontSize: 11 }} />
                  <Tooltip />
                  <Line type="monotone" dataKey="equity" stroke="var(--color-primary)" dot={false} strokeWidth={2} />
                  <Line type="monotone" dataKey="benchmark" stroke="var(--color-foreground-tertiary)" dot={false} strokeWidth={1.5} />
                </LineChart>
              </ResponsiveContainer>
            </CardContent>
          </Card>

          <Card>
            <CardHeader><CardTitle className="text-base">Drawdown Curve</CardTitle></CardHeader>
            <CardContent className="h-56">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={drawdownRows}>
                  <XAxis dataKey="date" tick={{ fontSize: 11 }} />
                  <YAxis tick={{ fontSize: 11 }} />
                  <Tooltip />
                  <Line type="monotone" dataKey="drawdown_pct" stroke="var(--color-loss)" dot={false} strokeWidth={2} />
                </LineChart>
              </ResponsiveContainer>
            </CardContent>
          </Card>

          <Card>
            <CardHeader><CardTitle className="text-base">Trade Log (Top 100)</CardTitle></CardHeader>
            <CardContent>
              {trades.length === 0 ? (
                <div className="text-sm text-foreground-secondary">No trades generated for selected range/strategies.</div>
              ) : (
                <div className="overflow-auto max-h-96">
                  <table className="w-full text-xs">
                    <thead>
                      <tr className="text-left text-foreground-secondary">
                        <th className="py-1 pr-2">Symbol</th>
                        <th className="py-1 pr-2">Entry</th>
                        <th className="py-1 pr-2">Exit</th>
                        <th className="py-1 pr-2">Entry Px</th>
                        <th className="py-1 pr-2">Exit Px</th>
                        <th className="py-1 pr-2">Return %</th>
                      </tr>
                    </thead>
                    <tbody>
                      {trades.slice(0, 100).map((t, i) => (
                        <tr key={`${t.symbol}-${t.entry_date}-${i}`} className="border-t border-border">
                          <td className="py-1 pr-2">{t.symbol}</td>
                          <td className="py-1 pr-2">{t.entry_date}</td>
                          <td className="py-1 pr-2">{t.exit_date}</td>
                          <td className="py-1 pr-2">{t.entry_price.toFixed(2)}</td>
                          <td className="py-1 pr-2">{t.exit_price.toFixed(2)}</td>
                          <td className={`py-1 pr-2 ${t.return_pct >= 0 ? 'text-profit' : 'text-loss'}`}>
                            {t.return_pct.toFixed(2)}%
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
