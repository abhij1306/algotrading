import createClient from "openapi-fetch";
import { ApiError, apiErrorFromResponse, apiErrorFromThrown } from "./errors";
import type { paths } from "./schema";

// The header the backend requires on every write (HAT-DEC-BD5, app/hat/api/guard.py). A page on
// another site cannot set it without a CORS preflight, which the backend answers only for the
// frontend's origins. It is not a secret: anything in the bundle is public.
export const WRITE_HEADER = "X-HAT-Request";
const READ_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

// Same-origin by default so the Vite dev proxy forwards `/api/*` to the backend.
// Set VITE_API_URL to call a backend on another origin.
export function createApiClient(options: { baseUrl?: string; fetch?: typeof fetch } = {}) {
  const client = createClient<paths>({
    baseUrl: options.baseUrl ?? (import.meta.env.VITE_API_URL || ""),
    // Resolve fetch per request so tests can stub it.
    fetch: (request) => (options.fetch ?? globalThis.fetch)(request),
  });
  client.use({
    onRequest({ request }) {
      if (!READ_METHODS.has(request.method)) request.headers.set(WRITE_HEADER, "1");
      return request;
    },
  });
  return client;
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
