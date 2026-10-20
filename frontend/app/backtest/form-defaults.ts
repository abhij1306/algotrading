import type { BacktestStatus } from "@/lib/queries/backtest";

export type SelectionMode = "universe" | "symbols";

export type FormDefaults = {
  selectionMode: SelectionMode;
  universe: string;
  startDate: string;
  endDate: string;
};

/**
 * Prefill the form from what data actually exists: an index universe when any index has
 * coverage (NIFTY50 preferred), otherwise specific symbols over the equity coverage range.
 */
export function deriveFormDefaults(status: BacktestStatus | undefined): FormDefaults {
  const empty: FormDefaults = {
    selectionMode: "universe",
    universe: "NIFTY50",
    startDate: "",
    endDate: "",
  };
  if (!status) return empty;

  const available = Object.entries(status.universe_ranges).filter(([, range]) => range.available);
  const preferred = available.find(([id]) => id === "NIFTY50") ?? available[0];
  if (preferred) {
    const [universe, range] = preferred;
    return {
      selectionMode: "universe",
      universe,
      startDate: range.min_date ?? "",
      endDate: range.max_date ?? "",
    };
  }
  if (status.stock_range.available) {
    return {
      selectionMode: "symbols",
      universe: empty.universe,
      startDate: status.stock_range.min_date ?? "",
      endDate: status.stock_range.max_date ?? "",
    };
  }
  return empty;
}
