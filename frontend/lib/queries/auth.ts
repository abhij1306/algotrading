import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import { queryKeys } from "./keys";

/** Re-check the Fyers status at most this often (and on window focus), so an offline badge clears. */
const FYERS_STATUS_STALE_MS = 30_000;

export type FyersStatus = { connected: boolean; userId: string; offline: boolean };

const DISCONNECTED: FyersStatus = { connected: false, userId: "", offline: false };

/** Reads the status body defensively: anything but `connected: true` is disconnected. */
function toFyersStatus(body: { connected?: unknown; user_id?: unknown }): FyersStatus {
  if (body.connected !== true) return DISCONNECTED;
  return {
    connected: true,
    userId: typeof body.user_id === "string" ? body.user_id : "",
    offline: false,
  };
}

/** Any failure to read the status reads as "backend offline" rather than an error. */
export function useFyersStatus() {
  return useQuery({
    queryKey: queryKeys.auth.fyersStatus(),
    queryFn: async (): Promise<FyersStatus> => {
      try {
        return toFyersStatus(await unwrap(api.GET("/api/hat/auth/fyers/status")));
      } catch {
        return { connected: false, userId: "", offline: true };
      }
    },
    retry: false,
    staleTime: FYERS_STATUS_STALE_MS,
    refetchOnWindowFocus: true,
  });
}

/** Asks the backend for the Fyers login page; a reply without a URL fails with its reason. */
export function useFyersLoginUrl() {
  return useMutation({
    mutationFn: async (): Promise<string> => {
      const body = await unwrap(api.GET("/api/hat/auth/fyers/url"));
      if (typeof body.url === "string" && body.url) return body.url;
      throw new ApiError({ code: "LOGIN_URL_MISSING", message: "Failed to get Login URL" });
    },
  });
}

/** Exchanges the pasted auth code, then re-reads the real status (including the user id). */
export function useFyersToken() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (authCode: string) => {
      await unwrap(api.POST("/api/hat/auth/fyers/token", { body: { auth_code: authCode } }));
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.auth.fyersStatus() }),
  });
}

export function useFyersDisconnect() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      await unwrap(api.POST("/api/hat/auth/fyers/disconnect"));
    },
    onSuccess: () => {
      queryClient.setQueryData<FyersStatus>(queryKeys.auth.fyersStatus(), DISCONNECTED);
    },
  });
}
