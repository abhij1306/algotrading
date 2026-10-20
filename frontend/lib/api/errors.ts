/**
 * The single error type thrown by the API layer. Every failure (HTTP error,
 * network failure, unexpected response shape) is normalised into this class.
 */
export type ApiErrorCode =
  | "NETWORK_ERROR"
  | "HTTP_ERROR"
  | "VALIDATION_ERROR"
  | "RESPONSE_SHAPE_ERROR"
  | (string & Record<never, never>);

export class ApiError extends Error {
  readonly code: ApiErrorCode;
  readonly status: number | null;
  readonly details: unknown;

  constructor(input: { code: ApiErrorCode; message: string; status?: number; details?: unknown }) {
    super(input.message);
    this.name = "ApiError";
    this.code = input.code;
    this.status = input.status ?? null;
    this.details = input.details;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function firstValidationMessage(detail: unknown[]): string | null {
  for (const item of detail) {
    if (isRecord(item) && typeof item.msg === "string") return item.msg;
  }
  return null;
}

/**
 * Normalise an error response body. Handles the backend envelope
 * `{ error: { code, message } }` and FastAPI's `{ detail: string | [{ msg }] }`.
 */
export function apiErrorFromResponse(input: {
  status: number;
  statusText: string;
  body: unknown;
}): ApiError {
  const { status, statusText, body } = input;
  const statusSuffix = statusText ? `: ${statusText}` : "";
  const fallback = `HTTP ${status}${statusSuffix}`;

  if (isRecord(body)) {
    const envelope = body.error;
    if (isRecord(envelope) && typeof envelope.message === "string") {
      return new ApiError({
        code: typeof envelope.code === "string" ? envelope.code : "HTTP_ERROR",
        message: envelope.message,
        status,
        details: envelope.details,
      });
    }
    const detail = body.detail;
    if (typeof detail === "string") {
      return new ApiError({ code: "HTTP_ERROR", message: detail, status, details: body });
    }
    if (Array.isArray(detail)) {
      return new ApiError({
        code: "VALIDATION_ERROR",
        message: firstValidationMessage(detail) ?? fallback,
        status,
        details: detail,
      });
    }
  }
  if (typeof body === "string" && body.trim() !== "") {
    return new ApiError({ code: "HTTP_ERROR", message: body, status });
  }
  return new ApiError({ code: "HTTP_ERROR", message: fallback, status });
}

export function apiErrorFromThrown(thrown: unknown): ApiError {
  if (thrown instanceof ApiError) return thrown;
  const message = thrown instanceof Error ? thrown.message : "Network request failed";
  return new ApiError({ code: "NETWORK_ERROR", message, details: thrown });
}
