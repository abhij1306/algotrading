import { useMemo, useState } from "react";
import dynamic from "@/lib/dynamic";
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
} from "@/components/ui";
import { useScreenerIndices } from "@/lib/queries/screener";
import {
  useCloseStrategyPosition,
  useRunStrategyBacktest,
  useRunStrategyScan,
  useStrategiesWorkspace,
  useStrategySignal,
  useToggleSignalQueue,
  useToggleStrategyHalt,
  type BacktestRun,
  type PositionRow,
  type ScanRow,
  type SignalDetail,
  type StrategyBacktestResult,
} from "@/lib/queries/strategies";

type UniverseOption = {
  id: string;
  name: string;
  count?: number;
};

const DynamicSignalChart = dynamic(() =>
  import("recharts").then((mod) => {
    const {
      Bar,
      CartesianGrid,
      ComposedChart,
      Legend,
      Line,
      ResponsiveContainer,
      Tooltip,
      XAxis,
      YAxis,
    } = mod;
    return function SignalChart({ data }: Readonly<{ data: SignalDetail["chart"] }>) {
      if (data.length === 0) {
        return (
          <div className="flex h-full items-center justify-center text-sm text-foreground-secondary">
            Select a signal to load chart data.
          </div>
        );
      }
      return (
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={data} margin={{ top: 12, right: 24, left: 0, bottom: 12 }}>
            <CartesianGrid stroke="var(--color-border)" strokeDasharray="3 3" />
            <XAxis dataKey="date" hide />
            <YAxis
              yAxisId="price"
              tick={{ fontSize: 11, fill: "var(--color-foreground-secondary)" }}
            />
            <YAxis
              yAxisId="volume"
              orientation="right"
              tick={{ fontSize: 11, fill: "var(--color-foreground-muted)" }}
            />
            <Tooltip
              contentStyle={{
                backgroundColor: "var(--color-elevated)",
                border: "1px solid var(--color-border)",
                borderRadius: "var(--radius-md)",
                fontSize: "12px",
              }}
            />
            <Legend />
            <Bar yAxisId="volume" dataKey="volume" fill="rgba(89, 141, 250, 0.18)" name="Volume" />
            <Line
              yAxisId="price"
              type="monotone"
              dataKey="close"
              stroke="#f97316"
              dot={false}
              strokeWidth={2.2}
              name="Close"
            />
            <Line
              yAxisId="price"
              type="monotone"
              dataKey="ema_21"
              stroke="#fafafa"
              dot={false}
              strokeDasharray="4 4"
              name="EMA 21"
            />
            <Line
              yAxisId="price"
              type="monotone"
              dataKey="ma_50"
              stroke="#3b82f6"
              dot={false}
              name="MA 50"
            />
            <Line
              yAxisId="price"
              type="monotone"
              dataKey="ma_150"
              stroke="#f59e0b"
              dot={false}
              name="MA 150"
            />
            <Line
              yAxisId="price"
              type="monotone"
              dataKey="ma_200"
              stroke="#ef4444"
              dot={false}
              name="MA 200"
            />
          </ComposedChart>
        </ResponsiveContainer>
      );
    };
  }),
);

const NO_UNIVERSES: UniverseOption[] = [];
const NO_SCAN_ROWS: ScanRow[] = [];
const NO_POSITIONS: PositionRow[] = [];
const NO_RUNS: BacktestRun[] = [];

function formatCurrency(value: number | null | undefined): string {
  if (typeof value !== "number" || Number.isNaN(value)) return "--";
  return `₹${value.toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;
}

/** An error message with the time it happened, so the newest one wins the banner. */
interface TimedError {
  message: string;
  at: number;
}

function latestError(errors: ReadonlyArray<TimedError | null>): string | null {
  let latest: TimedError | null = null;
  for (const entry of errors) {
    if (entry && (latest === null || entry.at >= latest.at)) latest = entry;
  }
  return latest?.message ?? null;
}

function formatPct(value: number | null | undefined): string {
  if (typeof value !== "number" || Number.isNaN(value)) return "--";
  return `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;
}

/** The chosen universe, or the first known one when the chosen one is not offered. */
function resolveUniverse(available: UniverseOption[], choice: string): string {
  if (available.length === 0 || available.some((row) => row.id === choice)) return choice;
  return available[0].id;
}

function timedQueryError(error: Error | null, at: number): TimedError | null {
  return error ? { message: error.message, at } : null;
}

function gradeVariant(grade: string): "profit" | "warning" | "neutral" {
  if (grade === "A") return "profit";
  return grade === "B" ? "warning" : "neutral";
}

export default function StrategiesPage() {
  const [selectedSymbol, setSelectedSymbol] = useState<string | null>(null);
  const [showAll, setShowAll] = useState(false);
  const [actionError, setActionError] = useState<TimedError | null>(null);
  const [universeChoice, setUniverseChoice] = useState("NIFTY500");
  const [backtestStart, setBacktestStart] = useState("2025-01-01");
  const [backtestEnd, setBacktestEnd] = useState("2026-03-13");
  const [backtestCapital, setBacktestCapital] = useState("1000000");
  const [backtestResult, setBacktestResult] = useState<StrategyBacktestResult | null>(null);

  const universesQuery = useScreenerIndices();
  const availableUniverses: UniverseOption[] = universesQuery.data ?? NO_UNIVERSES;
  // Fall back to the first known universe when the chosen one is not offered.
  const selectedUniverse = resolveUniverse(availableUniverses, universeChoice);

  const workspaceQuery = useStrategiesWorkspace({ universe: selectedUniverse, showAll });
  const status = workspaceQuery.data?.status ?? null;
  const results = workspaceQuery.data?.results ?? NO_SCAN_ROWS;
  const positions = workspaceQuery.data?.positions ?? NO_POSITIONS;
  const backtestRuns = workspaceQuery.data?.runs ?? NO_RUNS;
  const loading = workspaceQuery.isFetching;

  // The explicitly selected signal, else the first scan result.
  const signalSymbol = selectedSymbol ?? results[0]?.symbol ?? null;
  const signalQuery = useStrategySignal(signalSymbol);
  const signalDetail = signalSymbol === null ? null : (signalQuery.data ?? null);

  const runScan = useRunStrategyScan();
  const toggleHalt = useToggleStrategyHalt();
  const toggleQueue = useToggleSignalQueue();
  const closePosition = useCloseStrategyPosition();
  const runBacktest = useRunStrategyBacktest();
  const busy = [runScan, toggleHalt, toggleQueue, closePosition, runBacktest].some(
    (mutation) => mutation.isPending,
  );

  // The auto-selected first row fails quietly; only an explicitly picked signal shows its error.
  const error = latestError([
    actionError,
    timedQueryError(workspaceQuery.error, workspaceQuery.errorUpdatedAt),
    timedQueryError(selectedSymbol === null ? null : signalQuery.error, signalQuery.errorUpdatedAt),
  ]);

  const failAction = (failure: Error | string) => {
    const message = typeof failure === "string" ? failure : failure.message;
    setActionError({ message, at: Date.now() });
  };
  const clearActionError = () => setActionError(null);
  const actionCallbacks = { onSuccess: clearActionError, onError: failAction };

  const formingRows = useMemo(() => results.filter((row) => !row.is_breakout), [results]);
  const breakoutRows = useMemo(() => results.filter((row) => row.is_breakout), [results]);

  const onSelectSymbol = (symbol: string) => {
    setSelectedSymbol(symbol);
    setActionError(null);
  };

  const onRunScan = () => {
    setActionError(null);
    runScan.mutate(selectedUniverse, { onError: failAction });
  };

  const onToggleHalt = () => {
    toggleHalt.mutate(status?.halted ? "resume" : "halt", actionCallbacks);
  };

  const onQueueToggle = (row: ScanRow) => {
    toggleQueue.mutate(row, actionCallbacks);
  };

  const onClosePosition = (positionId: number) => {
    closePosition.mutate(positionId, actionCallbacks);
  };

  const onRunBacktest = () => {
    const parsedCapital = Number.parseFloat(backtestCapital.trim());
    if (!Number.isFinite(parsedCapital)) {
      failAction("Enter a valid numeric capital amount before running the backtest.");
      return;
    }
    runBacktest.mutate(
      {
        universe: selectedUniverse,
        startDate: backtestStart,
        endDate: backtestEnd,
        initialCapital: parsedCapital,
      },
      {
        onSuccess: (result) => {
          setBacktestResult(result);
          setActionError(null);
        },
        onError: failAction,
      },
    );
  };

  const selectedSignal = signalDetail?.signal ?? null;

  return (
    <PageContainer fullWidth>
      <div className="flex h-full w-full flex-col overflow-hidden bg-background">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border bg-surface px-4 py-3">
          <div>
            <h1 className="text-xl font-semibold text-foreground">Strategies</h1>
            <p className="text-sm text-foreground-secondary">
              VCP scanner, queue management, open positions, and backtest workflow for the selected
              universe.
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={status?.regime?.regime === "BULL" ? "profit" : "warning"}>
              {status?.regime?.regime ?? "--"} · Risk {status?.regime?.risk_pct ?? "--"}%
            </Badge>
            <Badge variant={status?.halted ? "loss" : "neutral"}>
              {status?.halted ? "HALTED" : "ACTIVE"}
            </Badge>
            <Button
              variant="secondary"
              size="sm"
              onClick={() => {
                setShowAll((value) => !value);
                setActionError(null);
              }}
            >
              {showAll ? "Hide C Grade" : "Show All"}
            </Button>
            <Button variant="secondary" size="sm" onClick={onRunScan} disabled={busy}>
              {busy ? "Running..." : "Run Scan Now"}
            </Button>
            <Button
              variant={status?.halted ? "secondary" : "destructive"}
              size="sm"
              onClick={onToggleHalt}
              disabled={busy}
            >
              {status?.halted ? "Resume" : "Halt"}
            </Button>
          </div>
        </div>

        {error && <div className="bg-loss-bg px-4 py-2 text-sm text-loss">{error}</div>}

        <div className="grid min-h-0 flex-1 grid-cols-[minmax(0,1.5fr)_minmax(360px,0.9fr)] overflow-hidden">
          <div className="min-h-0 overflow-hidden border-r border-border">
            <div className="grid grid-cols-5 gap-3 border-b border-border px-4 py-3">
              <Card>
                <CardContent className="p-3">
                  <Select
                    label="Universe"
                    value={selectedUniverse}
                    onChange={(e) => {
                      setUniverseChoice(e.target.value);
                      setSelectedSymbol(null);
                      setActionError(null);
                    }}
                  >
                    {availableUniverses.map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.name || item.id}
                      </option>
                    ))}
                  </Select>
                </CardContent>
              </Card>
              <Card>
                <CardContent className="p-3">
                  <div className="text-xs uppercase text-foreground-muted">Last Scan</div>
                  <div className="mt-1 text-sm font-semibold">
                    {status?.latest_scan_date ?? "Never"}
                  </div>
                </CardContent>
              </Card>
              <Card>
                <CardContent className="p-3">
                  <div className="text-xs uppercase text-foreground-muted">Pending Entry</div>
                  <div className="mt-1 text-sm font-semibold">{status?.pending_entries ?? 0}</div>
                </CardContent>
              </Card>
              <Card>
                <CardContent className="p-3">
                  <div className="text-xs uppercase text-foreground-muted">Open Positions</div>
                  <div className="mt-1 text-sm font-semibold">{status?.open_positions ?? 0}</div>
                </CardContent>
              </Card>
              <Card>
                <CardContent className="p-3">
                  <div className="text-xs uppercase text-foreground-muted">Signal Count</div>
                  <div className="mt-1 text-sm font-semibold">{results.length}</div>
                </CardContent>
              </Card>
            </div>

            <Tabs defaultValue="forming" className="flex h-[calc(100%-88px)] flex-col">
              <TabsList className="mx-4 mt-3 grid grid-cols-4">
                <TabsTrigger value="forming">Forming {formingRows.length}</TabsTrigger>
                <TabsTrigger value="breakouts">Breakouts {breakoutRows.length}</TabsTrigger>
                <TabsTrigger value="positions">Positions {positions.length}</TabsTrigger>
                <TabsTrigger value="backtest">Backtest</TabsTrigger>
              </TabsList>

              <TabsContent value="forming" className="min-h-0 flex-1 overflow-auto px-4 pb-4">
                {loading ? (
                  <div className="py-4 text-sm text-foreground-secondary">
                    Loading scanner results...
                  </div>
                ) : null}
                <SignalTable
                  rows={formingRows}
                  onSelect={onSelectSymbol}
                  onQueueToggle={onQueueToggle}
                  busy={busy}
                  selectedSymbol={signalSymbol}
                />
              </TabsContent>

              <TabsContent value="breakouts" className="min-h-0 flex-1 overflow-auto px-4 pb-4">
                <SignalTable
                  rows={breakoutRows}
                  onSelect={onSelectSymbol}
                  onQueueToggle={onQueueToggle}
                  busy={busy}
                  selectedSymbol={signalSymbol}
                />
              </TabsContent>

              <TabsContent value="positions" className="min-h-0 flex-1 overflow-auto px-4 pb-4">
                <PositionTable rows={positions} onClose={onClosePosition} busy={busy} />
              </TabsContent>

              <TabsContent value="backtest" className="min-h-0 flex-1 overflow-auto px-4 pb-4">
                <Card className="mb-4">
                  <CardHeader>
                    <CardTitle className="text-sm">Run VCP Backtest · {selectedUniverse}</CardTitle>
                  </CardHeader>
                  <CardContent className="grid grid-cols-4 gap-3">
                    <Input
                      label="Start Date"
                      type="date"
                      value={backtestStart}
                      onChange={(e) => setBacktestStart(e.target.value)}
                    />
                    <Input
                      label="End Date"
                      type="date"
                      value={backtestEnd}
                      onChange={(e) => setBacktestEnd(e.target.value)}
                    />
                    <Input
                      label="Capital (₹)"
                      type="number"
                      value={backtestCapital}
                      onChange={(e) => setBacktestCapital(e.target.value)}
                    />
                    <div className="flex items-end">
                      <Button className="w-full" onClick={onRunBacktest} disabled={busy}>
                        {busy ? "Running..." : "Run Backtest"}
                      </Button>
                    </div>
                  </CardContent>
                </Card>

                {backtestResult?.result?.metrics ? (
                  <Card className="mb-4">
                    <CardHeader>
                      <CardTitle className="text-sm">Latest Backtest Result</CardTitle>
                    </CardHeader>
                    <CardContent className="grid grid-cols-3 gap-3 text-sm">
                      <Metric
                        label="Return"
                        value={formatPct(
                          Number(backtestResult.result.metrics.total_return_pct ?? 0),
                        )}
                      />
                      <Metric
                        label="Sharpe"
                        value={String(backtestResult.result.metrics.sharpe_ratio ?? "--")}
                      />
                      <Metric
                        label="Trades"
                        value={String(backtestResult.result.metrics.total_trades ?? "--")}
                      />
                    </CardContent>
                  </Card>
                ) : null}

                <BacktestHistoryCard runs={backtestRuns} />
              </TabsContent>
            </Tabs>
          </div>

          <div className="flex min-h-0 flex-col bg-background-secondary">
            <SignalDetailCard signal={selectedSignal} />

            <Card variant="void" className="m-4 flex min-h-0 flex-1 flex-col">
              <CardHeader>
                <CardTitle className="text-sm">Chart</CardTitle>
              </CardHeader>
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
  busy,
}: Readonly<{
  rows: ScanRow[];
  selectedSymbol: string | null;
  onSelect: (symbol: string) => void;
  onQueueToggle: (row: ScanRow) => void;
  /** A strategy action is in flight; queue toggles wait for it. */
  busy: boolean;
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
            className={selectedSymbol === row.symbol ? "bg-background-tertiary" : ""}
            onClick={() => onSelect(row.symbol)}
            tabIndex={0}
            role="button"
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onSelect(row.symbol);
              }
            }}
          >
            <TableCell>
              <Badge variant={gradeVariant(row.grade)}>{row.grade}</Badge>
            </TableCell>
            <TableCell>
              <div className="font-medium">{row.symbol}</div>
              <div className="text-xs text-foreground-secondary">{row.company_name}</div>
            </TableCell>
            <TableCell>{row.rs_rating}</TableCell>
            <TableCell>{row.contraction_depths.join(" → ")}%</TableCell>
            <TableCell>{formatPct(-row.volume_dry_up_pct + 100)}</TableCell>
            <TableCell>{formatCurrency(row.pivot_high)}</TableCell>
            <TableCell>{formatPct(-row.stop_pct)}</TableCell>
            <TableCell>
              {row.is_breakout ? (
                <Button
                  size="sm"
                  variant={row.signal_status === "PENDING_ENTRY" ? "secondary" : "primary"}
                  disabled={busy}
                  onClick={(e) => {
                    e.stopPropagation();
                    onQueueToggle(row);
                  }}
                >
                  {row.signal_status === "PENDING_ENTRY" ? "Queued" : "Queue"}
                </Button>
              ) : (
                <Badge variant="neutral">Watch</Badge>
              )}
            </TableCell>
          </TableRow>
        ))}
        {rows.length === 0 ? (
          <TableRow>
            <TableCell colSpan={8} className="py-6 text-center text-sm text-foreground-secondary">
              No signals in this view.
            </TableCell>
          </TableRow>
        ) : null}
      </TableBody>
    </Table>
  );
}

function PositionTable({
  rows,
  onClose,
  busy,
}: Readonly<{
  rows: PositionRow[];
  onClose: (positionId: number) => void;
  /** A strategy action is in flight; closing twice would overwrite the recorded P&L. */
  busy: boolean;
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
              <div className="text-xs text-foreground-secondary">
                {formatPct(row.unrealized_pnl_pct)}
              </div>
            </TableCell>
            <TableCell>{row.r_multiple?.toFixed(2) ?? "--"}</TableCell>
            <TableCell>{row.status}</TableCell>
            <TableCell>
              <Button size="sm" variant="secondary" disabled={busy} onClick={() => onClose(row.id)}>
                Close Now
              </Button>
            </TableCell>
          </TableRow>
        ))}
        {rows.length === 0 ? (
          <TableRow>
            <TableCell colSpan={8} className="py-6 text-center text-sm text-foreground-secondary">
              No open positions.
            </TableCell>
          </TableRow>
        ) : null}
      </TableBody>
    </Table>
  );
}

function SignalDetailCard({ signal }: Readonly<{ signal: ScanRow | null }>) {
  return (
    <Card variant="void" className="m-4 mb-0">
      <CardHeader>
        <CardTitle className="text-sm">
          {signal ? `${signal.symbol} · ${signal.grade}-Grade` : "Signal Detail"}
        </CardTitle>
      </CardHeader>
      <CardContent className="grid grid-cols-2 gap-3 text-sm">
        <Metric label="RS Rating" value={signal ? String(signal.rs_rating) : "--"} />
        <Metric
          label="Contractions"
          value={signal ? signal.contraction_depths.map((d) => `${d}%`).join(" → ") : "--"}
        />
        <Metric label="Pivot High" value={signal ? formatCurrency(signal.pivot_high) : "--"} />
        <Metric label="Stop Level" value={signal ? formatCurrency(signal.stop_level) : "--"} />
        <Metric
          label="Position Value"
          value={signal ? formatCurrency(signal.planned_position_value) : "--"}
        />
        <Metric label="2R Target" value={signal ? formatCurrency(signal.two_r_target) : "--"} />
      </CardContent>
    </Card>
  );
}

function BacktestHistoryCard({ runs }: Readonly<{ runs: BacktestRun[] }>) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-sm">History</CardTitle>
      </CardHeader>
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
            {runs.map((run) => (
              <TableRow key={run.run_id}>
                <TableCell>{run.name || run.run_id}</TableCell>
                <TableCell>{run.universe || "--"}</TableCell>
                <TableCell>
                  {run.start_date} → {run.end_date}
                </TableCell>
                <TableCell>{run.status}</TableCell>
                <TableCell>{formatPct(run.metrics?.total_return_pct)}</TableCell>
                <TableCell>{run.metrics?.sharpe_ratio?.toFixed?.(2) ?? "--"}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}
