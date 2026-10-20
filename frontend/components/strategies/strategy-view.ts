/**
 * Pure view logic for the strategies page: which universe is active, which error wins the
 * banner, how scan rows split into tabs, and how backtest inputs and metrics are read.
 */

import { formatNumber, formatSignedPercent, PLACEHOLDER } from "@/lib/format";
import type { ScanRow, StrategyBacktestResult } from "@/lib/queries/strategies";

export interface UniverseOption {
  id: string;
  name: string;
  count?: number;
}

/** An error message with the time it happened, so the newest one wins the banner. */
export interface TimedError {
  message: string;
  at: number;
}

/** The newest error message, or null when there is none. Ties go to the later entry. */
export function latestError(errors: ReadonlyArray<TimedError | null>): string | null {
  let latest: TimedError | null = null;
  for (const entry of errors) {
    if (entry && (latest === null || entry.at >= latest.at)) latest = entry;
  }
  return latest?.message ?? null;
}

/** A query error stamped with when it happened, or null when the query has none. */
export function timedQueryError(error: Error | null, at: number): TimedError | null {
  return error ? { message: error.message, at } : null;
}

/** The chosen universe, or the first known one when the chosen one is not offered. */
export function resolveUniverse(available: ReadonlyArray<UniverseOption>, choice: string): string {
  if (available.length === 0 || available.some((row) => row.id === choice)) return choice;
  return available[0].id;
}

export function gradeVariant(grade: string): "profit" | "warning" | "neutral" {
  if (grade === "A") return "profit";
  return grade === "B" ? "warning" : "neutral";
}

/** Scan rows split into bases still forming and confirmed breakouts, in scan order. */
export function splitSignals(rows: ReadonlyArray<ScanRow>): {
  forming: ScanRow[];
  breakouts: ScanRow[];
} {
  const forming: ScanRow[] = [];
  const breakouts: ScanRow[] = [];
  for (const row of rows) (row.is_breakout ? breakouts : forming).push(row);
  return { forming, breakouts };
}

/** The capital typed into the backtest form, or null when it is not a finite number. */
export function parseCapital(text: string): number | null {
  const parsed = Number.parseFloat(text.trim());
  return Number.isFinite(parsed) ? parsed : null;
}

type MetricValue = NonNullable<StrategyBacktestResult["result"]>["metrics"][string] | undefined;

/** A backtest metric as display text: numbers get `decimals` digits, text passes through. */
export function formatMetric(value: MetricValue, decimals: number): string {
  if (value === undefined) return PLACEHOLDER;
  return typeof value === "number" ? formatNumber(value, decimals) : value;
}

/** The headline numbers of a finished strategy backtest, or null before one has metrics. */
export function backtestHeadline(
  result: StrategyBacktestResult | null,
): { returnPct: string; sharpe: string; trades: string } | null {
  const metrics = result?.result?.metrics;
  if (!metrics) return null;
  return {
    returnPct: formatSignedPercent(Number(metrics.total_return_pct ?? 0)),
    sharpe: formatMetric(metrics.sharpe_ratio, 2),
    trades: formatMetric(metrics.total_trades, 0),
  };
}
