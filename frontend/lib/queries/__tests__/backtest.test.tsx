import { QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vite-plus/test";
import { createQueryClient } from "../query-client";

// The client reads VITE_API_URL at import time; node cannot build a Request from a relative URL.
vi.stubEnv("VITE_API_URL", "http://api.test");
const { useBacktestRuns } = await import("../backtest");

function wrapper() {
  const client = createQueryClient();
  client.setDefaultOptions({ queries: { retry: false } });
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

describe("useBacktestRuns", () => {
  const fetchMock = vi.fn<(request: Request) => Promise<Response>>();

  beforeAll(() => {
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    fetchMock.mockReset();
  });

  it("keeps the summary of a symbol-mode run whose universe is null", async () => {
    fetchMock.mockResolvedValue(
      Response.json({
        runs: [
          {
            job_id: "job-1",
            status: "completed",
            created_at: "2026-01-01T00:00:00Z",
            params: {
              instrument_type: "equity",
              start_date: "2025-01-01",
              end_date: "2025-12-31",
              initial_capital: 1000000,
              selection: { mode: "symbols", universe: null, symbols: ["SBIN"] },
            },
          },
        ],
      }),
    );

    const { result } = renderHook(() => useBacktestRuns(), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(result.current.data?.[0].params).toEqual({
      instrument_type: "equity",
      start_date: "2025-01-01",
      end_date: "2025-12-31",
      initial_capital: 1000000,
      selection: { mode: "symbols", universe: null },
    });
  });

  it("drops only a malformed field, not the whole summary", async () => {
    fetchMock.mockResolvedValue(
      Response.json({
        runs: [
          {
            job_id: "job-2",
            status: "running",
            created_at: "2026-01-01T00:00:00Z",
            params: {
              instrument_type: "equity",
              initial_capital: "lots",
              start_date: "2025-01-01",
            },
          },
        ],
      }),
    );

    const { result } = renderHook(() => useBacktestRuns(), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(result.current.data?.[0].params).toMatchObject({
      instrument_type: "equity",
      start_date: "2025-01-01",
      initial_capital: undefined,
    });
  });
});
