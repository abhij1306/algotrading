/** Pure view logic for a backtest result: chart rows and the tone of each headline number. */

import { roundsNegative } from "@/lib/format";

export interface CurvePoint {
  date: string;
  equity: number;
}

export interface EquityRow {
  date: string;
  equity: number;
  benchmark: number | null;
}

/** The most points the equity chart draws; longer curves are thinned evenly. */
export const MAX_CHART_POINTS = 200;

/**
 * The portfolio curve joined to the benchmark by date (null where the benchmark has no point),
 * keeping every n-th row so at most about `maxPoints` remain.
 */
export function buildEquityRows(
  equity: ReadonlyArray<CurvePoint>,
  benchmark: ReadonlyArray<CurvePoint>,
  maxPoints: number = MAX_CHART_POINTS,
): EquityRow[] {
  const benchmarkByDate = new Map(benchmark.map((point) => [point.date, point.equity]));
  const step = Math.max(1, Math.floor(equity.length / maxPoints));
  const rows: EquityRow[] = [];
  for (let i = 0; i < equity.length; i += step) {
    const point = equity[i];
    rows.push({
      date: point.date,
      equity: point.equity,
      benchmark: benchmarkByDate.get(point.date) ?? null,
    });
  }
  return rows;
}

/** Green above a coin flip, red below, plain at exactly 50%. */
export function winRateTone(winRate: number): string {
  if (winRate > 50) return "text-profit";
  return winRate < 50 ? "text-loss" : "text-foreground";
}

/** Red when the return still shows a minus sign at one decimal, else green. */
export function returnTone(returnPct: number): string {
  return roundsNegative(returnPct, 1) ? "text-loss" : "text-profit";
}

/** Green at a Sharpe ratio of 1 or more. */
export function sharpeTone(sharpe: number): string {
  return sharpe >= 1 ? "text-profit" : "text-foreground";
}
