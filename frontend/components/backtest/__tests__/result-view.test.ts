import { describe, expect, it } from "vite-plus/test";
import { buildEquityRows, returnTone, sharpeTone, winRateTone } from "../result-view";

describe("buildEquityRows", () => {
  it("joins the benchmark by date and leaves gaps as null", () => {
    expect(
      buildEquityRows(
        [
          { date: "2025-01-01", equity: 100 },
          { date: "2025-01-02", equity: 104 },
        ],
        [{ date: "2025-01-02", equity: 101 }],
      ),
    ).toEqual([
      { date: "2025-01-01", equity: 100, benchmark: null },
      { date: "2025-01-02", equity: 104, benchmark: 101 },
    ]);
  });

  it("keeps every n-th point when the curve is longer than the limit", () => {
    const equity = Array.from({ length: 10 }, (_, i) => ({ date: `d${i}`, equity: i }));
    expect(buildEquityRows(equity, [], 3).map((row) => row.date)).toEqual(["d0", "d3", "d6", "d9"]);
  });

  it("ends on the curve's final point when thinning skips it", () => {
    const equity = Array.from({ length: 400 }, (_, i) => ({ date: `d${i}`, equity: i }));
    const rows = buildEquityRows(equity, []);
    expect(rows).toHaveLength(200);
    expect(rows.at(-1)).toEqual({ date: "d399", equity: 399, benchmark: null });
    expect(rows.slice(0, 3).map((row) => row.date)).toEqual(["d0", "d2", "d4"]);
  });

  it("keeps every point at or under the limit", () => {
    const equity = Array.from({ length: 4 }, (_, i) => ({ date: `d${i}`, equity: i }));
    expect(buildEquityRows(equity, [], 4)).toHaveLength(4);
  });
});

describe("tones", () => {
  it.each([
    [55, "text-profit"],
    [50, "text-foreground"],
    [45, "text-loss"],
  ])("win rate %s is %s", (winRate, tone) => {
    expect(winRateTone(winRate)).toBe(tone);
  });

  it.each([
    [3.2, "text-profit"],
    [-0.04, "text-profit"],
    [-0.06, "text-loss"],
  ])("return %s is %s", (returnPct, tone) => {
    expect(returnTone(returnPct)).toBe(tone);
  });

  it.each([
    [1, "text-profit"],
    [0.99, "text-foreground"],
  ])("Sharpe %s is %s", (sharpe, tone) => {
    expect(sharpeTone(sharpe)).toBe(tone);
  });
});
