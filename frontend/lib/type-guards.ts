/**
 * Type Guards
 * ===========
 * Runtime type checking utilities for unknown types
 */

// ============================================================================
// Basic Type Guards
// ============================================================================

export function isString(value: unknown): value is string {
  return typeof value === 'string';
}

export function isNumber(value: unknown): value is number {
  return typeof value === 'number' && !isNaN(value);
}

export function isBoolean(value: unknown): value is boolean {
  return typeof value === 'boolean';
}

export function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export function isArray(value: unknown): value is unknown[] {
  return Array.isArray(value);
}

// ============================================================================
// Error Type Guards
// ============================================================================

export function isError(value: unknown): value is Error {
  return value instanceof Error;
}

export function getErrorMessage(error: unknown): string {
  if (isError(error)) {
    return error.message;
  }
  if (isString(error)) {
    return error;
  }
  if (isObject(error) && 'message' in error && isString(error.message)) {
    return error.message;
  }
  return 'An unknown error occurred';
}

// ============================================================================
// API Response Type Guards
// ============================================================================

export interface APIError {
  message: string;
  code?: string;
  details?: unknown;
}

export function isAPIError(value: unknown): value is APIError {
  return (
    isObject(value) &&
    'message' in value &&
    isString(value.message)
  );
}

// ============================================================================
// WebSocket Message Type Guards
// ============================================================================

export interface TickerData {
  symbol: string;
  ltp: number;
  change_pct?: number;
  volume?: number;
  high?: number;
  low?: number;
  open?: number;
}

export function isTickerData(value: unknown): value is TickerData {
  return (
    isObject(value) &&
    'symbol' in value &&
    isString(value.symbol) &&
    'ltp' in value &&
    isNumber(value.ltp)
  );
}

export interface TickerBatchMessage {
  type: 'ticker_batch';
  data: TickerData[];
}

export function isTickerBatchMessage(value: unknown): value is TickerBatchMessage {
  return (
    isObject(value) &&
    'type' in value &&
    value.type === 'ticker_batch' &&
    'data' in value &&
    isArray(value.data) &&
    value.data.every(isTickerData)
  );
}

// ============================================================================
// Validation Helpers
// ============================================================================

export function assertNever(value: never): never {
  throw new Error(`Unexpected value: ${JSON.stringify(value)}`);
}

export function isDefined<T>(value: T | undefined | null): value is T {
  return value !== undefined && value !== null;
}

export function hasProperty<K extends string>(
  obj: unknown,
  key: K
): obj is Record<K, unknown> {
  return isObject(obj) && key in obj;
}

export function hasStringProperty<K extends string>(
  obj: unknown,
  key: K
): obj is Record<K, string> {
  return hasProperty(obj, key) && isString(obj[key]);
}

export function hasNumberProperty<K extends string>(
  obj: unknown,
  key: K
): obj is Record<K, number> {
  return hasProperty(obj, key) && isNumber(obj[key]);
}
