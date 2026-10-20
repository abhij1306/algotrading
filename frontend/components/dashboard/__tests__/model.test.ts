import { describe, expect, it } from "vite-plus/test";
import type { DashboardWatchlistItem, MarketIndex } from "@/lib/queries/dashboard";
import {
  applyIndexQuotes,
  applyWatchlistQuotes,
  getCompactIndexLabel,
  getMarketSectionMeta,
  getStatusBadge,
  terminalPath,
} from "../model";

const NIFTY: MarketIndex = {
  name: "NIFTY 50",
  symbol: "NIFTY50",
  value: 22450,
  change: 112.25,
  changePercent: 0.5,
  type: "indian",
  source: "api",
};

const SBIN: DashboardWatchlistItem = {
  symbol: "SBIN",
  price: 812.5,
  change: 5.5,
  changePercent: 0.68,
};

describe("getStatusBadge", () => {
  it.each([
    [true, true, "LIVE"],
    [true, false, "DELAYED"],
    [false, true, "CLOSED"],
    [false, false, "CLOSED"],
  ])("market open %s, connected %s reads %s", (marketOpen, isConnected, label) => {
    expect(getStatusBadge({ marketOpen, isConnected }).label).toBe(label);
  });
});

describe("getMarketSectionMeta", () => {
  it.each([
    [true, true, { title: "Indian Markets", subtitle: "NSE Real-time" }],
    [true, false, { title: "Indian Markets", subtitle: "NSE Delayed (Disconnected)" }],
    [false, true, { title: "Global Markets", subtitle: "Yahoo Delayed" }],
  ])("market open %s, connected %s", (marketOpen, isConnected, expected) => {
    expect(getMarketSectionMeta({ marketOpen, isConnected })).toEqual(expected);
  });
});

describe("getCompactIndexLabel", () => {
  it.each([
    ["NIFTY50", "NIFTY"],
    ["BANKNIFTY", "BANK"],
    ["NIFTYIT", "IT"],
  ])("%s is %s", (symbol, label) => {
    expect(getCompactIndexLabel(symbol)).toBe(label);
  });
});

describe("applyIndexQuotes", () => {
  it("lays a rounded live quote over the row and marks it streaming", () => {
    const [row] = applyIndexQuotes([NIFTY], { NIFTY50: { ltp: 22500.126, changePct: 0.7349 } });
    expect(row).toEqual({ ...NIFTY, value: 22500.13, changePercent: 0.73, source: "websocket" });
  });

  it("keeps the fetched value for a field the quote lacks", () => {
    const [row] = applyIndexQuotes([NIFTY], { NIFTY50: { changePct: -0.2 } });
    expect(row).toEqual({ ...NIFTY, changePercent: -0.2, source: "websocket" });
  });

  it("leaves an unquoted row as fetched", () => {
    expect(applyIndexQuotes([NIFTY], {})).toEqual([NIFTY]);
  });
});

describe("applyWatchlistQuotes", () => {
  it("lays the live last price and day percent over the row", () => {
    expect(applyWatchlistQuotes([SBIN], { SBIN: { ltp: 815.004, changePct: 1.005 } })).toEqual([
      { symbol: "SBIN", price: 815, change: 5.5, changePercent: 1.01 },
    ]);
  });

  it("leaves an unquoted row as fetched", () => {
    expect(applyWatchlistQuotes([SBIN], { TCS: { ltp: 3900 } })).toEqual([SBIN]);
  });
});

describe("terminalPath", () => {
  it.each([
    ["SBIN", "/terminal?symbol=SBIN"],
    ["M&M", "/terminal?symbol=M%26M"],
    ["NSE:BAJAJ-AUTO-EQ", "/terminal?symbol=NSE%3ABAJAJ-AUTO-EQ"],
  ])("%s opens %s", (symbol, path) => {
    expect(terminalPath(symbol)).toBe(path);
  });
});
