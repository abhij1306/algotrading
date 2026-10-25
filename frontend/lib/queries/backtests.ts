import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import type { StartBody } from "@/lib/backtest-form";
import { queryKeys } from "./keys";

export type BacktestJob = components["schemas"]["BacktestJob"];

/** The list shows the newest runs up to the route's cap; the filters work on these. */
export const RUNS_LIMIT = 200;
/** How often the jobs list polls while a job runs. */
export const JOB_POLL_MS = 3_000;

const isRunning = (jobs: readonly BacktestJob[] | undefined) =>
  jobs?.some((job) => job.status === "running") ?? false;

export function useBacktestRuns() {
  return useQuery({
    queryKey: queryKeys.backtests.runs(),
    queryFn: () =>
      unwrap(api.GET("/api/hat/backtests", { params: { query: { limit: RUNS_LIMIT } } })),
  });
}

function runQuery(id: string) {
  return {
    queryKey: queryKeys.backtests.run(id),
    queryFn: () =>
      unwrap(api.GET("/api/hat/backtests/{run_id}", { params: { path: { run_id: id } } })),
  };
}

export function useBacktestRun(id: string) {
  return useQuery(runQuery(id));
}

/** Several runs' details at once, for the compare view. */
export function useBacktestRunsById(ids: readonly string[]) {
  return useQueries({ queries: ids.map(runQuery) });
}

export function useBacktestTrades(id: string, enabled: boolean) {
  return useQuery({
    queryKey: queryKeys.backtests.trades(id),
    queryFn: () =>
      unwrap(api.GET("/api/hat/backtests/{run_id}/trades", { params: { path: { run_id: id } } })),
    enabled,
  });
}

export function useBacktestOptions() {
  return useQuery({
    queryKey: queryKeys.backtests.options(),
    queryFn: () => unwrap(api.GET("/api/hat/backtests/options")),
  });
}

/**
 * The jobs started since the server started, polled while one runs. A read that sees a job
 * finish (or a variant land) refreshes the runs list, so the new runs show without a reload.
 */
export function useBacktestJobs() {
  const queryClient = useQueryClient();
  return useQuery({
    queryKey: queryKeys.backtests.jobs(),
    queryFn: async () => {
      const before = queryClient.getQueryData<BacktestJob[]>(queryKeys.backtests.jobs());
      const jobs = await unwrap(api.GET("/api/hat/backtests/jobs"));
      if (jobsMoved(before, jobs)) {
        void queryClient.invalidateQueries({ queryKey: queryKeys.backtests.runs() });
      }
      return jobs;
    },
    refetchInterval: (query) => (isRunning(query.state.data) ? JOB_POLL_MS : false),
  });
}

/** Whether a job read shows new finished variants or a changed status since the last read. */
export function jobsMoved(
  before: readonly BacktestJob[] | undefined,
  after: readonly BacktestJob[],
): boolean {
  if (before === undefined) return false;
  const seen = new Map(before.map((job) => [job.id, job]));
  return after.some((job) => {
    const old = seen.get(job.id);
    return (
      old === undefined || old.status !== job.status || old.outcomes.length !== job.outcomes.length
    );
  });
}

/** Starts a job; the jobs list then polls it. */
export function useStartBacktest() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: StartBody) => unwrap(api.POST("/api/hat/backtests", { body })),
    onSuccess: (job) => {
      queryClient.setQueryData<BacktestJob[]>(queryKeys.backtests.jobs(), (old) => [
        job,
        ...(old ?? []).filter((other) => other.id !== job.id),
      ]);
      void queryClient.invalidateQueries({ queryKey: queryKeys.backtests.jobs() });
    },
  });
}
