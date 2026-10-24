import { isRouteErrorResponse, useRouteError } from "react-router";
import { Button } from "@/components/atoms/button";
import { PageLayout } from "@/components/templates/page-layout";

function errorText(error: unknown): string {
  if (isRouteErrorResponse(error)) return `${error.status} ${error.statusText}`;
  return error instanceof Error ? error.message : "Unknown error";
}

/** The one error screen: a page that failed to load or render, inside the shell when it can be. */
export default function RouteError() {
  const error = useRouteError();
  return (
    <PageLayout>
      <div role="alert" className="max-w-xl space-y-4">
        <h1 className="text-xl">This page failed to load</h1>
        <p className="text-sm text-foreground-secondary">
          Reload to try again. If it keeps failing, check the backend logs.
        </p>
        <p className="font-mono text-xs text-foreground-muted">{errorText(error)}</p>
        <Button variant="primary" onClick={() => globalThis.location.reload()}>
          Reload
        </Button>
      </div>
    </PageLayout>
  );
}
