/**
 * The single error type thrown by the API layer. Every failure (HTTP error,
 * network failure, unexpected response shape) is normalised into this class.
 */
export type ApiErrorCode =
  | "NETWORK_ERROR"
  | "HTTP_ERROR"
  | "VALIDATION_ERROR"
  | "RESPONSE_SHAPE_ERROR"
  | "DATABASE_UNAVAILABLE"
  | "BROKER_UNAVAILABLE"
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
 * Fixed, user-facing wording for the backend's "dependency is down" codes. These win over
 * the server's message so every screen reads the same when Postgres or the broker is down.
 */
const UNAVAILABLE_MESSAGES: ReadonlyMap<string, string> = new Map([
  [
    "DATABASE_UNAVAILABLE",
    "Database unavailable: the backend cannot reach Postgres. Retry once it is back.",
  ],
  ["BROKER_UNAVAILABLE", "Broker unavailable: Fyers could not be reached. Retry once it is back."],
]);

const SERVICE_UNAVAILABLE_MESSAGE = "Service unavailable: the backend is not ready. Retry shortly.";

/** The backend envelope `{ error: { code, message } }`, or null when it is not one. */
function fromEnvelope(envelope: unknown, status: number): ApiError | null {
  if (!isRecord(envelope)) return null;
  const code = typeof envelope.code === "string" ? envelope.code : null;
  const friendly = code === null ? undefined : UNAVAILABLE_MESSAGES.get(code);
  if (code !== null && friendly !== undefined) {
    return new ApiError({ code, message: friendly, status, details: envelope.details });
  }
  if (typeof envelope.message !== "string") return null;
  return new ApiError({
    code: code ?? "HTTP_ERROR",
    message: envelope.message,
    status,
    details: envelope.details,
  });
}

/** FastAPI's `{ detail: string | [{ msg }] }`, or null when the body is not one. */
function fromDetail(input: {
  body: Record<string, unknown>;
  status: number;
  fallback: string;
}): ApiError | null {
  const { body, status, fallback } = input;
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
    const parsed = fromEnvelope(body.error, status) ?? fromDetail({ body, status, fallback });
    if (parsed) return parsed;
  }
  if (typeof body === "string" && body.trim() !== "") {
    return new ApiError({ code: "HTTP_ERROR", message: body, status });
  }
  const message = status === 503 ? SERVICE_UNAVAILABLE_MESSAGE : fallback;
  return new ApiError({ code: "HTTP_ERROR", message, status });
}

export function apiErrorFromThrown(thrown: unknown): ApiError {
  if (thrown instanceof ApiError) return thrown;
  const message = thrown instanceof Error ? thrown.message : "Network request failed";
  return new ApiError({ code: "NETWORK_ERROR", message, details: thrown });
}
