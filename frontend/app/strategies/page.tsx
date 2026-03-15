'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import dynamic from 'next/dynamic';
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
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from '@/components/ui';
import { screenerAPI, strategiesAPI } from '@/lib/api-client';

type RegimePayload = {
  regime: string;
  close: number;
  ma_200: number;
  risk_pct: number;
};

type StrategyStatus = {
  universe?: string;
  halted: boolean;
  halt_reason?: string;
  halted_at?: string;
  latest_scan_date?: string | null;
  pending_entries: number;
  open_positions: number;
  regime: RegimePayload;
};

type UniverseOption = {
  id: string;
  name: string;
  count?: number;
};

type ScanRow = {
  id: number;
  symbol: string;
  company_name: string;
  sector: string;
  grade: string;
  rs_rating: number;
  contraction_count: number;
  contraction_depths: number[];
  final_contraction_depth: number;
  volume_dry_up_pct: number;
  pivot_high: number;
  stop_level: number;
  stop_pct: number;
  days_in_base: number;
  is_breakout: boolean;
  breakout_price?: number | null;
  breakout_volume_mult?: number | null;
  signal_status: string;
  planned_shares: number;
  planned_position_value: number;
  capital_risk: number;
  two_r_target: number;
};

type SignalDetail = {
  signal: ScanRow;
  chart: Array<{
    date: string;
    close: number;
    volume: number;
    ema_21?: number | null;
    ma_50?: number | null;
    ma_150?: number | null;
    ma_200?: number | null;
  }>;
};

type PositionRow = {
  id: number;
  symbol: string;
  entry_date: string;
  entry_price: number;
  shares: number;
  stop_price: number;
  ltp: number;
  unrealized_pnl: number;
  unrealized_pnl_pct: number;
  r_multiple: number;
  two_r_status: string;
  status: string;
};

type BacktestRun = {
  run_id: string;
  name?: string;
  status: string;
  universe?: string;
  created_at?: string | null;
  start_date?: string | null;
  end_date?: string | null;
  metrics?: {
    total_return_pct?: number;
    sharpe_ratio?: number;
    total_trades?: number;
  };
};

type BacktestResult = {
  run_id: string;
  status: string;
  result?: {
    metrics?: Record<string, number | string>;
    trade_log?: Array<Record<string, string | number>>;
  };
};

const DynamicSignalChart = dynamic(
  () =>
    import('recharts').then((mod) => {
      const { Bar, CartesianGrid, ComposedChart, Legend, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } = mod;
      return function SignalChart({ data }: Readonly<{ data: SignalDetail['chart'] }>) {
        if (data.length === 0) {
          return <div className="flex h-full items-center justify-center text-sm text-foreground-secondary">Select a signal to load chart data.</div>;
        }
        return (
          <ResponsiveContainer width="100%" height="100%">
            <ComposedChart data={data} margin={{ top: 12, right: 24, left: 0, bottom: 12 }}>
              <CartesianGrid stroke="var(--color-border)" strokeDasharray="3 3" />
              <XAxis dataKey="date" hide />
              <YAxis yAxisId="price" tick={{ fontSize: 11, fill: 'var(--color-foreground-secondary)' }} />
              <YAxis yAxisId="volume" orientation="right" tick={{ fontSize: 11, fill: 'var(--color-foreground-muted)' }} />
              <Tooltip
                contentStyle={{
                  backgroundColor: 'var(--color-elevated)',
                  border: '1px solid var(--color-border)',
                  borderRadius: 'var(--radius-md)',
                  fontSize: '12px',
                }}
              />
              <Legend />
              <Bar yAxisId="volume" dataKey="volume" fill="rgba(89, 141, 250, 0.18)" name="Volume" />
              <Line yAxisId="price" type="monotone" dataKey="close" stroke="#f97316" dot={false} strokeWidth={2.2} name="Close" />
              <Line yAxisId="price" type="monotone" dataKey="ema_21" stroke="#fafafa" dot={false} strokeDasharray="4 4" name="EMA 21" />
              <Line yAxisId="price" type="monotone" dataKey="ma_50" stroke="#3b82f6" dot={false} name="MA 50" />
              <Line yAxisId="price" type="monotone" dataKey="ma_150" stroke="#f59e0b" dot={false} name="MA 150" />
              <Line yAxisId="price" type="monotone" dataKey="ma_200" stroke="#ef4444" dot={false} name="MA 200" />
            </ComposedChart>
          </ResponsiveContainer>
        );
      };
    }),
  { ssr: false }
);

function formatCurrency(value: number | null | undefined): string {
  if (typeof value !== 'number' || Number.isNaN(value)) return '--';
  return `₹${value.toLocaleString('en-IN', { maximumFractionDigits: 2 })}`;
}

function formatPct(value: number | null | undefined): string {
  if (typeof value !== 'number' || Number.isNaN(value)) return '--';
  return `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`;
}

export default function StrategiesPage() {
  const [status, setStatus] = useState<StrategyStatus | null>(null);
  const [results, setResults] = useState<ScanRow[]>([]);
  const [positions, setPositions] = useState<PositionRow[]>([]);
  const [selectedSymbol, setSelectedSymbol] = useState<string | null>(null);
  const [signalDetail, setSignalDetail] = useState<SignalDetail | null>(null);
  const [showAll, setShowAll] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [availableUniverses, setAvailableUniverses] = useState<UniverseOption[]>([]);
  const [selectedUniverse, setSelectedUniverse] = useState('NIFTY500');
  const [backtestStart, setBacktestStart] = useState('2025-01-01');
  const [backtestEnd, setBacktestEnd] = useState('2026-03-13');
  const [backtestCapital, setBacktestCapital] = useState('1000000');
  const [backtestRuns, setBacktestRuns] = useState<BacktestRun[]>([]);
  const [backtestResult, setBacktestResult] = useState<BacktestResult | null>(null);

  const loadWorkspace = useCallback(async (includeSignal = true) => {
    setLoading(true);
    setError(null);
    try {
      const [statusRes, scanRes, positionsRes, historyRes] = await Promise.all([
        strategiesAPI.getStatus(selectedUniverse),
        strategiesAPI.getLatestScan(selectedUniverse, showAll),
        strategiesAPI.getPositions(),
        strategiesAPI.getBacktestHistory(selectedUniverse),
      ]);

      const firstError = statusRes.error ?? scanRes.error ?? positionsRes.error ?? historyRes.error;
      if (firstError) {
        setError(firstError.message);
        setLoading(false);
        return;
      }

      setStatus((statusRes.data ?? null) as StrategyStatus | null);
      setResults((((scanRes.data ?? {}) as { results?: ScanRow[] }).results ?? []) as ScanRow[]);
      setPositions((((positionsRes.data ?? {}) as { positions?: PositionRow[] }).positions ?? []) as PositionRow[]);
      setBacktestRuns((((historyRes.data ?? {}) as { runs?: BacktestRun[] }).runs ?? []) as BacktestRun[]);

      const nextSymbol =
        includeSignal
          ? selectedSymbol ?? ((((scanRes.data ?? {}) as { results?: ScanRow[] }).results ?? [])[0]?.symbol ?? null)
          : selectedSymbol;
      if (nextSymbol) {
        const detailRes = await strategiesAPI.getSignal(nextSymbol);
        if (!detailRes.error && detailRes.data) {
          setSelectedSymbol(nextSymbol);
          setSignalDetail(detailRes.data as SignalDetail);
        }
      }
      setLoading(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load workspace');
      setLoading(false);
    }
  }, [selectedSymbol, selectedUniverse, showAll]);

  useEffect(() => {
    void loadWorkspace();
  }, [loadWorkspace]);

  useEffect(() => {
    const loadUniverses = async () => {
      const response = await screenerAPI.getIndices();
      if (response.error) {
        return;
      }
      const rows = (((response.data ?? {}) as { indices?: UniverseOption[] }).indices ?? []) as UniverseOption[];
      setAvailableUniverses(rows);
      if (rows.length > 0 && !rows.some((row) => row.id === selectedUniverse)) {
        setSelectedUniverse(rows[0].id);
      }
    };
    void loadUniverses();
  }, []);

  const formingRows = useMemo(() => results.filter((row) => !row.is_breakout), [results]);
  const breakoutRows = useMemo(() => results.filter((row) => row.is_breakout), [results]);

  const onSelectSymbol = async (symbol: string) => {
    setSelectedSymbol(symbol);
    const detailRes = await strategiesAPI.getSignal(symbol);
    if (detailRes.error) {
      setError(detailRes.error.message);
      return;
    }
    setSignalDetail((detailRes.data ?? null) as SignalDetail | null);
  };

  const onRunScan = async () => {
    setBusy(true);
    setError(null);
    const response = await strategiesAPI.runScan({ universe: selectedUniverse });
    setBusy(false);
    if (response.error) {
      setError(response.error.message);
      return;
    }
    await loadWorkspace(false);
  };

  const onToggleHalt = async () => {
    setBusy(true);
    const response = status?.halted ? await strategiesAPI.resume() : await strategiesAPI.halt('Manual halt from Strategies page');
    setBusy(false);
    if (response.error) {
      setError(response.error.message);
      return;
    }
    setStatus((response.data ?? null) as StrategyStatus | null);
  };

  const onQueueToggle = async (row: ScanRow) => {
    setBusy(true);
    const response =
      row.signal_status === 'PENDING_ENTRY'
        ? await strategiesAPI.cancelSignal(row.id)
        : await strategiesAPI.queueSignal(row.id);
    setBusy(false);
    if (response.error) {
      setError(response.error.message);
      return;
    }
    await loadWorkspace(false);
  };

  const onClosePosition = async (positionId: number) => {
    setBusy(true);
    const response = await strategiesAPI.closePosition(positionId);
    setBusy(false);
    if (response.error) {
      setError(response.error.message);
      return;
    }
    await loadWorkspace(false);
  };

  const onRunBacktest = async () => {
    const parsedCapital = Number.parseFloat(backtestCapital.trim());
    if (!Number.isFinite(parsedCapital) || Number.isNaN(parsedCapital)) {
      setError('Enter a valid numeric capital amount before running the backtest.');
      return;
    }
    setBusy(true);
    const response = await strategiesAPI.runBacktest({
      universe: selectedUniverse,
      start_date: backtestStart,
      end_date: backtestEnd,
      initial_capital: Number(parsedCapital),
    });
    setBusy(false);
    if (response.error) {
      setError(response.error.message);
      return;
    }
    setBacktestResult((response.data ?? null) as BacktestResult | null);
    await loadWorkspace(false);
  };

  const selectedSignal = signalDetail?.signal ?? null;

  return (
    <PageContainer fullWidth>
      <div className="flex h-full w-full flex-col overflow-hidden bg-background">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border bg-surface px-4 py-3">
          <div>
            <h1 className="text-xl font-semibold text-foreground">Strategies</h1>
            <p className="text-sm text-foreground-secondary">
              VCP scanner, queue management, open positions, and backtest workflow for the selected universe.
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={status?.regime?.regime === 'BULL' ? 'profit' : 'warning'}>
              {status?.regime?.regime ?? '--'} · Risk {status?.regime?.risk_pct ?? '--'}%
            </Badge>
            <Badge variant={status?.halted ? 'loss' : 'neutral'}>
              {status?.halted ? 'HALTED' : 'ACTIVE'}
            </Badge>
            <Button variant="secondary" size="sm" onClick={() => setShowAll((value) => !value)}>
              {showAll ? 'Hide C Grade' : 'Show All'}
            </Button>
            <Button variant="secondary" size="sm" onClick={onRunScan} disabled={busy}>
              {busy ? 'Running...' : 'Run Scan Now'}
            </Button>
            <Button variant={status?.halted ? 'secondary' : 'destructive'} size="sm" onClick={onToggleHalt} disabled={busy}>
              {status?.halted ? 'Resume' : 'Halt'}
            </Button>
          </div>
        </div>

        {error && <div className="bg-loss-bg px-4 py-2 text-sm text-loss">{error}</div>}

        <div className="grid min-h-0 flex-1 grid-cols-[minmax(0,1.5fr)_minmax(360px,0.9fr)] overflow-hidden">
          <div className="min-h-0 overflow-hidden border-r border-border">
            <div className="grid grid-cols-5 gap-3 border-b border-border px-4 py-3">
              <Card>
                <CardContent className="p-3">
                  <Select label="Universe" value={selectedUniverse} onChange={(e) => { setSelectedUniverse(e.target.value); setSelectedSymbol(null); setSignalDetail(null); }}>
                    {availableUniverses.map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.name || item.id}
                      </option>
                    ))}
                  </Select>
                </CardContent>
              </Card>
              <Card><CardContent className="p-3"><div className="text-xs uppercase text-foreground-muted">Last Scan</div><div className="mt-1 text-sm font-semibold">{status?.latest_scan_date ?? 'Never'}</div></CardContent></Card>
              <Card><CardContent className="p-3"><div className="text-xs uppercase text-foreground-muted">Pending Entry</div><div className="mt-1 text-sm font-semibold">{status?.pending_entries ?? 0}</div></CardContent></Card>
              <Card><CardContent className="p-3"><div className="text-xs uppercase text-foreground-muted">Open Positions</div><div className="mt-1 text-sm font-semibold">{status?.open_positions ?? 0}</div></CardContent></Card>
              <Card><CardContent className="p-3"><div className="text-xs uppercase text-foreground-muted">Signal Count</div><div className="mt-1 text-sm font-semibold">{results.length}</div></CardContent></Card>
            </div>

            <Tabs defaultValue="forming" className="flex h-[calc(100%-88px)] flex-col">
              <TabsList className="mx-4 mt-3 grid grid-cols-4">
                <TabsTrigger value="forming">Forming {formingRows.length}</TabsTrigger>
                <TabsTrigger value="breakouts">Breakouts {breakoutRows.length}</TabsTrigger>
                <TabsTrigger value="positions">Positions {positions.length}</TabsTrigger>
                <TabsTrigger value="backtest">Backtest</TabsTrigger>
              </TabsList>

              <TabsContent value="forming" className="min-h-0 flex-1 overflow-auto px-4 pb-4">
                {loading ? <div className="py-4 text-sm text-foreground-secondary">Loading scanner results...</div> : null}
                <SignalTable rows={formingRows} onSelect={onSelectSymbol} onQueueToggle={onQueueToggle} selectedSymbol={selectedSymbol} />
              </TabsContent>

              <TabsContent value="breakouts" className="min-h-0 flex-1 overflow-auto px-4 pb-4">
                <SignalTable rows={breakoutRows} onSelect={onSelectSymbol} onQueueToggle={onQueueToggle} selectedSymbol={selectedSymbol} />
              </TabsContent>

              <TabsContent value="positions" className="min-h-0 flex-1 overflow-auto px-4 pb-4">
                <PositionTable rows={positions} onClose={onClosePosition} />
              </TabsContent>

              <TabsContent value="backtest" className="min-h-0 flex-1 overflow-auto px-4 pb-4">
                <Card className="mb-4">
                  <CardHeader><CardTitle className="text-sm">Run VCP Backtest · {selectedUniverse}</CardTitle></CardHeader>
                  <CardContent className="grid grid-cols-4 gap-3">
                    <Input label="Start Date" type="date" value={backtestStart} onChange={(e) => setBacktestStart(e.target.value)} />
                    <Input label="End Date" type="date" value={backtestEnd} onChange={(e) => setBacktestEnd(e.target.value)} />
                    <Input label="Capital (₹)" type="number" value={backtestCapital} onChange={(e) => setBacktestCapital(e.target.value)} />
                    <div className="flex items-end">
                      <Button className="w-full" onClick={onRunBacktest} disabled={busy}>{busy ? 'Running...' : 'Run Backtest'}</Button>
                    </div>
                  </CardContent>
                </Card>

                {backtestResult?.result?.metrics ? (
                  <Card className="mb-4">
                    <CardHeader><CardTitle className="text-sm">Latest Backtest Result</CardTitle></CardHeader>
                    <CardContent className="grid grid-cols-3 gap-3 text-sm">
                      <Metric label="Return" value={formatPct(Number(backtestResult.result.metrics.total_return_pct ?? 0))} />
                      <Metric label="Sharpe" value={String(backtestResult.result.metrics.sharpe_ratio ?? '--')} />
                      <Metric label="Trades" value={String(backtestResult.result.metrics.total_trades ?? '--')} />
                    </CardContent>
                  </Card>
                ) : null}

                <Card>
                  <CardHeader><CardTitle className="text-sm">History</CardTitle></CardHeader>
                  <CardContent className="p-0">
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead>Run</TableHead>
                          <TableHead>Universe</TableHead>
                          <TableHead>Period</TableHead>
                          <TableHead>Status</TableHead>
                          <TableHead>Return</TableHead>
                          <TableHead>Sharpe</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {backtestRuns.map((run) => (
                          <TableRow key={run.run_id}>
                            <TableCell>{run.name || run.run_id}</TableCell>
                            <TableCell>{run.universe || '--'}</TableCell>
                            <TableCell>{run.start_date} → {run.end_date}</TableCell>
                            <TableCell>{run.status}</TableCell>
                            <TableCell>{formatPct(run.metrics?.total_return_pct)}</TableCell>
                            <TableCell>{run.metrics?.sharpe_ratio?.toFixed?.(2) ?? '--'}</TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  </CardContent>
                </Card>
              </TabsContent>
            </Tabs>
          </div>

          <div className="flex min-h-0 flex-col bg-background-secondary">
            <Card variant="void" className="m-4 mb-0">
              <CardHeader>
                <CardTitle className="text-sm">
                  {selectedSignal ? `${selectedSignal.symbol} · ${selectedSignal.grade}-Grade` : 'Signal Detail'}
                </CardTitle>
              </CardHeader>
              <CardContent className="grid grid-cols-2 gap-3 text-sm">
                <Metric label="RS Rating" value={selectedSignal ? String(selectedSignal.rs_rating) : '--'} />
                <Metric label="Contractions" value={selectedSignal ? selectedSignal.contraction_depths.map(d => `${d}%`).join(' → ') : '--'} />
                <Metric label="Pivot High" value={selectedSignal ? formatCurrency(selectedSignal.pivot_high) : '--'} />
                <Metric label="Stop Level" value={selectedSignal ? formatCurrency(selectedSignal.stop_level) : '--'} />
                <Metric label="Position Value" value={selectedSignal ? formatCurrency(selectedSignal.planned_position_value) : '--'} />
                <Metric label="2R Target" value={selectedSignal ? formatCurrency(selectedSignal.two_r_target) : '--'} />
              </CardContent>
            </Card>

            <Card variant="void" className="m-4 flex min-h-0 flex-1 flex-col">
              <CardHeader><CardTitle className="text-sm">Chart</CardTitle></CardHeader>
              <CardContent className="min-h-0 flex-1">
                <div className="h-full min-h-[360px]">
                  <DynamicSignalChart data={signalDetail?.chart ?? []} />
                </div>
              </CardContent>
            </Card>
          </div>
        </div>
      </div>
    </PageContainer>
  );
}

function Metric({ label, value }: Readonly<{ label: string; value: string }>) {
  return (
    <div className="rounded-md border border-border bg-background px-3 py-2">
      <div className="text-xs uppercase tracking-wider text-foreground-muted">{label}</div>
      <div className="mt-1 text-sm font-semibold text-foreground">{value}</div>
    </div>
  );
}

function SignalTable({
  rows,
  selectedSymbol,
  onSelect,
  onQueueToggle,
}: Readonly<{
  rows: ScanRow[];
  selectedSymbol: string | null;
  onSelect: (symbol: string) => void;
  onQueueToggle: (row: ScanRow) => void;
}>) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Grade</TableHead>
          <TableHead>Symbol</TableHead>
          <TableHead>RS</TableHead>
          <TableHead>Contractions</TableHead>
          <TableHead>Vol Dry-up</TableHead>
          <TableHead>Pivot</TableHead>
          <TableHead>Stop %</TableHead>
          <TableHead>Action</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((row) => (
          <TableRow
            key={row.id}
            className={selectedSymbol === row.symbol ? 'bg-background-tertiary' : ''}
            onClick={() => onSelect(row.symbol)}
            tabIndex={0}
            role="button"
            onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelect(row.symbol); } }}
          >
            <TableCell><Badge variant={row.grade === 'A' ? 'profit' : row.grade === 'B' ? 'warning' : 'neutral'}>{row.grade}</Badge></TableCell>
            <TableCell>
              <div className="font-medium">{row.symbol}</div>
              <div className="text-xs text-foreground-secondary">{row.company_name}</div>
            </TableCell>
            <TableCell>{row.rs_rating}</TableCell>
            <TableCell>{row.contraction_depths.join(' → ')}%</TableCell>
            <TableCell>{formatPct(-row.volume_dry_up_pct + 100)}</TableCell>
            <TableCell>{formatCurrency(row.pivot_high)}</TableCell>
            <TableCell>{formatPct(-row.stop_pct)}</TableCell>
            <TableCell>
              {row.is_breakout ? (
                <Button size="sm" variant={row.signal_status === 'PENDING_ENTRY' ? 'secondary' : 'primary'} onClick={(e) => { e.stopPropagation(); onQueueToggle(row); }}>
                  {row.signal_status === 'PENDING_ENTRY' ? 'Queued' : 'Queue'}
                </Button>
              ) : (
                <Badge variant="neutral">Watch</Badge>
              )}
            </TableCell>
          </TableRow>
        ))}
        {rows.length === 0 ? (
          <TableRow><TableCell colSpan={8} className="py-6 text-center text-sm text-foreground-secondary">No signals in this view.</TableCell></TableRow>
        ) : null}
      </TableBody>
    </Table>
  );
}

function PositionTable({
  rows,
  onClose,
}: Readonly<{
  rows: PositionRow[];
  onClose: (positionId: number) => void;
}>) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Symbol</TableHead>
          <TableHead>Entry</TableHead>
          <TableHead>Stop</TableHead>
          <TableHead>LTP</TableHead>
          <TableHead>P&L</TableHead>
          <TableHead>R</TableHead>
          <TableHead>Status</TableHead>
          <TableHead>Action</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((row) => (
          <TableRow key={row.id}>
            <TableCell>{row.symbol}</TableCell>
            <TableCell>{formatCurrency(row.entry_price)}</TableCell>
            <TableCell>{formatCurrency(row.stop_price)}</TableCell>
            <TableCell>{formatCurrency(row.ltp)}</TableCell>
            <TableCell>
              <div>{formatCurrency(row.unrealized_pnl)}</div>
              <div className="text-xs text-foreground-secondary">{formatPct(row.unrealized_pnl_pct)}</div>
            </TableCell>
            <TableCell>{row.r_multiple?.toFixed(2) ?? '--'}</TableCell>
            <TableCell>{row.status}</TableCell>
            <TableCell><Button size="sm" variant="secondary" onClick={() => onClose(row.id)}>Close Now</Button></TableCell>
          </TableRow>
        ))}
        {rows.length === 0 ? (
          <TableRow><TableCell colSpan={8} className="py-6 text-center text-sm text-foreground-secondary">No open positions.</TableCell></TableRow>
        ) : null}
      </TableBody>
    </Table>
  );
}
