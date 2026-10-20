import { queryOptions, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { z } from "zod";
import { api, parseResponse, unwrap } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import { queryKeys } from "./keys";

const dateRangeSchema = z.object({
  available: z.boolean().catch(false),
  min_date: z.string().nullable().catch(null),
  max_date: z.string().nullable().catch(null),
  rows: z.number().optional(),
});

const backtestStatusSchema = z.object({
  data_ready: z.boolean().catch(false),
  instrument_capabilities: z
    .record(z.string(), z.object({ enabled: z.boolean(), note: z.string().catch("") }))
    .catch({}),
  universe_ranges: z.record(z.string(), dateRangeSchema).catch({}),
  stock_range: dateRangeSchema.catch({ available: false, min_date: null, max_date: null }),
});

const strategiesSchema = z.object({
  strategies: z.array(
    z.object({ id: z.string(), name: z.string(), default_weight: z.number().optional() }),
  ),
});

const runStatusSchema = z.enum(["running", "completed", "failed"]);

const runsSchema = z.object({
  runs: z.array(
    z.object({
      job_id: z.string(),
      status: runStatusSchema,
      created_at: z.string(),
      params: z
        .object({
          instrument_type: z.string().optional(),
          start_date: z.string().optional(),
          end_date: z.string().optional(),
          initial_capital: z.number().optional(),
          selection: z
            .object({ mode: z.string().optional(), universe: z.string().optional() })
            .optional(),
        })
        .catch({}),
    }),
  ),
});

const jobSchema = z.object({
  job_id: z.string(),
  status: runStatusSchema,
  created_at: z.string(),
  error: z.string().nullish(),
  params: z.object({
    instrument_type: z.string(),
    start_date: z.string(),
    end_date: z.string(),
    selection: z.object({
      mode: z.string(),
      universe: z.string().nullish(),
      symbols: z.array(z.string()).optional(),
    }),
  }),
  result: z
    .object({
      selection: z.object({ scope: z.string() }),
      metrics: z.object({
        final_equity: z.number(),
        total_return_pct: z.number(),
        sharpe_ratio: z.number(),
        max_drawdown_pct: z.number(),
        total_trades: z.number(),
        win_rate_pct: z.number(),
      }),
      equity_curve: z.array(z.object({ date: z.string(), equity: z.number() })).default([]),
      benchmark_curve: z.array(z.object({ date: z.string(), equity: z.number() })).default([]),
      drawdown_curve: z.array(z.object({ date: z.string(), drawdown_pct: z.number() })).default([]),
      trade_log: z
        .array(
          z.object({
            symbol: z.string(),
            entry_date: z.string(),
            exit_date: z.string(),
            entry_price: z.number(),
            exit_price: z.number(),
            return_pct: z.number(),
          }),
        )
        .default([]),
    })
    .optional(),
});

const runResponseSchema = z.object({ job_id: z.string(), status: z.string() });

export type BacktestStatus = z.output<typeof backtestStatusSchema>;
export type StrategyMeta = z.output<typeof strategiesSchema>["strategies"][number];
export type BacktestRunItem = z.output<typeof runsSchema>["runs"][number];
export type BacktestJob = z.output<typeof jobSchema>;
export type BacktestRunPayload = components["schemas"]["BacktestRunRequest"];

const RESULT_POLL_MS = 1200;
const MAX_POLL_ERRORS = 3;

export const backtestStatusQuery = () =>
  queryOptions({
    queryKey: queryKeys.backtest.status(),
    queryFn: async () =>
      parseResponse(backtestStatusSchema, await unwrap(api.GET("/api/backtest/status"))),
  });

export const backtestStrategiesQuery = () =>
  queryOptions({
    queryKey: queryKeys.backtest.strategies(),
    queryFn: async () =>
      parseResponse(strategiesSchema, await unwrap(api.GET("/api/backtest/strategies"))).strategies,
  });

export const backtestRunsQuery = () =>
  queryOptions({
    queryKey: queryKeys.backtest.runs(),
    queryFn: async () =>
      parseResponse(runsSchema, await unwrap(api.GET("/api/backtest/runs"))).runs,
  });

export function useBacktestStatus() {
  return useQuery(backtestStatusQuery());
}

export function useBacktestStrategies() {
  return useQuery(backtestStrategiesQuery());
}

export function useBacktestRuns() {
  return useQuery(backtestRunsQuery());
}

/** Polls until the job completes or fails, and gives up after repeated errors (e.g. a 404). */
export function useBacktestResult(jobId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.backtest.result(jobId ?? ""),
    queryFn: async () =>
      parseResponse(
        jobSchema,
        await unwrap(
          api.GET("/api/backtest/result/{job_id}", {
            params: { path: { job_id: jobId ?? "" } },
          }),
        ),
      ),
    enabled: jobId !== undefined && jobId !== "",
    staleTime: 0,
    refetchInterval: (query) => {
      const job = query.state.data;
      if (job && job.status !== "running") return false;
      if (query.state.errorUpdateCount >= MAX_POLL_ERRORS) return false;
      return RESULT_POLL_MS;
    },
  });
}

export function useRunBacktest() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: BacktestRunPayload) =>
      parseResponse(runResponseSchema, await unwrap(api.POST("/api/backtest/run", { body }))),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.backtest.runs() }),
  });
}
