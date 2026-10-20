import { describe, expect, it } from "vite-plus/test";
import type { ScanRow } from "@/lib/queries/strategies";
import {
  backtestHeadline,
  formatMetric,
  gradeVariant,
  latestError,
  parseCapital,
  resolveUniverse,
  splitSignals,
  timedQueryError,
} from "../strategy-view";

function scanRow(id: number, symbol: string, isBreakout: boolean): ScanRow {
  return {
    id,
    symbol,
    company_name: "",
    sector: "",
    grade: "A",
    rs_rating: 90,
    contraction_count: 2,
    contraction_depths: [10, 5],
    final_contraction_depth: 5,
    volume_dry_up_pct: 40,
    pivot_high: 100,
    stop_level: 95,
    stop_pct: 5,
    days_in_base: 20,
    is_breakout: isBreakout,
    breakout_price: null,
    breakout_volume_mult: null,
    signal_status: "SIGNAL",
    planned_shares: 10,
    planned_position_value: 1000,
    capital_risk: 50,
    two_r_target: 110,
  };
}

describe("latestError", () => {
  it("returns the newest message", () => {
    expect(
      latestError([
        { message: "old", at: 1 },
        null,
        { message: "new", at: 5 },
        { message: "middle", at: 3 },
      ]),
    ).toBe("new");
  });

  it("gives a tie to the later entry and null when there is no error", () => {
    expect(
      latestError([
        { message: "first", at: 2 },
        { message: "second", at: 2 },
      ]),
    ).toBe("second");
    expect(latestError([null, null])).toBeNull();
  });
});

describe("timedQueryError", () => {
  it("stamps a query error with its time and passes no error through", () => {
    expect(timedQueryError(new Error("down"), 42)).toEqual({ message: "down", at: 42 });
    expect(timedQueryError(null, 42)).toBeNull();
  });
});

describe("resolveUniverse", () => {
  const offered = [
    { id: "NIFTY50", name: "Nifty 50" },
    { id: "NIFTY500", name: "Nifty 500" },
  ];

  it("keeps an offered choice and falls back to the first offered one otherwise", () => {
    expect(resolveUniverse(offered, "NIFTY500")).toBe("NIFTY500");
    expect(resolveUniverse(offered, "MIDCAP")).toBe("NIFTY50");
  });

  it("keeps the choice while no universes are known", () => {
    expect(resolveUniverse([], "MIDCAP")).toBe("MIDCAP");
  });
});

describe("gradeVariant", () => {
  it.each([
    ["A", "profit"],
    ["B", "warning"],
    ["C", "neutral"],
  ])("grade %s is %s", (grade, variant) => {
    expect(gradeVariant(grade)).toBe(variant);
  });
});

describe("splitSignals", () => {
  it("splits forming bases from breakouts in scan order", () => {
    const { forming, breakouts } = splitSignals([
      scanRow(1, "SBIN", true),
      scanRow(2, "TCS", false),
      scanRow(3, "INFY", true),
    ]);
    expect(forming.map((row) => row.symbol)).toEqual(["TCS"]);
    expect(breakouts.map((row) => row.symbol)).toEqual(["SBIN", "INFY"]);
  });
});

describe("parseCapital", () => {
  it.each([
    ["1000000", 1_000_000],
    [" 2500.5 ", 2500.5],
    ["", null],
    ["abc", null],
    ["0", null],
    ["-5000", null],
  ])("reads %j as %s", (text, expected) => {
    expect(parseCapital(text)).toBe(expected);
  });
});

describe("formatMetric", () => {
  it("formats numbers to the given decimals and passes text through", () => {
    expect(formatMetric(1.23456, 2)).toBe("1.23");
    expect(formatMetric(1234, 0)).toBe("1,234");
    expect(formatMetric("n/a", 2)).toBe("n/a");
    expect(formatMetric(undefined, 2)).toBe("--");
  });
});

describe("backtestHeadline", () => {
  it("formats return, Sharpe and trades of a finished run", () => {
    expect(
      backtestHeadline({
        run_id: "r1",
        status: "completed",
        result: { metrics: { total_return_pct: 12.345, sharpe_ratio: 1.4567, total_trades: 31 } },
      }),
    ).toEqual({ returnPct: "+12.35%", sharpe: "1.46", trades: "31" });
  });

  it("is null before a run has metrics", () => {
    expect(backtestHeadline({ run_id: "r1", status: "running" })).toBeNull();
    expect(backtestHeadline(null)).toBeNull();
  });

  it("shows placeholders for metrics the run did not report", () => {
    expect(
      backtestHeadline({ run_id: "r1", status: "completed", result: { metrics: {} } }),
    ).toEqual({ returnPct: "+0.00%", sharpe: "--", trades: "--" });
  });
});
