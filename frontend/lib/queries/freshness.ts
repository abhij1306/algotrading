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

/** Several polled reads stale at once: one shared cause, such as the backend being down. */
export type SharedStale = Readonly<{ since: number; message: string }>;

/**
 * The shared stale state of the dashboard's queries: set when more than one of them is stale,
 * with the earliest last good read and the common error (or a summary when the errors differ).
 * A single stale query is that section's own problem, so it gives null.
 */
export function sharedStale(queries: readonly QueryState<unknown>[]): SharedStale | null {
  const stale = queries.filter((query) => freshness(query) === "stale");
  if (stale.length < 2) return null;
  const messages = new Set(stale.map((query) => query.error?.message ?? ""));
  const [only] = messages;
  return {
    since: Math.min(...stale.map((query) => query.dataUpdatedAt)),
    message: messages.size === 1 ? only : `${stale.length} reads failed`,
  };
}
