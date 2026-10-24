import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import type { components } from "@/lib/api/schema";
import { HAT_REFRESH_MS } from "./hat";
import { queryKeys } from "./keys";

/** Fyers logs in every day (market data); Zerodha too when it is the active order broker. */
export type Broker = "fyers" | "zerodha";

type BrokerStatusBody = components["schemas"]["BrokerStatusResponse"];

/**
 * What the header knows about a broker login. `unreadable` is the backend answering that it could
 * not read the saved token (or answering with a 4xx), which a re-login will not fix; `offline` is
 * the backend unreachable or failing (network error or 5xx). `active` marks the order broker.
 */
export type BrokerStatus =
  | { kind: "connected"; userId: string; active: boolean }
  | { kind: "disconnected"; active: boolean }
  | { kind: "unreadable"; error: string; active: boolean }
  | { kind: "offline" };

/** The header's state from the typed status body. */
export function toBrokerStatus(body: BrokerStatusBody): BrokerStatus {
  const active = body.active;
  if (body.error) return { kind: "unreadable", error: body.error, active };
  if (body.connected) return { kind: "connected", userId: body.user_id ?? "", active };
  return { kind: "disconnected", active };
}

/** The header's state when the status read failed: offline for network and 5xx errors only. */
export function failedBrokerStatus(error: unknown): BrokerStatus {
  if (!(error instanceof ApiError) || error.code === "NETWORK_ERROR") return { kind: "offline" };
  if (error.status === null || error.status >= 500) return { kind: "offline" };
  return { kind: "unreadable", error: error.message, active: false };
}

// The typed client takes literal paths, so each call names both brokers' routes.
const getStatus = (broker: Broker) =>
  broker === "fyers"
    ? api.GET("/api/hat/auth/fyers/status")
    : api.GET("/api/hat/auth/zerodha/status");
const getLoginUrl = (broker: Broker) =>
  broker === "fyers" ? api.GET("/api/hat/auth/fyers/url") : api.GET("/api/hat/auth/zerodha/url");
const postToken = (broker: Broker, code: string) =>
  broker === "fyers"
    ? api.POST("/api/hat/auth/fyers/token", { body: { auth_code: code } })
    : api.POST("/api/hat/auth/zerodha/token", { body: { auth_code: code } });
const postDisconnect = (broker: Broker) =>
  broker === "fyers"
    ? api.POST("/api/hat/auth/fyers/disconnect")
    : api.POST("/api/hat/auth/zerodha/disconnect");

/**
 * The login status, polled with the dashboard so a token expiring at midnight IST or a backend
 * going down shows without a reload. A failed read resolves to a state, not a query error.
 */
export function useBrokerStatus(broker: Broker) {
  return useQuery({
    queryKey: queryKeys.auth.status(broker),
    queryFn: async (): Promise<BrokerStatus> => {
      try {
        return toBrokerStatus(await unwrap(getStatus(broker)));
      } catch (error) {
        return failedBrokerStatus(error);
      }
    },
    retry: false,
    refetchInterval: HAT_REFRESH_MS,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: true,
  });
}

/** Asks the backend for the broker's login page; a reply without a URL fails with its reason. */
export function useBrokerLoginUrl(broker: Broker) {
  return useMutation({
    mutationFn: async (): Promise<string> => {
      const body = await unwrap(getLoginUrl(broker));
      if (typeof body.url === "string" && body.url) return body.url;
      throw new ApiError({ code: "LOGIN_URL_MISSING", message: "Failed to get Login URL" });
    },
  });
}

/** Exchanges the pasted code (Fyers' auth_code, Kite's request_token), then re-reads the status. */
export function useBrokerToken(broker: Broker) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (code: string) => {
      await unwrap(postToken(broker, code));
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.auth.status(broker) }),
  });
}

export function useBrokerDisconnect(broker: Broker) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      await unwrap(postDisconnect(broker));
    },
    onSuccess: () => {
      queryClient.setQueryData<BrokerStatus>(queryKeys.auth.status(broker), (old) => ({
        kind: "disconnected",
        active: old !== undefined && old.kind !== "offline" && old.active,
      }));
    },
  });
}
