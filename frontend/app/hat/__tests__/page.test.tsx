import { QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
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

const SIGNALS = [
  {
    id: "00000000-0000-0000-0000-000000003000",
    bot_id: BOT_ID,
    bot: "momentum",
    symbol: "INFY",
    playbook: "breakout",
    trade_date: "2026-10-21",
    triggered_at: "2026-10-21T04:30:00Z",
    trigger_price: "1520.0000",
    stop_price: "1480.0000",
    qty: 6,
    risk_amount: "240.0000",
    mode: "confirm",
    state: "AwaitingApproval",
    confidence: "medium",
    confidence_basis: { reason: "lower_bound_not_positive", trades: 42 },
  },
  {
    id: "00000000-0000-0000-0000-000000003001",
    bot_id: BOT_ID,
    bot: "momentum",
    symbol: "TCS",
    playbook: "breakout",
    trade_date: "2026-10-20",
    triggered_at: "2026-10-20T04:30:00Z",
    trigger_price: "4000.0000",
    stop_price: "3890.0000",
    qty: 12,
    risk_amount: "1320.0000",
    mode: "shadow",
    state: "Open",
    confidence: null,
    confidence_basis: { reason: null, trades: null },
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
  "/api/hat/signals": () => Response.json(SIGNALS),
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
  const client = createQueryClient();
  render(
    <QueryClientProvider client={client}>
      <HatPage />
    </QueryClientProvider>,
  );
  return client;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("HatPage", () => {
  it("shows every section from the read-only routes and only reads", async () => {
    const calls = stubBackend(ROUTES);
    renderPage();

    expect(await screen.findByText("11:30:00 IST")).toBeInTheDocument();
    const bots = await screen.findByRole("region", { name: "Bots" });
    expect(await within(bots).findByText("manage-only: bot_kill")).toBeInTheDocument();
    const status = screen.getByRole("region", { name: "Status" });
    expect(within(status).getByText(/bull_quiet \(unconfirmed\)/)).toBeInTheDocument();
    expect(within(status).getByText("fyers XA1234: live")).toBeInTheDocument();
    expect(within(status).getByText("scan: succeeded")).toBeInTheDocument();
    expect(within(status).getByText("momentum (risk)")).toBeInTheDocument();
    expect(
      within(bots).getByText(
        "shadow (asked confirm: ddpi_not_confirmed); auto from 22 Oct 2026, 09:15:00 IST",
      ),
    ).toBeInTheDocument();
    const allocations = screen.getByRole("region", { name: "Allocations" });
    expect(await within(allocations).findByText("25.00%")).toBeInTheDocument();
    expect(within(allocations).getByText("2,50,000.00")).toBeInTheDocument();
    const positions = screen.getByRole("region", { name: "Open positions" });
    expect(await within(positions).findByText("3,950.25")).toBeInTheDocument();
    expect(within(positions).getByText("virtual")).toBeInTheDocument();
    const watchlist = screen.getByRole("region", { name: "Watchlist" });
    expect(await within(watchlist).findByText("0.91")).toBeInTheDocument();
    const events = screen.getByRole("region", { name: "Recent events" });
    expect(await within(events).findByText("signal.triggered")).toBeInTheDocument();

    const signals = screen.getByRole("region", { name: "Signals" });
    expect(await within(signals).findByText("medium")).toBeInTheDocument();
    expect(within(signals).getByText("1,520.00")).toBeInTheDocument();

    expect(calls.every((call) => call.startsWith("GET /api/hat/"))).toBe(true);
    expect(calls).toContain("GET /api/hat/signals?limit=25");
    expect(calls).toContain("GET /api/hat/events?limit=25");
  });

  it("shows the empty states when the database holds nothing yet", async () => {
    stubBackend({
      ...ROUTES,
      "/api/hat/status": () => Response.json({ ...STATUS, bots: [], regime: null }),
      "/api/hat/allocations": () => Response.json({ trade_date: null, rows: [] }),
      "/api/hat/positions": () => Response.json([]),
      "/api/hat/events": () => Response.json([]),
      "/api/hat/signals": () => Response.json([]),
    });
    renderPage();

    expect(await screen.findByText("No open positions.")).toBeInTheDocument();
    expect(await screen.findByText("No allocation yet.")).toBeInTheDocument();
    expect(await screen.findByText("No events.")).toBeInTheDocument();
    expect(await screen.findByText("No signals.")).toBeInTheDocument();
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

  it("marks the data stale when a refetch fails after a good read", async () => {
    let up = true;
    stubBackend({
      ...ROUTES,
      "/api/hat/status": () =>
        up
          ? Response.json(STATUS)
          : Response.json({ detail: "The HAT database could not be read" }, { status: 422 }),
    });
    const client = renderPage();

    const status = await screen.findByRole("region", { name: "Status" });
    expect(await within(status).findByText("armed")).toBeInTheDocument();
    up = false;
    await client.refetchQueries({ queryKey: ["hat", "status"] });

    // The last good read stays on screen, under a notice that it is stale.
    expect((await within(status).findByRole("alert")).textContent).toMatch(
      /^Stale since \d{2}:\d{2}:\d{2} IST: the last refresh failed \(The HAT database could not be read\)/,
    );
    expect(within(status).getByText("armed")).toBeInTheDocument();
    expect(screen.getByText("Stale, last snapshot 11:30:00 IST").closest("output")).not.toBeNull();
  });

  it("shows one page banner, not one per section, when every read fails together", async () => {
    let up = true;
    const down = () => Response.json({ detail: "HTTP 502: Bad Gateway" }, { status: 422 });
    stubBackend(
      Object.fromEntries(
        Object.entries(ROUTES).map(([path, route]) => [path, () => (up ? route() : down())]),
      ),
    );
    const client = renderPage();

    const positions = await screen.findByRole("region", { name: "Open positions" });
    expect(await within(positions).findByText("3,950.25")).toBeInTheDocument();
    up = false;
    await client.refetchQueries({ queryKey: ["hat"] });

    const banner = await screen.findByText(/^Not refreshing since \d{2}:\d{2}:\d{2} IST/);
    expect(banner.textContent).toContain("(HTTP 502: Bad Gateway)");
    expect(screen.getAllByRole("alert")).toEqual([banner]);
    // The stale values stay on screen, dimmed.
    expect(within(positions).getByText("3,950.25").closest(".opacity-60")).not.toBeNull();
  });

  it("opens a signal's details in a drawer", async () => {
    stubBackend(ROUTES);
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "Details of signal INFY" }));

    const drawer = await screen.findByRole("dialog", { name: "INFY" });
    expect(within(drawer).getByText("lower_bound_not_positive · 42 trades")).toBeInTheDocument();
    expect(within(drawer).getByText("240.00")).toBeInTheDocument();
  });
});
