/**
 * How current a polled query's data is. TanStack Query keeps the last good `data` when a refetch
 * fails and sets `error`, so data plus an error means the view is stale, never current.
 */
export type Freshness = "loading" | "error" | "fresh" | "stale";

export type QueryState<T> = Readonly<{
  data: T | undefined;
  error: Error | null;
  /** When the last good data arrived (epoch ms; 0 before any). */
  dataUpdatedAt: number;
}>;

export function freshness(query: Readonly<{ data: unknown; error: Error | null }>): Freshness {
  if (query.data === undefined) return query.error ? "error" : "loading";
  return query.error ? "stale" : "fresh";
}
