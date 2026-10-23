import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vite-plus/test";
import { createQueryClient } from "@/lib/queries/query-client";

// The client reads VITE_API_URL at import time; Node cannot build a Request from a relative URL.
vi.stubEnv("VITE_API_URL", "http://api.test");
const { default: HatPage } = await import("../page");

type Route = () => Response | Promise<Response>;

const BOT_ID = "00000000-0000-0000-0000-0000000000b1";

const STATUS = {
  as_of: "2026-10-21T06:00:00Z",
  trade_date: "2026-10-21",
  kill_account: false,
  feed_stale_at: null,
  regime: { trade_date: "2026-10-20", state: "bull_quiet", confirmed: false },
  bots: [
    {
      id: BOT_ID,
      name: "momentum",
      execution_mode: "confirm",
      effective_mode: "shadow",
      mode_reason: "ddpi_not_confirmed",
      pending_mode: "auto",
      pending_from: "2026-10-22T03:45:00Z",
      status: "active",
      lifecycle_state: "live",
      risk_cluster: "c1",
      manage_only: true,
      reasons: ["bot_kill"],
    },
  ],
  kill_switches: [{ scope: "bot", bot_id: BOT_ID, channel: "risk", state: "tripped" }],
  sessions: [{ account: "fyers XA1234", status: "live" }],
  jobs: [
    {
      job: "scan",
      trade_date: "2026-10-21",
      status: "succeeded",
      started_at: "2026-10-21T03:00:00Z",
      finished_at: "2026-10-21T03:05:00Z",
      calls: 1,
      rows: 2,
      error: null,
    },
  ],
};

const ALLOCATIONS = {
  trade_date: "2026-10-20",
  rows: [
    {
      bot_id: BOT_ID,
      bot: "momentum",
      base_weight: "0.5",
      regime_mult: "1",
      health_mult: "1",
      final_weight: "0.25",
      capital: "250000.0000",
      risk_pct: "0.005",
      currency: "INR",
    },
  ],
};

const WATCHLIST = [
  {
    bot_id: BOT_ID,
    bot: "momentum",
    trade_date: "2026-10-20",
    entries: [{ symbol: "INFY", score: "0.91" }],
  },
];

const POSITIONS = [
  {
    id: "00000000-0000-0000-0000-000000002000",
    bot_id: BOT_ID,
    bot: "momentum",
    symbol: "TCS",
    qty_open: 12,
    avg_entry: "4012.5000",
    initial_stop: "3890.0000",
    current_stop: "3950.2500",
    opened_at: "2026-10-19T04:00:00Z",
    state: "Open",
    is_virtual: true,
  },
];

const EVENTS = [
  {
    id: 7,
    occurred_at: "2026-10-21T04:30:00Z",
    recorded_at: "2026-10-21T04:30:00Z",
    event_type: "signal.triggered",
    actor: "engine",
    aggregate_type: "signal",
    aggregate_id: BOT_ID,
    bot_id: BOT_ID,
    correlation_id: null,
    payload: {},
  },
];

const ROUTES: Record<string, Route> = {
  "/api/hat/status": () => Response.json(STATUS),
  "/api/hat/allocations": () => Response.json(ALLOCATIONS),
  "/api/hat/watchlist": () => Response.json(WATCHLIST),
  "/api/hat/positions": () => Response.json(POSITIONS),
  "/api/hat/events": () => Response.json(EVENTS),
};

function stubBackend(routes: Record<string, Route>) {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL): Promise<Response> => {
      const request = input instanceof Request ? input : new Request(String(input));
      const url = new URL(request.url);
      calls.push(`${request.method} ${url.pathname}${url.search}`);
      const route = routes[url.pathname];
      return Promise.resolve(route ? route() : Response.json({}, { status: 404 }));
    }),
  );
  return calls;
}

function renderPage() {
  render(
    <QueryClientProvider client={createQueryClient()}>
      <HatPage />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("HatPage", () => {
  it("shows every section from the read-only routes and only reads", async () => {
    const calls = stubBackend(ROUTES);
    renderPage();

    const bots = await screen.findByRole("region", { name: "Bots" });
    expect(await within(bots).findByText("manage-only: bot_kill")).toBeInTheDocument();
    const status = screen.getByRole("region", { name: "Status" });
    expect(within(status).getByText(/bull_quiet \(unconfirmed\)/)).toBeInTheDocument();
    expect(within(status).getByText("fyers XA1234: live")).toBeInTheDocument();
    expect(within(status).getByText("scan: succeeded")).toBeInTheDocument();
    expect(
      within(bots).getByText(
        "shadow (asked confirm: ddpi_not_confirmed); auto from 2026-10-22T03:45:00Z",
      ),
    ).toBeInTheDocument();
    const allocations = screen.getByRole("region", { name: "Allocations" });
    expect(await within(allocations).findByText("25.00%")).toBeInTheDocument();
    expect(within(allocations).getByText("2,50,000.00")).toBeInTheDocument();
    const positions = screen.getByRole("region", { name: "Open positions" });
    expect(await within(positions).findByText("3,950.25")).toBeInTheDocument();
    expect(within(positions).getByText("virtual")).toBeInTheDocument();
    const watchlist = screen.getByRole("region", { name: "Watchlist" });
    expect(await within(watchlist).findByText("INFY 0.91")).toBeInTheDocument();
    const events = screen.getByRole("region", { name: "Recent events" });
    expect(await within(events).findByText("signal.triggered")).toBeInTheDocument();

    expect(calls.every((call) => call.startsWith("GET /api/hat/"))).toBe(true);
    expect(calls).toContain("GET /api/hat/events?limit=25");
  });

  it("shows the empty states when the database holds nothing yet", async () => {
    stubBackend({
      ...ROUTES,
      "/api/hat/status": () => Response.json({ ...STATUS, bots: [], regime: null }),
      "/api/hat/allocations": () => Response.json({ trade_date: null, rows: [] }),
      "/api/hat/positions": () => Response.json([]),
      "/api/hat/events": () => Response.json([]),
    });
    renderPage();

    expect(await screen.findByText("No open positions.")).toBeInTheDocument();
    expect(await screen.findByText("No allocation yet.")).toBeInTheDocument();
    expect(await screen.findByText("No events.")).toBeInTheDocument();
    expect(await screen.findByText("No bots.")).toBeInTheDocument();
  });

  it("shows the fixed database message when a read fails", async () => {
    stubBackend({
      ...ROUTES,
      "/api/hat/positions": () =>
        Response.json({ detail: "The HAT database could not be read" }, { status: 422 }),
    });
    renderPage();

    const positions = await screen.findByRole("region", { name: "Open positions" });
    expect(await within(positions).findByRole("alert")).toHaveTextContent(
      "The HAT database could not be read",
    );
  });
});
