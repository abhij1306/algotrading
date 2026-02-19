'use client';

import { useEffect, useMemo, useState } from 'react';
import { useRouter } from 'next/navigation';
import { Badge, Button, Card, CardContent, CardHeader, CardTitle, Input } from '@/components/ui';
import { apiClient } from '@/lib/api-client';

type StrategyMeta = {
  id: string;
  name: string;
  default_weight?: number;
  params_schema?: Record<string, number>;
};

type BacktestStatus = {
  data_ready: boolean;
  instrument_capabilities: Record<string, { enabled: boolean; note: string }>;
  universe_ranges: Record<string, { available: boolean; min_date: string | null; max_date: string | null }>;
  stock_range: { available: boolean; min_date: string | null; max_date: string | null };
};

type RunResponse = { job_id: string; status: string };

type StrategyAllocation = {
  strategy_id: string;
  enabled: boolean;
  weight: string;
};

export default function NewBacktestPage() {
  const router = useRouter();
  const [status, setStatus] = useState<BacktestStatus | null>(null);
  const [strategies, setStrategies] = useState<StrategyMeta[]>([]);
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
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const load = async () => {
      const [statusRes, strategyRes] = await Promise.all([
        apiClient.get<BacktestStatus>('/api/backtest/status'),
        apiClient.get<{ strategies: StrategyMeta[] }>('/api/backtest/strategies'),
      ]);

      if (statusRes.error) {
        setError(statusRes.error.message);
        return;
      }
      const statusPayload = statusRes.data ?? null;
      setStatus(statusPayload);

      const strategyRows = strategyRes.data?.strategies ?? [];
      setStrategies(strategyRows);
      setAllocations(strategyRows.map((s) => ({
        strategy_id: s.id,
        enabled: s.id === 'MOMENTUM_2D',
        weight: String(s.default_weight ?? 1),
      })));

      const niftyRange = statusPayload?.universe_ranges?.NIFTY50;
      if (niftyRange?.min_date && niftyRange?.max_date) {
        setStartDate(niftyRange.min_date);
        setEndDate(niftyRange.max_date);
      } else if (statusPayload?.stock_range.min_date && statusPayload?.stock_range.max_date) {
        setStartDate(statusPayload.stock_range.min_date);
        setEndDate(statusPayload.stock_range.max_date);
      }
    };
    load();
  }, []);

  const selectedCount = useMemo(() => allocations.filter((a) => a.enabled).length, [allocations]);

  const onToggleStrategy = (strategyId: string, enabled: boolean) => {
    setAllocations((prev) => prev.map((a) => (
      a.strategy_id === strategyId ? { ...a, enabled } : a
    )));
  };

  const onWeightChange = (strategyId: string, weight: string) => {
    setAllocations((prev) => prev.map((a) => (
      a.strategy_id === strategyId ? { ...a, weight } : a
    )));
  };

  const onRun = async () => {
    setRunning(true);
    setError(null);

    const chosen = allocations
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
      selection: selectionMode === 'universe'
        ? { mode: 'universe', universe }
        : { mode: 'symbols', symbols: symbols.split(',').map((s) => s.trim().toUpperCase()).filter(Boolean) },
      strategies: chosen,
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
    <div className="p-6 space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">New Backtest Run</h1>
          <p className="text-sm text-foreground-secondary mt-1">
            Configure instrument, scope, and strategy portfolio.
          </p>
        </div>
        <Button variant="outline" onClick={() => router.push('/backtest')}>Back</Button>
      </div>

      <Card>
        <CardHeader><CardTitle className="text-base">Run Configuration</CardTitle></CardHeader>
        <CardContent className="grid gap-4 md:grid-cols-2">
          <label className="text-sm">
            <div className="mb-1">Run Name (optional)</div>
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="Nifty Portfolio Test" />
          </label>

          <label className="text-sm">
            <div className="mb-1">Instrument</div>
            <select
              className="w-full rounded border border-border bg-transparent px-3 py-2"
              value={instrumentType}
              onChange={(e) => setInstrumentType(e.target.value as 'equity' | 'options')}
            >
              <option value="equity">Equity</option>
              <option value="options">Options (blocked until dataset is ready)</option>
            </select>
          </label>

          <label className="text-sm">
            <div className="mb-1">Start Date</div>
            <Input type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)} />
          </label>

          <label className="text-sm">
            <div className="mb-1">End Date</div>
            <Input type="date" value={endDate} onChange={(e) => setEndDate(e.target.value)} />
          </label>

          <label className="text-sm">
            <div className="mb-1">Initial Capital (INR)</div>
            <Input
              type="number"
              min="1"
              step="1"
              value={initialCapital}
              onChange={(e) => setInitialCapital(e.target.value)}
            />
          </label>

          <label className="text-sm">
            <div className="mb-1">Selection Mode</div>
            <select
              className="w-full rounded border border-border bg-transparent px-3 py-2"
              value={selectionMode}
              onChange={(e) => setSelectionMode(e.target.value as 'universe' | 'symbols')}
            >
              <option value="universe">Universe</option>
              <option value="symbols">Specific Symbols</option>
            </select>
          </label>

          {selectionMode === 'universe' ? (
            <label className="text-sm md:col-span-2">
              <div className="mb-1">Universe</div>
              <select
                className="w-full rounded border border-border bg-transparent px-3 py-2"
                value={universe}
                onChange={(e) => setUniverse(e.target.value)}
              >
                {Object.keys(status?.universe_ranges ?? { NIFTY50: {} }).map((u) => (
                  <option key={u} value={u}>{u}</option>
                ))}
              </select>
            </label>
          ) : (
            <label className="text-sm md:col-span-2">
              <div className="mb-1">Symbols (comma separated)</div>
              <Input value={symbols} onChange={(e) => setSymbols(e.target.value)} />
            </label>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Strategy Portfolio</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="text-xs text-foreground-secondary">
            Select one or many strategies and assign weights. Weights are normalized at runtime.
          </div>
          {strategies.map((s) => {
            const allocation = allocations.find((a) => a.strategy_id === s.id);
            return (
              <div key={s.id} className="grid gap-2 md:grid-cols-[1fr_140px_80px] items-center border border-border rounded px-3 py-2">
                <label className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={!!allocation?.enabled}
                    onChange={(e) => onToggleStrategy(s.id, e.target.checked)}
                  />
                  <span>{s.name}</span>
                </label>
                <Input
                  type="number"
                  min="0"
                  step="0.1"
                  value={allocation?.weight ?? '0'}
                  onChange={(e) => onWeightChange(s.id, e.target.value)}
                />
                <Badge variant="outline">{s.id}</Badge>
              </div>
            );
          })}
        </CardContent>
      </Card>

      {status && (
        <div className="text-xs text-foreground-secondary">
          Data readiness: {status.data_ready ? 'Ready' : 'Not Ready'} | Selected strategies: {selectedCount}
        </div>
      )}

      {error && <div className="text-sm text-loss">{error}</div>}

      <div className="flex gap-2">
        <Button variant="outline" onClick={() => router.push('/backtest')}>Cancel</Button>
        <Button onClick={onRun} disabled={running || selectedCount === 0}>
          {running ? 'Running...' : 'Run Backtest'}
        </Button>
      </div>
    </div>
  );
}
