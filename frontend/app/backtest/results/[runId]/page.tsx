'use client';

import { useEffect, useMemo, useState, memo } from 'react';
import { useParams, useRouter } from 'next/navigation';
import { Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import {
  Badge,
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  PageContainer,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui';
import { apiClient } from '@/lib/api-client';

type CurvePoint = { date: string; equity?: number; drawdown_pct?: number };

type BacktestJobResponse = {
  job_id: string;
  status: 'running' | 'completed' | 'failed';
  created_at: string;
  error?: string;
  params: {
    instrument_type: string;
    selection: { mode: string; universe?: string; symbols?: string[] };
    start_date: string;
    end_date: string;
  };
  result?: {
    selection: { scope: string };
    metrics: {
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

function formatRupeeLakh(value: number): string {
  if (!Number.isFinite(value)) return '--';
  if (value >= 100000) return `₹${(value / 100000).toFixed(2)}L`;
  return `₹${value.toLocaleString('en-IN', { maximumFractionDigits: 2 })}`;
}

// OPTIMIZATION: Memoized chart components to prevent unnecessary re-renders
const EquityChart = memo(function EquityChart({ data }: { data: Array<{ date: string; equity: number; benchmark: number | null }> }) {
  if (data.length === 0) {
    return (
      <div className="flex h-full items-center justify-center rounded-md border border-border bg-background-secondary text-sm text-foreground-muted">
        No equity curve data available for this run.
      </div>
    );
  }

  return (
    <ResponsiveContainer width="100%" height="100%">
      <LineChart
        data={data}
        margin={{ top: 10, right: 30, left: 60, bottom: 40 }}
      >
        <XAxis
          dataKey="date"
          height={60}
          angle={-45}
          textAnchor="end"
          stroke="var(--color-foreground-muted)"
          tick={{ fill: 'var(--color-foreground-muted)', fontSize: 10 }}
          axisLine={{ stroke: 'var(--color-border)' }}
          tickLine={{ stroke: 'var(--color-border)' }}
        />
        <YAxis
          width={60}
          stroke="var(--color-foreground-muted)"
          tick={{ fill: 'var(--color-foreground-secondary)', fontSize: 11 }}
          axisLine={{ stroke: 'var(--color-border)' }}
          tickLine={{ stroke: 'var(--color-border)' }}
        />
        <Tooltip
          contentStyle={{
            backgroundColor: 'var(--color-elevated)',
            border: '1px solid var(--color-border)',
            borderRadius: 'var(--radius-md)',
            color: 'var(--color-foreground)',
            fontSize: '12px'
          }}
        />
        <Line
          type="monotone"
          dataKey="equity"
          stroke="var(--color-primary)"
          strokeWidth={2.5}
          dot={false}
          name="Portfolio"
        />
        <Line
          type="monotone"
          dataKey="benchmark"
          stroke="var(--color-foreground-muted)"
          strokeWidth={1.8}
          strokeDasharray="5 5"
          dot={false}
          name="Benchmark"
        />
      </LineChart>
    </ResponsiveContainer>
  );
});

const DrawdownChart = memo(function DrawdownChart({ data }: { data: CurvePoint[] }) {
  if (data.length === 0) {
    return (
      <div className="flex h-full items-center justify-center rounded-md border border-border bg-background-secondary text-xs text-foreground-muted">
        No drawdown data
      </div>
    );
  }

  return (
    <ResponsiveContainer width="100%" height="100%">
      <LineChart data={data}>
        <XAxis dataKey="date" tick={{ fontSize: 10, fill: 'var(--color-foreground-muted)' }} hide />
        <YAxis tick={{ fontSize: 10, fill: 'var(--color-foreground-secondary)' }} width={40} />
        <Tooltip contentStyle={{ backgroundColor: 'var(--color-surface)', border: '1px solid var(--color-border)', borderRadius: 'var(--radius-md)', fontSize: '11px' }} />
        <Line type="monotone" dataKey="drawdown_pct" stroke="var(--color-loss)" dot={false} strokeWidth={2} />
      </LineChart>
    </ResponsiveContainer>
  );
});

export default function BacktestResultPage() {
  const params = useParams<{ runId: string }>();
  const router = useRouter();
  const runId = params.runId;
  const [payload, setPayload] = useState<BacktestJobResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    let intervalId: NodeJS.Timeout | null = null;
    let errorCount = 0;

    const poll = async () => {
      const res = await apiClient.get<BacktestJobResponse>(`/api/backtest/result/${runId}`);
      if (!active) return;

      if (res.error) {
        errorCount++;
        setError(res.error.message);
        // Stop polling after 3 consecutive errors (e.g., 404 not found)
        if (errorCount >= 3 && intervalId) {
          clearInterval(intervalId);
        }
        return;
      }

      // Reset error count on success
      errorCount = 0;
      const data = res.data ?? null;
      setPayload(data);

      // Stop polling if backtest is completed or failed
      if (data && (data.status === 'completed' || data.status === 'failed')) {
        if (intervalId) clearInterval(intervalId);
      }
    };

    void poll();
    intervalId = setInterval(() => void poll(), 1200);

    return () => {
      active = false;
      if (intervalId) clearInterval(intervalId);
    };
  }, [runId]);

  // OPTIMIZATION: Memoize chart data to prevent recalculation on every render
  const chartRows = useMemo(() => {
    const eq = payload?.result?.equity_curve ?? [];
    const benchmark = payload?.result?.benchmark_curve ?? [];
    const benchMap = new Map(benchmark.map((b) => [b.date, b.equity]));
    const rows = eq.map((e) => ({ date: e.date, equity: e.equity, benchmark: benchMap.get(e.date) ?? null }));

    // Sample data to show max 200 points for cleaner chart
    const sampleRate = Math.max(1, Math.floor(rows.length / 200));
    const sampled = rows.filter((_, i) => i % sampleRate === 0);

    return sampled;
  }, [payload?.result?.equity_curve, payload?.result?.benchmark_curve]);

  // OPTIMIZATION: Memoize drawdown data
  const drawdownRows = useMemo(() => 
    payload?.result?.drawdown_curve ?? [], 
    [payload?.result?.drawdown_curve]
  );

  // OPTIMIZATION: Memoize trades and metrics
  const trades = useMemo(() => 
    payload?.result?.trade_log ?? [], 
    [payload?.result?.trade_log]
  );
  
  const metrics = useMemo(() => 
    payload?.result?.metrics, 
    [payload?.result?.metrics]
  );
  
  const winRate = metrics?.win_rate_pct ?? 0;

  return (
    <PageContainer fullWidth>
      {/* CACHE BUSTER v3 */}
      <div className="flex h-full w-full flex-col overflow-hidden bg-background">
        <div className="flex h-12 shrink-0 items-center justify-between border-b border-border bg-surface px-4">
          <div className="flex items-center gap-2 text-sm">
            <span className="text-foreground-secondary">Backtest</span>
            <span className="text-foreground-muted">/</span>
            <span className="font-semibold text-foreground">Results</span>
            <Badge variant="outline" className="ml-2">{runId}</Badge>
          </div>
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" onClick={() => router.push('/backtest')}>← Back</Button>
            <Button variant="primary" size="sm" onClick={() => router.push('/backtest')}>+ New Run</Button>
          </div>
        </div>

        {error && <div className="bg-loss-bg p-4 text-sm text-loss">{error}</div>}

        {payload?.status === 'running' && (
          <div className="p-4">
            <Card><CardContent className="p-4 text-sm text-foreground-secondary">Backtest executing...</CardContent></Card>
          </div>
        )}

        {payload?.status === 'failed' && (
          <div className="p-4">
            <Card variant="outline" className="border-loss bg-loss-bg"><CardContent className="p-4 text-sm text-loss">{payload.error || 'Backtest failed'}</CardContent></Card>
          </div>
        )}

        {payload?.status === 'completed' && (
          <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
            <div className="grid h-16 shrink-0 grid-cols-2 border-b border-border bg-surface md:grid-cols-3 xl:grid-cols-6">
              <div className="border-r border-border px-4 py-2">
                <div className="text-xxs uppercase tracking-wider text-foreground-muted">Total Return</div>
                <div className={`mono-num text-2xl font-semibold ${(metrics?.total_return_pct ?? 0) >= 0 ? 'text-profit' : 'text-loss'}`}>
                  {metrics ? `${metrics.total_return_pct.toFixed(1)}%` : '--'}
                </div>
              </div>
              <div className="border-r border-border px-4 py-2">
                <div className="text-xxs uppercase tracking-wider text-foreground-muted">Final Equity</div>
                <div className="mono-num text-2xl font-semibold text-foreground">{metrics ? formatRupeeLakh(metrics.final_equity) : '--'}</div>
              </div>
              <div className="border-r border-border px-4 py-2">
                <div className="text-xxs uppercase tracking-wider text-foreground-muted">Sharpe</div>
                <div className={`mono-num text-2xl font-semibold ${(metrics?.sharpe_ratio ?? 0) >= 1 ? 'text-profit' : 'text-foreground'}`}>
                  {metrics ? metrics.sharpe_ratio.toFixed(2) : '--'}
                </div>
              </div>
              <div className="border-r border-border px-4 py-2">
                <div className="text-xxs uppercase tracking-wider text-foreground-muted">Max Drawdown</div>
                <div className="mono-num text-2xl font-semibold text-loss">{metrics ? `${metrics.max_drawdown_pct.toFixed(1)}%` : '--'}</div>
              </div>
              <div className="border-r border-border px-4 py-2">
                <div className="text-xxs uppercase tracking-wider text-foreground-muted">Trades</div>
                <div className="mono-num text-2xl font-semibold text-foreground">{metrics ? metrics.total_trades : '--'}</div>
              </div>
              <div className="px-4 py-2">
                <div className="text-xxs uppercase tracking-wider text-foreground-muted">Win Rate</div>
                <div className={`mono-num text-2xl font-semibold ${winRate > 50 ? 'text-profit' : winRate < 50 ? 'text-loss' : 'text-foreground'}`}>
                  {metrics ? `${winRate.toFixed(1)}%` : '--'}
                </div>
              </div>
            </div>

            <div className="flex h-9 shrink-0 items-center gap-3 border-b border-border bg-background-secondary px-4 text-xs text-foreground-secondary">
              <Badge variant={payload.status === 'completed' ? 'profit' : payload.status === 'failed' ? 'loss' : 'warning'}>
                {payload.status}
              </Badge>
              <span className="h-3 w-px bg-border" />
              <span>Instrument <strong className="text-foreground">{payload.params.instrument_type.toUpperCase()}</strong></span>
              <span className="h-3 w-px bg-border" />
              <span>Range <strong className="mono-num text-foreground">{payload.params.start_date} → {payload.params.end_date}</strong></span>
              <span className="h-3 w-px bg-border" />
              <span>Scope <strong className="text-foreground">{payload.result?.selection.scope || payload.params.selection.mode}</strong></span>
            </div>

            <div className="grid min-h-0 flex-1 grid-cols-[1fr_360px] overflow-hidden">
              <div className="flex h-full min-h-0 flex-col overflow-hidden border-r border-border bg-background">
                <Card variant="void" className="shrink-0 rounded-none border-0 border-b border-border bg-surface">
                  <CardHeader className="flex-row items-center justify-between px-4 py-3">
                    <CardTitle className="text-foreground">Equity Curve vs Benchmark</CardTitle>
                    <div className="flex items-center gap-4 text-sm">
                      <span className="flex items-center gap-2 text-primary"><span className="h-0.5 w-5 bg-primary" />Portfolio</span>
                      <span className="flex items-center gap-2 text-foreground-muted"><span className="h-0.5 w-5 bg-foreground-muted" />Benchmark</span>
                    </div>
                  </CardHeader>
                  <CardContent className="p-4" style={{ height: '360px' }}>
                    <EquityChart data={chartRows} />
                  </CardContent>
                </Card>

                <Card variant="void" className="shrink-0 rounded-none border-0 border-b border-border bg-surface">
                  <CardHeader className="px-4 py-3">
                    <CardTitle className="text-sm uppercase tracking-wider text-foreground-muted">Drawdown</CardTitle>
                  </CardHeader>
                  <CardContent className="h-24 p-3">
                    <DrawdownChart data={drawdownRows} />
                  </CardContent>
                </Card>
              </div>

              <div className="flex h-full min-h-0 flex-col overflow-hidden bg-background-secondary">
                <Card variant="void" className="shrink-0 rounded-none border-0 border-b border-border bg-surface">
                  <CardHeader className="px-4 py-3">
                    <div className="flex items-center justify-between">
                      <CardTitle className="text-foreground">Trade Log v2</CardTitle>
                      <Badge variant="outline">{trades.length} trades</Badge>
                    </div>
                    <p className="text-xs text-foreground-muted">{winRate.toFixed(1)}% win rate · by date</p>
                  </CardHeader>
                </Card>

                <div className="min-h-0 flex-1 overflow-y-auto custom-scrollbar">
                  {trades.length === 0 ? (
                    <div className="p-4 text-sm text-foreground-secondary">No trades generated for this configuration.</div>
                  ) : (
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead>Symbol</TableHead>
                          <TableHead>Dates</TableHead>
                          <TableHead numeric>Prices</TableHead>
                          <TableHead numeric>Return</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {trades.slice(0, 200).map((t, i) => (
                          <TableRow key={`${t.symbol}-${t.entry_date}-${i}`}>
                            <TableCell className="font-semibold text-foreground">{t.symbol}</TableCell>
                            <TableCell>
                              <div className="mono-num text-xs text-foreground-muted">↑ {t.entry_date}</div>
                              <div className="mono-num text-xs text-foreground-muted">↓ {t.exit_date}</div>
                            </TableCell>
                            <TableCell numeric>
                              <div className="mono-num text-foreground">{t.entry_price.toFixed(2)}</div>
                              <div className="mono-num text-foreground-muted">{t.exit_price.toFixed(2)}</div>
                            </TableCell>
                            <TableCell numeric variant={t.return_pct >= 0 ? 'profit' : 'loss'}>
                              {t.return_pct >= 0 ? '+' : ''}{t.return_pct.toFixed(2)}%
                            </TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  )}
                </div>
              </div>
            </div>
          </div>
        )}
      </div>
    </PageContainer>
  );
}
