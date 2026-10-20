import { QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vite-plus/test";
import { createQueryClient } from "@/lib/queries/query-client";
import type { QuoteStore } from "@/lib/realtime/quote-store";
import { FakeSocket } from "@/lib/realtime/__tests__/fake-socket";

// A fresh quote store per test, so quotes streamed in one test never leak into the next.
const holder = vi.hoisted(() => ({ store: null as QuoteStore | null }));
vi.mock("@/lib/realtime/quote-store", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/realtime/quote-store")>();
  return {
    ...actual,
    get quoteStore() {
      holder.store ??= actual.createQuoteStore();
      return holder.store;
    },
  };
});

// The client reads VITE_API_URL at import time; Node cannot build a Request from a relative URL.
vi.stubEnv("VITE_API_URL", "http://api.test");
const { default: ScreenerPage } = await import("../page");

type Row = { symbol: string; name: string; price: number; change: number; volume: number };

const ROWS: Row[] = [
  { symbol: "INFY", name: "Infosys", price: 1500, change: 0.2, volume: 3000 },
  { symbol: "SBIN", name: "State Bank", price: 800, change: 1, volume: 5000 },
  { symbol: "TCS", name: "Tata Consultancy", price: 4000, change: -0.5, volume: 1000 },
];

/** The screener endpoints, sorting and filtering on the server side as the backend does. */
function stubBackend(liveQuotes: Record<string, unknown> = {}) {
  const fetchMock = vi.fn((input: RequestInfo | URL): Promise<Response> => {
    const url = new URL(input instanceof Request ? input.url : String(input));
    if (url.pathname === "/api/screener/indices") {
      return Promise.resolve(
        Response.json({ indices: [{ id: "NIFTY50", name: "Nifty 50", count: 3 }] }),
      );
    }
    if (url.pathname === "/api/screener/results") {
      const field = (url.searchParams.get("sort_by") ?? "symbol") as keyof Row;
      const direction = url.searchParams.get("sort_order") === "desc" ? -1 : 1;
      const query = url.searchParams.get("query") ?? "";
      const results = ROWS.filter((row) => row.symbol.includes(query)).sort((a, b) => {
        const left = a[field];
        const right = b[field];
        if (typeof left === "number" && typeof right === "number") {
          return (left - right) * direction;
        }
        return String(left).localeCompare(String(right)) * direction;
      });
      return Promise.resolve(Response.json({ results, total: results.length }));
    }
    if (url.pathname === "/api/market/quotes/live") {
      return Promise.resolve(Response.json(liveQuotes));
    }
    return Promise.resolve(Response.json({}));
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function renderScreener() {
  const client = createQueryClient();
  client.setDefaultOptions({ queries: { retry: false } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <ScreenerPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function shownSymbols(): string[] {
  return screen
    .getAllByRole("button", { name: /^Open .+ on TradingView$/ })
    .map((button) => button.getAttribute("aria-label")?.split(" ")[1] ?? "");
}

function rowOf(symbol: string): HTMLElement {
  const row = screen.getByText(symbol, { selector: "div.font-medium" }).closest("tr");
  if (!row) throw new Error(`row ${symbol} missing`);
  return row;
}

function socket(): FakeSocket {
  const [only] = FakeSocket.instances;
  if (!only) throw new Error("no socket opened");
  return only;
}

async function connectedScreener() {
  const view = renderScreener();
  await screen.findByText("SBIN");
  act(() => socket().open());
  await waitFor(() =>
    expect(socket().actions()).toContainEqual({
      action: "subscribe",
      symbols: ["INFY", "SBIN", "TCS"],
    }),
  );
  return view;
}

beforeEach(() => {
  FakeSocket.instances = [];
  holder.store = null;
  vi.stubGlobal("WebSocket", FakeSocket);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("screener live overlay", () => {
  it("lays streamed ticks over the rows and flashes the direction of each change", async () => {
    stubBackend();
    await connectedScreener();
    expect(screen.getByText("LIVE")).toBeInTheDocument();

    act(() =>
      socket().receive({
        type: "ticker_batch",
        data: [{ symbol: "SBIN", lp: 812.345, chp: 2.5, vol_traded_today: 7500 }],
      }),
    );

    const sbin = rowOf("SBIN");
    expect(within(sbin).getByText("812.35")).toHaveClass("screener-flash-up-a");
    expect(within(sbin).getByText("+2.50%")).toHaveClass("screener-flash-up-a");
    expect(within(sbin).getByText("7.5K")).toBeInTheDocument();
    expect(within(rowOf("TCS")).getByText("4000.00")).not.toHaveClass("screener-flash-up-a");

    act(() => socket().receive({ type: "ticker", data: { symbol: "SBIN", ltp: 810 } }));

    expect(within(rowOf("SBIN")).getByText("810.00")).toHaveClass("screener-flash-down-b");
    expect(within(rowOf("SBIN")).getByText("+2.50%")).toHaveClass("screener-flash-up-a");
  });

  it("keeps the server's sort order while ticks change the sorted column", async () => {
    stubBackend();
    await connectedScreener();
    expect(shownSymbols()).toEqual(["INFY", "SBIN", "TCS"]);

    fireEvent.click(screen.getByText("Change"));
    await waitFor(() => expect(shownSymbols()).toEqual(["SBIN", "INFY", "TCS"]));

    act(() => socket().receive({ type: "ticker", data: { symbol: "TCS", ltp: 4100, chp: 5 } }));

    expect(within(rowOf("TCS")).getByText("+5.00%")).toBeInTheDocument();
    expect(shownSymbols()).toEqual(["SBIN", "INFY", "TCS"]);
  });

  it("unsubscribes rows that are filtered out and everything on unmount", async () => {
    stubBackend();
    const view = await connectedScreener();

    fireEvent.change(screen.getByPlaceholderText("Search symbol..."), {
      target: { value: "TCS" },
    });
    await waitFor(() => expect(shownSymbols()).toEqual(["TCS"]));
    await waitFor(() =>
      expect(socket().actions().at(-1)).toEqual({
        action: "unsubscribe",
        symbols: ["INFY", "SBIN"],
      }),
    );

    view.unmount();
    await waitFor(() => expect(socket().readyState).toBe(FakeSocket.CLOSED));
    expect(socket().actions().at(-1)).toEqual({ action: "unsubscribe", symbols: ["TCS"] });
  });

  it("polls REST quotes for the rows while the socket is down", async () => {
    const fetchMock = stubBackend({
      SBIN: { ltp: 820, change_pct: 3, volume: 9000 },
      TCS: { ltp: 3990, change: -10, change_pct: 0, source: "database" },
    });
    renderScreener();

    const sbin = (await screen.findByText("820.00")).closest("tr");
    if (!sbin) throw new Error("SBIN row missing");
    expect(within(sbin).getByText("+3.00%")).toBeInTheDocument();
    // A database fallback entry gives a price but no percent: the row keeps its own.
    expect(within(rowOf("TCS")).getByText("3990.00")).toBeInTheDocument();
    expect(within(rowOf("TCS")).getByText("-0.50%")).toBeInTheDocument();
    expect(screen.queryByText("LIVE")).not.toBeInTheDocument();

    const polled = fetchMock.mock.calls
      .map(([input]) => new URL(input instanceof Request ? input.url : String(input)))
      .find((url) => url.pathname === "/api/market/quotes/live");
    expect(polled?.searchParams.get("symbols")).toBe("INFY,SBIN,TCS");
  });
});
