import { QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vite-plus/test";
import { createQueryClient } from "@/lib/queries/query-client";

// The API client reads VITE_API_URL at import time; node cannot build a Request from a relative URL.
vi.stubEnv("VITE_API_URL", "http://api.test");
const { default: TerminalPage } = await import("../page");

vi.mock("@/lib/realtime/hooks", () => ({
  useConnectionStatus: () => "disconnected",
  useQuotes: () => ({}),
}));

const ORDER_PATH = "/api/trading/order";

const BOARD = {
  underlying: "NIFTY",
  spot_price: 25010.5,
  expiry: "2026-10-27",
  atm_strike: 25000,
  timestamp: "2026-10-20T10:00:00Z",
  strikes: [
    {
      strike: 25000,
      ce: { symbol: "NIFTY26OCT25000CE", ltp: 120, oi: 1000, volume: 50, change_pct: 1 },
      pe: { symbol: "NIFTY26OCT25000PE", ltp: 110, oi: 2000, volume: 25, change_pct: -1 },
    },
  ],
};

type Captured = { url: string; method: string; body: string };

const OPEN_POSITION = {
  id: 7,
  symbol: "SBIN",
  net_qty: 10,
  entry_price: 790,
  current_price: 800,
  unrealized_pnl: 100,
  realized_pnl: 0,
  net_pnl: 100,
  product_type: "INTRADAY",
  mode: "PAPER",
};

let paperPositions: (typeof OPEN_POSITION)[] = [];

function respond(url: string): Response {
  if (url.includes("/api/trading/positions/square")) {
    paperPositions = [];
    return Response.json({ status: "ok" });
  }
  if (url.includes("/api/market/watchlist")) {
    return Response.json([{ symbol: "SBIN", name: "State Bank", price: 800, change: 1.2 }]);
  }
  if (url.includes("/api/terminal/options/board")) return Response.json(BOARD);
  if (url.includes("/api/trading/positions/book")) {
    return Response.json({ live_positions: [], paper_positions: paperPositions });
  }
  if (url.includes(ORDER_PATH)) return Response.json({ status: "accepted", order_id: "ord-1" });
  return Response.json([]);
}

function installFetch(): Captured[] {
  const calls: Captured[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      // The typed client passes a Request; plain fetch calls pass a relative URL and init.
      const source =
        input instanceof Request
          ? input
          : new Request(new URL(String(input), "http://api.test"), init);
      const body = source.method === "GET" ? "" : await source.clone().text();
      calls.push({ url: source.url, method: source.method, body });
      return respond(source.url);
    }),
  );
  return calls;
}

function orderBodies(calls: Captured[]): string[] {
  return calls.filter((call) => call.url.includes(ORDER_PATH)).map((call) => call.body);
}

function renderPage() {
  const client = createQueryClient();
  client.setDefaultOptions({ queries: { retry: false } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <TerminalPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function ticketButton(name: "Buy" | "Sell"): HTMLElement {
  const buttons = screen.getAllByRole("button", { name });
  return buttons[buttons.length - 1];
}

function setValue(label: RegExp, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

describe("terminal order payload", () => {
  let calls: Captured[];

  beforeEach(() => {
    paperPositions = [];
    calls = installFetch();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("sends a PAPER equity LIMIT order for the first watchlist symbol", async () => {
    renderPage();
    await screen.findAllByText("State Bank");
    setValue(/^price$/i, "801.5");
    setValue(/^quantity$/i, "3");

    fireEvent.click(ticketButton("Buy"));

    await screen.findByText("PAPER BUY accepted (ord-1)");
    expect(orderBodies(calls)).toEqual([
      '{"mode":"PAPER","symbol":"SBIN","side":"BUY","quantity":3,"product":"INTRADAY","type":"LIMIT","price":801.5,"trigger_price":0,"tag":"terminal-options","instrument_type":"EQ","is_live_confirmation_ack":false,"risk_override_reason":null}',
    ]);
  });

  it("sends an options MARKET order sized in lots of the underlying", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "NIFTY26OCT25000CE" }));
    setValue(/order type/i, "MARKET");
    setValue(/^lots$/i, "2");

    fireEvent.click(ticketButton("Sell"));

    await screen.findByText("PAPER SELL accepted (ord-1)");
    expect(orderBodies(calls)).toEqual([
      '{"mode":"PAPER","symbol":"NIFTY26OCT25000CE","side":"SELL","quantity":150,"product":"INTRADAY","type":"MARKET","price":0,"trigger_price":0,"tag":"terminal-options","instrument_type":"CE","is_live_confirmation_ack":false,"risk_override_reason":null}',
    ]);
  });

  it("blocks an SL order without a trigger, then sends it once the trigger is set", async () => {
    renderPage();
    await screen.findAllByText("State Bank");
    fireEvent.click(screen.getByRole("button", { name: /positions/i }));
    setValue(/order type/i, "SL");
    setValue(/^price$/i, "790");

    fireEvent.click(ticketButton("Sell"));
    expect(await screen.findByText("Enter a trigger price greater than 0")).toBeInTheDocument();
    expect(orderBodies(calls)).toEqual([]);

    setValue(/^trigger$/i, "792");
    fireEvent.click(ticketButton("Sell"));

    await screen.findByText("PAPER SELL accepted (ord-1)");
    expect(orderBodies(calls)).toEqual([
      '{"mode":"PAPER","symbol":"SBIN","side":"SELL","quantity":1,"product":"INTRADAY","type":"SL","price":790,"trigger_price":792,"tag":"terminal-live","instrument_type":"EQ","is_live_confirmation_ack":false,"risk_override_reason":null}',
    ]);
  });

  it("sends a LIVE order only after confirmation, with the trimmed override reason", async () => {
    const confirm = vi.fn(() => true);
    vi.stubGlobal("confirm", confirm);
    renderPage();
    await screen.findAllByText("State Bank");
    fireEvent.click(screen.getByRole("button", { name: "LIVE" }));
    setValue(/^price$/i, "800");
    setValue(/risk override reason/i, "  hedge  ");

    fireEvent.click(ticketButton("Buy"));

    await screen.findByText("LIVE BUY accepted (ord-1)");
    expect(confirm.mock.calls).toEqual([["Confirm LIVE BUY 1 SBIN?"]]);
    expect(orderBodies(calls)).toEqual([
      '{"mode":"LIVE","symbol":"SBIN","side":"BUY","quantity":1,"product":"INTRADAY","type":"LIMIT","price":800,"trigger_price":0,"tag":"terminal-options","instrument_type":"EQ","is_live_confirmation_ack":true,"risk_override_reason":"hedge"}',
    ]);
  });
});

describe("terminal data", () => {
  let calls: Captured[];

  beforeEach(() => {
    paperPositions = [];
    calls = installFetch();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    globalThis.history.replaceState(null, "", "/");
  });

  function boardUrls(): string[] {
    return calls
      .filter((call) => call.url.includes("/api/terminal/options/board"))
      .map((call) => new URL(call.url).search);
  }

  it("selects the ?symbol= symbol and lists it after the saved watchlist", async () => {
    globalThis.history.replaceState(null, "", "/terminal?symbol=TCS");
    renderPage();
    await screen.findAllByText("State Bank");

    const rows = screen.getAllByRole("button", { name: /on TradingView$/ });
    expect(rows.map((row) => row.getAttribute("aria-label"))).toEqual([
      "Open TCS on TradingView",
      "Open SBIN on TradingView",
      "Open TCS on TradingView",
    ]);
    expect(screen.getByText("Trade Symbol:").textContent).toBe("Trade Symbol: TCS");
  });

  it("shows the board's expiry until one is picked, then requests the picked expiry", async () => {
    renderPage();
    expect(await screen.findByText("PCR(OI): 2 | PCR(Vol): 0.5")).toBeInTheDocument();
    expect(screen.getByDisplayValue("2026-10-27")).toBeInTheDocument();
    expect(boardUrls()).toEqual(["?underlying=NIFTY&strike_count=15"]);

    fireEvent.change(screen.getByDisplayValue("2026-10-27"), { target: { value: "2026-11-03" } });

    await screen.findByDisplayValue("2026-11-03");
    await screen.findByText("NIFTY Spot: 25010.50");
    expect(boardUrls().at(-1)).toBe("?underlying=NIFTY&strike_count=15&expiry=2026-11-03");
  });

  it("lists open positions and drops a squared one", async () => {
    paperPositions = [OPEN_POSITION];
    renderPage();
    await screen.findAllByText("State Bank");
    fireEvent.click(screen.getByRole("button", { name: /positions/i }));

    const square = await screen.findByRole("button", { name: "Square" });
    expect(square.closest("tr")?.textContent).toBe("SBINPAPERLONG10790.00800.00100.00100.00Square");

    fireEvent.click(square);

    expect(await screen.findByText("PAPER square submitted for SBIN")).toBeInTheDocument();
    expect(await screen.findByText("No open positions")).toBeInTheDocument();
    expect(calls.filter((call) => call.url.includes("/square")).map((call) => call.body)).toEqual([
      '{"symbol":"SBIN","mode":"PAPER"}',
    ]);
  });
});
