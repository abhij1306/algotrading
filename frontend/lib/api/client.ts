import createClient from "openapi-fetch";
import type { z } from "zod";
import { ApiError, apiErrorFromResponse, apiErrorFromThrown } from "./errors";
import type { paths } from "./schema";

// Same-origin by default so the Vite dev proxy forwards `/api/*` to the backend.
// Set VITE_API_URL to call a backend on another origin.
export function createApiClient(options: { baseUrl?: string; fetch?: typeof fetch } = {}) {
  return createClient<paths>({
    baseUrl: options.baseUrl ?? (import.meta.env.VITE_API_URL || ""),
    // Resolve fetch per request so tests can stub it.
    fetch: (request) => (options.fetch ?? globalThis.fetch)(request),
  });
}

export const api = createApiClient();

type FetchResult<D, E> =
  | { data: D; error?: never; response: Response }
  | { data?: never; error: E; response: Response };

/** Resolve to the typed response body, or throw a normalised `ApiError`. */
export async function unwrap<D, E>(request: Promise<FetchResult<D, E>>): Promise<NonNullable<D>> {
  let result: FetchResult<D, E>;
  try {
    result = await request;
  } catch (error_) {
    throw apiErrorFromThrown(error_);
  }
  if (result.error !== undefined || !result.response.ok) {
    throw apiErrorFromResponse({
      status: result.response.status,
      statusText: result.response.statusText,
      body: result.error,
    });
  }
  const { data } = result;
  if (data === undefined || data === null) {
    throw new ApiError({
      code: "RESPONSE_SHAPE_ERROR",
      message: "Empty response from server",
      status: result.response.status,
    });
  }
  return data;
}

/**
 * Validate a loosely-typed (`unknown`) response against a schema at the
 * boundary. Rows are trusted afterwards.
 */
export function parseResponse<S extends z.ZodType>(schema: S, data: unknown): z.output<S> {
  const parsed = schema.safeParse(data);
  if (!parsed.success) {
    throw new ApiError({
      code: "RESPONSE_SHAPE_ERROR",
      message: "Unexpected response from server",
      details: parsed.error.issues,
    });
  }
  return parsed.data;
}
