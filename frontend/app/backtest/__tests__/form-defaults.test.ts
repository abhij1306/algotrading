import { describe, expect, it } from "vite-plus/test";
import type { BacktestStatus } from "@/lib/queries/backtest";
import { deriveFormDefaults } from "../form-defaults";

const none = { available: false, min_date: null, max_date: null };

function status(overrides: Partial<BacktestStatus>): BacktestStatus {
  return {
    data_ready: true,
    instrument_capabilities: {},
    universe_ranges: {},
    stock_range: none,
    ...overrides,
  };
}

describe("deriveFormDefaults", () => {
  it("prefers NIFTY50 when index coverage exists", () => {
    expect(
      deriveFormDefaults(
        status({
          universe_ranges: {
            NIFTY500: { available: true, min_date: "2024-01-01", max_date: "2024-12-31" },
            NIFTY50: { available: true, min_date: "2025-01-01", max_date: "2025-06-30" },
          },
        }),
      ),
    ).toEqual({
      selectionMode: "universe",
      universe: "NIFTY50",
      startDate: "2025-01-01",
      endDate: "2025-06-30",
    });
  });

  it("skips universes without data", () => {
    expect(
      deriveFormDefaults(
        status({
          universe_ranges: {
            NIFTY50: none,
            NIFTY100: { available: true, min_date: "2025-01-01", max_date: "2025-02-01" },
          },
        }),
      ),
    ).toMatchObject({ selectionMode: "universe", universe: "NIFTY100" });
  });

  it("falls back to symbol mode over the equity range when no index has data", () => {
    expect(
      deriveFormDefaults(
        status({
          universe_ranges: { NIFTY50: none },
          stock_range: { available: true, min_date: "2025-01-01", max_date: "2025-03-01" },
        }),
      ),
    ).toEqual({
      selectionMode: "symbols",
      universe: "NIFTY50",
      startDate: "2025-01-01",
      endDate: "2025-03-01",
    });
  });
});
