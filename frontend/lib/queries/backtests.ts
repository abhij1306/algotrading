import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import type { StartBody } from "@/lib/backtest-form";
import { queryKeys } from "./keys";

export type BacktestJob = components["schemas"]["BacktestJob"];

/** How often the jobs list polls while a job runs. */
export const JOB_POLL_MS = 3_000;

const isRunning = (jobs: readonly BacktestJob[] | undefined) =>
  jobs?.some((job) => job.status === "running") ?? false;

export function useBacktestOptions() {
  return useQuery({
    queryKey: queryKeys.backtests.options(),
    queryFn: () => unwrap(api.GET("/api/hat/backtests/options")),
  });
}

/** The jobs started since the server started, polled while one runs. */
export function useBacktestJobs() {
  return useQuery({
    queryKey: queryKeys.backtests.jobs(),
    queryFn: () => unwrap(api.GET("/api/hat/backtests/jobs")),
    refetchInterval: (query) => (isRunning(query.state.data) ? JOB_POLL_MS : false),
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
