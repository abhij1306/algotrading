import { describe, expect, it } from "vite-plus/test";
import {
  compareEquity,
  compareYearly,
  equityRows,
  filterRuns,
  formatMetric,
  num,
  overridesList,
  reasonRows,
  runFacets,
  scatterPoints,
  toggleSelection,
  type Report,
  type RunSummary,
  type Trade,
} from "../backtests";

function run(id: string, extra: Partial<RunSummary> = {}): RunSummary {
  return {
    id,
    status: "completed",
    family_key: "fam-a",
    strategy: "trend-pullback-ema",
    strategy_version_id: "v",
    strategy_version: 1,
    risk_policy_version_id: "p",
    period_start: "2019-01-01",
    period_end: "2024-12-31",
    variant: null,
    overrides: null,
    dsr: null,
    pbo: null,
    has_report: true,
    headline: {},
    ...extra,
  };
}

function report(equity: [string, string, number, number | null][], yearly: Report["yearly"] = []) {
  return {
    version: 1,
    benchmark: null,
    regime_rule: "nifty500_close_above_rising_sma200",
    equity: equity.map(([date, value, drawdown, benchmark]) => ({
      date,
      equity: value,
      drawdown,
      benchmark,
    })),
    yearly,
    r_curve: [],
    exit_reasons: [],
    hold_time: [],
    regime: [],
    funnel: {
      watchlisted: 0,
      skipped: 0,
      skips: {},
      signals: 0,
      rejected: 0,
      rejections: {},
      entered: 0,
      trades: 0,
    },
  } satisfies Report;
}

describe("num", () => {
  it("parses decimal strings and keeps missing values missing", () => {
    expect(num("4012.5000")).toBe(4012.5);
    expect(num(-0.25)).toBe(-0.25);
    for (const missing of [null, undefined, "", "abc", Number.NaN, {}]) {
      expect(num(missing)).toBeNull();
    }
  });
});

describe("overridesList", () => {
  it("lists the sets in order, then the policy overrides", () => {
    const overrides = {
      set: [
        { path: "exits[2].at_r", value: 3 },
        { path: "exits[+]", value: { rule: "max_hold", days: 60 } },
      ],
      risk_pct: "0.005",
      max_positions: 8,
    };
    expect(overridesList(overrides)).toEqual([
      "exits[2].at_r=3",
      'exits[+]={"rule":"max_hold","days":60}',
      "risk_pct=0.005",
      "max_positions=8",
    ]);
    expect(overridesList(null)).toEqual([]);
  });
});

describe("runFacets and filterRuns", () => {
  const runs = [
    run("1", { family_key: "fam-b", variant: { label: "a", sweep_id: "s1" } }),
    run("2", { family_key: "fam-b", variant: { label: "b", sweep_id: "s1" } }),
    run("3"),
  ];

  it("finds each family once and counts each sweep's variants", () => {
    const facets = runFacets(runs);
    expect(facets.families).toEqual(["fam-a", "fam-b"]);
    expect(facets.sweeps).toEqual([
      { id: "s1", label: "trend-pullback-ema v1, 2019-01-01", runs: 2 },
    ]);
  });

  it("filters by family and by sweep", () => {
    const ids = (filter: Parameters<typeof filterRuns>[1]) =>
      filterRuns(runs, filter).map((r) => r.id);
    expect(ids({ family: null, sweep: null })).toEqual(["1", "2", "3"]);
    expect(ids({ family: "fam-a", sweep: null })).toEqual(["3"]);
    expect(ids({ family: null, sweep: "s1" })).toEqual(["1", "2"]);
    expect(ids({ family: "fam-a", sweep: "s1" })).toEqual([]);
  });
});

describe("toggleSelection", () => {
  it("adds up to four runs and removes a picked one", () => {
    expect(toggleSelection(["a", "b", "c", "d"], "e")).toEqual(["a", "b", "c", "d"]);
    expect(toggleSelection(["a", "b"], "a")).toEqual(["b"]);
    expect(toggleSelection([], "a")).toEqual(["a"]);
  });
});

describe("equityRows", () => {
  it("rebases equity to the first session and measures NIFTY 500's drawdown from its peak", () => {
    const rows = equityRows(
      report([
        ["2024-01-01", "1000000.00", 0, null],
        ["2024-01-02", "1100000.00", 0, 1],
        ["2024-01-03", "990000.00", -0.1, 2],
        ["2024-01-04", "1045000.00", -0.05, 1.5],
      ]),
    );
    expect(rows.map((r) => r.strategy)).toEqual([1, 1.1, 0.99, 1.045]);
    expect(rows.map((r) => r.benchmarkDrawdown)).toEqual([null, 0, 0, -0.25]);
    expect(rows[2]?.drawdown).toBe(-0.1);
  });
});

describe("scatterPoints", () => {
  it("pairs MFE with realised R and leaves out a trade missing either", () => {
    const trade = (symbol: string | null, mfe: string, r: string) =>
      ({
        symbol,
        instrument_id: "i1",
        mfe_r: mfe,
        r_multiple: r,
        exit_date: "2024-02-01",
      }) as Trade;
    expect(scatterPoints([trade("TCS", "2.50", "-1.00"), trade(null, "x", "1")])).toEqual([
      { mfe: 2.5, r: -1, symbol: "TCS", exit: "2024-02-01" },
    ]);
  });
});

describe("compare shaping", () => {
  const a = report(
    [
      ["2024-01-01", "100", 0, null],
      ["2024-01-03", "110", 0, null],
    ],
    [{ year: 2024, return: 0.1, benchmark: null }],
  );
  const b = report(
    [
      ["2024-01-02", "50", 0, null],
      ["2024-01-03", "45", -0.1, null],
    ],
    [
      { year: 2023, return: null, benchmark: null },
      { year: 2024, return: -0.1, benchmark: null },
    ],
  );

  it("puts each run's rebased equity on the union of the dates", () => {
    expect(compareEquity([a, null, b])).toEqual([
      { date: "2024-01-01", s0: 1 },
      { date: "2024-01-02", s2: 1 },
      { date: "2024-01-03", s0: 1.1, s2: 0.9 },
    ]);
  });

  it("lines up the yearly returns by year", () => {
    expect(compareYearly([a, b])).toEqual([
      { year: 2023, s1: null },
      { year: 2024, s0: 0.1, s1: -0.1 },
    ]);
  });
});

describe("reasonRows and formatMetric", () => {
  it("sorts reasons largest first, ties by name", () => {
    expect(reasonRows({ b: 2, a: 2, c: 5 })).toEqual([
      ["c", 5],
      ["a", 2],
      ["b", 2],
    ]);
  });

  it("formats ratios as percents and leaves missing values as the placeholder", () => {
    expect(formatMetric(0.1234, "pct")).toBe("12.34%");
    expect(formatMetric("0.4567", "r")).toBe("0.46");
    expect(formatMetric(1234567, "int")).toBe("12,34,567");
    expect(formatMetric(undefined, "pct")).toBe("--");
    expect(formatMetric("score", "text")).toBe("score");
    expect(formatMetric(0.02561, "drawdown")).toBe("-2.56%");
  });
});
