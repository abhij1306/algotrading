import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { z } from "zod";
import { api, parseResponse, unwrap } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import type { components } from "@/lib/api/schema";
import { roundToDecimals } from "@/lib/format";
import { queryKeys } from "./keys";
import { shouldRetry } from "./query-client";

const POSITIONS_POLL_MS = 7000;
const MAX_SEARCH_RESULTS = 8;

const rowsSchema = z.array(z.record(z.string(), z.unknown())).catch([]);

// Number(value || 0), as the page did before validation moved here.
const lenientNumber = z.coerce.number().catch(0);

const positionSchema = z.object({
  id: z.union([z.string(), z.number()]).transform(String).catch(""),
  symbol: z.string(),
  net_qty: lenientNumber,
  entry_price: lenientNumber,
  current_price: lenientNumber,
  unrealized_pnl: lenientNumber,
  realized_pnl: lenientNumber,
  // Absent means unknown (fall back to unrealized); 0 is a real net P&L.
  net_pnl: z.number().nullish().catch(undefined),
  product_type: z.string().catch(""),
  mode: z.enum(["PAPER", "LIVE"]).optional().catch(undefined),
  // A LIVE row from a failed broker sync: the last synced values, not the current book.
  stale: z.boolean().catch(false),
});

const positionsBookSchema = z.object({
  live_positions: z.array(positionSchema).catch([]),
  paper_positions: z.array(positionSchema).catch([]),
  // The LIVE sync failed: live rows (if any) are the last synced ones.
  live_stale: z.boolean().catch(false),
});

const squareResultSchema = z.object({
  status: z.string().optional().catch(undefined),
  message: z.string().optional().catch(undefined),
});

export interface WatchlistItem {
  symbol: string;
  name: string;
  price: number | null;
  /** Day change in percent (the backend's `changePercent`; its `change` is absolute). */
  changePct: number | null;
  ltp: number | null;
}

export interface SearchResultItem {
  symbol: string;
  name: string;
  type?: string;
  instrument_type?: string;
}

export type PositionItem = Omit<z.output<typeof positionSchema>, "net_pnl"> & {
  net_pnl: number;
};

function toSafeNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function toSafeString(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function roundOrNull(value: number | null): number | null {
  return value === null ? null : roundToDecimals(value, 2);
}

function toWatchlistItem(row: Record<string, unknown>): WatchlistItem {
  const symbol = toSafeString(row.symbol);
  return {
    symbol,
    name: toSafeString(row.name, symbol),
    price: roundOrNull(toSafeNumber(row.price)),
    changePct: roundOrNull(toSafeNumber(row.changePercent)),
    ltp: roundOrNull(toSafeNumber(row.ltp) ?? toSafeNumber(row.price)),
  };
}

function toSearchResult(row: Record<string, unknown>): SearchResultItem {
  return {
    symbol: toSafeString(row.symbol).trim(),
    name: toSafeString(row.name, toSafeString(row.symbol)).trim(),
    type: typeof row.type === "string" ? row.type : undefined,
    instrument_type: typeof row.instrument_type === "string" ? row.instrument_type : undefined,
  };
}

function toPositionRow(row: z.output<typeof positionSchema>): PositionItem {
  return { ...row, net_pnl: row.net_pnl ?? row.unrealized_pnl };
}

export function useTerminalWatchlist() {
  return useQuery({
    queryKey: queryKeys.terminal.watchlist(),
    queryFn: async () =>
      parseResponse(rowsSchema, await unwrap(api.GET("/api/market/watchlist")))
        .map(toWatchlistItem)
        .filter((row) => row.symbol.length > 0),
  });
}

/** Symbol search for the watchlist box; failures read as "no matches". */
export function useSymbolSearch(term: string) {
  return useQuery({
    queryKey: queryKeys.terminal.symbolSearch(term),
    queryFn: async ({ signal }): Promise<SearchResultItem[]> => {
      try {
        const rows = parseResponse(
          rowsSchema,
          await unwrap(
            api.GET("/api/market/search", {
              params: { query: { query: term, exclude_indices: true } },
              signal,
            }),
          ),
        );
        return rows
          .map(toSearchResult)
          .filter((row) => row.symbol.length > 0)
          .slice(0, MAX_SEARCH_RESULTS);
      } catch {
        return [];
      }
    },
    enabled: term.length >= 2,
    placeholderData: keepPreviousData,
  });
}

export interface PositionsBook {
  /** Live then paper positions. */
  rows: PositionItem[];
  /** The broker sync failed: LIVE positions are unknown or the last synced values. */
  liveStale: boolean;
}

/** The positions book, polled every 7s while `enabled`. */
export function usePositionsBook(enabled: boolean) {
  return useQuery({
    queryKey: queryKeys.terminal.positions(),
    queryFn: async (): Promise<PositionsBook> => {
      const book = parseResponse(
        positionsBookSchema,
        await unwrap(api.GET("/api/trading/positions/book")),
      );
      return {
        rows: [...book.live_positions, ...book.paper_positions].map(toPositionRow),
        liveStale: book.live_stale,
      };
    },
    enabled,
    refetchInterval: POSITIONS_POLL_MS,
  });
}

/** Adds a symbol, puts it at the top of the cached watchlist and refreshes the dashboard's. */
export function useAddToWatchlist() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: { symbol: string; name: string; instrumentType: string }) => {
      await unwrap(
        api.POST("/api/market/watchlist", {
          body: { symbol: input.symbol, instrument_type: input.instrumentType },
        }),
      );
    },
    onSuccess: (_data, input) => {
      queryClient.setQueryData<WatchlistItem[]>(queryKeys.terminal.watchlist(), (prev = []) =>
        prev.some((row) => row.symbol === input.symbol)
          ? prev
          : [
              {
                symbol: input.symbol,
                name: input.name || input.symbol,
                price: null,
                changePct: null,
                ltp: null,
              },
              ...prev,
            ],
      );
      return queryClient.invalidateQueries({ queryKey: queryKeys.dashboard.watchlist() });
    },
  });
}

export interface SquareInput {
  row: PositionItem;
  /** Idempotency key: one per click, reused by every retry of that click. */
  clientOrderId: string;
}

const SQUARE_RETRY_DELAY_MS = 250;
/** Status of a LIVE order or exit that may have reached the broker without confirmation. */
export const SUBMIT_UNCONFIRMED = "SUBMIT_UNCONFIRMED";

/**
 * Squares a position and drops it from the cached book; a rejected square-off throws.
 * A dropped request is retried with the same client_order_id, so the backend exits at most once.
 */
export function useSquarePosition() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ row, clientOrderId }: SquareInput) => {
      const result = parseResponse(
        squareResultSchema,
        await unwrap(
          api.POST("/api/trading/positions/square", {
            body: {
              symbol: row.symbol,
              mode: row.mode || "PAPER",
              product_type: row.product_type || null,
              client_order_id: clientOrderId,
            },
          }),
        ),
      );
      // A rejected LIVE square-off comes back as HTTP 200 with status ERROR: fail closed.
      if (result.status === "ERROR") {
        throw new ApiError({
          code: "SQUARE_REJECTED",
          message: result.message || "Failed to square position",
        });
      }
      // Sent but not confirmed: not a rejection, so the user must not square again.
      const unconfirmed = result.status === SUBMIT_UNCONFIRMED;
      return { unconfirmed, message: result.message ?? "" };
    },
    retry: shouldRetry,
    retryDelay: SQUARE_RETRY_DELAY_MS,
    onSuccess: (result, { row }) => {
      if (!result.unconfirmed) {
        queryClient.setQueryData<PositionsBook>(queryKeys.terminal.positions(), (prev) =>
          prev
            ? {
                ...prev,
                rows: prev.rows.filter((p) => !(p.symbol === row.symbol && p.mode === row.mode)),
              }
            : prev,
        );
      }
      return queryClient.invalidateQueries({ queryKey: queryKeys.terminal.positions() });
    },
  });
}

/**
 * The backend still resolves the order's user from this legacy query parameter and answers 401
 * without it outside DEV_MODE. Lane 3 task 5 replaces it with a `current_user_id` dependency.
 */
const ORDER_USER_ID = "default_user";
const ORDER_RETRY_DELAY_MS = 250;

/** The exact body POSTed to /api/trading/order; every order carries its idempotency key. */
export type PlaceOrderBody = components["schemas"]["OrderRequest"] & { client_order_id: string };

/** The risk warning messages the backend listed; anything that is not a string is dropped. */
const warningsSchema = z
  .array(z.unknown())
  .catch([])
  .optional()
  .transform((items = []) => items.filter((item): item is string => typeof item === "string"));

/** A non-empty string the backend sent, or undefined. */
const optionalText = z
  .union([z.string(), z.number()])
  .transform((value) => String(value) || undefined)
  .optional()
  .catch(undefined);

const placeOrderResultSchema = z.object({
  status: optionalText,
  message: optionalText,
  order_id: optionalText,
  id: optionalText,
  duplicate: z.boolean().optional().catch(undefined),
  warnings: warningsSchema,
});

export interface PlaceOrderResult {
  /** Sent but not confirmed by the broker: the order may be live, so it must not be placed again. */
  unconfirmed: boolean;
  status: string | undefined;
  message: string | undefined;
  orderId: string | undefined;
  /** The client_order_id was already used: this is the existing order, not a new one. */
  duplicate: boolean;
  /** Risk checks that warned and were overridden with the order's risk_override_reason. */
  warnings: string[];
}

/** True when the request may have reached the backend but its answer was lost or cut off. */
function isCutOff(error: unknown): error is ApiError {
  if (!(error instanceof ApiError)) return false;
  if (error.code === "RESPONSE_SHAPE_ERROR") return true;
  return error.code === "NETWORK_ERROR" && error.details instanceof SyntaxError;
}

async function postOrder(body: PlaceOrderBody): Promise<unknown> {
  try {
    return await unwrap(
      api.POST("/api/trading/order", { params: { query: { x_user_id: ORDER_USER_ID } }, body }),
    );
  } catch (error) {
    if (!isCutOff(error)) throw error;
    // The order may have been placed: retry (same client_order_id) to read its state.
    throw new ApiError({
      code: "NETWORK_ERROR",
      message: "Order response was cut off",
      status: error.status ?? undefined,
    });
  }
}

/**
 * The one call site that places an order; a rejected order throws. A dropped request, or a 200
 * whose body was cut off, is retried with the same client_order_id, so the backend places the
 * order at most once. A filled order can change the positions book, so it is refreshed.
 */
export function usePlaceOrder() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: PlaceOrderBody): Promise<PlaceOrderResult> => {
      const result = parseResponse(placeOrderResultSchema, await postOrder(body));
      return {
        unconfirmed: result.status === SUBMIT_UNCONFIRMED,
        status: result.status,
        message: result.message,
        orderId: result.order_id ?? result.id,
        duplicate: result.duplicate === true,
        warnings: result.warnings,
      };
    },
    retry: shouldRetry,
    retryDelay: ORDER_RETRY_DELAY_MS,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.terminal.positions() }),
  });
}

/** The fields the backend's pre-trade risk check reads. */
export type RiskCheckBody = components["schemas"]["RiskCheckRequest"];

const riskCheckSchema = z.object({
  // No fallback: a response without a known status is not a risk check, so it reads as unavailable.
  status: z.enum(["PASS", "WARNING", "FAIL"]),
  message: z.string().catch(""),
  details: z.object({ warnings: warningsSchema }).nullish().catch(null),
});

export interface RiskCheck {
  status: "PASS" | "WARNING" | "FAIL";
  message: string;
  /** Every warning; a WARNING order needs a risk_override_reason to go through. */
  warnings: string[];
}

/**
 * The backend's pre-trade risk check for an order, without placing it. Advisory: placing the
 * order runs the same checks again and is what decides.
 *
 * `openId` names one opening of the confirmation dialog. Each opening is its own query, never
 * served from an earlier one's cache, so the result shown is always a fresh check of this order,
 * and nothing is kept once the dialog closes.
 */
export function useRiskCheck(order: RiskCheckBody, openId: number) {
  return useQuery({
    queryKey: queryKeys.terminal.riskCheck(order, openId),
    queryFn: async (): Promise<RiskCheck> => {
      const result = parseResponse(
        riskCheckSchema,
        await unwrap(api.POST("/api/trading/risk-check", { body: order })),
      );
      return {
        status: result.status,
        message: result.message,
        warnings: result.details?.warnings ?? [],
      };
    },
    staleTime: 0,
    gcTime: 0,
  });
}
