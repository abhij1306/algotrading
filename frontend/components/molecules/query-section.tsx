import type { ReactNode } from "react";
import { Text } from "@/components/atoms/text";
import { cn } from "@/lib/utils";
import { freshness, type QueryState } from "@/lib/queries/freshness";
import { formatIstTime } from "@/lib/time";

/** The stale-data notice: the data below is the last good read, not the current state. */
export function StaleNotice({ since, message }: Readonly<{ since: number; message: string }>) {
  const when = since > 0 ? ` since ${formatIstTime(since)} IST` : "";
  return (
    <p role="alert" className="mb-3 rounded-md bg-warning-bg px-3 py-2 text-sm text-warning">
      Stale{when}: the last refresh failed ({message}). Showing the last good read.
    </p>
  );
}

function Body<T>({
  query,
  isEmpty,
  empty,
  children,
}: Readonly<{
  query: QueryState<T>;
  isEmpty: (data: T) => boolean;
  empty: string;
  children: (data: T) => ReactNode;
}>) {
  const { data, error } = query;
  if (data === undefined) {
    return error ? (
      <Text role="alert" tone="loss">
        Could not load: {error.message}
      </Text>
    ) : (
      <Text tone="muted" aria-busy="true">
        Loading…
      </Text>
    );
  }
  return isEmpty(data) ? <Text tone="muted">{empty}</Text> : children(data);
}

/**
 * One titled dashboard section with the loading, error, empty and stale states of its query.
 * A failed refetch keeps the last data on screen, dimmed, under a stale notice.
 */
export function QuerySection<T>(
  props: Readonly<{
    title: string;
    query: QueryState<T>;
    isEmpty: (data: T) => boolean;
    empty: string;
    /** A short note beside the title, such as a count or a date. */
    meta?: (data: T) => ReactNode;
    className?: string;
    children: (data: T) => ReactNode;
  }>,
) {
  const { title, query, isEmpty, meta, className } = props;
  const stale = freshness(query) === "stale";
  const note = meta && query.data !== undefined && !isEmpty(query.data) ? meta(query.data) : null;
  return (
    <section aria-label={title} className={cn("min-w-0", className)}>
      <header className="mb-3 flex items-baseline justify-between gap-4">
        <h2 className="text-lg">{title}</h2>
        {note === null ? null : <span className="text-xs text-foreground-muted">{note}</span>}
      </header>
      {stale && query.error ? (
        <StaleNotice since={query.dataUpdatedAt} message={query.error.message} />
      ) : null}
      <div className={stale ? "opacity-60" : undefined}>
        <Body {...props} />
      </div>
    </section>
  );
}

/** `isEmpty` for a list section. */
export const noRows = (rows: readonly unknown[]) => rows.length === 0;

/** `meta` for a list section: its row count. */
export const rowCount = (rows: readonly unknown[]) => `${rows.length}`;
