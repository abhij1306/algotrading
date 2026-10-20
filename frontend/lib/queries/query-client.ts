import { QueryClient } from "@tanstack/react-query";
import { ApiError } from "@/lib/api/errors";

const MAX_RETRIES = 2;

/** Retry network/5xx failures a couple of times; never retry 4xx or shape errors. */
export function shouldRetry(failureCount: number, error: Error): boolean {
  if (failureCount >= MAX_RETRIES) return false;
  if (!(error instanceof ApiError)) return false;
  if (error.code === "NETWORK_ERROR") return true;
  return error.status !== null && error.status >= 500;
}

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: shouldRetry,
        refetchOnWindowFocus: false,
        staleTime: 15_000,
      },
    },
  });
}
