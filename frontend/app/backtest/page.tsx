'use client';

import { useEffect, useMemo, useState } from 'react';
import { useRouter } from 'next/navigation';
import {
  Badge,
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  Input,
  PageContainer,
  Select,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui';
import { apiClient } from '@/lib/api-client';

type StrategyMeta = {
  id: string;
  name: string;
  default_weight?: number;
};

type BacktestStatus = {
  data_ready: boolean;
  instrument_capabilities: Record<string, { enabled: boolean; note: string }>;
  universe_ranges: Record<string, { available: boolean; min_date: string | null; max_date: string | null; rows?: number }>;
  stock_range: { available: boolean; min_date: string | null; max_date: string | null; rows?: number };
};

type BacktestRunItem = {
  job_id: string;
  status: 'running' | 'completed' | 'failed';
  created_at: string;
  params: {
    instrument_type?: string;
    start_date?: string;
    end_date?: string;
    initial_capital?: number;
    selection?: { mode?: string; universe?: string };
  };
};

type RunResponse = { job_id: string; status: string };

type StrategyAllocation = {
  strategy_id: string;
  enabled: boolean;
  weight: string;
};

function formatCompactCapital(value: number | null | undefined): string {
  if (typeof value !== 'number' || Number.isNaN(value)) return '--';
  if (value >= 10000000) return `₹${(value / 10000000).toFixed(1)}Cr`;
  if (value >= 100000) return `₹${(value / 100000).toFixed(1)}L`;
  return `₹${value.toLocaleString('en-IN')}`;
}

export default function BacktestPage() {
  const router = useRouter();
  const [status, setStatus] = useState<BacktestStatus | null>(null);
  const [strategies, setStrategies] = useState<StrategyMeta[]>([]);
  const [runs, setRuns] = useState<BacktestRunItem[]>([]);
  const [name, setName] = useState('');
  const [instrumentType, setInstrumentType] = useState<'equity' | 'options'>('equity');
  const [selectionMode, setSelectionMode] = useState<'universe' | 'symbols'>('universe');
  const [universe, setUniverse] = useState('NIFTY50');
  const [symbols, setSymbols] = useState('RELIANCE,SBIN,TCS');
  const [startDate, setStartDate] = useState('');
  const [endDate, setEndDate] = useState('');
  const [initialCapital, setInitialCapital] = useState('1000000');
  const [allocations, setAllocations] = useState<StrategyAllocation[]>([]);
  const [running, setRunning] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const load = async () => {
      setLoading(true);
      setError(null);

      const [statusRes, strategyRes, runsRes] = await Promise.all([
        apiClient.get<BacktestStatus>('/api/backtest/status'),
        apiClient.get<{ strategies: StrategyMeta[] }>('/api/backtest/strategies'),
        apiClient.get<{ runs: BacktestRunItem[] }>('/api/backtest/runs'),
      ]);

      if (statusRes.error) {
        setError(statusRes.error.message);
        setLoading(false);
        return;
      }

      const statusPayload = statusRes.data ?? null;
      setStatus(statusPayload);

      const strategyRows = strategyRes.data?.strategies ?? [];
      setStrategies(strategyRows);
      setAllocations(
        strategyRows.map((s) => ({
          strategy_id: s.id,
          enabled: s.id === 'MOMENTUM_2D',
          weight: String(s.default_weight ?? 1),
        }))
      );

      const firstUniverse = Object.keys(statusPayload?.universe_ranges ?? {})[0];
      const prefillUniverse = (statusPayload?.universe_ranges?.NIFTY50 ? 'NIFTY50' : firstUniverse) || 'NIFTY50';
      setUniverse(prefillUniverse);

      const range = statusPayload?.universe_ranges?.[prefillUniverse];
      if (range?.min_date && range?.max_date) {
        setStartDate(range.min_date);
        setEndDate(range.max_date);
      } else if (statusPayload?.stock_range.min_date && statusPayload?.stock_range.max_date) {
        setStartDate(statusPayload.stock_range.min_date);
        setEndDate(statusPayload.stock_range.max_date);
      }

      if (!runsRes.error && runsRes.data?.runs) {
        setRuns(runsRes.data.runs.slice(0, 12));
      }
      setLoading(false);
    };
    void load();
  }, []);

  const selectedCount = useMemo(() => allocations.filter((a) => a.enabled).length, [allocations]);
  const selectedWeightSum = useMemo(
    () => allocations.filter((a) => a.enabled).reduce((sum, a) => sum + (Number(a.weight) || 0), 0),
    [allocations]
  );

  const onToggleStrategy = (id: string, enabled: boolean) => {
    setAllocations((prev) => prev.map((a) => (a.strategy_id === id ? { ...a, enabled } : a)));
  };

  const onWeightChange = (id: string, weight: string) => {
    setAllocations((prev) => prev.map((a) => (a.strategy_id === id ? { ...a, weight } : a)));
  };

  const onRun = async () => {
    setRunning(true);
    setError(null);

    const strategiesPayload = allocations
      .filter((a) => a.enabled)
      .map((a) => ({
        strategy_id: a.strategy_id,
        enabled: true,
        weight: Number(a.weight) || 0,
        params: {},
      }));

    const payload = {
      name: name || undefined,
      instrument_type: instrumentType,
      start_date: startDate,
      end_date: endDate,
      initial_capital: Number(initialCapital),
      selection:
        selectionMode === 'universe'
          ? { mode: 'universe', universe }
          : { mode: 'symbols', symbols: symbols.split(',').map((s) => s.trim().toUpperCase()).filter(Boolean) },
      strategies: strategiesPayload,
      execution: { rebalance: 'daily' },
    };

    const res = await apiClient.post<RunResponse>('/api/backtest/run', payload);
    setRunning(false);

    if (res.error || !res.data) {
      setError(res.error?.message || 'Failed to run backtest');
      return;
    }
    router.push(`/backtest/results/${res.data.job_id}`);
  };

  return (
    <PageContainer fullWidth>
      <div className="flex h-full w-full flex-col overflow-hidden bg-background">
        <div className="flex h-12 shrink-0 items-center justify-between border-b border-border bg-surface px-4">
          <h1 className="text-xl font-semibold text-foreground">Backtest</h1>
          <div className="flex items-center gap-2">
            <Badge size="sm" variant={status?.data_ready ? 'profit' : 'warning'}>
              {status?.data_ready ? 'Data Ready' : 'Data Not Ready'}
            </Badge>
            <Badge size="sm" variant={status?.instrument_capabilities?.options?.enabled ? 'neutral' : 'warning'}>
              {status?.instrument_capabilities?.options?.enabled ? 'Options On' : 'Options Blocked'}
            </Badge>
          </div>
        </div>

        {loading && <div className="p-4 text-sm text-foreground-secondary">Loading backtest workspace...</div>}
        {error && <div className="bg-loss-bg p-4 text-sm text-loss">{error}</div>}

        {!loading && (
          <div className="grid min-h-0 flex-1 grid-cols-[360px_1fr] overflow-hidden">
            <div className="flex h-full flex-col overflow-hidden border-r border-border bg-background-secondary">
              <div className="flex-1 overflow-y-auto custom-scrollbar">
                <Card variant="void" className="rounded-none border-0 border-b border-border">
                  <CardHeader className="px-4 py-3">
                    <CardTitle className="text-sm uppercase tracking-wider text-foreground-muted">Run Configuration</CardTitle>
                  </CardHeader>
                  <CardContent className="space-y-3 px-4 pb-4 pt-0">
                    <label className="block text-sm">
                      <span className="mb-1 block text-foreground-secondary">Run Name (optional)</span>
                      <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Nifty EMA Test Q1" />
                    </label>

                    <div className="grid grid-cols-2 gap-3">
                      <label className="block text-sm">
                        <span className="mb-1 block text-foreground-secondary">Instrument</span>
                        <Select
                          value={instrumentType}
                          onChange={(e) => setInstrumentType(e.target.value as 'equity' | 'options')}
                        >
                          <option value="equity">Equity</option>
                          <option value="options">Options</option>
                        </Select>
                      </label>
                      <label className="block text-sm">
                        <span className="mb-1 block text-foreground-secondary">Capital (₹)</span>
                        <Input type="number" min="1" step="1" value={initialCapital} onChange={(e) => setInitialCapital(e.target.value)} />
                      </label>
                    </div>

                    <div className="grid grid-cols-2 gap-3">
                      <label className="block text-sm">
                        <span className="mb-1 block text-foreground-secondary">Start Date</span>
                        <Input type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)} />
                      </label>
                      <label className="block text-sm">
                        <span className="mb-1 block text-foreground-secondary">End Date</span>
                        <Input type="date" value={endDate} onChange={(e) => setEndDate(e.target.value)} />
                      </label>
                    </div>

                    <label className="block text-sm">
                      <span className="mb-1 block text-foreground-secondary">Selection Mode</span>
                      <Select
                        value={selectionMode}
                        onChange={(e) => setSelectionMode(e.target.value as 'universe' | 'symbols')}
                      >
                        <option value="universe">Index Universe</option>
                        <option value="symbols">Specific Symbols</option>
                      </Select>
                    </label>

                    {selectionMode === 'universe' ? (
                      <label className="block text-sm">
                        <span className="mb-1 block text-foreground-secondary">Universe</span>
                        <Select
                          value={universe}
                          onChange={(e) => setUniverse(e.target.value)}
                        >
                          {Object.keys(status?.universe_ranges ?? { NIFTY50: {} }).map((u) => (
                            <option key={u} value={u}>{u}</option>
                          ))}
                        </Select>
                      </label>
                    ) : (
                      <label className="block text-sm">
                        <span className="mb-1 block text-foreground-secondary">Symbols (comma separated)</span>
                        <Input value={symbols} onChange={(e) => setSymbols(e.target.value)} />
                      </label>
                    )}
                  </CardContent>
                </Card>

                <div className="p-4">
                  <Card variant="outline" className="border-dashed">
                    <CardContent className="space-y-2 p-4 text-xs">
                      <div className="flex justify-between">
                        <span className="text-foreground-muted">Universe</span>
                        <span className="mono-num text-foreground">{selectionMode === 'universe' ? universe : 'Custom Symbols'}</span>
                      </div>
                      <div className="flex justify-between">
                        <span className="text-foreground-muted">Range</span>
                        <span className="mono-num text-foreground">{startDate || '--'} → {endDate || '--'}</span>
                      </div>
                      <div className="flex justify-between">
                        <span className="text-foreground-muted">Capital</span>
                        <span className="mono-num text-foreground">{formatCompactCapital(Number(initialCapital) || 0)}</span>
                      </div>
                      <div className="flex justify-between">
                        <span className="text-foreground-muted">Strategies</span>
                        <span className="mono-num text-foreground">{selectedCount} active · Σw={selectedWeightSum.toFixed(2)}</span>
                      </div>
                      <Button variant="primary" className="mt-2 w-full" onClick={onRun} disabled={running || selectedCount === 0}>
                        {running ? 'Running...' : 'Run Backtest'}
                      </Button>
                    </CardContent>
                  </Card>
                </div>
              </div>
            </div>

            <div className="flex h-full min-h-0 flex-col overflow-hidden bg-background">
              <div className="shrink-0 overflow-y-auto border-b border-border bg-surface custom-scrollbar" style={{ maxHeight: '40vh' }}>
                <Card variant="void" className="rounded-none border-0">
                  <CardHeader className="flex-row items-center justify-between px-5 py-3">
                    <div>
                      <CardTitle className="text-foreground">Strategies</CardTitle>
                      <p className="text-xs text-foreground-muted">Select and weight strategies for this run</p>
                    </div>
                    <Badge variant="outline">{selectedCount} active · Σw={selectedWeightSum.toFixed(2)}</Badge>
                  </CardHeader>
                  <CardContent className="grid gap-3 px-5 pb-4 pt-0 md:grid-cols-2 xl:grid-cols-3">
                    {strategies.map((s) => {
                      const allocation = allocations.find((a) => a.strategy_id === s.id);
                      return (
                        <Card key={s.id} variant={allocation?.enabled ? 'glass' : 'outline'}>
                          <CardContent className="space-y-2 p-4">
                            <div className="flex items-start justify-between gap-2">
                              <div>
                                <div className="text-sm font-semibold text-foreground">{s.name}</div>
                                <div className="text-xs text-foreground-muted">{s.id.replaceAll('_', ' ')}</div>
                              </div>
                              <input
                                type="checkbox"
                                checked={!!allocation?.enabled}
                                onChange={(e) => onToggleStrategy(s.id, e.target.checked)}
                                className="mt-1 h-4 w-4 cursor-pointer accent-primary"
                              />
                            </div>
                            <div className="flex items-center justify-between gap-2">
                              <Badge variant="outline" size="xs">{s.id}</Badge>
                              <div className="flex items-center gap-2">
                                <span className="text-xs text-foreground-muted">Weight</span>
                                <Input
                                  type="number"
                                  min="0"
                                  step="0.1"
                                  value={allocation?.weight ?? '0'}
                                  onChange={(e) => onWeightChange(s.id, e.target.value)}
                                  className="w-20"
                                />
                              </div>
                            </div>
                          </CardContent>
                        </Card>
                      );
                    })}
                  </CardContent>
                </Card>
              </div>

              <div className="flex min-h-0 flex-1 flex-col overflow-hidden border-t border-border">
                <div className="shrink-0 border-b border-border bg-surface px-5 py-3">
                  <h3 className="text-lg font-semibold text-foreground">Recent Runs</h3>
                  <p className="text-xs text-foreground-muted">Click a completed run to view results</p>
                </div>

                <div className="min-h-0 flex-1 overflow-auto custom-scrollbar">
                  {runs.length === 0 ? (
                    <div className="p-5 text-sm text-foreground-secondary">No runs yet.</div>
                  ) : (
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead>Run ID</TableHead>
                          <TableHead>Status</TableHead>
                          <TableHead>Instrument</TableHead>
                          <TableHead>Date Range</TableHead>
                          <TableHead>Scope</TableHead>
                          <TableHead numeric>Capital</TableHead>
                          <TableHead className="text-right">Action</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {runs.map((r) => (
                          <TableRow key={r.job_id}>
                            <TableCell className="mono-num font-semibold text-foreground">{r.job_id}</TableCell>
                            <TableCell>
                              <Badge variant={r.status === 'completed' ? 'profit' : r.status === 'failed' ? 'loss' : 'warning'}>
                                {r.status}
                              </Badge>
                            </TableCell>
                            <TableCell className="text-foreground">{(r.params.instrument_type || 'equity').replace(/^./, (c) => c.toUpperCase())}</TableCell>
                            <TableCell className="mono-num text-foreground-secondary">{r.params.start_date} → {r.params.end_date}</TableCell>
                            <TableCell className="text-foreground-secondary">{r.params.selection?.mode === 'symbols' ? 'Symbols' : (r.params.selection?.universe || 'NIFTY50')}</TableCell>
                            <TableCell numeric className="text-foreground">{formatCompactCapital(r.params.initial_capital)}</TableCell>
                            <TableCell className="text-right">
                              <Button size="sm" variant={r.status === 'failed' ? 'secondary' : 'link'} onClick={() => router.push(`/backtest/results/${r.job_id}`)}>
                                {r.status === 'failed' ? 'Retry' : 'View →'}
                              </Button>
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
