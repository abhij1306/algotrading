import { describe, expect, it } from "vite-plus/test";
import example from "./fixtures/example-run.json";
import {
  bucketRows,
  changeText,
  compareCells,
  compareEquity,
  deltaTone,
  dsrText,
  equitySeries,
  filterRuns,
  filterTrades,
  formatDelta,
  groupRuns,
  longestDd,
  metricDelta,
  nextSort,
  NO_FILTER,
  parseCsv,
  parseCsvTable,
  REGIME_ORDER,
  refusalCosts,
  regimeOffSpans,
  runFacets,
  runName,
  stockSummary,
  toggleSelection,
  topSymbols,
  topTrades,
  yearRows,
  type Run,
  type RunSummary,
} from "../runs";

const fixture = example as unknown as Run;

function run(
  id: string,
  identity: Partial<Run["identity"]> = {},
  data: Partial<Run["data"]> = {},
  metrics: Partial<Run["metrics"]> = {},
): RunSummary {
  return {
    identity: { ...fixture.identity, run_id: id, base_run_id: null, ...identity },
    data: { ...fixture.data, ...data },
    metrics: { ...fixture.metrics, ...metrics },
    refusals: {},
    binding: fixture.binding,
  };
}

describe("deltas", () => {
  it("is run minus base, null when either side is missing", () => {
    expect(metricDelta({ cagr_pct: 9.4 }, { cagr_pct: 7.4 }, "cagr_pct")).toBeCloseTo(2);
    expect(metricDelta({ cagr_pct: 9.4 }, { cagr_pct: null }, "cagr_pct")).toBeNull();
    expect(metricDelta({ cagr_pct: 9.4 }, null, "cagr_pct")).toBeNull();
  });

  it("formats with a sign and points for percents", () => {
    expect(formatDelta(1.234, "pct")).toBe("+1.2 pp");
    expect(formatDelta(-0.051, "r")).toBe("−0.05");
    expect(formatDelta(0.001, "r")).toBe("±0.00");
    expect(formatDelta(null, "r")).toBe("");
  });

  it("tones a delta by the metric's better direction", () => {
    expect(deltaTone(2, "higher")).toBe("profit");
    expect(deltaTone(2, "lower")).toBe("loss");
    expect(deltaTone(-2, "lower")).toBe("profit");
    expect(deltaTone(2, null)).toBe("muted");
    expect(deltaTone(0, "higher")).toBe("muted");
  });

  it("states the longest drawdown's end and the DSR trial count", () => {
    expect(longestDd({ longest_dd_sessions: 739, longest_dd_recovered: false })).toBe("739 open");
    expect(longestDd({ longest_dd_sessions: 739, longest_dd_recovered: null })).toBe("739");
    expect(dsrText({ dsr_prob: 0.3346, dsr_n_trials: 12 })).toBe("0.33 of 12");
    expect(dsrText(fixture.metrics)).toBe("0.33, trials unknown");
  });
});

describe("grouping", () => {
  const base = run("b1", { sweep_id: "sw-1", variant: "base" }, {}, { cagr_pct: 5 });
  const variant = run(
    "v1",
    { sweep_id: "sw-1", variant: "v1", base_run_id: "b1" },
    {},
    { cagr_pct: 9 },
  );
  const holdOut = run(
    "h1",
    { sweep_id: "sw-2", variant: "v1 hold-out" },
    { split: "hold_out", validates_run_id: "b1" },
  );
  const other = run("s2", { strategy: "S2", sweep_id: "sw-3", variant: "x" });
  const all = [holdOut, variant, other, base];

  it("groups by strategy then sweep in first-seen order, hold-outs under their run", () => {
    const groups = groupRuns(all, all);
    expect(groups.map((g) => g.strategy)).toEqual(["S1", "S2"]);
    const [s1] = groups;
    expect(s1?.sweeps.map((s) => s.id)).toEqual(["sw-1"]);
    expect(s1?.sweeps[0]?.rows.map((r) => r.run.identity.run_id)).toEqual(["v1", "b1", "h1"]);
    const nested = s1?.sweeps[0]?.rows[2];
    expect(nested?.validates?.identity.run_id).toBe("b1");
    expect(s1?.sweeps[0]?.rows[0]?.base?.identity.run_id).toBe("b1");
  });

  it("keeps a hold-out in its own sweep when its run is filtered out", () => {
    const shown = filterRuns(all, { ...NO_FILTER, split: "hold_out" });
    const groups = groupRuns(shown, all);
    expect(groups[0]?.sweeps[0]?.id).toBe("sw-2");
    expect(groups[0]?.sweeps[0]?.rows[0]?.validates).toBeNull();
  });

  it("sorts within a sweep, missing values last", () => {
    const groups = groupRuns(all, all, { key: "cagr_pct", dir: "desc" });
    expect(groups[0]?.sweeps[0]?.rows.map((r) => r.run.identity.run_id)).toEqual([
      "v1",
      "b1",
      "h1",
    ]);
    const asc = groupRuns(all, all, { key: "cagr_pct", dir: "asc" });
    expect(asc[0]?.sweeps[0]?.rows.map((r) => r.run.identity.run_id)).toEqual(["b1", "h1", "v1"]);
    expect([3, null, 1].toSorted((a, b) => compareCells(a, b, "asc"))).toEqual([1, 3, null]);
  });

  it("lists the facets and flips a sort on the second click", () => {
    expect(runFacets(all).strategies).toEqual(["S1", "S2"]);
    expect(runFacets(all).sweeps.find((s) => s.id === "sw-1")?.runs).toBe(2);
    expect(nextSort(null, "mar")).toEqual({ key: "mar", dir: "desc" });
    expect(nextSort({ key: "mar", dir: "desc" }, "mar")).toEqual({ key: "mar", dir: "asc" });
  });

  it("gathers runs that set no sweep into one group and names unlabelled runs", () => {
    const loose = run("019a2b3c-4d5e-7f60-8a1b-2c3d4e5f6a7b", {
      sweep_id: null,
      sweep_name: null,
      variant: null,
      change: null,
    });
    const looseToo = run("r2", {
      sweep_id: null,
      sweep_name: null,
      base_run_id: "r1",
      change: null,
    });
    const groups = groupRuns([loose, looseToo], [loose, looseToo]);
    expect(groups[0]?.sweeps.map((s) => [s.id, s.name, s.rows.length])).toEqual([
      ["", "No sweep", 2],
    ]);
    expect(runFacets([loose]).sweeps).toEqual([{ id: "", name: "No sweep", runs: 1 }]);
    expect(filterRuns([loose, other], { ...NO_FILTER, sweep: "" })).toEqual([loose]);
    expect(runName(loose.identity)).toBe("4e5f6a7b");
    expect(changeText(loose.identity)).toBe("baseline");
    expect(changeText(looseToo.identity)).toBe("change not stated");
  });

  it("keeps at most four compare picks", () => {
    expect(toggleSelection(["a", "b", "c", "d"], "e")).toEqual(["b", "c", "d", "e"]);
    expect(toggleSelection(["a", "b"], "a")).toEqual(["b"]);
  });
});

describe("CSV parsing", () => {
  it("reads quotes, doubled quotes, CRLF, a BOM and skips blank lines", () => {
    expect(parseCsv('﻿a,b\r\n"x, y","say ""hi"""\r\n\r\n1,\n')).toEqual([
      ["a", "b"],
      ["x, y", 'say "hi"'],
      ["1", ""],
    ]);
  });

  it("types cells by column: text, flags, numbers, empty as null", () => {
    const table = parseCsvTable(
      "symbol,trade_id,entry_date,index_regime_on,r_net,mcap_cr\n500180,7,2020-01-02,True,1.5,\n",
    );
    expect(table.columns).toEqual([
      "symbol",
      "trade_id",
      "entry_date",
      "index_regime_on",
      "r_net",
      "mcap_cr",
    ]);
    expect(table.rows[0]).toEqual({
      symbol: "500180",
      trade_id: "7",
      entry_date: "2020-01-02",
      index_regime_on: true,
      r_net: 1.5,
      mcap_cr: null,
    });
  });
});

const EQUITY = `date,equity,drawdown_pct,index_regime_on,breadth_pct,benchmark_tri
2020-01-01,100,0,true,60,1000
2020-01-02,110,0,false,40,
2020-01-03,99,10,false,35,1100
2020-01-06,105,4.5,true,52,1050
2020-01-07,104,5.4,false,48,1040
`;

describe("equity", () => {
  const points = equitySeries(parseCsvTable(EQUITY).rows);

  it("rebases equity and the benchmark to the first session; drawdown hangs below zero", () => {
    expect(points[1]?.growth).toBeCloseTo(10);
    expect(points[1]?.benchmark).toBeNull();
    expect(points[2]?.benchmark).toBeCloseTo(10);
    expect(points[2]?.drawdown).toBe(-10);
  });

  it("finds the regime-off stretches, one still open at the end", () => {
    expect(regimeOffSpans(points)).toEqual([
      { start: "2020-01-02", end: "2020-01-03" },
      { start: "2020-01-07", end: "2020-01-07" },
    ]);
  });

  it("merges several runs' curves on date", () => {
    const rows = compareEquity([points.slice(0, 2), null, points.slice(1, 3)]);
    expect(rows.map((r) => r.date)).toEqual(["2020-01-01", "2020-01-02", "2020-01-03"]);
    expect(rows[1]).toMatchObject({ s0: points[1]?.growth, s2: points[1]?.growth });
    expect(rows[0]?.s2).toBeUndefined();
  });
});

describe("bucket summaries", () => {
  it("orders fixed buckets and shares the trades", () => {
    const rows = bucketRows(
      {
        small: { trades: 30, win_pct: 30, expectancy_r: 0.2 },
        large: { trades: 10, win_pct: 40, expectancy_r: 0.5 },
        other: { trades: 10, win_pct: null, expectancy_r: null },
      },
      ["large", "mid", "small", "micro"],
    );
    expect(rows.map((r) => r.key)).toEqual(["large", "small", "other"]);
    expect(rows[1]?.sharePct).toBe(60);
  });

  it("orders the regime buckets index first, then breadth", () => {
    const bucket = { trades: 1, win_pct: null, expectancy_r: null };
    const rows = bucketRows(
      { breadth_ge60: bucket, index_off: bucket, breadth_lt40: bucket, index_on: bucket },
      REGIME_ORDER,
    );
    expect(rows.map((r) => r.key)).toEqual([
      "index_on",
      "index_off",
      "breadth_lt40",
      "breadth_ge60",
    ]);
  });

  it("puts the most traded bucket first when unordered", () => {
    const rows = bucketRows({
      a: { trades: 1, win_pct: 0, expectancy_r: 0 },
      b: { trades: 5, win_pct: 0, expectancy_r: 0 },
    });
    expect(rows.map((r) => r.key)).toEqual(["b", "a"]);
    expect(bucketRows({})).toEqual([]);
  });

  it("adds each year's margin over the benchmark, in year order", () => {
    const rows = yearRows([
      {
        year: 2021,
        return_pct: 12,
        max_dd_pct: 5,
        trades: 3,
        expectancy_r: 0.1,
        benchmark_return_pct: 20,
      },
      {
        year: 2020,
        return_pct: 8,
        max_dd_pct: 5,
        trades: 3,
        expectancy_r: 0.1,
        benchmark_return_pct: null,
      },
    ]);
    expect(rows.map((r) => [r.year, r.excess_pct])).toEqual([
      [2020, null],
      [2021, -8],
    ]);
  });

  it("costs each refusal reason by the forward R of its refused entries", () => {
    const refused = parseCsvTable(
      "date,symbol,reason,fwd_r_20d_gross,fwd_r_60d_gross\n" +
        "2020-01-01,A,industry_cap,1,3\n" +
        "2020-01-02,B,industry_cap,-1,-1\n" +
        "2020-01-03,C,industry_cap,0.5,\n" +
        "2020-01-03,D,max_chase,0.2,0.4\n",
    ).rows;
    const costs = refusalCosts({ industry_cap: 316, max_positions: 4 }, refused);
    expect(costs.map((c) => c.reason)).toEqual(["industry_cap", "max_positions", "max_chase"]);
    const cap = costs[0];
    expect(cap?.count).toBe(316);
    expect(cap?.meanFwd20).toBeCloseTo(0.5 / 3);
    expect(cap?.meanFwd60).toBe(1);
    expect(cap?.n60).toBe(2);
    expect(cap?.wouldWin60Pct).toBe(50);
    expect(costs[1]?.meanFwd60).toBeNull();
    expect(costs[2]?.count).toBe(1);
  });
});

describe("trades, stocks and concentration", () => {
  const trades = parseCsvTable(
    "trade_id,leg,symbol,industry,entry_date,exit_date,exit_reason,net_pnl,r_net\n" +
      "1,1,AAA,Banks,2020-01-01,2020-02-01,partial_target,300,1.5\n" +
      "1,2,AAA,Banks,2020-01-01,2020-03-01,trail_step,500,2\n" +
      "2,1,BBB,Pharma,2020-01-05,2020-01-09,initial_stop,-200,-1\n" +
      "3,1,CCC,Pharma,2020-02-05,2020-03-09,sma50_close,400,1\n",
  ).rows;

  it("joins a partial exit's legs into one top trade with its share of net P&L", () => {
    const top = topTrades(trades, 2);
    expect(top.map((t) => [t.tradeId, t.netPnl, t.exitDate])).toEqual([
      ["1", 800, "2020-03-01"],
      ["3", 400, "2020-03-09"],
    ]);
    expect(top[0]?.sharePct).toBe(80);
  });

  it("filters trades by text, exit reason and outcome", () => {
    const filter = { query: "", exitReason: null, outcome: "all" } as const;
    expect(filterTrades(trades, { ...filter, query: "pharma" })).toHaveLength(2);
    expect(filterTrades(trades, { ...filter, exitReason: "initial_stop" })).toHaveLength(1);
    expect(filterTrades(trades, { ...filter, outcome: "loss" })).toHaveLength(1);
    expect(filterTrades(trades, { ...filter, outcome: "win" })).toHaveLength(3);
  });

  it("summarises who carries the P&L and who keeps stopping out", () => {
    const stocks = parseCsvTable(
      "symbol,trades,net_pnl,stopouts\nAAA,3,900,0\nBBB,4,-300,3\nCCC,1,100,2\nDDD,0,0,0\n",
    ).rows;
    expect(stockSummary(stocks)).toEqual({
      traded: 3,
      profitable: 2,
      losing: 1,
      repeatStopouts: 2,
    });
    const top = topSymbols(stocks, 2);
    expect(top.map((s) => s.symbol)).toEqual(["AAA", "CCC"]);
    expect(top[0]?.sharePct).toBeCloseTo(128.57, 1);
  });
});
