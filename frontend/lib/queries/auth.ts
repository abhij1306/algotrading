import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { z } from "zod";
import { api, parseResponse, unwrap } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import { queryKeys } from "./keys";

/** Re-check the Fyers status at most this often (and on window focus), so an offline badge clears. */
const FYERS_STATUS_STALE_MS = 30_000;

const fyersStatusSchema = z.object({
  connected: z.boolean().catch(false),
  user_id: z.string().nullish().catch(null),
});

const loginUrlSchema = z.object({
  url: z.string().nullish().catch(null),
  detail: z.string().nullish().catch(null),
  error: z.string().nullish().catch(null),
});

export type FyersStatus = { connected: boolean; userId: string; offline: boolean };

const DISCONNECTED: FyersStatus = { connected: false, userId: "", offline: false };

/** Any failure to read the status reads as "backend offline" rather than an error. */
export function useFyersStatus() {
  return useQuery({
    queryKey: queryKeys.auth.fyersStatus(),
    queryFn: async (): Promise<FyersStatus> => {
      try {
        const body = parseResponse(
          fyersStatusSchema,
          await unwrap(api.GET("/api/auth/fyers/status")),
        );
        return body.connected
          ? { connected: true, userId: body.user_id ?? "", offline: false }
          : DISCONNECTED;
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
      const body = parseResponse(loginUrlSchema, await unwrap(api.GET("/api/auth/fyers/url")));
      if (body.url) return body.url;
      throw new ApiError({
        code: "LOGIN_URL_MISSING",
        message: body.detail || body.error || "Failed to get Login URL",
      });
    },
  });
}

/** Exchanges the pasted auth code, then re-reads the real status (including the user id). */
export function useFyersToken() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (authCode: string) => {
      await unwrap(api.POST("/api/auth/fyers/token", { body: { auth_code: authCode } }));
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.auth.fyersStatus() }),
  });
}

export function useFyersDisconnect() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      await unwrap(api.POST("/api/auth/fyers/disconnect"));
    },
    onSuccess: () => {
      queryClient.setQueryData<FyersStatus>(queryKeys.auth.fyersStatus(), DISCONNECTED);
    },
  });
}
