import { describe, expect, it } from "vite-plus/test";
import type { ScreenerIndex, ScreenerRow } from "@/lib/queries/screener";
import type { Quote } from "@/lib/realtime/quote-store";
import {
  formatRowCells,
  getEmptyStateMessage,
  getFlashClassName,
  getMacdClassName,
  getRsiClassName,
  isStale,
  newestFirst,
  nextFlash,
  nextSort,
  pageRange,
  resolveSelectedUniverse,
  rowQuotes,
  sortIndicesForUi,
  symbolsToPoll,
  toNumber,
  toSafeString,
  totalPagesFor,
  withLiveQuote,
  type FlashState,
  type SortField,
  type SortState,
  type StreamState,
} from "../screener-view";

function row(overrides: Partial<ScreenerRow> = {}): ScreenerRow {
  return {
    symbol: "SBIN",
    name: "State Bank",
    sector: "Banks",
    marketCap: 720_000,
    change: 1.25,
    price: 812.5,
    volume: 1_234_567,
    rsi: 55.55,
    macd: 1.234,
    adx: 20,
    ...overrides,
  };
}

function index(id: string, name: string): ScreenerIndex {
  return { id, name, count: 50 };
}

const ASC = (field: SortField): SortState => ({ field, direction: "asc" });
const DESC = (field: SortField): SortState => ({ field, direction: "desc" });

describe("nextSort", () => {
  it.each<[SortState, SortField, SortState]>([
    // A new column: the symbol starts ascending, every number descending.
    [ASC("price"), "symbol", ASC("symbol")],
    [ASC("symbol"), "price", DESC("price")],
    [ASC("symbol"), "change", DESC("change")],
    [ASC("symbol"), "volume", DESC("volume")],
    [ASC("symbol"), "marketCap", DESC("marketCap")],
    [ASC("symbol"), "rsi", DESC("rsi")],
    [ASC("symbol"), "macd", DESC("macd")],
    [ASC("symbol"), "adx", DESC("adx")],
    // The same column flips, whichever way it started.
    [ASC("symbol"), "symbol", DESC("symbol")],
    [DESC("symbol"), "symbol", ASC("symbol")],
    [DESC("price"), "price", ASC("price")],
    [ASC("macd"), "macd", DESC("macd")],
    // Leaving a column ascending does not carry its direction over.
    [ASC("rsi"), "volume", DESC("volume")],
    [DESC("volume"), "symbol", ASC("symbol")],
  ])("from %o, clicking %s gives %o", (current, field, expected) => {
    expect(nextSort(current, field)).toEqual(expected);
  });
});

describe("sortIndicesForUi", () => {
  it.each<[string, ScreenerIndex[], string[]]>([
    ["empty", [], []],
    [
      "NIFTY50 first, the rest by name",
      [index("BANK", "Nifty Bank"), index("NIFTY50", "Nifty 50"), index("AUTO", "Nifty Auto")],
      ["NIFTY50", "AUTO", "BANK"],
    ],
    [
      "NIFTY50 first even when its name sorts last",
      [index("A", "Alpha"), index("NIFTY50", "Zeta")],
      ["NIFTY50", "A"],
    ],
    [
      "equal names keep their order",
      [index("X", "Same"), index("Y", "Same"), index("W", "Earlier")],
      ["W", "X", "Y"],
    ],
    ["no NIFTY50", [index("B", "Beta"), index("A", "Alpha")], ["A", "B"]],
  ])("%s", (_name, input, expected) => {
    expect(sortIndicesForUi(input).map((entry) => entry.id)).toEqual(expected);
  });

  it("leaves its input in place", () => {
    const input = [index("B", "Beta"), index("A", "Alpha")];
    sortIndicesForUi(input);
    expect(input.map((entry) => entry.id)).toEqual(["B", "A"]);
  });
});

describe("resolveSelectedUniverse", () => {
  const offered = [index("NIFTY50", "Nifty 50"), index("BANK", "Nifty Bank")];
  it.each<[string, ScreenerIndex[], string]>([
    ["NIFTY50", [], ""],
    ["BANK", offered, "BANK"],
    ["GONE", offered, "NIFTY50"],
    ["", offered, "NIFTY50"],
  ])("%s among %j gives %s", (choice, indices, expected) => {
    expect(resolveSelectedUniverse(choice, indices)).toBe(expected);
  });
});

const NOW = 100_000;

function quote(receivedAt: number | undefined, fields: Partial<Quote> = {}): Quote {
  return { receivedAt, ...fields };
}

describe("isStale", () => {
  it.each<[string, Quote | undefined, boolean]>([
    ["missing", undefined, true],
    ["never stamped", quote(undefined), true],
    ["fresh", quote(NOW - 1000), false],
    ["exactly 5s old", quote(NOW - 5000), false],
    ["just over 5s old", quote(NOW - 5001), true],
  ])("%s", (_name, input, expected) => {
    expect(isStale(input, NOW)).toBe(expected);
  });
});

describe("newestFirst", () => {
  const older = quote(1);
  const newer = quote(2);
  const tieA = quote(5, { ltp: 1 });
  const tieB = quote(5, { ltp: 2 });
  const unstamped = quote(undefined);
  it.each<[string, Quote | undefined, Quote | undefined, [Quote | undefined, Quote | undefined]]>([
    ["newer first", newer, older, [newer, older]],
    ["newer second", older, newer, [newer, older]],
    ["a tie keeps the order", tieA, tieB, [tieA, tieB]],
    ["both missing", undefined, undefined, [undefined, undefined]],
    ["one missing", undefined, older, [older, undefined]],
    ["unstamped loses to stamped", unstamped, older, [older, unstamped]],
  ])("%s", (_name, a, b, expected) => {
    expect(newestFirst(a, b)).toEqual(expected);
  });
});

describe("symbolsToPoll", () => {
  const symbols = ["FRESH", "STALE", "NONE"];
  const streamed = { FRESH: quote(NOW - 100), STALE: quote(NOW - 6000) };
  it.each<[string, StreamState, string[]]>([
    ["disconnected polls everything", { connected: false, aging: false, now: NOW }, symbols],
    [
      "connected after hours polls only unstreamed",
      { connected: true, aging: false, now: NOW },
      ["NONE"],
    ],
    [
      "connected in market hours polls stale and unstreamed",
      { connected: true, aging: true, now: NOW },
      ["STALE", "NONE"],
    ],
    [
      "in market hours a later clock ages the fresh quote out too",
      { connected: true, aging: true, now: NOW + 10_000 },
      symbols,
    ],
  ])("%s", (_name, stream, expected) => {
    expect(symbolsToPoll(symbols, streamed, stream)).toEqual(expected);
  });

  it("polls nothing for no rows", () => {
    expect(symbolsToPoll([], {}, { connected: false, aging: false, now: NOW })).toEqual([]);
  });
});

describe("rowQuotes", () => {
  const streamedOld = quote(1, { ltp: 10 });
  const polledNew = quote(2, { ltp: 20 });
  it.each<[string, StreamState, [Quote | undefined, Quote | undefined]]>([
    [
      "disconnected: poll wins",
      { connected: false, aging: false, now: NOW },
      [polledNew, streamedOld],
    ],
    [
      "connected: stream wins",
      { connected: true, aging: false, now: NOW },
      [streamedOld, polledNew],
    ],
    [
      "market hours: newer wins",
      { connected: true, aging: true, now: NOW },
      [polledNew, streamedOld],
    ],
  ])("%s", (_name, stream, expected) => {
    expect(rowQuotes(streamedOld, polledNew, stream)).toEqual(expected);
  });
});

describe("withLiveQuote", () => {
  it.each<
    [string, Quote | undefined, Quote | undefined, Pick<ScreenerRow, "price" | "change" | "volume">]
  >([
    [
      "no quotes keeps the row",
      undefined,
      undefined,
      { price: 812.5, change: 1.25, volume: 1_234_567 },
    ],
    [
      "first wins per field",
      quote(1, { ltp: 820.456, changePct: 2.005, volume: 10 }),
      quote(2, { ltp: 1, changePct: 1, volume: 1 }),
      { price: 820.46, change: 2.01, volume: 10 },
    ],
    [
      "second fills what first lacks",
      quote(1, { ltp: 830 }),
      quote(2, { changePct: -0.5, volume: 99 }),
      { price: 830, change: -0.5, volume: 99 },
    ],
    [
      "row fills what both lack",
      quote(1, { volume: 5 }),
      undefined,
      { price: 812.5, change: 1.25, volume: 5 },
    ],
  ])("%s", (_name, first, second, expected) => {
    const merged = withLiveQuote(row(), first, second);
    expect({ price: merged.price, change: merged.change, volume: merged.volume }).toEqual(expected);
    expect(merged.symbol).toBe("SBIN");
  });
});

describe("formatRowCells", () => {
  it.each<[string, Partial<ScreenerRow>, ReturnType<typeof formatRowCells>]>([
    [
      "a typical row",
      {},
      {
        price: "812.50",
        change: "+1.25%",
        isUp: true,
        volume: "12.3L",
        marketCap: "7L Cr",
        rsi: "55.5",
        macd: "1.23",
      },
    ],
    [
      "a falling row",
      { change: -0.5, macd: -0.456, rsi: 25, volume: 999, marketCap: 820 },
      {
        price: "812.50",
        change: "-0.50%",
        isUp: false,
        volume: "999",
        marketCap: "820 Cr",
        rsi: "25.0",
        macd: "-0.46",
      },
    ],
    [
      "zero change counts as up; zero indicators show as missing",
      { change: 0, rsi: 0, macd: 0, price: 0, volume: 0, marketCap: 0 },
      {
        price: "0.00",
        change: "+0.00%",
        isUp: true,
        volume: "0",
        marketCap: "0 Cr",
        rsi: "--",
        macd: "--",
      },
    ],
    [
      "a change that rounds to zero shows no minus",
      { change: -0.004 },
      {
        price: "812.50",
        change: "+0.00%",
        isUp: false,
        volume: "12.3L",
        marketCap: "7L Cr",
        rsi: "55.5",
        macd: "1.23",
      },
    ],
    [
      "non-finite values read as zero",
      { price: Number.NaN, change: Number.POSITIVE_INFINITY, rsi: Number.NaN, macd: Number.NaN },
      {
        price: "0.00",
        change: "+0.00%",
        isUp: true,
        volume: "12.3L",
        marketCap: "7L Cr",
        rsi: "--",
        macd: "--",
      },
    ],
    [
      "large values use crore and lakh units",
      { volume: 25_000_000, marketCap: 1_500_000, change: 12.345 },
      {
        price: "812.50",
        change: "+12.35%",
        isUp: true,
        volume: "2.5Cr",
        marketCap: "15L Cr",
        rsi: "55.5",
        macd: "1.23",
      },
    ],
  ])("%s", (_name, overrides, expected) => {
    expect(formatRowCells(row(overrides))).toEqual(expected);
  });
});

describe("indicator colours", () => {
  it.each<[number, string]>([
    [80, "text-loss"],
    [70, ""],
    [50, ""],
    [30, ""],
    [29.9, "text-profit"],
    [0, "text-profit"],
  ])("RSI %d gives %j", (rsi, expected) => {
    expect(getRsiClassName(rsi)).toBe(expected);
  });

  it.each<[number, string]>([
    [0.01, "text-profit"],
    [0, "text-foreground-muted"],
    [-0.01, "text-loss"],
  ])("MACD %d gives %j", (macd, expected) => {
    expect(getMacdClassName(macd)).toBe(expected);
  });
});

describe("flashes", () => {
  it.each<[string, number, number, FlashState | undefined, FlashState]>([
    ["first rise", 1, 2, undefined, { direction: "up", phase: 0 }],
    ["first fall", 2, 1, undefined, { direction: "down", phase: 0 }],
    [
      "second change alternates the phase",
      2,
      3,
      { direction: "up", phase: 0 },
      { direction: "up", phase: 1 },
    ],
    [
      "third change alternates back",
      3,
      1,
      { direction: "up", phase: 1 },
      { direction: "down", phase: 0 },
    ],
  ])("%s", (_name, previous, value, flash, expected) => {
    expect(nextFlash(previous, value, flash)).toEqual(expected);
  });

  it.each<[FlashState | undefined, string | undefined]>([
    [undefined, undefined],
    [{ direction: "up", phase: 0 }, "screener-flash-up-a"],
    [{ direction: "up", phase: 1 }, "screener-flash-up-b"],
    [{ direction: "down", phase: 0 }, "screener-flash-down-a"],
    [{ direction: "down", phase: 1 }, "screener-flash-down-b"],
  ])("%o gives %s", (flash, expected) => {
    expect(getFlashClassName(flash)).toBe(expected);
  });
});

describe("getEmptyStateMessage", () => {
  it.each<[string, ScreenerIndex[], string | null, string]>([
    ["no universes, no error", [], null, "No universes available"],
    ["no universes beats an error", [], "boom", "No universes available"],
    ["an error", [index("NIFTY50", "Nifty 50")], "boom", "Failed to load screener data"],
    ["no rows", [index("NIFTY50", "Nifty 50")], null, "No results found"],
  ])("%s", (_name, indices, error, expected) => {
    expect(getEmptyStateMessage(indices, error)).toBe(expected);
  });
});

describe("paging", () => {
  it.each<[number, number, number]>([
    [0, 25, 1],
    [1, 25, 1],
    [25, 25, 1],
    [26, 25, 2],
    [100, 50, 2],
    [101, 100, 2],
  ])("%d results at %d a page give %d pages", (total, size, expected) => {
    expect(totalPagesFor(total, size)).toBe(expected);
  });

  it.each<[number, number, number, { from: number; to: number }]>([
    [1, 25, 60, { from: 1, to: 25 }],
    [2, 25, 60, { from: 26, to: 50 }],
    [3, 25, 60, { from: 51, to: 60 }],
  ])("page %d of %d-row pages over %d results", (page, size, total, expected) => {
    expect(pageRange(page, size, total)).toEqual(expected);
  });
});

describe("coercion", () => {
  it.each<[unknown, number]>([
    [3.5, 3.5],
    [0, 0],
    [-2, -2],
    [Number.NaN, 0],
    [Number.NEGATIVE_INFINITY, 0],
    [null, 0],
    [undefined, 0],
    ["7", 0],
  ])("toNumber(%o) is %d", (value, expected) => {
    expect(toNumber(value)).toBe(expected);
  });

  it.each<[unknown, string]>([
    ["SBIN", "SBIN"],
    ["", ""],
    [null, ""],
    [42, ""],
  ])("toSafeString(%o) is %j", (value, expected) => {
    expect(toSafeString(value)).toBe(expected);
  });
});
