import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { z } from "zod";
import { api, parseResponse, unwrap } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import type { components } from "@/lib/api/schema";
import { roundToDecimals } from "@/lib/format";
import { isMarketOpen } from "@/lib/market-hours";
import { queryKeys } from "./keys";
import { shouldRetry } from "./query-client";

const OPTIONS_POLL_OPEN_MS = 3000;
const OPTIONS_POLL_CLOSED_MS = 20_000;
const POSITIONS_POLL_MS = 7000;
const OPTIONS_STRIKE_COUNT = 15;
const MAX_SEARCH_RESULTS = 8;

const rowsSchema = z.array(z.record(z.string(), z.unknown())).catch([]);

const nullableNumber = z.number().nullable().catch(null);

const optionsLegSchema = z
  .object({
    symbol: z.string().nullable().catch(null),
    ltp: nullableNumber,
    oi: nullableNumber,
    volume: nullableNumber,
    change_pct: nullableNumber,
  })
  .nullable()
  .catch(null);

const optionsBoardSchema = z.object({
  underlying: z.string().catch(""),
  spot_price: z.number().catch(0),
  expiry: z.string().catch(""),
  atm_strike: z.number().catch(0),
  strikes: z
    .array(z.object({ strike: z.number(), ce: optionsLegSchema, pe: optionsLegSchema }))
    .catch([]),
  timestamp: z.string().catch(""),
});

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

export type OptionsBoard = z.output<typeof optionsBoardSchema>;
export type PositionItem = Omit<z.output<typeof positionSchema>, "net_pnl"> & {
  net_pnl: number;
};

export interface OptionsOrderflow {
  pcr_oi: number | null;
  pcr_volume: number | null;
  ce_oi: number;
  pe_oi: number;
  ce_volume: number;
  pe_volume: number;
  timestamp: string;
}

export interface OptionsBoardSnapshot {
  board: OptionsBoard;
  orderflow: OptionsOrderflow;
}

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

/** Put/call totals and ratios across the board's strikes. */
export function buildOptionsOrderflow(board: OptionsBoard): OptionsOrderflow {
  let ceOi = 0;
  let peOi = 0;
  let ceVol = 0;
  let peVol = 0;
  for (const row of board.strikes) {
    ceOi += Number(row.ce?.oi || 0);
    peOi += Number(row.pe?.oi || 0);
    ceVol += Number(row.ce?.volume || 0);
    peVol += Number(row.pe?.volume || 0);
  }
  return {
    ce_oi: ceOi,
    pe_oi: peOi,
    ce_volume: ceVol,
    pe_volume: peVol,
    pcr_oi: ceOi > 0 ? roundToDecimals(peOi / ceOi, 4) : null,
    pcr_volume: ceVol > 0 ? roundToDecimals(peVol / ceVol, 4) : null,
    timestamp: board.timestamp,
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

/**
 * Polls the options board while `enabled`: every 3s in market hours, 20s otherwise.
 * An empty `expiry` lets the backend pick the nearest one.
 */
export function useOptionsBoard(params: { underlying: string; expiry: string; enabled: boolean }) {
  const { underlying, expiry, enabled } = params;
  return useQuery({
    queryKey: queryKeys.terminal.optionsBoard({ underlying, expiry }),
    queryFn: async (): Promise<OptionsBoardSnapshot> => {
      const board = parseResponse(
        optionsBoardSchema,
        await unwrap(
          api.GET("/api/terminal/options/board", {
            params: {
              query: {
                underlying,
                strike_count: OPTIONS_STRIKE_COUNT,
                expiry: expiry || undefined,
              },
            },
          }),
        ),
      );
      return { board, orderflow: buildOptionsOrderflow(board) };
    },
    enabled,
    placeholderData: keepPreviousData,
    refetchInterval: () => (isMarketOpen().isOpen ? OPTIONS_POLL_OPEN_MS : OPTIONS_POLL_CLOSED_MS),
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
});

export interface PlaceOrderResult {
  /** Sent but not confirmed by the broker: the order may be live, so it must not be placed again. */
  unconfirmed: boolean;
  status: string | undefined;
  message: string | undefined;
  orderId: string | undefined;
  /** The client_order_id was already used: this is the existing order, not a new one. */
  duplicate: boolean;
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
      };
    },
    retry: shouldRetry,
    retryDelay: ORDER_RETRY_DELAY_MS,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.terminal.positions() }),
  });
}
