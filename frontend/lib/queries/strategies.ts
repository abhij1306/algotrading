import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { z } from "zod";
import { api, parseResponse, unwrap } from "@/lib/api/client";
import { queryKeys } from "./keys";

const HALT_REASON = "Manual halt from Strategies page";

const optionalNumber = z.number().nullish().catch(null);

const regimeSchema = z.object({
  regime: z.string().catch(""),
  close: z.number().catch(0),
  ma_200: z.number().catch(0),
  risk_pct: z.number().catch(0),
});

const strategyStatusSchema = z.object({
  universe: z.string().optional().catch(undefined),
  halted: z.boolean().catch(false),
  halt_reason: z.string().nullish().catch(null),
  halted_at: z.string().nullish().catch(null),
  latest_scan_date: z.string().nullish().catch(null),
  pending_entries: z.number().catch(0),
  open_positions: z.number().catch(0),
  regime: regimeSchema.nullable().catch(null),
});

const scanRowSchema = z.object({
  id: z.number(),
  symbol: z.string(),
  company_name: z.string().catch(""),
  sector: z.string().catch(""),
  grade: z.string().catch(""),
  rs_rating: z.number().catch(0),
  contraction_count: z.number().catch(0),
  contraction_depths: z.array(z.number()).catch([]),
  final_contraction_depth: z.number().catch(0),
  volume_dry_up_pct: z.number().catch(0),
  pivot_high: z.number().catch(0),
  stop_level: z.number().catch(0),
  stop_pct: z.number().catch(0),
  days_in_base: z.number().catch(0),
  is_breakout: z.boolean().catch(false),
  breakout_price: optionalNumber,
  breakout_volume_mult: optionalNumber,
  signal_status: z.string().catch(""),
  planned_shares: z.number().catch(0),
  planned_position_value: z.number().catch(0),
  capital_risk: z.number().catch(0),
  two_r_target: z.number().catch(0),
});

/** Where the last price sits against the stop and 2R target; unknown values read as unavailable. */
export const PRICE_STATUSES = ["OPEN", "STOP_HIT", "TARGET_HIT", "PRICE_UNAVAILABLE"] as const;
export type PriceStatus = (typeof PRICE_STATUSES)[number];

/** A live-priced number: null when the backend has no quote, and null for garbage too, never 0. */
const livePrice = z.number().nullable().catch(null);

export const positionRowSchema = z.object({
  id: z.number(),
  symbol: z.string(),
  entry_date: z.string().catch(""),
  entry_price: z.number().catch(0),
  shares: z.number().catch(0),
  stop_price: z.number().catch(0),
  ltp: livePrice,
  unrealized_pnl: livePrice,
  unrealized_pnl_pct: livePrice,
  r_multiple: livePrice,
  price_status: z.enum(PRICE_STATUSES).catch("PRICE_UNAVAILABLE"),
  two_r_status: z.string().catch(""),
  status: z.string().catch(""),
});

const backtestRunSchema = z.object({
  run_id: z.string(),
  name: z.string().nullish().catch(null),
  status: z.string().catch(""),
  universe: z.string().nullish().catch(null),
  created_at: z.string().nullish().catch(null),
  start_date: z.string().nullish().catch(null),
  end_date: z.string().nullish().catch(null),
  metrics: z
    .object({
      total_return_pct: z.number().optional().catch(undefined),
      sharpe_ratio: z.number().optional().catch(undefined),
      total_trades: z.number().optional().catch(undefined),
    })
    .catch({}),
});

const chartPointSchema = z.object({
  date: z.string(),
  close: z.number(),
  volume: z.number().catch(0),
  ema_21: optionalNumber,
  ma_50: optionalNumber,
  ma_150: optionalNumber,
  ma_200: optionalNumber,
});

const signalDetailSchema = z.object({
  signal: scanRowSchema,
  chart: z.array(chartPointSchema).catch([]),
});

const metricValueSchema = z.union([z.number(), z.string()]);

const backtestResultSchema = z.object({
  run_id: z.string(),
  status: z.string().catch(""),
  result: z
    .object({
      metrics: z.record(z.string(), metricValueSchema).catch({}),
    })
    .optional()
    .catch(undefined),
});

export type StrategyStatus = z.output<typeof strategyStatusSchema>;
export type ScanRow = z.output<typeof scanRowSchema>;
export type PositionRow = z.output<typeof positionRowSchema>;
export type BacktestRun = z.output<typeof backtestRunSchema>;
export type SignalDetail = z.output<typeof signalDetailSchema>;
export type StrategyBacktestResult = z.output<typeof backtestResultSchema>;

export interface StrategiesWorkspace {
  status: StrategyStatus;
  results: ScanRow[];
  positions: PositionRow[];
  runs: BacktestRun[];
}

/** Status, latest scan, open positions and backtest history for one universe. */
export function useStrategiesWorkspace(params: { universe: string; showAll: boolean }) {
  const { universe, showAll } = params;
  return useQuery({
    queryKey: queryKeys.strategies.workspace({ universe, showAll }),
    queryFn: async (): Promise<StrategiesWorkspace> => {
      const [status, scan, positions, history] = await Promise.all([
        unwrap(api.GET("/api/strategies/status", { params: { query: { universe } } })).then(
          (body) => parseResponse(strategyStatusSchema, body),
        ),
        unwrap(
          api.GET("/api/strategies/vcp/scan/latest", {
            params: { query: { universe, show_all: showAll } },
          }),
        ).then((body) => parseResponse(z.object({ results: z.array(scanRowSchema) }), body)),
        unwrap(api.GET("/api/strategies/positions")).then((body) =>
          parseResponse(z.object({ positions: z.array(positionRowSchema) }), body),
        ),
        unwrap(
          api.GET("/api/strategies/vcp/backtest/history", { params: { query: { universe } } }),
        ).then((body) => parseResponse(z.object({ runs: z.array(backtestRunSchema) }), body)),
      ]);
      return {
        status,
        results: scan.results,
        positions: positions.positions,
        runs: history.runs,
      };
    },
    retry: false,
  });
}

/** Scan row and price chart for one symbol; off while `symbol` is null. */
export function useStrategySignal(symbol: string | null) {
  return useQuery({
    queryKey: queryKeys.strategies.signal(symbol ?? ""),
    queryFn: async () =>
      parseResponse(
        signalDetailSchema,
        await unwrap(
          api.GET("/api/strategies/vcp/signal/{symbol}", {
            params: { path: { symbol: symbol ?? "" } },
          }),
        ),
      ),
    enabled: symbol !== null,
    retry: false,
  });
}

function useInvalidateStrategies() {
  const queryClient = useQueryClient();
  return () => queryClient.invalidateQueries({ queryKey: queryKeys.strategies.all });
}

export function useRunStrategyScan() {
  const invalidate = useInvalidateStrategies();
  return useMutation({
    mutationFn: async (universe: string) => {
      await unwrap(api.POST("/api/strategies/vcp/scan/run", { body: { universe } }));
    },
    onSuccess: invalidate,
  });
}

/**
 * Halts (or resumes) trading. The halt flag is global, so it is written into every cached
 * workspace at once; the rest of the returned status is for the default universe only, so
 * the workspaces are refetched for their own counts.
 */
export function useToggleStrategyHalt() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (action: "halt" | "resume") => {
      const request =
        action === "halt"
          ? api.POST("/api/strategies/halt", { body: { reason: HALT_REASON } })
          : api.POST("/api/strategies/resume");
      return parseResponse(strategyStatusSchema, await unwrap(request));
    },
    onSuccess: ({ halted, halt_reason, halted_at }) => {
      queryClient.setQueriesData<StrategiesWorkspace>(
        { queryKey: queryKeys.strategies.workspaceRoot() },
        (prev) =>
          prev ? { ...prev, status: { ...prev.status, halted, halt_reason, halted_at } } : prev,
      );
      return queryClient.invalidateQueries({ queryKey: queryKeys.strategies.workspaceRoot() });
    },
  });
}

/** Cancels a queued breakout, or queues it for entry. */
export function useToggleSignalQueue() {
  const invalidate = useInvalidateStrategies();
  return useMutation({
    mutationFn: async (row: Pick<ScanRow, "id" | "signal_status">) => {
      const path = { signal_id: row.id };
      await unwrap(
        row.signal_status === "PENDING_ENTRY"
          ? api.POST("/api/strategies/vcp/signal/{signal_id}/cancel", { params: { path } })
          : api.POST("/api/strategies/vcp/signal/{signal_id}/queue", { params: { path } }),
      );
    },
    onSuccess: invalidate,
  });
}

export function useCloseStrategyPosition() {
  const invalidate = useInvalidateStrategies();
  return useMutation({
    mutationFn: async (positionId: number) => {
      await unwrap(
        api.POST("/api/strategies/positions/{position_id}/close", {
          params: { path: { position_id: positionId } },
        }),
      );
    },
    onSuccess: invalidate,
  });
}

export function useRunStrategyBacktest() {
  const invalidate = useInvalidateStrategies();
  return useMutation({
    mutationFn: async (input: {
      universe: string;
      startDate: string;
      endDate: string;
      initialCapital: number;
    }) =>
      parseResponse(
        backtestResultSchema,
        await unwrap(
          api.POST("/api/strategies/vcp/backtest/run", {
            body: {
              universe: input.universe,
              start_date: input.startDate,
              end_date: input.endDate,
              initial_capital: input.initialCapital,
            },
          }),
        ),
      ),
    onSuccess: invalidate,
  });
}
