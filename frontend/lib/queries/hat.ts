import { useQuery } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import { queryKeys } from "./keys";

/** The dashboard is read-only and polls: Telegram is where anything is changed. */
export const HAT_REFRESH_MS = 15_000;
export const HAT_EVENT_LIMIT = 25;
export const HAT_SIGNAL_LIMIT = 25;

// Money, weights and scores arrive as exact decimal strings; they stay strings until display.
type Schemas = components["schemas"];
export type HatStatus = Schemas["StatusResponse"];
export type HatAllocations = Schemas["AllocationsResponse"];
export type HatWatchlist = Schemas["WatchlistView"][];
export type HatPositions = Schemas["PositionView"][];
export type HatEvents = Schemas["EventView"][];
export type HatSignals = Schemas["SignalView"][];

const polling = { refetchInterval: HAT_REFRESH_MS, refetchIntervalInBackground: false };

export function useHatStatus() {
  return useQuery({
    queryKey: queryKeys.hat.status(),
    queryFn: () => unwrap(api.GET("/api/hat/status")),
    ...polling,
  });
}

export function useHatAllocations() {
  return useQuery({
    queryKey: queryKeys.hat.allocations(),
    queryFn: () => unwrap(api.GET("/api/hat/allocations")),
    ...polling,
  });
}

export function useHatWatchlist() {
  return useQuery({
    queryKey: queryKeys.hat.watchlist(),
    queryFn: () => unwrap(api.GET("/api/hat/watchlist")),
    ...polling,
  });
}

export function useHatPositions() {
  return useQuery({
    queryKey: queryKeys.hat.positions(),
    queryFn: () => unwrap(api.GET("/api/hat/positions")),
    ...polling,
  });
}

export function useHatSignals(limit: number = HAT_SIGNAL_LIMIT) {
  return useQuery({
    queryKey: queryKeys.hat.signals(limit),
    queryFn: () => unwrap(api.GET("/api/hat/signals", { params: { query: { limit } } })),
    ...polling,
  });
}

export function useHatEvents(limit: number = HAT_EVENT_LIMIT) {
  return useQuery({
    queryKey: queryKeys.hat.events(limit),
    queryFn: () => unwrap(api.GET("/api/hat/events", { params: { query: { limit } } })),
    ...polling,
  });
}
