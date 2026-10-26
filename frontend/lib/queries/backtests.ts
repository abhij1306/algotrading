import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import type { StartBody } from "@/lib/backtest-form";
import {
  parseCsvTable,
  type AuditDoc,
  type Run,
  type RunAudit,
  type RunFile,
  type RunSummary,
} from "@/lib/runs";
import { queryKeys } from "./keys";

export type BacktestJob = components["schemas"]["BacktestJob"];

/** How often the jobs list polls while a job runs. */
export const JOB_POLL_MS = 3_000;

const isRunning = (jobs: readonly BacktestJob[] | undefined) =>
  jobs?.some((job) => job.status === "running") ?? false;

// The API types run.json's sections as open objects; the writer validates every run.json against
// docs/backtests/run.schema.json before it publishes the folder, which lib/runs.ts types.
const asRuns = (items: readonly components["schemas"]["BacktestRunItem"][]) =>
  items as unknown as RunSummary[];
const asRun = (body: Readonly<Record<string, unknown>>) => body as unknown as Run;

/** Every run folder's summary, newest first; the page filters and groups them. */
export function useBacktestRuns() {
  return useQuery({
    queryKey: queryKeys.backtests.runs(),
    queryFn: async () => asRuns(await unwrap(api.GET("/api/hat/backtests/runs"))),
  });
}

function runQuery(id: string) {
  return {
    queryKey: queryKeys.backtests.run(id),
    queryFn: async () =>
      asRun(
        await unwrap(
          api.GET("/api/hat/backtests/runs/{run_id}", { params: { path: { run_id: id } } }),
        ),
      ),
    enabled: id !== "",
  };
}

/** One run's run.json; an empty id reads nothing (a run without a base). */
export function useBacktestRun(id: string) {
  return useQuery(runQuery(id));
}

/** Several runs' run.json at once, for the compare view. */
export function useBacktestRunsById(ids: readonly string[]) {
  return useQueries({ queries: ids.map(runQuery) });
}

function fileQuery(id: string, name: RunFile) {
  return {
    queryKey: queryKeys.backtests.file(id, name),
    queryFn: async () =>
      parseCsvTable(
        await unwrap(
          api.GET("/api/hat/backtests/runs/{run_id}/files/{name}", {
            params: { path: { run_id: id, name } },
            parseAs: "text",
          }),
        ),
      ),
    // A run's files never change once its folder is published.
    staleTime: Infinity,
  };
}

/** One CSV of a run folder, parsed. */
export function useRunFile(id: string, name: RunFile) {
  return useQuery(fileQuery(id, name));
}

/** The same CSV of several runs, for the compare view. */
export function useRunFiles(ids: readonly string[], name: RunFile) {
  return useQueries({ queries: ids.map((id) => fileQuery(id, name)) });
}

/** A run's run audit or the data audit it read; a run without one reads 404. */
export function useRunAudit(id: string, name: RunAudit) {
  return useQuery({
    queryKey: queryKeys.backtests.audit(id, name),
    queryFn: async () =>
      (await unwrap(
        api.GET("/api/hat/backtests/runs/{run_id}/audits/{name}", {
          params: { path: { run_id: id, name } },
        }),
      )) as unknown as AuditDoc,
    // A run's audits never change once its folder is published.
    staleTime: Infinity,
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
