import { describe, expect, it } from "vite-plus/test";
import type { PositionItem, WatchlistItem } from "@/lib/queries/terminal";
import type { Quote } from "@/lib/realtime/quote-store";
import {
  atLeastOne,
  currentRow,
  getChangeText,
  getChangeTone,
  listWithSelected,
  optionChainKey,
  screenSymbols,
  searchView,
  shownOptionSymbol,
  selectedSymbolFor,
  squareMessage,
  summarizePositions,
  ticketSizing,
  tradeSymbolFor,
  watchlistInstrumentType,
  withQuote,
  withQuotePnl,
  zeroIfBlank,
  type TerminalView,
  type TicketSizing,
} from "../terminal-view";

function item(symbol: string, overrides: Partial<WatchlistItem> = {}): WatchlistItem {
  return { symbol, name: `${symbol} Ltd`, price: 100, changePct: 1.5, ltp: 100, ...overrides };
}

function position(overrides: Partial<PositionItem> = {}): PositionItem {
  return {
    id: "1",
    symbol: "SBIN",
    net_qty: 10,
    entry_price: 800,
    current_price: 810,
    unrealized_pnl: 100,
    realized_pnl: 0,
    net_pnl: 100,
    product_type: "INTRADAY",
    mode: "PAPER",
    stale: false,
    ...overrides,
  };
}

function quote(overrides: Partial<Quote> = {}): Quote {
  return { ltp: 812.345, changePct: 2.345, receivedAt: 0, ...overrides };
}

describe("ticketSizing", () => {
  const cases: [
    string,
    { view: TerminalView; symbol: string; orderQty: number; optionLots: number },
    TicketSizing,
  ][] = [
    [
      "equity uses the quantity input",
      { view: "options", symbol: "SBIN", orderQty: 5, optionLots: 3 },
      { lotsMode: false, lotSize: null, lots: null, quantity: 5 },
    ],
    [
      "a quantity below 1 becomes 1",
      { view: "options", symbol: "SBIN", orderQty: 0, optionLots: 1 },
      { lotsMode: false, lotSize: null, lots: null, quantity: 1 },
    ],
    [
      "a NIFTY option is sized in lots of 75",
      { view: "options", symbol: "NIFTY26OCT25000CE", orderQty: 1, optionLots: 2 },
      { lotsMode: true, lotSize: 75, lots: { count: 2, size: 75 }, quantity: 150 },
    ],
    [
      "a BANKNIFTY option uses its own lot size",
      { view: "options", symbol: "NSE:BANKNIFTY26OCT52000PE", orderQty: 1, optionLots: 0 },
      { lotsMode: true, lotSize: 30, lots: { count: 1, size: 30 }, quantity: 30 },
    ],
    [
      "an unknown underlying leaves no quantity",
      { view: "options", symbol: "MIDCPNIFTY26OCT12000CE", orderQty: 4, optionLots: 2 },
      { lotsMode: true, lotSize: null, lots: null, quantity: null },
    ],
    [
      "the positions view sizes an option by quantity",
      { view: "positions", symbol: "NIFTY26OCT25000CE", orderQty: 3, optionLots: 2 },
      { lotsMode: false, lotSize: null, lots: null, quantity: 3 },
    ],
  ];
  it.each(cases)("%s", (_name, input, expected) => {
    expect(ticketSizing(input)).toEqual(expected);
  });
});

describe("change display", () => {
  it.each([
    [1.5, "text-profit", "+1.50%"],
    [0, "text-profit", "+0.00%"],
    [-2.25, "text-loss", "-2.25%"],
    [null, "text-foreground-muted", "Unavailable"],
    [undefined, "text-foreground-muted", "Unavailable"],
  ])("%s -> %s, %s", (change, tone, text) => {
    expect(getChangeTone(change)).toBe(tone);
    expect(getChangeText(change)).toBe(text);
  });
});

describe("watchlistInstrumentType", () => {
  it.each([
    [{ symbol: "X", name: "X", instrument_type: "FUT", type: "PE" }, "FUT"],
    [{ symbol: "X", name: "X", type: "PE" }, "PE"],
    [{ symbol: "X", name: "X", type: "CE" }, "CE"],
    [{ symbol: "X", name: "X", type: "INDEX" }, "EQ"],
    [{ symbol: "X", name: "X" }, "EQ"],
  ])("%o -> %s", (input, expected) => {
    expect(watchlistInstrumentType(input)).toBe(expected);
  });
});

describe("withQuote", () => {
  it("returns the row unchanged without a quote", () => {
    const row = item("SBIN");
    expect(withQuote(row, undefined)).toBe(row);
  });

  it("takes the quote's LTP and day change, rounded to 2 decimals", () => {
    expect(withQuote(item("SBIN"), quote())).toEqual({
      symbol: "SBIN",
      name: "SBIN Ltd",
      price: 812.35,
      changePct: 2.35,
      ltp: 812.35,
    });
  });

  it("keeps the row's values for fields the quote lacks", () => {
    const row = item("SBIN", { ltp: 99, price: 98, changePct: -1 });
    expect(withQuote(row, quote({ ltp: undefined, changePct: undefined }))).toEqual(row);
  });
});

describe("withQuotePnl", () => {
  it("returns the position unchanged without a quote LTP", () => {
    const pos = position();
    expect(withQuotePnl(pos, undefined)).toBe(pos);
  });

  it("reprices unrealized and net P&L at the quote", () => {
    const pos = position({ net_qty: -10, entry_price: 800, realized_pnl: 25.5 });
    expect(withQuotePnl(pos, quote({ ltp: 790.004 }))).toEqual({
      ...pos,
      current_price: 790,
      unrealized_pnl: 100,
      net_pnl: 125.5,
    });
  });
});

describe("summarizePositions", () => {
  it("sums net P&L by mode, counting a missing mode as LIVE", () => {
    expect(
      summarizePositions([
        position({ mode: "PAPER", net_pnl: 10.1 }),
        position({ mode: "PAPER", net_pnl: 0.2 }),
        position({ mode: "LIVE", net_pnl: -50 }),
        position({ mode: undefined, net_pnl: 20 }),
      ]),
    ).toEqual({ net_pnl_live: -30, net_pnl_paper: 10.3, net_pnl_total: -19.7 });
  });

  it("is zero with no positions", () => {
    expect(summarizePositions([])).toEqual({
      net_pnl_live: 0,
      net_pnl_paper: 0,
      net_pnl_total: 0,
    });
  });
});

describe("symbol selection", () => {
  const saved = [item("SBIN"), item("TCS")];

  it.each([
    ["", "SBIN"],
    ["INFY", "INFY"],
    ["TCS", "TCS"],
  ])("?symbol=%s selects %s", (url, expected) => {
    expect(selectedSymbolFor(url, saved)).toBe(expected);
  });

  it("selects nothing with no URL symbol and no saved rows", () => {
    expect(selectedSymbolFor("", [])).toBe("");
  });

  it("lists an unsaved selection after the saved rows", () => {
    expect(listWithSelected(saved, "INFY").map((row) => row.symbol)).toEqual([
      "SBIN",
      "TCS",
      "INFY",
    ]);
    expect(listWithSelected(saved, "INFY").at(-1)).toEqual({
      symbol: "INFY",
      name: "INFY",
      price: null,
      changePct: null,
      ltp: null,
    });
  });

  it.each([["TCS"], [""]])("keeps the saved list as is for %o", (selected) => {
    expect(listWithSelected(saved, selected)).toBe(saved);
  });

  it.each([
    ["TCS", "TCS"],
    ["MISSING", "SBIN"],
  ])("current row for %s is %s", (selected, expected) => {
    expect(currentRow(saved, selected)?.symbol).toBe(expected);
  });

  it("has no current row with no rows", () => {
    expect(currentRow([], "SBIN")).toBeNull();
  });
});

describe("tradeSymbolFor", () => {
  it.each([
    ["options" as const, "NIFTY26OCT25000CE", "NIFTY26OCT25000CE"],
    ["options" as const, "", "SBIN"],
    ["positions" as const, "NIFTY26OCT25000CE", "SBIN"],
  ])("%s view with option %o trades %s", (view, option, expected) => {
    expect(tradeSymbolFor(view, option, item("SBIN"))).toBe(expected);
  });

  it("trades nothing with no option and no current row", () => {
    expect(tradeSymbolFor("options", "", null)).toBe("");
  });
});

describe("screenSymbols", () => {
  it("dedupes and drops empty symbols, keeping first-seen order", () => {
    expect(
      screenSymbols([
        ["SBIN", "TCS"],
        ["SBIN", ""],
        ["INFY", "TCS"],
      ]),
    ).toEqual(["SBIN", "TCS", "INFY"]);
  });
});

describe("number inputs", () => {
  it.each([
    ["5", 5],
    ["0", 1],
    ["-3", 1],
    ["", 1],
    ["abc", 1],
  ])("atLeastOne(%o) is %s", (raw, expected) => {
    expect(atLeastOne(raw)).toBe(expected);
  });

  it.each([
    ["12.5", 12.5],
    ["", 0],
    ["abc", 0],
  ])("zeroIfBlank(%o) is %s", (raw, expected) => {
    expect(zeroIfBlank(raw)).toBe(expected);
  });
});

describe("squareMessage", () => {
  it.each([
    [{ mode: "LIVE" as const, symbol: "SBIN" }, false, "LIVE square submitted for SBIN"],
    [{ mode: undefined, symbol: "TCS" }, false, "PAPER square submitted for TCS"],
    [
      { mode: "LIVE" as const, symbol: "SBIN" },
      true,
      "LIVE square for SBIN unconfirmed: broker timeout",
    ],
  ])("%o unconfirmed=%s", (row, unconfirmed, expected) => {
    expect(squareMessage(row, { unconfirmed, message: "broker timeout" })).toBe(expected);
  });
});

describe("shownOptionSymbol", () => {
  const leg = (symbol: string) => ({ symbol, ltp: 1, oi: 1, volume: 1, change_pct: 0 });
  const niftyBoard = {
    strikes: [{ strike: 25000, ce: leg("NIFTY26OCT25000CE"), pe: leg("NIFTY26OCT25000PE") }],
  };
  const nifty = optionChainKey("NIFTY", "");

  it.each([
    [
      "a CE on the shown chain",
      { chain: nifty, symbol: "NIFTY26OCT25000CE" },
      nifty,
      "NIFTY26OCT25000CE",
    ],
    [
      "a PE on the shown chain",
      { chain: nifty, symbol: "NIFTY26OCT25000PE" },
      nifty,
      "NIFTY26OCT25000PE",
    ],
    ["no pick", null, nifty, ""],
    [
      "a pick from before the underlying changed",
      { chain: nifty, symbol: "NIFTY26OCT25000CE" },
      optionChainKey("BANKNIFTY", ""),
      "",
    ],
    [
      "a pick from before the expiry changed",
      { chain: nifty, symbol: "NIFTY26OCT25000CE" },
      optionChainKey("NIFTY", "2026-11-03"),
      "",
    ],
    ["a contract no longer on the board", { chain: nifty, symbol: "NIFTY26OCT24000CE" }, nifty, ""],
  ])("%s", (_name, pick, chain, expected) => {
    expect(shownOptionSymbol(pick, chain, niftyBoard)).toBe(expected);
  });

  it("trades nothing while no board is shown", () => {
    expect(shownOptionSymbol({ chain: nifty, symbol: "NIFTY26OCT25000CE" }, nifty, null)).toBe("");
  });
});

describe("searchView", () => {
  const sbin = [{ symbol: "SBIN", name: "State Bank" }];
  const settled = { isPlaceholderData: false, isFetching: false };

  it.each([
    [
      "results for exactly the typed text",
      { text: " SB ", searchTerm: "SB", data: sbin, ...settled },
      { active: true, results: sbin, loading: false },
    ],
    [
      "nothing while the debounce has not caught up",
      { text: "INF", searchTerm: "SB", data: sbin, ...settled },
      { active: true, results: [], loading: true },
    ],
    [
      "nothing while the previous query's results stand in",
      { text: "INF", searchTerm: "INF", data: sbin, isPlaceholderData: true, isFetching: true },
      { active: true, results: [], loading: true },
    ],
    [
      "nothing under two characters",
      { text: "S", searchTerm: "SB", data: sbin, ...settled },
      { active: false, results: [], loading: false },
    ],
  ])("%s", (_name, input, expected) => {
    expect(searchView(input)).toEqual(expected);
  });
});
