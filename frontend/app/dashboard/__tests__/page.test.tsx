import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vite-plus/test";
import { createQueryClient } from "@/lib/queries/query-client";

// The client reads VITE_API_URL at import time; Node cannot build a Request from a relative URL.
vi.stubEnv("VITE_API_URL", "http://api.test");
const { default: DashboardPage } = await import("../page");

const RESPONSES: Record<string, unknown> = {
  "/api/market/status": { is_open: false, message: "Market closed" },
  "/api/portfolio/stats": {
    totalValue: 100000,
    dayChange: 1500,
    dayChangePercent: 1.5,
    totalReturn: -2000,
    totalReturnPercent: -2,
  },
  "/api/market/watchlist": [
    // `change` is the absolute price difference; no `changePercent` was sent for SBIN.
    { symbol: "SBIN", price: 812.5, change: 5.5 },
    { symbol: "TCS", price: 3890.25, change: -19.5, changePercent: -0.5 },
  ],
  "/api/market/overview": {
    indices: [
      { symbol: "^NSEI", name: "NIFTY 50", price: 22450, change: 112.25, change_pct: 0.5 },
      { symbol: "GC=F", name: "Gold", price: 2400.5, change: 12, change_pct: 0.5 },
    ],
    timestamp: "2026-10-20T10:05:09+00:00",
  },
};

function databaseDown(): Response {
  return Response.json(
    { error: { code: "DATABASE_UNAVAILABLE", message: "Database unavailable" } },
    { status: 503 },
  );
}

function stubBackend(overrides: Record<string, () => Response | Promise<Response>> = {}) {
  const fetchMock = vi.fn((input: RequestInfo | URL): Promise<Response> => {
    const url = new URL(input instanceof Request ? input.url : String(input));
    const override = overrides[url.pathname];
    return Promise.resolve(override ? override() : Response.json(RESPONSES[url.pathname] ?? {}));
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function requestedPaths(fetchMock: ReturnType<typeof stubBackend>): string[] {
  return fetchMock.mock.calls
    .map(([input]) => new URL(input instanceof Request ? input.url : String(input)).pathname)
    .sort((a, b) => a.localeCompare(b));
}

function renderDashboard() {
  const client = createQueryClient();
  client.setDefaultOptions({ queries: { retry: false } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <DashboardPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("dashboard change columns", () => {
  beforeEach(() => {
    stubBackend();
  });

  it("shows the watchlist percent and never the absolute change as a percent", async () => {
    renderDashboard();

    const sbin = (await screen.findByText("SBIN")).closest("tr");
    const tcs = screen.getByText("TCS").closest("tr");
    if (!sbin || !tcs) throw new Error("watchlist rows missing");

    expect(within(sbin).getByText("Unavailable")).toBeInTheDocument();
    expect(within(sbin).queryByText("+5.50%")).not.toBeInTheDocument();
    expect(within(tcs).getByText("-0.50%")).toBeInTheDocument();
  });

  it("shows the index day change in percent, not the point change", async () => {
    renderDashboard();

    const nifty = (await screen.findByText("NIFTY 50")).closest("tr");
    if (!nifty) throw new Error("index row missing");

    expect(within(nifty).getByText("+0.50%")).toBeInTheDocument();
    expect(within(nifty).queryByText("+112.25%")).not.toBeInTheDocument();
  });

  it("prints a single sign before the portfolio percentages", async () => {
    renderDashboard();

    expect(await screen.findByText("+1.50%")).toBeInTheDocument();
    expect(screen.getByText("-2.00%")).toBeInTheDocument();
    expect(screen.queryByText("++1.50%")).not.toBeInTheDocument();
  });
});

describe("dashboard data loading", () => {
  it("loads the closed-market endpoints and shows the post-market snapshot", async () => {
    const fetchMock = stubBackend();
    renderDashboard();

    expect(await screen.findByText("NIFTY 50")).toBeInTheDocument();
    expect(new Set(requestedPaths(fetchMock))).toEqual(
      new Set([
        "/api/market/overview",
        "/api/market/status",
        "/api/market/watchlist",
        "/api/portfolio/stats",
      ]),
    );
  });

  it("explains a database outage in the watchlist instead of an empty list", async () => {
    stubBackend({ "/api/market/watchlist": databaseDown });
    renderDashboard();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Database unavailable: the backend cannot reach Postgres. Retry once it is back.",
    );
    expect(screen.queryByText("No stocks in watchlist")).not.toBeInTheDocument();
    expect(await screen.findByText("NIFTY 50")).toBeInTheDocument();
  });
});

describe("dashboard post-market insights", () => {
  beforeEach(() => {
    stubBackend();
  });

  it("lists the overview's Gold row in the commodities panel", async () => {
    renderDashboard();

    expect(await screen.findByText("$2,400.50")).toBeInTheDocument();
  });

  it("shows the snapshot time in IST whatever the machine's zone", async () => {
    renderDashboard();

    expect(await screen.findByText("Snapshot: 20 Oct 2026, 15:35:09 IST")).toBeInTheDocument();
  });
});

describe("dashboard during market hours", () => {
  it("shows the delayed badge and the NSE indices when the socket is not connected", async () => {
    stubBackend({
      "/api/market/status": () =>
        Response.json({ is_open: true, current_time_ist: "10:00:00 IST" }),
      "/api/market/indices": () =>
        Response.json([
          {
            name: "NIFTY 50",
            symbol: "NIFTY50",
            value: 22450.5,
            change: -10,
            changePercent: -0.04,
          },
        ]),
    });
    renderDashboard();

    expect(await screen.findByText("DELAYED")).toBeInTheDocument();
    expect(screen.getByText("10:00:00 IST")).toBeInTheDocument();
    expect(screen.getByText("NSE Delayed (Disconnected)")).toBeInTheDocument();
    const nifty = (await screen.findByText("NIFTY 50")).closest("tr");
    if (!nifty) throw new Error("index row missing");
    expect(within(nifty).getByText("22,450.50")).toBeInTheDocument();
    expect(within(nifty).getByText("-0.04%")).toHaveClass("text-loss");
  });
});

describe("dashboard empty state", () => {
  it("keeps the panels while the watchlist still loads after the stats failed", async () => {
    stubBackend({
      "/api/portfolio/stats": databaseDown,
      "/api/market/overview": () => new Promise<Response>(() => {}),
      "/api/market/watchlist": () => new Promise<Response>(() => {}),
    });
    renderDashboard();

    expect(await screen.findByText("Global Markets")).toBeInTheDocument();
    expect(screen.queryByText("No Data Available")).not.toBeInTheDocument();
  });

  it("shows the load failure once every panel has settled empty", async () => {
    stubBackend({
      "/api/portfolio/stats": databaseDown,
      "/api/market/overview": () => Response.json({ indices: [] }),
      "/api/market/watchlist": () => Response.json([]),
    });
    renderDashboard();

    expect(await screen.findByText("No Data Available")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Database unavailable");
  });
});
