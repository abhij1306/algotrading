import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vite-plus/test";
import { createQueryClient } from "@/lib/queries/query-client";
import DashboardPage from "../page";

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
    indices: [{ symbol: "^NSEI", name: "NIFTY 50", price: 22450, change: 112.25, change_pct: 0.5 }],
  },
};

function respond(input: RequestInfo | URL): Promise<Response> {
  const url = new URL(input instanceof Request ? input.url : String(input), "http://api.test");
  return Promise.resolve(Response.json(RESPONSES[url.pathname] ?? {}));
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

describe("dashboard change columns", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(respond));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
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
