import { useEffect } from "react";
import { useRouteError } from "react-router";
import { Button } from "@/components/ui/button";

export default function RouteError() {
  const error = useRouteError();

  useEffect(() => {
    console.error(error);
  }, [error]);

  return (
    <div className="flex flex-col items-center justify-center min-h-[400px] gap-4">
      <h2 className="text-lg font-semibold">Something went wrong</h2>
      <Button onClick={() => globalThis.location.reload()}>Try again</Button>
    </div>
  );
}
