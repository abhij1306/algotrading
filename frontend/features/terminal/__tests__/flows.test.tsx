import { QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vite-plus/test";
import { createQueryClient } from "@/lib/queries/query-client";

// The API client reads VITE_API_URL at import time; node cannot build a Request from a relative URL.
vi.stubEnv("VITE_API_URL", "http://api.test");
const { TerminalScreen: TerminalPage } = await import("../TerminalScreen");

// Every symbol list the screen asks the quote store to stream, in render order.
const quoteSubscriptions = vi.hoisted((): string[][] => []);

vi.mock("@/lib/realtime/hooks", () => ({
  useConnectionStatus: () => "disconnected",
  useQuotes: (symbols: readonly string[]) => {
    quoteSubscriptions.push([...symbols]);
    return {};
  },
}));

const ORDER_PATH = "/api/trading/order";
const SEARCH_PATH = "/api/market/search";
const BOARD_PATH = "/api/terminal/options/board";
// While set, options board requests wait for it, so a test can see the chain mid-load.
let boardHold: Promise<void> | null = null;
const WATCHLIST_PATH = "/api/market/watchlist";

// Symbol search answers, by query text.
const SEARCH_RESULTS: Record<string, { symbol: string; name: string }[]> = {
  SB: [{ symbol: "SBIN", name: "State Bank" }],
  INF: [{ symbol: "INFY", name: "Infosys" }],
};

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

const BANKNIFTY_BOARD = {
  ...BOARD,
  underlying: "BANKNIFTY",
  spot_price: 56020,
  atm_strike: 56000,
  strikes: [
    {
      strike: 56000,
      ce: { symbol: "BANKNIFTY26OCT56000CE", ltp: 300, oi: 500, volume: 40, change_pct: 2 },
      pe: { symbol: "BANKNIFTY26OCT56000PE", ltp: 280, oi: 600, volume: 30, change_pct: -2 },
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

// Fixed ids so the payload assertions stay literal; every submit intent takes the next one.
const CLIENT_ORDER_IDS = [
  "11111111-1111-4111-8111-111111111111",
  "22222222-2222-4222-8222-222222222222",
] as const;

function stubClientOrderIds() {
  const ids = [...CLIENT_ORDER_IDS];
  return vi.spyOn(crypto, "randomUUID").mockImplementation(() => {
    const next = ids.shift();
    if (!next) throw new Error("more submit intents than stubbed ids");
    return next;
  });
}

let paperPositions: Record<string, unknown>[] = [];
let livePositions: Record<string, unknown>[] = [];
let liveStale = false;
const BROKER_UNAVAILABLE_NOTE =
  "Broker unavailable: live positions could not be refreshed. Any shown are the last synced values and cannot be squared until it reconnects.";
// Order POSTs that fail as a dropped connection before any response arrives.
let orderNetworkFailures = 0;
// Order POSTs answered 200 with a body cut off mid-JSON.
let orderTruncatedResponses = 0;
// Body every order POST answers with.
let orderResult: Record<string, unknown> = { status: "accepted", order_id: "ord-1" };
let squareResult: { status: string; message?: string } = { status: "SUCCESS" };
const RISK_CHECK_PATH = "/api/trading/risk-check";
const RISK_PASS = {
  can_trade: true,
  status: "PASS",
  message: "All risk checks passed",
  code: "OK",
  details: { warnings: [] },
};
// Body every pre-trade risk check answers with.
let riskResult: Record<string, unknown> = RISK_PASS;
// While set, risk-check requests wait for it, so a test can see the dialog mid-check.
let riskHold: Promise<void> | null = null;

/** The dialog's Confirm button once the risk check has settled and enabled it. */
async function enabledConfirm(dialog: HTMLElement, side: "BUY" | "SELL"): Promise<HTMLElement> {
  const confirm = within(dialog).getByRole("button", { name: `Confirm LIVE ${side}` });
  await vi.waitFor(() => expect(confirm).toBeEnabled());
  return confirm;
}
// Square-off POSTs that fail as a dropped connection before any response arrives.
let squareNetworkFailures = 0;

function respond(url: string): Response {
  if (url.includes("/api/trading/positions/square")) {
    if (squareResult.status === "SUCCESS") paperPositions = [];
    return Response.json(squareResult);
  }
  if (url.includes(WATCHLIST_PATH)) {
    // `change` is the absolute move; `changePercent` is what the terminal shows.
    return Response.json([
      { symbol: "SBIN", name: "State Bank", price: 800, change: 9.6, changePercent: 1.21 },
    ]);
  }
  if (url.includes("/api/terminal/options/board")) {
    const underlying = new URL(url).searchParams.get("underlying");
    return Response.json(underlying === "BANKNIFTY" ? BANKNIFTY_BOARD : BOARD);
  }
  if (url.includes(SEARCH_PATH)) {
    return Response.json(SEARCH_RESULTS[new URL(url).searchParams.get("query") ?? ""] ?? []);
  }
  if (url.includes("/api/trading/positions/book")) {
    return Response.json({
      live_positions: livePositions,
      paper_positions: paperPositions,
      live_stale: liveStale,
    });
  }
  if (url.includes(ORDER_PATH)) return Response.json(orderResult);
  if (url.includes(RISK_CHECK_PATH)) return Response.json(riskResult);
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
      if (source.url.includes(ORDER_PATH) && orderNetworkFailures > 0) {
        orderNetworkFailures -= 1;
        throw new TypeError("Failed to fetch");
      }
      if (source.url.includes("/positions/square") && squareNetworkFailures > 0) {
        squareNetworkFailures -= 1;
        throw new TypeError("Failed to fetch");
      }
      if (source.url.includes(RISK_CHECK_PATH) && riskHold) await riskHold;
      if (source.url.includes(BOARD_PATH) && boardHold) await boardHold;
      if (source.url.includes(ORDER_PATH) && orderTruncatedResponses > 0) {
        orderTruncatedResponses -= 1;
        return new Response('{"status":"acc', { status: 200 });
      }
      return respond(source.url);
    }),
  );
  return calls;
}

function orderBodies(calls: Captured[]): string[] {
  return calls.filter((call) => call.url.includes(ORDER_PATH)).map((call) => call.body);
}

function renderPage(url = "/terminal") {
  const client = createQueryClient();
  client.setDefaultOptions({ queries: { retry: false } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[url]}>
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
    orderNetworkFailures = 0;
    orderTruncatedResponses = 0;
    calls = installFetch();
    stubClientOrderIds();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("sends a PAPER equity LIMIT order for the first watchlist symbol", async () => {
    renderPage();
    await screen.findAllByText("State Bank");
    setValue(/^price$/i, "801.5");
    setValue(/^quantity$/i, "3");

    fireEvent.click(ticketButton("Buy"));

    await screen.findByText("PAPER BUY accepted (ord-1)");
    // PAPER sends on click: no confirmation dialog.
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(orderBodies(calls)).toEqual([
      '{"mode":"PAPER","symbol":"SBIN","side":"BUY","quantity":3,"product":"INTRADAY","type":"LIMIT","price":801.5,"trigger_price":0,"tag":"terminal-equity","instrument_type":"EQ","is_live_confirmation_ack":false,"risk_override_reason":null,"client_order_id":"11111111-1111-4111-8111-111111111111"}',
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
      '{"mode":"PAPER","symbol":"NIFTY26OCT25000CE","side":"SELL","quantity":150,"product":"INTRADAY","type":"MARKET","price":0,"trigger_price":0,"tag":"terminal-options","instrument_type":"CE","is_live_confirmation_ack":false,"risk_override_reason":null,"client_order_id":"11111111-1111-4111-8111-111111111111"}',
    ]);
  });

  it("sizes a ?symbol= option in lots of its own underlying, not the board's", async () => {
    renderPage("/terminal?symbol=BANKNIFTY26OCT50000CE");
    await screen.findByText("State Bank");
    setValue(/order type/i, "MARKET");

    fireEvent.click(ticketButton("Buy"));

    await screen.findByText("PAPER BUY accepted (ord-1)");
    expect(orderBodies(calls).map((body) => JSON.parse(body).quantity)).toEqual([30]);
  });

  it("refuses an option whose lot size it does not know", async () => {
    renderPage("/terminal?symbol=MIDCPNIFTY26OCT12000CE");
    await screen.findByText("State Bank");
    setValue(/order type/i, "MARKET");

    fireEvent.click(ticketButton("Buy"));

    expect(
      await screen.findByText("Unknown lot size for MIDCPNIFTY26OCT12000CE"),
    ).toBeInTheDocument();
    expect(orderBodies(calls)).toEqual([]);
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
      '{"mode":"PAPER","symbol":"SBIN","side":"SELL","quantity":1,"product":"INTRADAY","type":"SL","price":790,"trigger_price":792,"tag":"terminal-equity","instrument_type":"EQ","is_live_confirmation_ack":false,"risk_override_reason":null,"client_order_id":"11111111-1111-4111-8111-111111111111"}',
    ]);
  });

  it("sends a LIVE order only after confirmation, with the trimmed override reason", async () => {
    renderPage();
    await screen.findAllByText("State Bank");
    fireEvent.click(screen.getByRole("button", { name: "LIVE" }));
    setValue(/^price$/i, "800");
    setValue(/risk override reason/i, "  hedge  ");

    fireEvent.click(ticketButton("Buy"));
    const dialog = await screen.findByRole("dialog", { name: "Place LIVE BUY order for SBIN?" });
    expect(orderBodies(calls)).toEqual([]);

    fireEvent.click(await enabledConfirm(dialog, "BUY"));

    await screen.findByText("LIVE BUY accepted (ord-1)");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(orderBodies(calls)).toEqual([
      '{"mode":"LIVE","symbol":"SBIN","side":"BUY","quantity":1,"product":"INTRADAY","type":"LIMIT","price":800,"trigger_price":0,"tag":"terminal-equity","instrument_type":"EQ","is_live_confirmation_ack":true,"risk_override_reason":"hedge","client_order_id":"11111111-1111-4111-8111-111111111111"}',
    ]);
  });
});

function dialogRows(dialog: HTMLElement): [string, string][] {
  return within(dialog)
    .getAllByRole("term")
    .map((term) => [term.textContent ?? "", term.nextElementSibling?.textContent ?? ""]);
}

describe("terminal LIVE confirmation", () => {
  let calls: Captured[];

  beforeEach(() => {
    paperPositions = [];
    orderNetworkFailures = 0;
    orderTruncatedResponses = 0;
    orderResult = { status: "accepted", order_id: "ord-1" };
    riskResult = RISK_PASS;
    riskHold = null;
    calls = installFetch();
    stubClientOrderIds();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function riskBodies(): string[] {
    return calls.filter((call) => call.url.includes(RISK_CHECK_PATH)).map((call) => call.body);
  }

  async function riskPanelText(): Promise<string> {
    const panel = screen.getByRole("region", { name: "Risk check" });
    await vi.waitFor(() => expect(panel.textContent).not.toBe("Checking risk..."));
    return panel.textContent ?? "";
  }

  it("runs the risk check for the shown order and lists its warnings before Confirm", async () => {
    riskResult = {
      can_trade: true,
      status: "WARNING",
      message: "Approaching daily loss limit",
      code: "DAILY_LOSS_WARNING",
      details: { warnings: ["Approaching daily loss limit. Current P&L: ₹-4500.00"] },
    };
    await openLiveStopOrder();

    expect(await riskPanelText()).toBe(
      "Risk warnings, overridden by your reason:Approaching daily loss limit. Current P&L: ₹-4500.00",
    );
    expect(riskBodies()).toEqual([
      '{"symbol":"SBIN","side":"SELL","quantity":4,"product":"INTRADAY","type":"SL","price":790,"instrument_type":"EQ"}',
    ]);
    expect(orderBodies(calls)).toEqual([]);
  });

  it("says a warned order without a reason will be rejected", async () => {
    riskResult = {
      can_trade: true,
      status: "WARNING",
      message: "Large order",
      code: "LARGE_ORDER",
      details: { warnings: ["Large order value"] },
    };
    renderPage();
    await screen.findAllByText("State Bank");
    fireEvent.click(screen.getByRole("button", { name: "LIVE" }));
    setValue(/^price$/i, "800");
    fireEvent.click(ticketButton("Buy"));
    await screen.findByRole("dialog");

    expect(await riskPanelText()).toBe(
      "Risk warnings: add a risk override reason, or the order will be rejected.Large order value",
    );
  });

  it("keeps Confirm disabled until the risk check has answered", async () => {
    let release = () => {};
    riskHold = new Promise((resolve) => {
      release = resolve;
    });
    const dialog = await openLiveStopOrder();

    const confirm = within(dialog).getByRole("button", { name: "Confirm LIVE SELL" });
    expect(screen.getByRole("region", { name: "Risk check" }).textContent).toBe("Checking risk...");
    expect(confirm).toBeDisabled();
    fireEvent.click(confirm);
    expect(orderBodies(calls)).toEqual([]);

    release();

    expect(await riskPanelText()).toBe("All risk checks passed.");
    expect(confirm).toBeEnabled();
  });

  it("checks risk afresh each time it opens, never showing an earlier answer", async () => {
    const first = await openLiveStopOrder();
    expect(await riskPanelText()).toBe("All risk checks passed.");
    fireEvent.click(within(first).getByRole("button", { name: "Cancel" }));

    riskResult = {
      can_trade: true,
      status: "WARNING",
      message: "Large order",
      code: "LARGE_ORDER",
      details: { warnings: ["Large order value"] },
    };
    let release = () => {};
    riskHold = new Promise((resolve) => {
      release = resolve;
    });
    fireEvent.click(ticketButton("Sell"));
    const reopened = await screen.findByRole("dialog", { name: "Place LIVE SELL order for SBIN?" });

    // The same order: the first opening's PASS must not stand in for this check.
    const confirm = within(reopened).getByRole("button", { name: "Confirm LIVE SELL" });
    expect(screen.getByRole("region", { name: "Risk check" }).textContent).toBe("Checking risk...");
    expect(confirm).toBeDisabled();

    release();

    expect(await riskPanelText()).toBe(
      "Risk warnings, overridden by your reason:Large order value",
    );
    expect(confirm).toBeEnabled();
    const body =
      '{"symbol":"SBIN","side":"SELL","quantity":4,"product":"INTRADAY","type":"SL","price":790,"instrument_type":"EQ"}';
    expect(riskBodies()).toEqual([body, body]);
    expect(orderBodies(calls)).toEqual([]);
  });

  it("says a failing order will be rejected", async () => {
    riskResult = {
      can_trade: false,
      status: "FAIL",
      message: "Daily order limit reached",
      code: "DAILY_ORDER_LIMIT",
      details: null,
    };
    await openLiveStopOrder();

    expect(await riskPanelText()).toBe("Will be rejected: Daily order limit reached");
  });

  it("says when the risk check is unavailable", async () => {
    riskResult = { detail: "not a risk check" };
    await openLiveStopOrder();

    expect(await riskPanelText()).toBe(
      "Risk check unavailable; the order is checked again when placed.",
    );
  });

  async function openLiveStopOrder() {
    renderPage();
    await screen.findAllByText("State Bank");
    fireEvent.click(screen.getByRole("button", { name: "LIVE" }));
    setValue(/order type/i, "SL");
    setValue(/^quantity$/i, "4");
    setValue(/^price$/i, "790");
    setValue(/^trigger$/i, "792.5");
    setValue(/risk override reason/i, "  hedge  ");
    fireEvent.click(ticketButton("Sell"));
    return screen.findByRole("dialog", { name: "Place LIVE SELL order for SBIN?" });
  }

  it("lists every field it will send and sends nothing until confirmed", async () => {
    const dialog = await openLiveStopOrder();

    expect(dialogRows(dialog)).toEqual([
      ["Mode", "LIVE"],
      ["Side", "SELL"],
      ["Symbol", "SBIN"],
      ["Instrument", "EQ"],
      ["Quantity", "4"],
      ["Order type", "SL"],
      ["Price", "790.00"],
      ["Trigger", "792.50"],
      ["Product", "INTRADAY"],
      ["Risk override reason", "hedge"],
    ]);
    // Focus starts on Cancel, so Enter cancels rather than places the order.
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toHaveFocus();
    expect(orderBodies(calls)).toEqual([]);

    fireEvent.click(await enabledConfirm(dialog, "SELL"));

    await screen.findByText("LIVE SELL accepted (ord-1)");
    expect(orderBodies(calls)).toEqual([
      '{"mode":"LIVE","symbol":"SBIN","side":"SELL","quantity":4,"product":"INTRADAY","type":"SL","price":790,"trigger_price":792.5,"tag":"terminal-equity","instrument_type":"EQ","is_live_confirmation_ack":true,"risk_override_reason":"hedge","client_order_id":"11111111-1111-4111-8111-111111111111"}',
    ]);
  });

  it("sends the order it showed, even if the ticket changes while it is open", async () => {
    const dialog = await openLiveStopOrder();
    setValue(/^quantity$/i, "40");

    fireEvent.click(await enabledConfirm(dialog, "SELL"));

    await screen.findByText("LIVE SELL accepted (ord-1)");
    expect(orderBodies(calls).map((body) => JSON.parse(body).quantity)).toEqual([4]);
  });

  it("shows an option order's lots and the market price it will send", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "NIFTY26OCT25000CE" }));
    fireEvent.click(screen.getByRole("button", { name: "LIVE" }));
    setValue(/order type/i, "MARKET");
    setValue(/^lots$/i, "2");

    fireEvent.click(ticketButton("Buy"));

    const dialog = await screen.findByRole("dialog", {
      name: "Place LIVE BUY order for NIFTY26OCT25000CE?",
    });
    expect(dialogRows(dialog)).toEqual([
      ["Mode", "LIVE"],
      ["Side", "BUY"],
      ["Symbol", "NIFTY26OCT25000CE"],
      ["Instrument", "CE"],
      ["Quantity", "150 (2 lots of 75)"],
      ["Order type", "MARKET"],
      ["Price", "Market"],
      ["Trigger", "None"],
      ["Product", "INTRADAY"],
      ["Risk override reason", "None"],
    ]);
  });

  it("shows a price with more than two decimals exactly as it will be sent", async () => {
    renderPage();
    await screen.findAllByText("State Bank");
    fireEvent.click(screen.getByRole("button", { name: "LIVE" }));
    setValue(/^price$/i, "800.004");

    fireEvent.click(ticketButton("Buy"));

    const dialog = await screen.findByRole("dialog", { name: "Place LIVE BUY order for SBIN?" });
    expect(dialogRows(dialog)[6]).toEqual(["Price", "800.004"]);
  });

  it("sends nothing when cancelled", async () => {
    const dialog = await openLiveStopOrder();

    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

    expect(await screen.findByText("Live order cancelled")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(orderBodies(calls)).toEqual([]);
  });

  it("sends nothing when dismissed with Escape", async () => {
    const dialog = await openLiveStopOrder();

    // The browser fires `cancel` on a modal dialog when Escape is pressed.
    fireEvent(dialog, new Event("cancel", { cancelable: true }));

    expect(await screen.findByText("Live order cancelled")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(orderBodies(calls)).toEqual([]);
  });
});

function clientOrderIds(calls: Captured[]): string[] {
  return orderBodies(calls).map((body) => JSON.parse(body).client_order_id);
}

describe("terminal order idempotency", () => {
  let calls: Captured[];

  beforeEach(() => {
    paperPositions = [];
    orderNetworkFailures = 0;
    orderTruncatedResponses = 0;
    orderResult = { status: "accepted", order_id: "ord-1" };
    calls = installFetch();
    stubClientOrderIds();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("reports an unconfirmed submit as unconfirmed, not as placed or rejected", async () => {
    orderResult = {
      status: "SUBMIT_UNCONFIRMED",
      order_id: "ORD-1A2B",
      message: "Order sent but not confirmed by the broker. Check the broker orderbook.",
    };
    renderPage();
    await screen.findAllByText("State Bank");
    setValue(/^price$/i, "801.5");

    fireEvent.click(ticketButton("Buy"));

    expect(
      await screen.findByText(
        "PAPER BUY unconfirmed: Order sent but not confirmed by the broker. Check the broker orderbook.",
      ),
    ).toBeInTheDocument();
    expect(orderBodies(calls)).toHaveLength(1);
  });

  it("shows the risk warnings an accepted order carries", async () => {
    orderResult = {
      status: "SUBMITTED",
      order_id: "B-1",
      warnings: ["Approaching daily loss limit. Current P&L: ₹-4500.00"],
    };
    renderPage();
    await screen.findAllByText("State Bank");
    setValue(/^price$/i, "801.5");

    fireEvent.click(ticketButton("Buy"));

    expect(
      await screen.findByText(
        "PAPER BUY SUBMITTED (B-1) - risk warnings: Approaching daily loss limit. Current P&L: ₹-4500.00",
      ),
    ).toBeInTheDocument();
  });

  it("retries a dropped submit with the same client_order_id, then a new click gets a new one", async () => {
    orderNetworkFailures = 1;
    renderPage();
    await screen.findAllByText("State Bank");
    setValue(/^price$/i, "801.5");

    fireEvent.click(ticketButton("Buy"));

    await screen.findByText("PAPER BUY accepted (ord-1)");
    expect(clientOrderIds(calls)).toEqual([CLIENT_ORDER_IDS[0], CLIENT_ORDER_IDS[0]]);

    fireEvent.click(ticketButton("Buy"));

    await vi.waitFor(() => expect(orderBodies(calls)).toHaveLength(3));
    expect(clientOrderIds(calls)).toEqual([
      CLIENT_ORDER_IDS[0],
      CLIENT_ORDER_IDS[0],
      CLIENT_ORDER_IDS[1],
    ]);
  });

  it("retries a cut-off 200 response with the same client_order_id instead of reporting success", async () => {
    orderTruncatedResponses = 1;
    renderPage();
    await screen.findAllByText("State Bank");
    setValue(/^price$/i, "801.5");

    fireEvent.click(ticketButton("Buy"));

    await screen.findByText("PAPER BUY accepted (ord-1)");
    expect(clientOrderIds(calls)).toEqual([CLIENT_ORDER_IDS[0], CLIENT_ORDER_IDS[0]]);
  });
});

describe("terminal data", () => {
  let calls: Captured[];

  beforeEach(() => {
    paperPositions = [];
    livePositions = [];
    liveStale = false;
    squareResult = { status: "SUCCESS" };
    squareNetworkFailures = 0;
    calls = installFetch();
    stubClientOrderIds();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function squareBodies(): string[] {
    return calls.filter((call) => call.url.includes("/square")).map((call) => call.body);
  }

  function boardUrls(): string[] {
    return calls
      .filter((call) => call.url.includes("/api/terminal/options/board"))
      .map((call) => new URL(call.url).search);
  }

  it("selects the ?symbol= symbol and lists it after the saved watchlist", async () => {
    renderPage("/terminal?symbol=TCS");
    await screen.findAllByText("State Bank");

    const rows = screen.getAllByRole("button", { name: /on TradingView$/ });
    expect(rows.map((row) => row.getAttribute("aria-label"))).toEqual([
      "Open TCS on TradingView",
      "Open SBIN on TradingView",
      "Open TCS on TradingView",
    ]);
    expect(screen.getByText("Trade Symbol:").textContent).toBe("Trade Symbol: TCS");
  });

  it("replaces the ?symbol= selection with a watchlist pick", async () => {
    renderPage("/terminal?symbol=TCS");

    fireEvent.click(await screen.findByText("State Bank"));

    expect(screen.getByText("Trade Symbol:").textContent).toBe("Trade Symbol: SBIN");
    const rows = screen.getAllByRole("button", { name: /on TradingView$/ });
    expect(rows.map((row) => row.getAttribute("aria-label"))).toEqual([
      "Open SBIN on TradingView",
      "Open SBIN on TradingView",
    ]);
  });

  it("offers one Buy and one Sell button, the order ticket's", async () => {
    renderPage();
    await screen.findAllByText("State Bank");

    expect(screen.getAllByRole("button", { name: "Buy" })).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "Sell" })).toHaveLength(1);
  });

  it("shows the board's expiry until one is picked, then requests the picked expiry", async () => {
    renderPage();
    expect(await screen.findByText("PCR(OI): 2 | PCR(Vol): 0.5")).toBeInTheDocument();
    expect(screen.getByDisplayValue("2026-10-27")).toBeInTheDocument();
    expect(boardUrls()).toEqual(["?underlying=NIFTY&strike_count=15"]);

    fireEvent.change(screen.getByDisplayValue("2026-10-27"), { target: { value: "2026-11-03" } });

    await screen.findByDisplayValue("2026-11-03");
    await screen.findByText("NIFTY Spot: 25,010.50");
    expect(boardUrls().at(-1)).toBe("?underlying=NIFTY&strike_count=15&expiry=2026-11-03");
  });

  it("shows the watchlist day change as the backend's percent, not its absolute move", async () => {
    renderPage();
    await screen.findAllByText("State Bank");

    // The header has the same button; the watchlist row is the one inside a table row.
    const [, rowButton] = screen.getAllByRole("button", { name: "Open SBIN on TradingView" });
    const row = rowButton.closest("tr");
    expect(row?.textContent).toBe("SBINState Bank800.00+1.21%");
  });

  it("keeps a net P&L of exactly 0 instead of showing the unrealized amount", async () => {
    paperPositions = [{ ...OPEN_POSITION, realized_pnl: -100, net_pnl: 0 }];
    renderPage();
    await screen.findAllByText("State Bank");
    fireEvent.click(screen.getByRole("button", { name: /positions/i }));

    const square = await screen.findByRole("button", { name: "Square" });
    expect(square.closest("tr")?.textContent).toBe("SBINPAPERLONG10790.00800.00100.000.00Square");
    expect(screen.getByText(/Paper Net P&L/).textContent).toBe("Paper Net P&L: 0.00");
  });

  it("falls back to unrealized P&L only when net P&L is absent", async () => {
    const { net_pnl: _omitted, ...withoutNet } = OPEN_POSITION;
    paperPositions = [withoutNet];
    renderPage();
    await screen.findAllByText("State Bank");
    fireEvent.click(screen.getByRole("button", { name: /positions/i }));

    const square = await screen.findByRole("button", { name: "Square" });
    expect(square.closest("tr")?.textContent).toBe("SBINPAPERLONG10790.00800.00100.00100.00Square");
  });

  it("says the broker is unavailable even when no LIVE rows were ever synced", async () => {
    liveStale = true;
    renderPage();
    await screen.findAllByText("State Bank");
    fireEvent.click(screen.getByRole("button", { name: /positions/i }));

    expect(await screen.findByText(BROKER_UNAVAILABLE_NOTE)).toBeInTheDocument();
    expect(screen.getByText("No open positions")).toBeInTheDocument();
  });

  it("greys a stale LIVE position, blocks its square-off and says the broker is unavailable", async () => {
    liveStale = true;
    livePositions = [{ ...OPEN_POSITION, mode: "LIVE", stale: true }];
    paperPositions = [{ ...OPEN_POSITION, id: 8, symbol: "INFY" }];
    renderPage();
    await screen.findAllByText("State Bank");
    fireEvent.click(screen.getByRole("button", { name: /positions/i }));

    expect(await screen.findByText(BROKER_UNAVAILABLE_NOTE)).toBeInTheDocument();
    const [live, paper] = screen.getAllByRole("button", { name: "Square" });
    expect(live.closest("tr")?.textContent).toBe("SBINLIVELONG10790.00800.00100.00100.00Square");
    expect(live).toBeDisabled();
    expect(live.closest("tr")).toHaveClass("opacity-50");
    expect(paper.closest("tr")?.textContent).toBe("INFYPAPERLONG10790.00800.00100.00100.00Square");
    expect(paper).toBeEnabled();
    expect(paper.closest("tr")).not.toHaveClass("opacity-50");
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
    expect(squareBodies()).toEqual([
      '{"symbol":"SBIN","mode":"PAPER","product_type":"INTRADAY","client_order_id":"11111111-1111-4111-8111-111111111111"}',
    ]);
  });

  it("retries a dropped square-off with the same client_order_id, then a new click gets a new one", async () => {
    paperPositions = [OPEN_POSITION];
    squareNetworkFailures = 1;
    squareResult = { status: "ERROR", message: "Order rejected by broker" };
    renderPage();
    await screen.findAllByText("State Bank");
    fireEvent.click(screen.getByRole("button", { name: /positions/i }));

    fireEvent.click(await screen.findByRole("button", { name: "Square" }));

    expect(await screen.findByText("Order rejected by broker")).toBeInTheDocument();
    const ids = () => squareBodies().map((body) => JSON.parse(body).client_order_id);
    expect(ids()).toEqual([CLIENT_ORDER_IDS[0], CLIENT_ORDER_IDS[0]]);

    squareResult = { status: "SUCCESS" };
    fireEvent.click(screen.getByRole("button", { name: "Square" }));

    expect(await screen.findByText("PAPER square submitted for SBIN")).toBeInTheDocument();
    expect(ids()).toEqual([CLIENT_ORDER_IDS[0], CLIENT_ORDER_IDS[0], CLIENT_ORDER_IDS[1]]);
  });

  it("keeps a position whose square-off is unconfirmed and says so", async () => {
    paperPositions = [OPEN_POSITION];
    squareResult = { status: "SUBMIT_UNCONFIRMED", message: "Check the broker orderbook." };
    renderPage();
    await screen.findAllByText("State Bank");
    fireEvent.click(screen.getByRole("button", { name: /positions/i }));

    fireEvent.click(await screen.findByRole("button", { name: "Square" }));

    expect(
      await screen.findByText("PAPER square for SBIN unconfirmed: Check the broker orderbook."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Square" }).closest("tr")?.textContent).toBe(
      "SBINPAPERLONG10790.00800.00100.00100.00Square",
    );
  });

  it("keeps a position whose square-off the broker rejected and shows the reason", async () => {
    paperPositions = [OPEN_POSITION];
    squareResult = { status: "ERROR", message: "Order rejected by broker" };
    renderPage();
    await screen.findAllByText("State Bank");
    fireEvent.click(screen.getByRole("button", { name: /positions/i }));

    fireEvent.click(await screen.findByRole("button", { name: "Square" }));

    expect(await screen.findByText("Order rejected by broker")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Square" }).closest("tr")?.textContent).toBe(
      "SBINPAPERLONG10790.00800.00100.00100.00Square",
    );
  });
});

describe("terminal stale selections", () => {
  let calls: Captured[];

  beforeEach(() => {
    paperPositions = [];
    orderNetworkFailures = 0;
    orderTruncatedResponses = 0;
    orderResult = { status: "accepted", order_id: "ord-1" };
    boardHold = null;
    calls = installFetch();
    stubClientOrderIds();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  /** Moves the faked clock and its intervals on, inside act so React renders the tick. */
  function advanceClock(ms: number) {
    act(() => {
      vi.advanceTimersByTime(ms);
    });
  }

  function tradeSymbol(): string {
    return screen.getByText("Trade Symbol:").textContent ?? "";
  }

  function watchlistAdds(): Captured[] {
    return calls.filter((call) => call.url.includes(WATCHLIST_PATH) && call.method === "POST");
  }

  it("drops the picked option when the underlying changes, so the old contract cannot be sent", async () => {
    renderPage();
    await screen.findAllByText("State Bank");
    fireEvent.click(await screen.findByRole("button", { name: "NIFTY26OCT25000CE" }));
    expect(tradeSymbol()).toBe("Trade Symbol: NIFTY26OCT25000CE");

    fireEvent.change(screen.getByDisplayValue("NIFTY"), { target: { value: "BANKNIFTY" } });

    expect(tradeSymbol()).toBe("Trade Symbol: SBIN");
    await screen.findByRole("button", { name: "BANKNIFTY26OCT56000CE" });
    expect(screen.queryByRole("button", { name: "NIFTY26OCT25000CE" })).not.toBeInTheDocument();
    expect(tradeSymbol()).toBe("Trade Symbol: SBIN");

    setValue(/order type/i, "MARKET");
    fireEvent.click(ticketButton("Buy"));

    await screen.findByText("PAPER BUY accepted (ord-1)");
    expect(orderBodies(calls).map((body) => JSON.parse(body).symbol)).toEqual(["SBIN"]);
  });

  it("drops the picked option when the expiry changes, even if the new chain lists it", async () => {
    renderPage();
    await screen.findAllByText("State Bank");
    fireEvent.click(await screen.findByRole("button", { name: "NIFTY26OCT25000CE" }));

    fireEvent.change(screen.getByDisplayValue("2026-10-27"), { target: { value: "2026-11-03" } });

    await screen.findByDisplayValue("2026-11-03");
    await screen.findByRole("button", { name: "NIFTY26OCT25000CE" });
    expect(tradeSymbol()).toBe("Trade Symbol: SBIN");
  });

  it("returning to a chain shows no cached board and does not revive the old pick", async () => {
    renderPage();
    await screen.findAllByText("State Bank");
    fireEvent.click(await screen.findByRole("button", { name: "NIFTY26OCT25000CE" }));
    fireEvent.change(screen.getByDisplayValue("NIFTY"), { target: { value: "BANKNIFTY" } });
    await screen.findByRole("button", { name: "BANKNIFTY26OCT56000CE" });

    let release = () => {};
    boardHold = new Promise((resolve) => {
      release = resolve;
    });
    fireEvent.change(screen.getByDisplayValue("BANKNIFTY"), { target: { value: "NIFTY" } });

    // Until the fresh NIFTY board answers, none of its contracts can be picked.
    expect(await screen.findByText("Loading options board...")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "NIFTY26OCT25000CE" })).not.toBeInTheDocument();
    expect(tradeSymbol()).toBe("Trade Symbol: SBIN");

    release();

    await screen.findByRole("button", { name: "NIFTY26OCT25000CE" });
    expect(tradeSymbol()).toBe("Trade Symbol: SBIN");
  });

  it("starts streaming quotes at the open and stops at the close with no symbol change", async () => {
    vi.useFakeTimers({ toFake: ["Date", "setInterval", "clearInterval"] });
    // Monday 19 Oct 2026, 09:14:59.5 IST: half a second before the open.
    vi.setSystemTime(new Date("2026-10-19T03:44:59.500Z"));
    renderPage();
    await screen.findAllByText("State Bank");
    expect(quoteSubscriptions.at(-1)).toEqual([]);

    advanceClock(1000);
    expect(quoteSubscriptions.at(-1)).toEqual(["SBIN"]);

    // 15:29:59.5 IST, still in session; the next tick, at 15:30:00.5, is past the close.
    vi.setSystemTime(new Date("2026-10-19T09:59:59.500Z"));
    advanceClock(0);
    expect(quoteSubscriptions.at(-1)).toEqual(["SBIN"]);
    advanceClock(1000);
    expect(quoteSubscriptions.at(-1)).toEqual([]);
  });

  it("never offers a search result for an earlier query", async () => {
    renderPage();
    await screen.findAllByText("State Bank");
    const box = screen.getByPlaceholderText("Search...");
    const add = screen.getByRole("button", { name: "Add symbol to watchlist" });
    fireEvent.change(box, { target: { value: "SB" } });
    await screen.findByRole("button", { name: /^SBIN\s?State Bank$/ });

    fireEvent.change(box, { target: { value: "INF" } });

    // The SB results are not for "INF": they must not be clickable, nor picked by Enter or +.
    expect(screen.queryByRole("button", { name: /^SBIN\s?State Bank$/ })).not.toBeInTheDocument();
    expect(screen.getByText("Searching...")).toBeInTheDocument();
    expect(add).toBeDisabled();
    fireEvent.keyDown(box, { key: "Enter" });
    expect(watchlistAdds()).toEqual([]);

    await screen.findByRole("button", { name: /^INFY\s?Infosys$/ });
    expect(add).toBeEnabled();
    expect(watchlistAdds()).toEqual([]);
  });

  it("blocks a fractional quantity with an inline error, then sends a whole one", async () => {
    renderPage();
    await screen.findAllByText("State Bank");
    setValue(/^price$/i, "800");
    setValue(/^quantity$/i, "2.5");

    expect(screen.getByText("Quantity must be a whole number greater than 0")).toBeInTheDocument();
    expect(ticketButton("Buy")).toBeDisabled();
    expect(ticketButton("Sell")).toBeDisabled();
    fireEvent.click(ticketButton("Buy"));
    expect(orderBodies(calls)).toEqual([]);

    setValue(/^quantity$/i, "2");
    expect(
      screen.queryByText("Quantity must be a whole number greater than 0"),
    ).not.toBeInTheDocument();
    fireEvent.click(ticketButton("Buy"));

    await screen.findByText("PAPER BUY accepted (ord-1)");
    expect(orderBodies(calls).map((body) => JSON.parse(body).quantity)).toEqual([2]);
  });

  it("blocks a fractional number of option lots", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "NIFTY26OCT25000CE" }));
    setValue(/order type/i, "MARKET");
    setValue(/^lots$/i, "1.5");

    expect(screen.getByText("Lots must be a whole number (lot size 75)")).toBeInTheDocument();
    expect(ticketButton("Buy")).toBeDisabled();
    fireEvent.click(ticketButton("Buy"));
    expect(orderBodies(calls)).toEqual([]);
  });
});
