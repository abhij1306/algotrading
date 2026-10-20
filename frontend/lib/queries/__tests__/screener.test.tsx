import { QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vite-plus/test";
import { ApiError } from "@/lib/api/errors";
import { createQueryClient, shouldRetry } from "../query-client";

// The client reads VITE_API_URL at import time; node cannot build a Request from a relative URL.
vi.stubEnv("VITE_API_URL", "http://api.test");
const { liveQuoteChangePercent, useScreenerResults } = await import("../screener");

function wrapperFor() {
  const client = createQueryClient();
  client.setDefaultOptions({ queries: { retry: false } });
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

const params = {
  universe: "NIFTY50",
  page: 2,
  limit: 25,
  sortBy: "symbol",
  sortOrder: "asc",
  query: "",
} as const;

describe("useScreenerResults", () => {
  const fetchMock = vi.fn<(request: Request) => Promise<Response>>();

  beforeAll(() => {
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    fetchMock.mockReset();
  });

  it("requests the page with the query parameters and parses the rows", async () => {
    fetchMock.mockResolvedValue(
      Response.json({
        results: [{ symbol: "SBIN", name: "State Bank", price: 812.5, change: 1.2 }],
        total: 51,
      }),
    );

    const { result } = renderHook(() => useScreenerResults(params), { wrapper: wrapperFor() });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const requested = new URL(fetchMock.mock.calls[0][0].url);
    expect(requested.pathname).toBe("/api/screener/results");
    expect(Object.fromEntries(requested.searchParams)).toEqual({
      universe: "NIFTY50",
      page: "2",
      limit: "25",
      sort_by: "symbol",
      sort_order: "asc",
    });
    expect(result.current.data).toEqual({
      rows: [
        {
          symbol: "SBIN",
          name: "State Bank",
          sector: "",
          marketCap: 0,
          change: 1.2,
          price: 812.5,
          volume: 0,
          rsi: 0,
          macd: 0,
          adx: 0,
        },
      ],
      total: 51,
    });
  });

  it("surfaces a normalised ApiError when the request fails", async () => {
    fetchMock.mockResolvedValue(
      Response.json({ detail: "Universe not found" }, { status: 404, statusText: "Not Found" }),
    );

    const { result } = renderHook(() => useScreenerResults(params), { wrapper: wrapperFor() });
    await waitFor(() => expect(result.current.isError).toBe(true));

    expect(result.current.error).toBeInstanceOf(ApiError);
    expect(result.current.error?.message).toBe("Universe not found");
  });
});

describe("liveQuoteChangePercent", () => {
  it("reads the percent field and never treats the absolute change as a percent", () => {
    expect(liveQuoteChangePercent({ ltp: 812.5, change: 5.5, change_pct: 0.68 })).toBe(0.68);
    expect(liveQuoteChangePercent({ ltp: 812.5, change: 5.5 })).toBeUndefined();
  });
});

describe("shouldRetry", () => {
  it("retries network and 5xx errors only, up to twice", () => {
    const network = new ApiError({ code: "NETWORK_ERROR", message: "offline" });
    const server = new ApiError({ code: "HTTP_ERROR", message: "boom", status: 503 });
    const client = new ApiError({ code: "HTTP_ERROR", message: "nope", status: 404 });

    expect(shouldRetry(0, network)).toBe(true);
    expect(shouldRetry(1, server)).toBe(true);
    expect(shouldRetry(2, server)).toBe(false);
    expect(shouldRetry(0, client)).toBe(false);
    expect(shouldRetry(0, new Error("x"))).toBe(false);
  });
});
