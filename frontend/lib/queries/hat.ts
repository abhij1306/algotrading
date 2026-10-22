import { useQuery } from "@tanstack/react-query";
import { z } from "zod";
import { api, parseResponse, unwrap } from "@/lib/api/client";
import { queryKeys } from "./keys";

/** The dashboard is read-only and polls: Telegram is where anything is changed. */
export const HAT_REFRESH_MS = 15_000;
export const HAT_EVENT_LIMIT = 25;

// Money, weights and scores arrive as exact decimal strings; they stay strings until display.
const decimal = z.string();
const uuid = z.string();
const nullableText = z.string().nullable();

const botSchema = z.object({
  id: uuid,
  name: z.string(),
  execution_mode: z.string(),
  status: z.string(),
  lifecycle_state: z.string(),
  risk_cluster: z.string(),
  manage_only: z.boolean(),
  reasons: z.array(z.string()),
});

const regimeSchema = z.object({
  trade_date: z.string(),
  state: z.string(),
  confirmed: z.boolean(),
});

const statusSchema = z.object({
  as_of: z.string(),
  trade_date: z.string(),
  kill_account: z.boolean(),
  feed_stale_at: nullableText,
  regime: regimeSchema.nullable(),
  bots: z.array(botSchema),
  kill_switches: z.array(
    z.object({
      scope: z.string(),
      bot_id: uuid.nullable(),
      channel: z.string(),
      state: z.string(),
    }),
  ),
  sessions: z.array(z.object({ account: z.string(), status: z.string() })),
  jobs: z.array(
    z.object({
      job: z.string(),
      trade_date: z.string(),
      status: z.string(),
      started_at: z.string(),
      finished_at: nullableText,
      calls: z.number(),
      rows: z.number(),
      error: nullableText,
    }),
  ),
});

const allocationsSchema = z.object({
  trade_date: nullableText,
  rows: z.array(
    z.object({
      bot_id: uuid,
      bot: z.string(),
      base_weight: decimal,
      regime_mult: decimal,
      health_mult: decimal,
      final_weight: decimal,
      capital: decimal,
      risk_pct: decimal,
      currency: z.string(),
    }),
  ),
});

const watchlistSchema = z.array(
  z.object({
    bot_id: uuid,
    bot: z.string(),
    trade_date: nullableText,
    entries: z.array(z.object({ symbol: z.string(), score: decimal })),
  }),
);

const positionsSchema = z.array(
  z.object({
    id: uuid,
    bot: z.string(),
    symbol: z.string(),
    qty_open: z.number(),
    avg_entry: decimal,
    initial_stop: decimal,
    current_stop: decimal,
    currency: z.string(),
    opened_at: z.string(),
    state: z.string(),
    is_virtual: z.boolean(),
  }),
);

const eventsSchema = z.array(
  z.object({
    id: z.number(),
    occurred_at: z.string(),
    event_type: z.string(),
    actor: z.string(),
    aggregate_type: z.string(),
  }),
);

export type HatStatus = z.output<typeof statusSchema>;
export type HatAllocations = z.output<typeof allocationsSchema>;
export type HatWatchlist = z.output<typeof watchlistSchema>;
export type HatPositions = z.output<typeof positionsSchema>;
export type HatEvents = z.output<typeof eventsSchema>;

const polling = { refetchInterval: HAT_REFRESH_MS, refetchIntervalInBackground: false };

export function useHatStatus() {
  return useQuery({
    queryKey: queryKeys.hat.status(),
    queryFn: async () => parseResponse(statusSchema, await unwrap(api.GET("/api/hat/status"))),
    ...polling,
  });
}

export function useHatAllocations() {
  return useQuery({
    queryKey: queryKeys.hat.allocations(),
    queryFn: async () =>
      parseResponse(allocationsSchema, await unwrap(api.GET("/api/hat/allocations"))),
    ...polling,
  });
}

export function useHatWatchlist() {
  return useQuery({
    queryKey: queryKeys.hat.watchlist(),
    queryFn: async () =>
      parseResponse(watchlistSchema, await unwrap(api.GET("/api/hat/watchlist"))),
    ...polling,
  });
}

export function useHatPositions() {
  return useQuery({
    queryKey: queryKeys.hat.positions(),
    queryFn: async () =>
      parseResponse(positionsSchema, await unwrap(api.GET("/api/hat/positions"))),
    ...polling,
  });
}

export function useHatEvents(limit: number = HAT_EVENT_LIMIT) {
  return useQuery({
    queryKey: queryKeys.hat.events(limit),
    queryFn: async () =>
      parseResponse(
        eventsSchema,
        await unwrap(api.GET("/api/hat/events", { params: { query: { limit } } })),
      ),
    ...polling,
  });
}
