import { QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vite-plus/test";
import { createQueryClient } from "@/lib/queries/query-client";

// The client reads VITE_API_URL at import time; Node cannot build a Request from a relative URL.
vi.stubEnv("VITE_API_URL", "http://api.test");
const { default: StrategiesPage } = await import("../page");

const SCAN_ROW = {
  id: 7,
  symbol: "SBIN",
  company_name: "State Bank of India",
  sector: "Banks",
  grade: "A",
  rs_rating: 91,
  contraction_count: 3,
  contraction_depths: [18, 9, 4],
  final_contraction_depth: 4,
  volume_dry_up_pct: 40,
  pivot_high: 812.5,
  stop_level: 780,
  stop_pct: 4,
  days_in_base: 30,
  is_breakout: true,
  signal_status: "SIGNAL",
  planned_shares: 100,
  planned_position_value: 81250,
  capital_risk: 3250,
  two_r_target: 877.5,
};

const RESPONSES: Record<string, unknown> = {
  "/api/screener/indices": { indices: [{ id: "NIFTY500", name: "Nifty 500", count: 500 }] },
  "/api/strategies/status": {
    halted: false,
    latest_scan_date: "2026-10-16",
    pending_entries: 2,
    open_positions: 1,
    regime: { regime: "BULL", close: 22450, ma_200: 21000, risk_pct: 1 },
  },
  "/api/strategies/vcp/scan/latest": { results: [SCAN_ROW] },
  "/api/strategies/positions": { positions: [] },
  "/api/strategies/vcp/backtest/history": { runs: [] },
  "/api/strategies/vcp/signal/SBIN": { signal: SCAN_ROW, chart: [] },
  "/api/strategies/vcp/signal/7/queue": { ...SCAN_ROW, signal_status: "PENDING_ENTRY" },
};

type Call = { method: string; path: string; search: string };

function stubBackend(overrides: Record<string, () => Response | Promise<Response>> = {}) {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL): Promise<Response> => {
      const request = input instanceof Request ? input : new Request(String(input));
      const url = new URL(request.url);
      calls.push({ method: request.method, path: url.pathname, search: url.search });
      const override = overrides[url.pathname];
      return Promise.resolve(override ? override() : Response.json(RESPONSES[url.pathname] ?? {}));
    }),
  );
  return calls;
}

function renderStrategies() {
  const client = createQueryClient();
  client.setDefaultOptions({ queries: { retry: false } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <StrategiesPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("strategies page", { timeout: 15_000 }, () => {
  it("loads the workspace for the selected universe and shows its numbers", async () => {
    const calls = stubBackend();
    renderStrategies();

    expect(await screen.findByText("2026-10-16")).toBeInTheDocument();
    expect(screen.getByText("BULL · Risk 1%")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Breakouts 1" })).toBeInTheDocument();
    expect(await screen.findByText("SBIN · A-Grade")).toBeInTheDocument();

    const gets = calls.filter((call) => call.method === "GET").map((c) => `${c.path}${c.search}`);
    expect(gets).toEqual(
      expect.arrayContaining([
        "/api/strategies/status?universe=NIFTY500",
        "/api/strategies/vcp/scan/latest?universe=NIFTY500&show_all=false",
        "/api/strategies/positions",
        "/api/strategies/vcp/backtest/history?universe=NIFTY500",
        "/api/strategies/vcp/signal/SBIN",
      ]),
    );
  });

  it("shows the selected signal's levels in rupees", async () => {
    stubBackend();
    renderStrategies();

    expect(await screen.findByText("SBIN · A-Grade")).toBeInTheDocument();
    expect(screen.getByText("₹812.50")).toBeInTheDocument();
    expect(screen.getByText("₹780.00")).toBeInTheDocument();
    expect(screen.getByText("₹81,250.00")).toBeInTheDocument();
    expect(screen.getByText("₹877.50")).toBeInTheDocument();
    expect(screen.getByText("18% → 9% → 4%")).toBeInTheDocument();
  });

  it("sends one halt request when Halt is clicked twice", async () => {
    let releaseHalt: () => void = () => {};
    const haltReply = new Promise<Response>((resolve) => {
      releaseHalt = () => resolve(Response.json({ status: "ok" }));
    });
    const calls = stubBackend({ "/api/strategies/halt": () => haltReply });
    renderStrategies();
    await screen.findByText("2026-10-16");

    const halt = screen.getByRole("button", { name: "Halt" });
    fireEvent.click(halt);
    await waitFor(() => expect(halt).toBeDisabled());
    expect(screen.getByRole("button", { name: "Running..." })).toBeDisabled();
    fireEvent.click(halt);
    releaseHalt();

    await waitFor(() => expect(halt).not.toBeDisabled());
    expect(calls.filter((call) => call.method === "POST").map((call) => call.path)).toEqual([
      "/api/strategies/halt",
    ]);
  });

  it("shows the database outage message when the workspace cannot load", async () => {
    stubBackend({
      "/api/strategies/status": () =>
        Response.json(
          { error: { code: "DATABASE_UNAVAILABLE", message: "Database unavailable" } },
          { status: 503 },
        ),
    });
    renderStrategies();

    expect(
      await screen.findByText(
        "Database unavailable: the backend cannot reach Postgres. Retry once it is back.",
      ),
    ).toBeInTheDocument();
  });

  it("queues a breakout and reloads the workspace", async () => {
    const calls = stubBackend();
    renderStrategies();

    // Radix tabs switch on mouse down.
    fireEvent.mouseDown(await screen.findByRole("tab", { name: "Breakouts 1" }), { button: 0 });
    fireEvent.click(await screen.findByRole("button", { name: "Queue" }));

    await waitFor(() =>
      expect(calls).toContainEqual({
        method: "POST",
        path: "/api/strategies/vcp/signal/7/queue",
        search: "",
      }),
    );
    const queuedAt = calls.findIndex((call) => call.method === "POST");
    await waitFor(() =>
      expect(calls.slice(queuedAt).map((call) => call.path)).toContain("/api/strategies/status"),
    );
  });

  it("sends one close request when Close Now is clicked twice", async () => {
    let releaseClose: () => void = () => {};
    const closeReply = new Promise<Response>((resolve) => {
      releaseClose = () => resolve(Response.json({ status: "ok", position_id: 3 }));
    });
    const calls = stubBackend({
      "/api/strategies/positions": () =>
        Response.json({
          positions: [{ id: 3, symbol: "TCS", entry_price: 3890, stop_price: 3700, ltp: 3950 }],
        }),
      "/api/strategies/positions/3/close": () => closeReply,
    });
    renderStrategies();

    fireEvent.mouseDown(await screen.findByRole("tab", { name: "Positions 1" }), { button: 0 });
    const close = await screen.findByRole("button", { name: "Close Now" });
    fireEvent.click(close);
    await waitFor(() => expect(close).toBeDisabled());
    fireEvent.click(close);
    releaseClose();

    await waitFor(() => expect(close).not.toBeDisabled());
    expect(calls.filter((call) => call.method === "POST").map((call) => call.path)).toEqual([
      "/api/strategies/positions/3/close",
    ]);
  });

  it("shows the broker outage message when halting fails", async () => {
    stubBackend({
      "/api/strategies/halt": () =>
        Response.json({ error: { code: "BROKER_UNAVAILABLE" } }, { status: 503 }),
    });
    renderStrategies();
    await screen.findByText("2026-10-16");

    fireEvent.click(screen.getByRole("button", { name: "Halt" }));

    expect(
      await screen.findByText(
        "Broker unavailable: Fyers could not be reached. Retry once it is back.",
      ),
    ).toBeInTheDocument();
  });
});
