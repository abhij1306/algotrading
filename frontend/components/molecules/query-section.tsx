import type { ReactNode } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/atoms/card";
import { Text } from "@/components/atoms/text";

export type Query<T> = Readonly<{ data: T | undefined; error: Error | null; isPending: boolean }>;

/** A titled card with the loading, error and empty states of one query; `children` renders the data. */
export function QuerySection<T>(
  props: Readonly<{
    title: string;
    query: Query<T>;
    isEmpty: (data: T) => boolean;
    empty: string;
    children: (data: T) => ReactNode;
  }>,
) {
  const { title, query, isEmpty, empty, children } = props;
  let body: ReactNode;
  if (query.data !== undefined) {
    body = isEmpty(query.data) ? <Text tone="muted">{empty}</Text> : children(query.data);
  } else if (query.error) {
    body = (
      <Text role="alert" tone="loss">
        {query.error.message}
      </Text>
    );
  } else {
    body = <Text tone="muted">Loading…</Text>;
  }
  return (
    <Card aria-label={title} role="region">
      <CardHeader>
        <CardTitle>{title}</CardTitle>
      </CardHeader>
      <CardContent>{body}</CardContent>
    </Card>
  );
}

/** `isEmpty` for a list section. */
export const noRows = (rows: readonly unknown[]) => rows.length === 0;
