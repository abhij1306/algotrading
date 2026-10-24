import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import { queryKeys } from "./keys";

/** Fyers logs in every day (market data); Zerodha too when it is the active order broker. */
export type Broker = "fyers" | "zerodha";

/** Re-check a login status at most this often (and on window focus), so an offline badge clears. */
const STATUS_STALE_MS = 30_000;

export type BrokerStatus = {
  connected: boolean;
  userId: string;
  offline: boolean;
  /** This broker is the active order broker (`HAT_BROKER`). */
  active: boolean;
};

const DISCONNECTED: BrokerStatus = { connected: false, userId: "", offline: false, active: false };

/** Reads the status body defensively: anything but `connected: true` is disconnected. */
function toBrokerStatus(body: {
  connected?: unknown;
  user_id?: unknown;
  active?: unknown;
}): BrokerStatus {
  const active = body.active === true;
  if (body.connected !== true) return { ...DISCONNECTED, active };
  return {
    connected: true,
    userId: typeof body.user_id === "string" ? body.user_id : "",
    offline: false,
    active,
  };
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

/** Any failure to read the status reads as "backend offline" rather than an error. */
export function useBrokerStatus(broker: Broker) {
  return useQuery({
    queryKey: queryKeys.auth.status(broker),
    queryFn: async (): Promise<BrokerStatus> => {
      try {
        return toBrokerStatus(await unwrap(getStatus(broker)));
      } catch {
        return { ...DISCONNECTED, offline: true };
      }
    },
    retry: false,
    staleTime: STATUS_STALE_MS,
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
        ...DISCONNECTED,
        active: old?.active ?? false,
      }));
    },
  });
}
