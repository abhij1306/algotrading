import { useMemo, useState } from "react";
import { useNavigate } from "react-router";
import {
  Badge,
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  Checkbox,
  Input,
  PageContainer,
  Select,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui";
import {
  useBacktestRuns,
  useBacktestStatus,
  useBacktestStrategies,
  useRunBacktest,
  type BacktestRunPayload,
  type StrategyMeta,
} from "@/lib/queries/backtest";
import { formatCompactInr } from "@/lib/format";
import { runStatusVariant } from "@/lib/status-variants";
import { deriveFormDefaults, type SelectionMode } from "./form-defaults";

type StrategyAllocation = {
  strategy_id: string;
  enabled: boolean;
  weight: string;
};

type AllocationEdit = Partial<Pick<StrategyAllocation, "enabled" | "weight">>;

const RECENT_RUNS_LIMIT = 12;
const NO_STRATEGIES: StrategyMeta[] = [];

export default function BacktestPage() {
  const navigate = useNavigate();
  const statusQuery = useBacktestStatus();
  const strategiesQuery = useBacktestStrategies();
  const runsQuery = useBacktestRuns();
  const runBacktest = useRunBacktest();

  const status = statusQuery.data;
  const strategies = strategiesQuery.data ?? NO_STRATEGIES;
  const runs = useMemo(() => (runsQuery.data ?? []).slice(0, RECENT_RUNS_LIMIT), [runsQuery.data]);
  const loading = statusQuery.isPending || strategiesQuery.isPending || runsQuery.isPending;
  const running = runBacktest.isPending;
  const queryError =
    statusQuery.error?.message ??
    (strategiesQuery.error && `Failed to load strategies: ${strategiesQuery.error.message}`) ??
    (runsQuery.error && `Failed to load recent runs: ${runsQuery.error.message}`) ??
    null;
  const error = queryError ?? runBacktest.error?.message ?? null;

  const [name, setName] = useState("");
  const [instrumentType, setInstrumentType] = useState<"equity" | "options">("equity");
  const [symbols, setSymbols] = useState("RELIANCE,SBIN,TCS");
  const [initialCapital, setInitialCapital] = useState("1000000");
  // Fields the user has edited; anything null/absent falls back to the server-derived default.
  const [selectionModeEdit, setSelectionModeEdit] = useState<SelectionMode | null>(null);
  const [universeEdit, setUniverseEdit] = useState<string | null>(null);
  const [startDateEdit, setStartDateEdit] = useState<string | null>(null);
  const [endDateEdit, setEndDateEdit] = useState<string | null>(null);
  const [allocationEdits, setAllocationEdits] = useState<Record<string, AllocationEdit>>({});

  const defaults = useMemo(() => deriveFormDefaults(status), [status]);
  const selectionMode = selectionModeEdit ?? defaults.selectionMode;
  const universe = universeEdit ?? defaults.universe;
  const startDate = startDateEdit ?? defaults.startDate;
  const endDate = endDateEdit ?? defaults.endDate;

  const allocations = useMemo<StrategyAllocation[]>(
    () =>
      strategies.map((s) => ({
        strategy_id: s.id,
        enabled: allocationEdits[s.id]?.enabled ?? s.id === "MOMENTUM_2D",
        weight: allocationEdits[s.id]?.weight ?? String(s.default_weight ?? 1),
      })),
    [strategies, allocationEdits],
  );

  const selectedCount = useMemo(() => allocations.filter((a) => a.enabled).length, [allocations]);
  let runDisabledReason: string | null = null;
  if (strategiesQuery.isError) runDisabledReason = "Run disabled: strategies could not be loaded.";
  else if (!loading && selectedCount === 0) {
    runDisabledReason = "Run disabled: select at least one strategy.";
  }
  const selectedWeightSum = useMemo(
    () => allocations.filter((a) => a.enabled).reduce((sum, a) => sum + (Number(a.weight) || 0), 0),
    [allocations],
  );

  const onToggleStrategy = (id: string, enabled: boolean) => {
    setAllocationEdits((prev) => ({ ...prev, [id]: { ...prev[id], enabled } }));
  };

  const onWeightChange = (id: string, weight: string) => {
    setAllocationEdits((prev) => ({ ...prev, [id]: { ...prev[id], weight } }));
  };

  const onRun = () => {
    const strategiesPayload = allocations
      .filter((a) => a.enabled)
      .map((a) => ({
        strategy_id: a.strategy_id,
        enabled: true,
        weight: Number(a.weight) || 0,
        params: {},
      }));

    const payload: BacktestRunPayload = {
      name: name || undefined,
      instrument_type: instrumentType,
      start_date: startDate,
      end_date: endDate,
      initial_capital: Number(initialCapital),
      selection:
        selectionMode === "universe"
          ? { mode: "universe", universe }
          : {
              mode: "symbols",
              universe: null,
              symbols: symbols
                .split(",")
                .map((s) => s.trim().toUpperCase())
                .filter(Boolean),
            },
      strategies: strategiesPayload,
      execution: { rebalance: "daily" },
    };

    runBacktest.mutate(payload, {
      onSuccess: (res) => {
        void navigate(`/backtest/results/${res.job_id}`);
      },
    });
  };

  return (
    <PageContainer fullWidth>
      <div className="flex h-full w-full flex-col overflow-hidden bg-background">
        <div className="flex h-12 shrink-0 items-center justify-between border-b border-border bg-surface px-4">
          <h1 className="text-xl font-semibold text-foreground">Backtest</h1>
          <div className="flex items-center gap-2">
            <Badge size="sm" variant={status?.data_ready ? "profit" : "warning"}>
              {status?.data_ready ? "Data Ready" : "Data Not Ready"}
            </Badge>
            <Badge
              size="sm"
              variant={status?.instrument_capabilities?.options?.enabled ? "neutral" : "warning"}
            >
              {status?.instrument_capabilities?.options?.enabled ? "Options On" : "Options Blocked"}
            </Badge>
          </div>
        </div>

        {loading && (
          <div className="p-4 text-sm text-foreground-secondary">Loading backtest workspace...</div>
        )}
        {error && <div className="bg-loss-bg p-4 text-sm text-loss">{error}</div>}

        {!loading && (
          <div className="grid min-h-0 flex-1 grid-cols-[360px_1fr] overflow-hidden">
            <div className="flex h-full flex-col overflow-hidden border-r border-border bg-background-secondary">
              <div className="flex-1 overflow-y-auto custom-scrollbar">
                <Card variant="void" className="rounded-none border-0 border-b border-border">
                  <CardHeader className="px-4 py-3">
                    <CardTitle className="text-sm uppercase tracking-wider text-foreground-muted">
                      Run Configuration
                    </CardTitle>
                  </CardHeader>
                  <CardContent className="space-y-3 px-4 pb-4 pt-0">
                    <Input
                      label="Run Name (optional)"
                      value={name}
                      onChange={(e) => setName(e.target.value)}
                      placeholder="e.g. Nifty EMA Test Q1"
                    />

                    <div className="grid grid-cols-2 gap-4">
                      <Select
                        label="Instrument"
                        value={instrumentType}
                        onChange={(e) => setInstrumentType(e.target.value as "equity" | "options")}
                      >
                        <option value="equity">Equity</option>
                        <option value="options">Options</option>
                      </Select>

                      <Input
                        label="Capital (₹)"
                        type="number"
                        min="1"
                        step="1"
                        value={initialCapital}
                        onChange={(e) => setInitialCapital(e.target.value)}
                      />
                    </div>

                    <div className="grid grid-cols-2 gap-4">
                      <Input
                        label="Start Date"
                        type="date"
                        value={startDate}
                        onChange={(e) => setStartDateEdit(e.target.value)}
                      />
                      <Input
                        label="End Date"
                        type="date"
                        value={endDate}
                        onChange={(e) => setEndDateEdit(e.target.value)}
                      />
                    </div>

                    <Select
                      label="Selection Mode"
                      value={selectionMode}
                      onChange={(e) =>
                        setSelectionModeEdit(e.target.value === "symbols" ? "symbols" : "universe")
                      }
                    >
                      <option value="universe">Index Universe</option>
                      <option value="symbols">Specific Symbols</option>
                    </Select>

                    {selectionMode === "universe" ? (
                      <Select
                        label="Universe"
                        value={universe}
                        onChange={(e) => setUniverseEdit(e.target.value)}
                      >
                        {Object.keys(status?.universe_ranges ?? { NIFTY50: {} }).map((u) => (
                          <option key={u} value={u}>
                            {u}
                          </option>
                        ))}
                      </Select>
                    ) : (
                      <Input
                        label="Symbols (comma separated)"
                        value={symbols}
                        onChange={(e) => setSymbols(e.target.value)}
                      />
                    )}
                  </CardContent>
                </Card>

                <div className="p-4">
                  <Card variant="outline" className="border-dashed">
                    <CardContent className="space-y-2 p-4 text-xs">
                      <div className="flex justify-between">
                        <span className="text-foreground-muted">Universe</span>
                        <span className="mono-num text-foreground">
                          {selectionMode === "universe" ? universe : "Custom Symbols"}
                        </span>
                      </div>
                      <div className="flex justify-between">
                        <span className="text-foreground-muted">Range</span>
                        <span className="mono-num text-foreground">
                          {startDate || "--"} → {endDate || "--"}
                        </span>
                      </div>
                      <div className="flex justify-between">
                        <span className="text-foreground-muted">Capital</span>
                        <span className="mono-num text-foreground">
                          {formatCompactInr(Number(initialCapital) || 0, 1)}
                        </span>
                      </div>
                      <div className="flex justify-between">
                        <span className="text-foreground-muted">Strategies</span>
                        <span className="mono-num text-foreground">
                          {selectedCount} active · Σw={selectedWeightSum.toFixed(2)}
                        </span>
                      </div>
                      <Button
                        variant="primary"
                        className="mt-2 w-full"
                        onClick={onRun}
                        disabled={running || selectedCount === 0}
                      >
                        {running ? "Running..." : "Run Backtest"}
                      </Button>
                      {runDisabledReason && (
                        <div className="text-xs text-foreground-muted">{runDisabledReason}</div>
                      )}
                    </CardContent>
                  </Card>
                </div>
              </div>
            </div>

            <div className="flex h-full min-h-0 flex-col overflow-hidden bg-background">
              <div
                className="shrink-0 overflow-y-auto border-b border-border bg-surface custom-scrollbar"
                style={{ maxHeight: "40vh" }}
              >
                <Card variant="void" className="rounded-none border-0">
                  <CardHeader className="flex-row items-center justify-between px-5 py-3">
                    <div>
                      <CardTitle className="text-foreground">Strategies</CardTitle>
                      <p className="text-xs text-foreground-muted">
                        Select and weight strategies for this run
                      </p>
                    </div>
                    <Badge variant="outline">
                      {selectedCount} active · Σw={selectedWeightSum.toFixed(2)}
                    </Badge>
                  </CardHeader>
                  <CardContent className="grid gap-3 px-5 pb-4 pt-0 md:grid-cols-2 xl:grid-cols-3">
                    {strategies.map((s) => {
                      const allocation = allocations.find((a) => a.strategy_id === s.id);
                      return (
                        <Card key={s.id} variant={allocation?.enabled ? "glass" : "outline"}>
                          <CardContent className="space-y-2 p-4">
                            <div className="flex items-start justify-between gap-2">
                              <div>
                                <div className="text-sm font-semibold text-foreground">
                                  {s.name}
                                </div>
                                <div className="text-xs text-foreground-muted">
                                  {s.id.replaceAll("_", " ")}
                                </div>
                              </div>
                              <Checkbox
                                checked={!!allocation?.enabled}
                                onChange={(e) =>
                                  onToggleStrategy(s.id, (e.target as HTMLInputElement).checked)
                                }
                              />
                            </div>
                            <div className="flex items-center justify-between gap-2">
                              <Badge variant="outline" size="xs">
                                {s.id}
                              </Badge>
                              <div className="flex items-center gap-2">
                                <span className="text-xs text-foreground-muted">Weight</span>
                                <Input
                                  type="number"
                                  min="0"
                                  step="0.1"
                                  value={allocation?.weight ?? "0"}
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
                  <p className="text-xs text-foreground-muted">
                    Click a completed run to view results
                  </p>
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
                            <TableCell className="mono-num font-semibold text-foreground">
                              {r.job_id}
                            </TableCell>
                            <TableCell>
                              <Badge variant={runStatusVariant(r.status)}>{r.status}</Badge>
                            </TableCell>
                            <TableCell className="text-foreground">
                              {(r.params.instrument_type || "equity").replace(/^./, (c) =>
                                c.toUpperCase(),
                              )}
                            </TableCell>
                            <TableCell className="mono-num text-foreground-secondary">
                              {r.params.start_date} → {r.params.end_date}
                            </TableCell>
                            <TableCell className="text-foreground-secondary">
                              {r.params.selection?.mode === "symbols"
                                ? "Symbols"
                                : r.params.selection?.universe || "NIFTY50"}
                            </TableCell>
                            <TableCell numeric className="text-foreground">
                              {formatCompactInr(r.params.initial_capital, 1)}
                            </TableCell>
                            <TableCell className="text-right">
                              <Button
                                size="sm"
                                variant={r.status === "failed" ? "secondary" : "link"}
                                onClick={() => void navigate(`/backtest/results/${r.job_id}`)}
                              >
                                {r.status === "failed" ? "Retry" : "View →"}
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
